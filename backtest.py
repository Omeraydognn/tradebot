"""
backtest.py — Vektörel Geri Test (Backtest) ve Risk Yönetimi

Görevi:
  1. Eğitilmiş modeli + scaler'ı yüklemek (yoksa train.py ile üretmek),
     BTC/USDT'nin geçmiş 2000 mumunu (1h) çekip her mum için sinyal üretmek.
  2. 10.000 USDT sanal bakiye ile portföy simülasyonu; işlem başına
     %0.1 (0.001) Binance komisyonu.
  3. Sabit Stop-Loss: Long pozisyonda fiyat giriş fiyatının %2 altına
     düşerse pozisyon otomatik zararına kapatılır.
  4. Performans metrikleri: başlangıç/bitiş bakiyesi, net kâr/zarar %,
     toplam işlem sayısı, Max Drawdown (MDD).

NOT: Bu dosya SADECE geçmiş veri üzerinde simülasyon yapar. Canlı emir
(execution) ve API entegrasyonu kapsam dışıdır. Tüm veri GERÇEK piyasa
verisidir (ccxt / Binance).
"""

import os

import joblib
import numpy as np
import torch

from data_pipeline import (
    fetch_ohlcv,
    add_indicators,
    FEATURE_COLUMNS,
    CLOSE_COL_INDEX,
)
from model import TradeAILSTM
import train  # hiperparametreler ve yeniden-eğitim fallback'i için


# ----------------------- Backtest parametreleri -----------------------
SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"
LIMIT = 5000  # ~7 ay (sayfalama ile derin veri) — genişletilmiş backtest

MODEL_PATH = "trade_model.pth"
SCALER_PATH = "scaler.pkl"

INITIAL_BALANCE = 10_000.0   # USDT
COMMISSION = 0.001           # işlem başına %0.1 (Binance taker)
STOP_LOSS = 0.02             # %2 sabit stop-loss (long)
TAKE_PROFIT = 0.03           # %3 sabit take-profit (long)

# NOT: Eşik (threshold) mantığı artık burada YOK; yön kararı doğrudan modelin
# sınıf tahmininden (0=SAT, 1=BEKLE, 2=AL) gelir. Etiketleme eşiği eğitim
# verisine (data_pipeline.add_indicators) gömülüdür.

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def load_model_and_scaler():
    """
    Kayıtlı model (.pth) ve scaler (.pkl) varsa yükler; yoksa train.py'deki
    eğitim fonksiyonunu çağırıp gerçek veriyle üretir (ve diske kaydeder).
    """
    if os.path.exists(MODEL_PATH) and os.path.exists(SCALER_PATH):
        print("[YÜKLE] Kayıtlı model ve scaler bulundu, yükleniyor...")
        scaler = joblib.load(SCALER_PATH)
        model = TradeAILSTM(
            input_size=train.INPUT_SIZE,
            hidden_size=train.HIDDEN_SIZE,
            num_layers=train.NUM_LAYERS,
            output_size=train.OUTPUT_SIZE,
        ).to(DEVICE)
        model.load_state_dict(torch.load(MODEL_PATH, map_location=DEVICE))
    else:
        print("[YÜKLE] Kayıt bulunamadı -> train.py ile yeniden eğitiliyor...")
        model, scaler = train.train()          # gerçek veriyle eğit
        model = model.to(DEVICE)
        # İleride tekrar kullanmak için kaydet
        torch.save(model.state_dict(), MODEL_PATH)
        joblib.dump(scaler, SCALER_PATH)

    model.eval()
    return model, scaler


def generate_signals(model, scaler, df):
    """
    Geçmiş verinin her mumu için sinyal üretir (vektörel/batch tahmin).

    Model 3 sınıflı logits döndürür; argmax ile en yüksek olasılıklı sınıf
    seçilir ve işlem sinyaline çevrilir. Eşik (threshold) hesabı YOKTUR.

    Her karar noktasında:
      - Girdi  : son `seq_len` mumun ölçekli özellikleri  -> 3 sınıf logits
      - Sınıf  : argmax(logits) -> 0 (SAT) / 1 (BEKLE) / 2 (AL)
      - Sinyal : signal = sınıf - 1  -> -1 / 0 / +1

    Dönüş
    -----
    prices  : np.ndarray  -> karar anındaki mevcut kapanış fiyatları (USDT)
    signals : np.ndarray  -> her karar için +1 / -1 / 0
    """
    seq_len = train.SEQUENCE_LENGTH

    # 9 özellik: OHLCV + RSI + MACD + ATR + Returns
    values = df[FEATURE_COLUMNS].values
    scaled = scaler.transform(values)

    # Tüm kayan pencereleri tek tensöre yığ (vektörel tahmin)
    windows = []
    current_prices = []
    for i in range(seq_len, len(scaled)):
        windows.append(scaled[i - seq_len:i, :])
        # Karar anında bilinen (ham) fiyat: penceredeki son mumun kapanışı
        current_prices.append(values[i - 1, CLOSE_COL_INDEX])

    X = torch.tensor(np.array(windows), dtype=torch.float32).to(DEVICE)
    current_prices = np.array(current_prices)

    # Batch tahmin: 3 sınıf logits -> argmax -> sınıf (0/1/2)
    with torch.no_grad():
        logits = model(X)                                   # (N, 3)
        predicted_classes = torch.argmax(logits, dim=1).cpu().numpy()

    # Sınıfı sinyale çevir: 2(AL)->+1, 1(BEKLE)->0, 0(SAT)->-1  (sınıf - 1)
    signals = predicted_classes - 1

    return current_prices, signals


