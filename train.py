"""
train.py — Eğitim Döngüsü (Training Loop)

Görevi:
  1. data_pipeline.py ile Binance'ten GERÇEK BTC/USDT verisi çekmek,
     ölçeklemek ve pencerelemek.
  2. Zaman sırasını bozmadan (no shuffle) %80 Train / %20 Test bölmek
     ve DataLoader'lara sarmak.
  3. TradeAILSTM modelini Adam + MSELoss ile 50 epoch eğitmek,
     her 10 epoch'ta Train ve Test loss'u yazdırmak.

NOT: Bu dosya SADECE eğitim yapar. Al-sat, backtest ve cüzdan
işlemleri kapsam dışıdır. Tüm veri GERÇEK piyasa verisidir.
"""

import joblib
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset
from sklearn.preprocessing import MinMaxScaler

from data_pipeline import fetch_ohlcv, create_sequences, OHLCV_COLUMNS
from model import TradeAILSTM


# ----------------------- Hiperparametreler -----------------------
SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"
LIMIT = 1000
SEQUENCE_LENGTH = 60
TARGET_COL_INDEX = OHLCV_COLUMNS.index("close")  # 'close' = 3

TRAIN_RATIO = 0.8
BATCH_SIZE = 32

INPUT_SIZE = len(OHLCV_COLUMNS)  # 5 (OHLCV)
HIDDEN_SIZE = 64
NUM_LAYERS = 2
OUTPUT_SIZE = 1

LEARNING_RATE = 0.001
EPOCHS = 50

# Kaydedilecek artefaktların dosya yolları (inference.py bunları yükler)
MODEL_PATH = "trade_model.pth"
SCALER_PATH = "scaler.pkl"

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def prepare_data():
    """
    Gerçek veriyi çeker, zaman-serisi güvenli şekilde bölerek ölçekler,
    pencereler ve Train/Test DataLoader'larını döndürür.

    Data leakage'i önlemek için MinMaxScaler yalnızca eğitim (train)
    dilimindeki ham veri üzerine fit edilir; test dilimi aynı scaler
    ile sadece transform edilir.
    """
    # 1) Gerçek OHLCV verisini çek
    print(f"[VERİ] {SYMBOL} çekiliyor ({TIMEFRAME}, limit={LIMIT})...")
    df = fetch_ohlcv(symbol=SYMBOL, timeframe=TIMEFRAME, limit=LIMIT)
    values = df[OHLCV_COLUMNS].values
    print(f"[VERİ] Ham veri şekli: {values.shape}")

    # 2) Ham veriyi zaman sırasına göre böl (KARIŞTIRMA YOK)
    split_idx = int(len(values) * TRAIN_RATIO)
    train_raw = values[:split_idx]
    test_raw = values[split_idx:]

    # 3) Scaler'ı SADECE train üzerine fit et, ikisini de transform et
    scaler = MinMaxScaler(feature_range=(0, 1))
    scaler.fit(train_raw)
    train_scaled = scaler.transform(train_raw)
    test_scaled = scaler.transform(test_raw)

    # 4) Test dilimi için ilk pencerenin geçmişe ihtiyacı var; bu yüzden
    #    train'in son SEQUENCE_LENGTH mumunu test'in başına ekliyoruz.
    #    Böylece test sekansları kesintisiz ve leakage'siz üretilir.
    test_scaled_ext = np.concatenate(
        [train_scaled[-SEQUENCE_LENGTH:], test_scaled], axis=0
    )

    # 5) Pencereleme (sliding window)
    X_train, y_train = create_sequences(
        train_scaled, sequence_length=SEQUENCE_LENGTH, target_col_index=TARGET_COL_INDEX
    )
    X_test, y_test = create_sequences(
        test_scaled_ext, sequence_length=SEQUENCE_LENGTH, target_col_index=TARGET_COL_INDEX
    )
    print(f"[VERİ] X_train: {X_train.shape} | X_test: {X_test.shape}")

    # 6) NumPy -> PyTorch tensör
    X_train_t = torch.tensor(X_train, dtype=torch.float32)
    y_train_t = torch.tensor(y_train, dtype=torch.float32)
    X_test_t = torch.tensor(X_test, dtype=torch.float32)
    y_test_t = torch.tensor(y_test, dtype=torch.float32)

    # 7) DataLoader'lar (zaman serisi -> shuffle=False)
    train_loader = DataLoader(
        TensorDataset(X_train_t, y_train_t), batch_size=BATCH_SIZE, shuffle=False
    )
    test_loader = DataLoader(
        TensorDataset(X_test_t, y_test_t), batch_size=BATCH_SIZE, shuffle=False
    )

    return train_loader, test_loader, scaler


def evaluate(model, loader, criterion):
    """Bir veri kümesi üzerindeki ortalama loss'u hesaplar (gradyansız)."""
    model.eval()
    total_loss = 0.0
    total_samples = 0
    with torch.no_grad():
        for X_batch, y_batch in loader:
            X_batch = X_batch.to(DEVICE)
            y_batch = y_batch.to(DEVICE)
            preds = model(X_batch)
            loss = criterion(preds, y_batch)
            total_loss += loss.item() * X_batch.size(0)
            total_samples += X_batch.size(0)
    return total_loss / max(total_samples, 1)


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
    criterion = nn.MSELoss()

    print(f"\n[EĞİTİM] Cihaz: {DEVICE} | Epoch: {EPOCHS} | Batch: {BATCH_SIZE}")
    print("-" * 55)

    # ---- Eğitim döngüsü ----
    for epoch in range(1, EPOCHS + 1):
        model.train()
        running_loss = 0.0
        running_samples = 0

        for X_batch, y_batch in train_loader:
            X_batch = X_batch.to(DEVICE)
            y_batch = y_batch.to(DEVICE)

            optimizer.zero_grad()          # gradyanları sıfırla
            preds = model(X_batch)         # ileri geçiş (forward)
            loss = criterion(preds, y_batch)  # hatayı hesapla (MSE)
            loss.backward()                # geriye yayılım (backward)
            optimizer.step()               # ağırlıkları güncelle

            running_loss += loss.item() * X_batch.size(0)
            running_samples += X_batch.size(0)

        # Her 10 epoch'ta bir Train ve Test loss'u yazdır
        if epoch % 10 == 0 or epoch == 1:
            train_loss = running_loss / max(running_samples, 1)
            test_loss = evaluate(model, test_loader, criterion)
            print(
                f"Epoch [{epoch:>3}/{EPOCHS}]  "
                f"Train Loss: {train_loss:.6f}  |  Test Loss: {test_loss:.6f}"
            )

    print("-" * 55)
    print("[EĞİTİM] Tamamlandı.")

    # ---- Model ağırlıklarını ve scaler'ı diske kaydet ----
    torch.save(model.state_dict(), MODEL_PATH)
    joblib.dump(scaler, SCALER_PATH)
    print(f"[KAYIT] Model  -> {MODEL_PATH}")
    print(f"[KAYIT] Scaler -> {SCALER_PATH}")

    return model, scaler


if __name__ == "__main__":
    train()
