"""
train.py — Eğitim Döngüsü (Training Loop)

Görevi:
  1. data_pipeline.py ile GERÇEK BTC/USDT verisi çekmek, indikatör + returns
     eklemek (9 özellik), GİRDİYİ ölçeklemek ve pencerelemek.
  2. Zaman sırasını bozmadan (no shuffle) %80 Train / %20 Test bölmek.
  3. Modeli, YÖN sınıfını (0=SAT, 1=BEKLE, 2=AL) tahmin edecek şekilde
     Adam + CrossEntropyLoss ile eğitmek.
  4. Eğitim bitince modeli (trade_model.pth) ve scaler'ı (scaler.pkl)
     MUTLAKA diske kaydetmek.

NOT: Bu dosya SADECE eğitim yapar. Al-sat, backtest ve cüzdan
işlemleri kapsam dışıdır. Tüm veri GERÇEK piyasa verisidir.
"""

import joblib
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from data_pipeline import (
    fetch_ohlcv,
    add_indicators,
    scale_features,
    create_sequences,
    FEATURE_COLUMNS,
    TARGET_COLUMN,
)
from model import TradeAILSTM


# ----------------------- Hiperparametreler -----------------------
SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"
LIMIT = 5000  # ~7 ay (sayfalama ile derin veri) — daha geniş piyasa döngüsü
SEQUENCE_LENGTH = 60

TRAIN_RATIO = 0.8
BATCH_SIZE = 32

# Özellik sayısı dinamik: 9 (OHLCV + RSI + MACD + ATR + Returns)
INPUT_SIZE = len(FEATURE_COLUMNS)
HIDDEN_SIZE = 64
NUM_LAYERS = 2
OUTPUT_SIZE = 3  # 3 sınıf: 0 (SAT), 1 (BEKLE), 2 (AL) -> CrossEntropy

LEARNING_RATE = 0.001
EPOCHS = 50

# Kaydedilecek artefaktlar (inference.py / backtest.py bunları yükler)
MODEL_PATH = "trade_model.pth"
SCALER_PATH = "scaler.pkl"

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def prepare_data():
    """
    Gerçek veriyi çeker, indikatör + returns ekler, zaman-serisi güvenli
    şekilde bölerek GİRDİ özelliklerini (X) ölçekler, pencereler ve
    Train/Test DataLoader'larını döndürür.

    - MinMaxScaler yalnızca train dilimindeki GİRDİ (X) üzerine fit edilir;
      test dilimi aynı scaler ile sadece transform edilir (leakage yok).
    - Hedef (y) HAM 'returns' değeridir; ölçeklenmez.
    """
    # 1) Gerçek OHLCV verisini çek + indikatör & returns ekle (9 özellik)
    print(f"[VERİ] {SYMBOL} çekiliyor ({TIMEFRAME}, limit={LIMIT})...")
    df = fetch_ohlcv(symbol=SYMBOL, timeframe=TIMEFRAME, limit=LIMIT)
    df = add_indicators(df)
    print(f"[VERİ] İşlenmiş veri şekli: {df.shape}  ({FEATURE_COLUMNS})")

    # 2) Zaman sırasına göre böl (KARIŞTIRMA YOK)
    split_idx = int(len(df) * TRAIN_RATIO)
    train_df = df.iloc[:split_idx]
    test_df = df.iloc[split_idx:]

    # 3) GİRDİ özelliklerini ölçekle: scaler SADECE train'e fit
    train_scaled, scaler = scale_features(train_df)             # fit + transform
    test_scaled, _ = scale_features(test_df, scaler=scaler)     # sadece transform

    # Hedef sınıf etiketleri (ölçeksiz tamsayı: 0/1/2)
    train_target = train_df[TARGET_COLUMN].values
    test_target = test_df[TARGET_COLUMN].values

    # 4) Test sekanslarının kesintisiz üretilmesi için train'in son
    #    SEQUENCE_LENGTH mumunu test'in başına ekle (hem X hem y).
    test_scaled_ext = np.concatenate(
        [train_scaled[-SEQUENCE_LENGTH:], test_scaled], axis=0
    )
    test_target_ext = np.concatenate(
        [train_target[-SEQUENCE_LENGTH:], test_target], axis=0
    )

    # 5) Pencereleme (X = ölçekli özellikler, y = HAM returns)
    X_train, y_train = create_sequences(train_scaled, train_target, SEQUENCE_LENGTH)
    X_test, y_test = create_sequences(test_scaled_ext, test_target_ext, SEQUENCE_LENGTH)
    print(f"[VERİ] X_train: {X_train.shape} | X_test: {X_test.shape}")
    classes, counts = np.unique(y_train, return_counts=True)
    print(f"[VERİ] Train sınıf dağılımı (0=SAT,1=BEKLE,2=AL): "
          f"{dict(zip(classes.tolist(), counts.tolist()))}")

    # 6) NumPy -> PyTorch tensör
    #    Girdi (X) float; hedef (y) sınıf etiketi olduğu için torch.long
    #    ve CrossEntropyLoss'un beklediği (N,) şeklinde (squeeze).
    X_train_t = torch.tensor(X_train, dtype=torch.float32)
    y_train_t = torch.tensor(y_train, dtype=torch.long).squeeze(-1)
    X_test_t = torch.tensor(X_test, dtype=torch.float32)
    y_test_t = torch.tensor(y_test, dtype=torch.long).squeeze(-1)

    # 7) DataLoader'lar (zaman serisi -> shuffle=False)
    train_loader = DataLoader(
        TensorDataset(X_train_t, y_train_t), batch_size=BATCH_SIZE, shuffle=False
    )
    test_loader = DataLoader(
        TensorDataset(X_test_t, y_test_t), batch_size=BATCH_SIZE, shuffle=False
    )

    return train_loader, test_loader, scaler


