"""
inference.py — Canlı Tahmin ve Sinyal Üretimi (Inference)

Görevi:
  1. Diske kaydedilmiş modeli (trade_model.pth) ve scaler'ı (scaler.pkl)
     YÜKLEMEK. (Yeniden eğitim yapmaz; artefakt yoksa kullanıcıyı train.py'ye
     yönlendirir.)
  2. ccxt ile BTC/USDT'nin ANLIK son mumlarını çekip indikatörleri ekleyip
     (8 özellik) son `sequence_length` mumu ölçekli tensöre çevirmek.
  3. Model tahminini alıp inverse_transform ile gerçek USDT fiyatına çevirmek
     ve %0.2 eşik (threshold) mantığıyla AL / SAT / BEKLE sinyali üretmek.

NOT: Bu dosya SADECE tahmin ve sinyal üretir. Canlı emir (execution)
ve API key yönetimi kapsam dışıdır. Tüm veri GERÇEK piyasa verisidir.
"""

import os

import joblib
import numpy as np
import torch

from data_pipeline import fetch_ohlcv, add_indicators, FEATURE_COLUMNS, TARGET_COL_INDEX
from model import TradeAILSTM
import train  # hiperparametreler (INPUT_SIZE, SEQUENCE_LENGTH, yollar) için


# Sinyal eşiği: tahmin, mevcut fiyattan bu oranın üstünde/altında ise işlem sinyali
THRESHOLD = 0.002  # %0.2

# İndikatör ısınması için ekstra mum tamponu (RSI/MACD/ATR warm-up)
WARMUP_BUFFER = 100

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def load_model_and_scaler():
    """
    Diske kaydedilmiş modeli ve scaler'ı yükler.

    Artefakt dosyaları yoksa hata verir ve kullanıcıyı `python train.py`
    çalıştırmaya yönlendirir (bu dosya yeniden eğitim YAPMAZ).
    """
    if not (os.path.exists(train.MODEL_PATH) and os.path.exists(train.SCALER_PATH)):
        raise FileNotFoundError(
            f"Model/scaler bulunamadı ({train.MODEL_PATH}, {train.SCALER_PATH}). "
            f"Önce eğitimi çalıştırın:  python train.py"
        )

    print("[YÜKLE] Kayıtlı model ve scaler yükleniyor...")
    scaler = joblib.load(train.SCALER_PATH)

    model = TradeAILSTM(
        input_size=train.INPUT_SIZE,      # 8 (OHLCV + RSI + MACD + ATR)
        hidden_size=train.HIDDEN_SIZE,
        num_layers=train.NUM_LAYERS,
        output_size=train.OUTPUT_SIZE,
    ).to(DEVICE)
    model.load_state_dict(torch.load(train.MODEL_PATH, map_location=DEVICE))
    model.eval()

    return model, scaler


def fetch_latest_window(symbol="BTC/USDT", timeframe="1h", sequence_length=None):
    """
    Anlık veriyi çeker, indikatörleri ekler ve son `sequence_length` mumun
    8 özellikli ham matrisini döndürür.

    RSI/MACD/ATR ısınması için `sequence_length + WARMUP_BUFFER` mum çekilir,
    indikatörler eklenip NaN'ler atıldıktan sonra son N satır alınır.

    Dönüş
    -----
    window_raw : numpy.ndarray, şekil (sequence_length, 8)
    last_close : float  (en son mumun gerçek kapanış fiyatı, USDT)
    """
    if sequence_length is None:
        sequence_length = train.SEQUENCE_LENGTH

    df = fetch_ohlcv(
        symbol=symbol, timeframe=timeframe, limit=sequence_length + WARMUP_BUFFER
    )
    df = add_indicators(df)  # NaN'ler burada temizlenir

    if len(df) < sequence_length:
        raise ValueError(
            f"Yetersiz veri: indikatör sonrası {len(df)} mum var, "
            f"{sequence_length} gerekiyor. WARMUP_BUFFER'ı artırın."
        )

    window_df = df.iloc[-sequence_length:]
    window_raw = window_df[FEATURE_COLUMNS].values
    last_close = float(window_df["close"].iloc[-1])
    return window_raw, last_close


def predict_price(model, scaler, window_raw):
    """
    Ham (sequence_length, 8) pencereyi ölçekler, modele verir ve tahmin
    edilen kapanış fiyatını gerçek USDT değerine (inverse_transform) çevirir.
    """
    # 1) Ölçekle (scaler 8 özellik üzerine fit edilmiştir)
    window_scaled = scaler.transform(window_raw)

    # 2) LSTM girdisine dönüştür: (1, sequence_length, 8)
    x = torch.tensor(window_scaled, dtype=torch.float32).unsqueeze(0).to(DEVICE)

    # 3) Tahmin (ölçekli, 0-1 aralığında bir 'close' değeri)
    with torch.no_grad():
        pred_scaled = model(x).cpu().numpy().item()

    # 4) inverse_transform: 8 sütunlu dummy kurup yalnızca 'close'u geri çevir
    dummy = np.zeros((1, len(FEATURE_COLUMNS)))
    dummy[0, TARGET_COL_INDEX] = pred_scaled
    predicted_price = float(scaler.inverse_transform(dummy)[0, TARGET_COL_INDEX])

    return predicted_price


def generate_signal(current_price, predicted_price, threshold=THRESHOLD):
    """
    Eşik (threshold) mantığıyla sinyal üretir.

    Dönüş
    -----
    signal : int   ( 1 = AL, -1 = SAT, 0 = BEKLE )
    label  : str
    change : float (oransal değişim)
    """
    change = (predicted_price - current_price) / current_price

    if change > threshold:
        return 1, "AL", change
    elif change < -threshold:
        return -1, "SAT", change
    else:
        return 0, "BEKLE", change


if __name__ == "__main__":
    SYMBOL = "BTC/USDT"
    TIMEFRAME = "1h"

    # 1) Diskten model + scaler
    model, scaler = load_model_and_scaler()

    # 2) Anlık son 60 mum (indikatörlü, 8 özellik)
    print(f"\n[VERİ] {SYMBOL} anlık veri çekiliyor ({TIMEFRAME})...")
    window_raw, current_price = fetch_latest_window(symbol=SYMBOL, timeframe=TIMEFRAME)

    # 3) Tahmin + sinyal
    predicted_price = predict_price(model, scaler, window_raw)
    signal, label, change = generate_signal(current_price, predicted_price)

    # ----------------------- Sonuç -----------------------
    print("\n" + "=" * 45)
    print(f"  Sembol              : {SYMBOL} ({TIMEFRAME})")
    print(f"  Şu anki Fiyat       : {current_price:,.2f} USDT")
    print(f"  Tahmin Edilen Fiyat : {predicted_price:,.2f} USDT")
    print(f"  Beklenen Değişim    : {change * 100:+.3f} %  (eşik: ±{THRESHOLD * 100:.1f}%)")
    print(f"  Üretilen Sinyal     : {label} ({signal})")
    print("=" * 45)
