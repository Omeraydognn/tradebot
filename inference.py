"""
inference.py — Canlı Tahmin ve Sinyal Üretimi (Inference)

Görevi:
  1. Diske kaydedilmiş modeli (trade_model.pth) ve scaler'ı (scaler.pkl)
     YÜKLEMEK. (Yeniden eğitim yapmaz; artefakt yoksa kullanıcıyı train.py'ye
     yönlendirir.)
  2. ccxt ile BTC/USDT'nin ANLIK son mumlarını çekip indikatör + returns
     ekleyip (9 özellik) son `sequence_length` mumu ölçekli tensöre çevirmek.
  3. Model 3 sınıflı olasılık vektörü (logits) döndürür. argmax ile en yüksek
     olasılıklı sınıf (0=SAT, 1=BEKLE, 2=AL) seçilir. Eşik (threshold) mantığı
     YOKTUR — o mantık zaten eğitim etiketlerine (data_pipeline) gömülüdür.

NOT: Bu dosya SADECE tahmin ve sinyal üretir. Canlı emir (execution)
ve API key yönetimi kapsam dışıdır. Tüm veri GERÇEK piyasa verisidir.
"""

import os

import joblib
import torch

from data_pipeline import fetch_ohlcv, add_indicators, FEATURE_COLUMNS
from model import TradeAILSTM
import train  # hiperparametreler (INPUT_SIZE, SEQUENCE_LENGTH, yollar) için


# İndikatör ısınması için ekstra mum tamponu (RSI/MACD/ATR + pct_change)
WARMUP_BUFFER = 100

# Sınıf -> (etiket, işlem sinyali) eşlemesi
# Sınıf 0=SAT -> -1, Sınıf 1=BEKLE -> 0, Sınıf 2=AL -> +1  (yani signal = sınıf - 1)
CLASS_LABELS = {0: "SAT", 1: "BEKLE", 2: "AL"}

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
        input_size=train.INPUT_SIZE,      # 9 (OHLCV + RSI + MACD + ATR + Returns)
        hidden_size=train.HIDDEN_SIZE,
        num_layers=train.NUM_LAYERS,
        output_size=train.OUTPUT_SIZE,    # 3 sınıf (logits)
    ).to(DEVICE)
    model.load_state_dict(torch.load(train.MODEL_PATH, map_location=DEVICE))
    model.eval()

    return model, scaler


def fetch_latest_window(symbol="BTC/USDT", timeframe="1h", sequence_length=None):
    """
    Anlık veriyi çeker, indikatör + returns ekler ve son `sequence_length`
    mumun 9 özellikli ham matrisini döndürür.

    Dönüş
    -----
    window_raw : numpy.ndarray, şekil (sequence_length, 9)
    last_close : float  (en son mumun gerçek kapanış fiyatı, USDT)
    """
    if sequence_length is None:
        sequence_length = train.SEQUENCE_LENGTH

    df = fetch_ohlcv(
        symbol=symbol, timeframe=timeframe, limit=sequence_length + WARMUP_BUFFER
    )
    df = add_indicators(df)  # indikatör + returns + target_class, NaN'ler temizlenir

    if len(df) < sequence_length:
        raise ValueError(
            f"Yetersiz veri: işlem sonrası {len(df)} mum var, "
            f"{sequence_length} gerekiyor. WARMUP_BUFFER'ı artırın."
        )

    window_df = df.iloc[-sequence_length:]
    window_raw = window_df[FEATURE_COLUMNS].values
    last_close = float(window_df["close"].iloc[-1])
    return window_raw, last_close


def predict_class(model, scaler, window_raw):
    """
    Ham (sequence_length, 9) pencerenin GİRDİ özelliklerini ölçekler, modele
    verir ve argmax ile en yüksek olasılıklı SINIFI (0/1/2) döndürür.

    Dönüş
    -----
    predicted_class : int   (0=SAT, 1=BEKLE, 2=AL)
    probs           : list  (3 sınıfın softmax olasılıkları)
    """
    window_scaled = scaler.transform(window_raw)
    x = torch.tensor(window_scaled, dtype=torch.float32).unsqueeze(0).to(DEVICE)

    with torch.no_grad():
        logits = model(x)                                  # (1, 3)
        probs = torch.softmax(logits, dim=1).cpu().numpy().reshape(-1)
        predicted_class = int(torch.argmax(logits, dim=1).item())

    return predicted_class, probs.tolist()


def class_to_signal(predicted_class):
    """
    Sınıfı işlem sinyaline çevirir:
      Sınıf 2 (AL)    -> +1
      Sınıf 1 (BEKLE) ->  0
      Sınıf 0 (SAT)   -> -1
    (Matematiksel olarak: signal = predicted_class - 1)
    """
    return predicted_class - 1


if __name__ == "__main__":
    SYMBOL = "BTC/USDT"
    TIMEFRAME = "1h"

    # 1) Diskten model + scaler
    model, scaler = load_model_and_scaler()

    # 2) Anlık son 60 mum (indikatör + returns, 9 özellik)
    print(f"\n[VERİ] {SYMBOL} anlık veri çekiliyor ({TIMEFRAME})...")
    window_raw, current_price = fetch_latest_window(symbol=SYMBOL, timeframe=TIMEFRAME)

    # 3) Sınıf tahmini (argmax) -> işlem sinyali
    predicted_class, probs = predict_class(model, scaler, window_raw)
    signal = class_to_signal(predicted_class)
    label = CLASS_LABELS[predicted_class]

    # ----------------------- Sonuç -----------------------
    print("\n" + "=" * 52)
    print(f"  Sembol                : {SYMBOL} ({TIMEFRAME})")
    print(f"  Şu anki Fiyat         : {current_price:,.2f} USDT")
    print(f"  Sınıf Olasılıkları    : SAT %{probs[0]*100:.1f} | "
          f"BEKLE %{probs[1]*100:.1f} | AL %{probs[2]*100:.1f}")
    print(f"  Tahmin Edilen Sınıf   : {predicted_class} ({label})")
    print(f"  Üretilen Sinyal       : {signal}")
    print("=" * 52)