def run_backtest(prices, signals):
    """
    Long-only portföy simülasyonu (komisyon + stop-loss + take-profit dahil).

    Kurallar
    --------
    - AL (+1) ve pozisyon yokken  -> tüm bakiyeyle long aç.
    - SAT (-1) ve pozisyondayken  -> pozisyonu kapat.
    - Stop-Loss  : fiyat giriş * (1 - STOP_LOSS) altına düşerse zararına kapat.
    - Take-Profit: fiyat giriş * (1 + TAKE_PROFIT) üstüne çıkarsa kârla kapat.
    - Her alım ve satımda COMMISSION uygulanır.

    Not: Short (açığa satış) altyapısı olmadığından SL/TP yalnızca Long
    pozisyonlar üzerinden hesaplanır.

    Dönüş
    -----
    result : dict  -> metrikler ve equity eğrisi
    """
    balance = INITIAL_BALANCE   # eldeki nakit (USDT)
    position_qty = 0.0          # elde tutulan BTC miktarı
    entry_price = 0.0
    in_position = False

    total_trades = 0            # tamamlanan işlem (round-trip) sayısı
    stop_loss_hits = 0
    take_profit_hits = 0
    equity_curve = []

    for price, signal in zip(prices, signals):
        # --- 1) Risk yönetimi: Stop-Loss / Take-Profit (pozisyondayken) ---
        if in_position and price <= entry_price * (1 - STOP_LOSS):
            balance = position_qty * price * (1 - COMMISSION)  # zararına kapat
            in_position = False
            position_qty = 0.0
            total_trades += 1
            stop_loss_hits += 1
        elif in_position and price >= entry_price * (1 + TAKE_PROFIT):
            balance = position_qty * price * (1 - COMMISSION)  # kârla kapat
            in_position = False
            position_qty = 0.0
            total_trades += 1
            take_profit_hits += 1

        # --- 2) Sinyale göre aksiyon ---
        if signal == 1 and not in_position:
            # Long aç: nakit -> BTC (komisyon düşülür)
            position_qty = (balance * (1 - COMMISSION)) / price
            entry_price = price
            balance = 0.0
            in_position = True
        elif signal == -1 and in_position:
            # Pozisyonu kapat: BTC -> nakit (komisyon düşülür)
            balance = position_qty * price * (1 - COMMISSION)
            in_position = False
            position_qty = 0.0
            total_trades += 1

        # --- 3) Anlık portföy değeri (equity) ---
        equity = balance if not in_position else position_qty * price
        equity_curve.append(equity)

    # Simülasyon sonunda açık pozisyon varsa son fiyattan kapat
    if in_position:
        balance = position_qty * prices[-1] * (1 - COMMISSION)
        in_position = False
        total_trades += 1
        equity_curve[-1] = balance

    equity_curve = np.array(equity_curve)

    # --- Max Drawdown (MDD) ---
    running_max = np.maximum.accumulate(equity_curve)
    drawdowns = (equity_curve - running_max) / running_max
    max_drawdown = drawdowns.min() if len(drawdowns) else 0.0

    final_balance = equity_curve[-1] if len(equity_curve) else INITIAL_BALANCE
    net_pnl_pct = (final_balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100

    return {
        "initial_balance": INITIAL_BALANCE,
        "final_balance": final_balance,
        "net_pnl_pct": net_pnl_pct,
        "total_trades": total_trades,
        "stop_loss_hits": stop_loss_hits,
        "take_profit_hits": take_profit_hits,
        "max_drawdown_pct": max_drawdown * 100,
    }


def print_report(result):
    """Metrikleri temiz, okunaklı bir tablo halinde yazdırır."""
    line = "═" * 46
    print("\n" + line)
    print("            BACKTEST SONUÇLARI (BTC/USDT 1h)")
    print(line)
    print(f"  Başlangıç Bakiyesi   : {result['initial_balance']:>14,.2f} USDT")
    print(f"  Bitiş Bakiyesi       : {result['final_balance']:>14,.2f} USDT")
    print(f"  Net Kâr / Zarar      : {result['net_pnl_pct']:>13,.2f} %")
    print("  " + "-" * 42)
    print(f"  Toplam İşlem Sayısı  : {result['total_trades']:>14d}")
    print(f"  Stop-Loss Tetiklenme : {result['stop_loss_hits']:>14d}")
    print(f"  Take-Profit Tetikl.  : {result['take_profit_hits']:>14d}")
    print(f"  Max Drawdown (MDD)   : {result['max_drawdown_pct']:>13,.2f} %")
    print(line)


if __name__ == "__main__":
    # 1) Model + scaler
    model, scaler = load_model_and_scaler()

    # 2) Geçmiş mumlar + indikatörler (8 özellik)
    print(f"\n[VERİ] {SYMBOL} son {LIMIT} mum çekiliyor ({TIMEFRAME})...")
    df = fetch_ohlcv(symbol=SYMBOL, timeframe=TIMEFRAME, limit=LIMIT)
    df = add_indicators(df)  # RSI + MACD + ATR, NaN'ler temizlenir
    print(f"[VERİ] İndikatörlü veri şekli: {df.shape}  ({FEATURE_COLUMNS})")

    # 3) Sinyaller
    print("[SİNYAL] Geçmiş mumlar için tahmin/sinyal üretiliyor...")
    prices, signals = generate_signals(model, scaler, df)
    n_buy = int((signals == 1).sum())
    n_sell = int((signals == -1).sum())
    n_hold = int((signals == 0).sum())
    print(f"[SİNYAL] AL: {n_buy} | SAT: {n_sell} | BEKLE: {n_hold}")

    # 4) Simülasyon + rapor
    result = run_backtest(prices, signals)
    print_report(result)