def evaluate(model, loader, criterion):
    """
    Bir veri kümesi üzerindeki ortalama loss ve doğruluğu (accuracy)
    hesaplar (gradyansız). Sınıflandırmada accuracy, loss'tan daha
    anlaşılır bir öğrenme göstergesidir.
    """
    model.eval()
    total_loss = 0.0
    total_correct = 0
    total_samples = 0
    with torch.no_grad():
        for X_batch, y_batch in loader:
            X_batch = X_batch.to(DEVICE)
            y_batch = y_batch.to(DEVICE)
            logits = model(X_batch)                 # (batch, 3) logits
            loss = criterion(logits, y_batch)
            preds = torch.argmax(logits, dim=1)     # en yüksek olasılıklı sınıf
            total_correct += (preds == y_batch).sum().item()
            total_loss += loss.item() * X_batch.size(0)
            total_samples += X_batch.size(0)
    avg_loss = total_loss / max(total_samples, 1)
    accuracy = total_correct / max(total_samples, 1)
    return avg_loss, accuracy


def train():
    # ---- Veri hazırlığı ----
    train_loader, test_loader, scaler = prepare_data()

    # ---- Model, optimizer, loss ----
    model = TradeAILSTM(
        input_size=INPUT_SIZE,
        hidden_size=HIDDEN_SIZE,
        num_layers=NUM_LAYERS,
        output_size=OUTPUT_SIZE,
    ).to(DEVICE)

    optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
    criterion = nn.CrossEntropyLoss()  # sınıflandırma standardı (3 sınıf)

    print(f"\n[EĞİTİM] Cihaz: {DEVICE} | Özellik: {INPUT_SIZE} | "
          f"Sınıf: {OUTPUT_SIZE} | Epoch: {EPOCHS}")
    print("-" * 68)

    # ---- Eğitim döngüsü ----
    for epoch in range(1, EPOCHS + 1):
        model.train()
        running_loss = 0.0
        running_samples = 0

        for X_batch, y_batch in train_loader:
            X_batch = X_batch.to(DEVICE)
            y_batch = y_batch.to(DEVICE)

            optimizer.zero_grad()             # gradyanları sıfırla
            preds = model(X_batch)            # ileri geçiş (forward)
            loss = criterion(preds, y_batch)  # hatayı hesapla (MSE)
            loss.backward()                   # geriye yayılım (backward)
            optimizer.step()                  # ağırlıkları güncelle

            running_loss += loss.item() * X_batch.size(0)
            running_samples += X_batch.size(0)

        # Her 10 epoch'ta bir Train/Test loss + accuracy yazdır
        if epoch % 10 == 0 or epoch == 1:
            train_loss = running_loss / max(running_samples, 1)
            _, train_acc = evaluate(model, train_loader, criterion)
            test_loss, test_acc = evaluate(model, test_loader, criterion)
            print(
                f"Epoch [{epoch:>3}/{EPOCHS}]  "
                f"Train Loss: {train_loss:.4f} (acc {train_acc:.2%})  |  "
                f"Test Loss: {test_loss:.4f} (acc {test_acc:.2%})"
            )

    print("-" * 68)
    print("[EĞİTİM] Tamamlandı.")

    # ---- MİMARİ ONARIM: model ağırlıklarını ve scaler'ı MUTLAKA kaydet ----
    torch.save(model.state_dict(), MODEL_PATH)
    joblib.dump(scaler, SCALER_PATH)
    print(f"[KAYIT] Model  -> {MODEL_PATH}")
    print(f"[KAYIT] Scaler -> {SCALER_PATH}")

    return model, scaler


if __name__ == "__main__":
    train()
