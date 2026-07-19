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

# Dinamik (ATR bazlı) risk yönetimi — sabit yüzde yerine volatiliteye uyar.
# Girişteki ATR (entry_atr) baz alınır:
#   Stop-Loss   = entry_price - (entry_atr * ATR_SL_MULT)
#   Take-Profit = entry_price + (entry_atr * ATR_TP_MULT)
ATR_SL_MULT = 1.5
ATR_TP_MULT = 2.0

# Güven eşiği: AL/SAT sinyali ancak sınıf olasılığı bu değerin üstündeyse
# geçerli sayılır; altındaysa sinyal BEKLE'ye zorlanır (düşük güvenli işlem yok).
CONFIDENCE_THRESHOLD = 0.65

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

    Model 3 sınıflı logits döndürür. Logits softmax ile olasılığa çevrilir,
    argmax ile sınıf seçilir; ancak GÜVEN EŞİĞİ altındaki AL/SAT sinyalleri
    BEKLE'ye zorlanır (düşük güvenli işlem yok).

    Her karar noktasında:
      - Girdi  : son `seq_len` mumun ölçekli özellikleri  -> 3 sınıf logits
      - Olasılık: softmax(logits)
      - Sınıf  : argmax; AL/SAT ise ve olasılık < CONFIDENCE_THRESHOLD -> 1 (BEKLE)
      - Sinyal : signal = sınıf - 1  -> -1 / 0 / +1

    Dönüş
    -----
    prices   : np.ndarray -> karar anındaki mevcut kapanış fiyatları (USDT)
    atrs     : np.ndarray -> karar anındaki ATR değerleri (dinamik SL/TP için)
    ema_200s : np.ndarray -> karar anındaki EMA-200 değerleri (trend filtresi için)
    signals  : np.ndarray -> her karar için +1 / -1 / 0
    """
    seq_len = train.SEQUENCE_LENGTH
    atr_idx = FEATURE_COLUMNS.index("atr")
    ema_idx = FEATURE_COLUMNS.index("ema_200")

    # 11 özellik: OHLCV + RSI + MACD + ATR + EMA200 + Returns
    values = df[FEATURE_COLUMNS].values
    scaled = scaler.transform(values)

    # Tüm kayan pencereleri tek tensöre yığ (vektörel tahmin)
    windows = []
    current_prices = []
    current_atrs = []
    current_emas = []
    for i in range(seq_len, len(scaled)):
        windows.append(scaled[i - seq_len:i, :])
        # Karar anında bilinen (ham) fiyat, ATR ve EMA-200: penceredeki son mum
        current_prices.append(values[i - 1, CLOSE_COL_INDEX])
        current_atrs.append(values[i - 1, atr_idx])
        current_emas.append(values[i - 1, ema_idx])

    X = torch.tensor(np.array(windows), dtype=torch.float32).to(DEVICE)
    current_prices = np.array(current_prices)
    current_atrs = np.array(current_atrs)
    current_emas = np.array(current_emas)

    # Batch tahmin: logits -> softmax olasılıkları -> argmax sınıf
    with torch.no_grad():
        logits = model(X)                                   # (N, 3)
        probs = torch.softmax(logits, dim=1).cpu().numpy()  # (N, 3)
        predicted_classes = probs.argmax(axis=1)            # (N,)

    # --- GÜVEN EŞİĞİ ---
    # Seçilen sınıfın olasılığı; AL(2)/SAT(0) ise ve eşiğin altındaysa BEKLE(1) yap.
    chosen_conf = probs[np.arange(len(probs)), predicted_classes]
    low_conf_trade = (chosen_conf < CONFIDENCE_THRESHOLD) & (
        (predicted_classes == 2) | (predicted_classes == 0)
    )
    predicted_classes[low_conf_trade] = 1  # düşük güvenli AL/SAT -> BEKLE

    # Sınıfı sinyale çevir: 2(AL)->+1, 1(BEKLE)->0, 0(SAT)->-1  (sınıf - 1)
    signals = predicted_classes - 1

    return current_prices, current_atrs, current_emas, signals


def run_backtest(prices, atrs, ema_200s, signals):
    """
    Long-only portföy simülasyonu (komisyon + DİNAMİK ATR bazlı SL/TP +
    EMA-200 trend filtresi dahil).

    Kurallar
    --------
    - AL (+1), pozisyon yok VE fiyat > EMA-200 (yukarı trend)  -> long aç.
      Girişteki ATR (entry_atr) ile dinamik seviyeler hesaplanır:
          stop_price = entry_price - (entry_atr * ATR_SL_MULT)
          take_price = entry_price + (entry_atr * ATR_TP_MULT)
    - Trend Filtresi: AL sinyali gelse bile fiyat EMA-200'ün ALTINDAysa
      işlem REDDEDİLİR (aşağı trendde long açma).
    - SAT (-1) ve pozisyondayken  -> pozisyonu kapat.
    - Stop-Loss  : fiyat stop_price'a (veya altına) inerse zararına kapat.
    - Take-Profit: fiyat take_price'a (veya üstüne) çıkarsa kârla kapat.
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
    stop_price = 0.0            # girişte hesaplanan dinamik SL seviyesi
    take_price = 0.0           # girişte hesaplanan dinamik TP seviyesi
    in_position = False

    total_trades = 0            # tamamlanan işlem (round-trip) sayısı
    stop_loss_hits = 0
    take_profit_hits = 0
    trend_rejects = 0          # EMA filtresiyle reddedilen AL sinyalleri
    equity_curve = []

    for price, atr, ema, signal in zip(prices, atrs, ema_200s, signals):
        # --- 1) Risk yönetimi: DİNAMİK Stop-Loss / Take-Profit ---
        if in_position and price <= stop_price:
            balance = position_qty * price * (1 - COMMISSION)  # zararına kapat
            in_position = False
            position_qty = 0.0
            total_trades += 1
            stop_loss_hits += 1
        elif in_position and price >= take_price:
            balance = position_qty * price * (1 - COMMISSION)  # kârla kapat
            in_position = False
            position_qty = 0.0
            total_trades += 1
            take_profit_hits += 1

        # --- 2) Sinyale göre aksiyon ---
        if signal == 1 and not in_position:
            # TREND FİLTRESİ: yalnızca fiyat EMA-200 üstündeyse (yukarı trend) al
            if price > ema:
                # Long aç: nakit -> BTC (komisyon düşülür)
                position_qty = (balance * (1 - COMMISSION)) / price
                entry_price = price
                # Girişteki ATR'ye göre dinamik SL/TP seviyelerini sabitle
                stop_price = entry_price - (atr * ATR_SL_MULT)
                take_price = entry_price + (atr * ATR_TP_MULT)
                balance = 0.0
                in_position = True
            else:
                trend_rejects += 1  # aşağı trend -> AL reddedildi
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
        "trend_rejects": trend_rejects,
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
    print(f"  Trend Filtre Reddi   : {result['trend_rejects']:>14d}")
    print(f"  Max Drawdown (MDD)   : {result['max_drawdown_pct']:>13,.2f} %")
    print(line)


