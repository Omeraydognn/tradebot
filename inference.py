"""
inference.py — Canlı Tahmin ve Sinyal Üretimi (Inference)

Görevi:
  1. Diske kaydedilmiş modeli (trade_model.pth) ve scaler'ı (scaler.pkl) YÜKLEMEK.
  2. ccxt ile BTC/USDT'nin ANLIK son mumlarını çekip indikatör + returns + ADX +
     ema_dist ekleyip (11 özellik) son `sequence_length` mumu ölçekli tensöre çevirmek.
  3. Model 3 sınıflı logits döndürür. KARAR MANTIĞI BACKTEST İLE BİREBİR AYNIDIR:
       - softmax olasılığı,
       - GÜVEN EŞİĞİ (CONFIDENCE_THRESHOLD): düşük güvenli AL/SAT -> BEKLE,
       - EMA-200 TREND FİLTRESİ: AL yalnızca fiyat>EMA, SAT yalnızca fiyat<EMA.
  4. Sinyal AL/SAT ise, backtest'le AYNI slippage + dinamik ATR SL/TP seviyelerini
     hesaplayarak eksiksiz bir 'işlem planı' üretir.

Böylece CANLI sinyal, out-of-sample'da doğrulanan stratejiyle tam olarak örtüşür.

NOT: Bu dosya SADECE tahmin ve sinyal/plan üretir. Canlı emir (execution) ve API
key yönetimi kapsam dışıdır. Tüm veri GERÇEK piyasa verisidir.
"""

import os

import joblib
import numpy as np
import torch

from data_pipeline import fetch_ohlcv, add_indicators, FEATURE_COLUMNS
from model import TradeAILSTM
import train  # hiperparametreler (INPUT_SIZE, SEQUENCE_LENGTH, yollar)
# Karar/risk sabitlerini BACKTEST'ten al -> tek doğruluk kaynağı, sürüklenme (drift) olmaz
from backtest import CONFIDENCE_THRESHOLD, ATR_SL_MULT, ATR_TP_MULT, SLIPPAGE


# İndikatör ısınması için ekstra mum tamponu (EMA-200 için >=200 gerekir)
WARMUP_BUFFER = 250

CLASS_LABELS = {0: "SAT", 1: "BEKLE", 2: "AL"}

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def load_model_and_scaler():
    """
    Diske kaydedilmiş modeli ve scaler'ı yükler. Artefakt yoksa kullanıcıyı
    `python train.py` çalıştırmaya yönlendirir (bu dosya yeniden eğitim YAPMAZ).
    """
    if not (os.path.exists(train.MODEL_PATH) and os.path.exists(train.SCALER_PATH)):
        raise FileNotFoundError(
            f"Model/scaler bulunamadı ({train.MODEL_PATH}, {train.SCALER_PATH}). "
            f"Önce eğitimi çalıştırın:  python train.py"
        )

    print("[YÜKLE] Kayıtlı model ve scaler yükleniyor...")
    scaler = joblib.load(train.SCALER_PATH)

    model = TradeAILSTM(
        input_size=train.INPUT_SIZE,      # 11 (OHLCV + RSI + MACD + ATR + ADX + ema_dist + Returns)
        hidden_size=train.HIDDEN_SIZE,
        num_layers=train.NUM_LAYERS,
        output_size=train.OUTPUT_SIZE,    # 3 sınıf (logits)
    ).to(DEVICE)
    model.load_state_dict(torch.load(train.MODEL_PATH, map_location=DEVICE))
    model.eval()

    return model, scaler


def fetch_latest_window(symbol="BTC/USDT", timeframe="1h", sequence_length=None):
    """
    Anlık veriyi çeker, indikatör + returns ekler ve son `sequence_length` mumun
    11 özellikli ham matrisini + karar için gereken ham değerleri döndürür.

    Dönüş
    -----
    window_raw : numpy.ndarray, şekil (sequence_length, 11)
    last_close : float  (en son mumun kapanışı, USDT)
    last_atr   : float  (dinamik SL/TP için)
    last_ema   : float  (EMA-200, trend filtresi için)
    """
    if sequence_length is None:
        sequence_length = train.SEQUENCE_LENGTH

    df = fetch_ohlcv(
        symbol=symbol, timeframe=timeframe, limit=sequence_length + WARMUP_BUFFER
    )
    df = add_indicators(df)  # indikatör + returns (+ yardımcı ema_200), NaN temizlenir

    if len(df) < sequence_length:
        raise ValueError(
            f"Yetersiz veri: işlem sonrası {len(df)} mum var, "
            f"{sequence_length} gerekiyor. WARMUP_BUFFER'ı artırın."
        )

    window_df = df.iloc[-sequence_length:]
    window_raw = window_df[FEATURE_COLUMNS].values
    last_close = float(window_df["close"].iloc[-1])
    last_atr = float(window_df["atr"].iloc[-1])
    last_ema = float(window_df["ema_200"].iloc[-1])
    return window_raw, last_close, last_atr, last_ema