if __name__ == "__main__":
    # 1) Model + scaler
    model, scaler = load_model_and_scaler()

    # 2) Geçmiş mumlar + indikatörler (RSI, MACD, ATR, EMA-200, returns)
    print(f"\n[VERİ] {SYMBOL} son {LIMIT} mum çekiliyor ({TIMEFRAME})...")
    df = fetch_ohlcv(symbol=SYMBOL, timeframe=TIMEFRAME, limit=LIMIT)
    df = add_indicators(df)  # indikatörler + returns, NaN'ler temizlenir
    print(f"[VERİ] İndikatörlü veri şekli: {df.shape}  ({FEATURE_COLUMNS})")

    # 3) Sinyaller (+ dinamik SL/TP için ATR, trend filtresi için EMA-200)
    print("[SİNYAL] Geçmiş mumlar için tahmin/sinyal üretiliyor...")
    prices, atrs, ema_200s, signals = generate_signals(model, scaler, df)
    n_buy = int((signals == 1).sum())
    n_sell = int((signals == -1).sum())
    n_hold = int((signals == 0).sum())
    print(f"[SİNYAL] AL: {n_buy} | SAT: {n_sell} | BEKLE: {n_hold}  "
          f"(güven eşiği: %{CONFIDENCE_THRESHOLD*100:.0f})")

    # 4) Simülasyon + rapor
    result = run_backtest(prices, atrs, ema_200s, signals)
    print_report(result)