def predict_probs(model, scaler, window_raw):
    """Pencereyi ölçekler, modele verir ve 3 sınıfın softmax olasılıklarını döndürür."""
    window_scaled = scaler.transform(window_raw)
    x = torch.tensor(window_scaled, dtype=torch.float32).unsqueeze(0).to(DEVICE)
    with torch.no_grad():
        logits = model(x)                                   # (1, 3)
        probs = torch.softmax(logits, dim=1).cpu().numpy().reshape(-1)
    return probs


def decide_signal(probs, current_price, ema):
    """
    BACKTEST İLE AYNI karar mantığı: argmax -> güven eşiği -> trend filtresi.

    Dönüş
    -----
    final_class : int   (0/1/2)  filtrelerden SONRAKİ nihai sınıf
    signal      : int   (-1/0/1) = final_class - 1
    raw_class   : int   (filtreden önceki ham argmax sınıfı)
    confidence  : float (ham sınıfın olasılığı)
    reason      : str   (kararın gerekçesi)
    """
    raw_class = int(np.argmax(probs))
    confidence = float(probs[raw_class])

    # 1) GÜVEN EŞİĞİ — düşük güvenli AL/SAT -> BEKLE
    if raw_class in (0, 2) and confidence < CONFIDENCE_THRESHOLD:
        return 1, 0, raw_class, confidence, (
            f"düşük güven %{confidence*100:.1f} < %{CONFIDENCE_THRESHOLD*100:.0f} -> BEKLE"
        )

    # 2) TREND FİLTRESİ — trend yönünde işlem
    if raw_class == 2 and current_price <= ema:
        return 1, 0, raw_class, confidence, "AL sinyali ama fiyat EMA-200 altında -> BEKLE (trend reddi)"
    if raw_class == 0 and current_price >= ema:
        return 1, 0, raw_class, confidence, "SAT sinyali ama fiyat EMA-200 üstünde -> BEKLE (trend reddi)"

    # Onaylandı
    return raw_class, raw_class - 1, raw_class, confidence, "onaylandı (güven + trend uygun)"


def build_trade_plan(signal, current_price, atr):
    """
    Onaylanmış AL/SAT sinyali için backtest'le AYNI slippage + dinamik ATR
    seviyelerini içeren işlem planını döndürür (sinyal 0 ise None).
    """
    if signal == 1:   # LONG
        entry = current_price * (1 + SLIPPAGE)
        stop = entry - atr * ATR_SL_MULT
        take = entry + atr * ATR_TP_MULT
        return {"yön": "LONG", "entry": entry, "stop_loss": stop, "take_profit": take}
    if signal == -1:  # SHORT
        entry = current_price * (1 - SLIPPAGE)
        stop = entry + atr * ATR_SL_MULT
        take = entry - atr * ATR_TP_MULT
        return {"yön": "SHORT", "entry": entry, "stop_loss": stop, "take_profit": take}
    return None


if __name__ == "__main__":
    SYMBOL = "BTC/USDT"
    TIMEFRAME = train.TIMEFRAME  # eğitimle aynı zaman dilimi

    # 1) Diskten model + scaler
    model, scaler = load_model_and_scaler()

    # 2) Anlık pencere + karar için ham değerler
    print(f"\n[VERİ] {SYMBOL} anlık veri çekiliyor ({TIMEFRAME})...")
    window_raw, current_price, current_atr, current_ema = fetch_latest_window(
        symbol=SYMBOL, timeframe=TIMEFRAME
    )

    # 3) Tahmin + BACKTEST ile aynı karar mantığı (güven eşiği + trend filtresi)
    probs = predict_probs(model, scaler, window_raw)
    final_class, signal, raw_class, confidence, reason = decide_signal(
        probs, current_price, current_ema
    )
    plan = build_trade_plan(signal, current_price, current_atr)

    # ----------------------- Sonuç -----------------------
    print("\n" + "=" * 56)
    print(f"  Sembol                : {SYMBOL} ({TIMEFRAME})")
    print(f"  Şu anki Fiyat         : {current_price:,.2f} USDT")
    print(f"  EMA-200               : {current_ema:,.2f} USDT  "
          f"({'YUKARI trend' if current_price > current_ema else 'AŞAĞI trend'})")
    print(f"  Sınıf Olasılıkları    : SAT %{probs[0]*100:.1f} | "
          f"BEKLE %{probs[1]*100:.1f} | AL %{probs[2]*100:.1f}")
    print(f"  Ham Tahmin            : {CLASS_LABELS[raw_class]} (güven %{confidence*100:.1f})")
    print(f"  Karar                 : {reason}")
    print(f"  NİHAİ SİNYAL          : {CLASS_LABELS[final_class]} ({signal})")
    if plan:
        print("  " + "-" * 50)
        print(f"  İŞLEM PLANI ({plan['yön']}) — slippage + ATR dinamik seviyeler")
        print(f"    Giriş (entry)     : {plan['entry']:,.2f} USDT")
        print(f"    Stop-Loss         : {plan['stop_loss']:,.2f} USDT")
        print(f"    Take-Profit       : {plan['take_profit']:,.2f} USDT")
    print("=" * 56)
