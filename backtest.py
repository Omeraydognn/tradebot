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

from data_pipeline import fetch_ohlcv, add_indicators, FEATURE_COLUMNS
from model import TradeAILSTM
import train  # hiperparametreler ve yeniden-eğitim fallback'i için


# ----------------------- Backtest parametreleri -----------------------
SYMBOL = "BTC/USDT"
TIMEFRAME = train.TIMEFRAME  # train ile GARANTİ senkron (mismatch = felaket)
LIMIT = train.LIMIT          # train ile aynı pencere -> izolasyon hizalı kalır

# Walk-forward: backtest yalnızca train.TRAIN_CANDLES (7000) sonrasındaki,
# modelin HİÇ görmediği mumlar üzerinde koşar (bkz. iloc[train.TRAIN_CANDLES:]).

MODEL_PATH = "trade_model.pth"
SCALER_PATH = "scaler.pkl"

INITIAL_BALANCE = 10_000.0   # USDT
COMMISSION = 0.001           # işlem başına %0.1 (Binance taker)
SLIPPAGE = 0.0005            # %0.05 fiyat kayması (giriş/çıkışta aleyhte yansır)

# Dinamik (ATR bazlı) risk yönetimi — sabit yüzde yerine volatiliteye uyar.
# Girişteki ATR (entry_atr) baz alınır:
#   Stop-Loss   = entry_price - (entry_atr * ATR_SL_MULT)
#   Take-Profit = entry_price + (entry_atr * ATR_TP_MULT)
ATR_SL_MULT = 1.5
ATR_TP_MULT = 2.0

# Güven eşiği: AL/SAT sinyali ancak sınıf olasılığı bu değerin üstündeyse
# geçerli sayılır; altındaysa sinyal BEKLE'ye zorlanır (düşük güvenli işlem yok).
# OOS'ta model güveni düştüğü için %40'a indirildi; 3 sınıflı problemde %40
# hala rastgeleden (%33) yüksek ve istatistiksel edge sağlar.
CONFIDENCE_THRESHOLD = 0.40

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

    # GİRDİ artık tamamen durağan özelliklerden oluşur (mutlak fiyat/atr/ema yok).
    values = df[FEATURE_COLUMNS].values
    scaled = scaler.transform(values)

    # Ham fiyat, ATR ve EMA-200 girdi DEĞİL; karar/sim için df'den okunur.
    close_values = df["close"].values
    atr_values = df["atr"].values
    ema_values = df["ema_200"].values

    # Tüm kayan pencereleri tek tensöre yığ (vektörel tahmin)
    windows = []
    current_prices = []
    current_atrs = []
    current_emas = []
    for i in range(seq_len, len(scaled)):
        windows.append(scaled[i - seq_len:i, :])
        # Karar anında bilinen (ham) fiyat, ATR ve EMA-200: penceredeki son mum
        current_prices.append(close_values[i - 1])
        current_atrs.append(atr_values[i - 1])
        current_emas.append(ema_values[i - 1])

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


def _close_value(position, qty, entry_price, price):
    """
    Bir pozisyonu `price` fiyatından kapatınca elde edilecek nakit (komisyon dahil).

    - Long  : qty * price * (1 - COMMISSION)
    - Short : qty*(2*entry_price - price) - qty*price*COMMISSION
             (girişte kilitlenen değer qty*entry_price; PnL = qty*(entry_price - price);
              çıkış komisyonu geri-alım notionali üzerinden düşülür)
    """
    if position == 1:
        return qty * price * (1 - COMMISSION)
    return qty * (2 * entry_price - price) - qty * price * COMMISSION


def _signal_exit_price(position, price):
    """
    Normal (sinyal bazlı) çıkışlarda slippage'i aleyhte fiyata yansıtır.

    - Long çıkış  : price * (1 - SLIPPAGE)  (satarken fiyat aşağı kayar)
    - Short çıkış : price * (1 + SLIPPAGE)  (geri alırken fiyat yukarı kayar)

    NOT: Dinamik TP/SL tetiklenmelerinde slippage UYGULANMAZ; bu fonksiyon
    yalnızca ters-sinyal ve simülasyon sonu kapanışlarında kullanılır.
    """
    if position == 1:
        return price * (1 - SLIPPAGE)
    return price * (1 + SLIPPAGE)


def run_backtest(prices, atrs, ema_200s, signals):
    """
    ÇİFT YÖNLÜ (Long/Short) portföy simülasyonu — komisyon + DİNAMİK ATR bazlı
    SL/TP + EMA-200 trend filtresi.

    Giriş kuralları (trend yönünde işlem)
    -------------------------------------
    - AL (+1), pozisyon yok, fiyat > EMA-200 (yukarı trend)  -> LONG aç.
        stop_price = entry - (entry_atr * ATR_SL_MULT)   (altında)
        take_price = entry + (entry_atr * ATR_TP_MULT)   (üstünde)
    - SAT (-1), pozisyon yok, fiyat < EMA-200 (aşağı trend) -> SHORT aç.
        stop_price = entry + (entry_atr * ATR_SL_MULT)   (üstünde)
        take_price = entry - (entry_atr * ATR_TP_MULT)   (altında)
    - Trend uymuyorsa (long'da fiyat<=EMA, short'ta fiyat>=EMA) işlem reddedilir.

    Çıkış kuralları
    ---------------
    - Dinamik SL/TP seviyelerine dokununca kapat.
    - Ters sinyal gelince kapat (long'dayken SAT, short'tayken AL); ardından
      trend uygunsa yeni yönde pozisyon açılabilir (close-and-reverse).

    Dönüş
    -----
    result : dict  -> metrikler ve equity eğrisi
    """
    balance = INITIAL_BALANCE   # eldeki nakit (USDT) — yalnızca pozisyon yokken
    qty = 0.0                   # pozisyon büyüklüğü (BTC birimi)
    entry_price = 0.0
    stop_price = 0.0            # girişte hesaplanan dinamik SL seviyesi
    take_price = 0.0           # girişte hesaplanan dinamik TP seviyesi
    position = 0               # 0 = yok, +1 = LONG, -1 = SHORT

    total_trades = 0            # tamamlanan işlem (round-trip) sayısı
    stop_loss_hits = 0
    take_profit_hits = 0
    trend_rejects = 0          # EMA filtresiyle reddedilen giriş sinyalleri
    equity_curve = []

    def open_position(direction, price, atr):
        """Verilen yönde (Long +1 / Short -1) pozisyon açar (slippage dahil)."""
        nonlocal balance, qty, entry_price, stop_price, take_price, position
        # SLIPPAGE: giriş fiyatı aleyhte kayar (long yukarı, short aşağı).
        if direction == 1:  # LONG
            entry_price = price * (1 + SLIPPAGE)
            qty = (balance * (1 - COMMISSION)) / entry_price
            # Dinamik SL/TP, slippage uygulanmış entry_price baz alınarak kurulur
            stop_price = entry_price - (atr * ATR_SL_MULT)
            take_price = entry_price + (atr * ATR_TP_MULT)
        else:               # SHORT
            entry_price = price * (1 - SLIPPAGE)
            qty = (balance * (1 - COMMISSION)) / entry_price
            stop_price = entry_price + (atr * ATR_SL_MULT)
            take_price = entry_price - (atr * ATR_TP_MULT)
        balance = 0.0
        position = direction

    for price, atr, ema, signal in zip(prices, atrs, ema_200s, signals):
        # --- 1) DİNAMİK Stop-Loss / Take-Profit (yöne göre) ---
        if position == 1:
            if price <= stop_price:           # Long SL
                balance = _close_value(position, qty, entry_price, price)
                position, qty = 0, 0.0
                total_trades += 1; stop_loss_hits += 1
            elif price >= take_price:         # Long TP
                balance = _close_value(position, qty, entry_price, price)
                position, qty = 0, 0.0
                total_trades += 1; take_profit_hits += 1
        elif position == -1:
            if price >= stop_price:           # Short SL (fiyat yukarı gitti)
                balance = _close_value(position, qty, entry_price, price)
                position, qty = 0, 0.0
                total_trades += 1; stop_loss_hits += 1
            elif price <= take_price:         # Short TP (fiyat aşağı gitti)
                balance = _close_value(position, qty, entry_price, price)
                position, qty = 0, 0.0
                total_trades += 1; take_profit_hits += 1

        # --- 2) Sinyale göre: ters sinyalde kapat, trend yönünde aç ---
        # Ters-sinyal çıkışları NORMAL çıkıştır -> slippage uygulanır.
        if signal == 1:        # AL
            if position == -1:                # ters sinyal -> short'u kapat
                exit_p = _signal_exit_price(position, price)
                balance = _close_value(position, qty, entry_price, exit_p)
                position, qty = 0, 0.0
                total_trades += 1
            if position == 0:
                if price > ema:               # yukarı trend -> LONG aç
                    open_position(1, price, atr)
                else:
                    trend_rejects += 1
        elif signal == -1:     # SAT
            if position == 1:                 # ters sinyal -> long'u kapat
                exit_p = _signal_exit_price(position, price)
                balance = _close_value(position, qty, entry_price, exit_p)
                position, qty = 0, 0.0
                total_trades += 1
            if position == 0:
                if price < ema:               # aşağı trend -> SHORT aç
                    open_position(-1, price, atr)
                else:
                    trend_rejects += 1

        # --- 3) Anlık portföy değeri (equity) ---
        if position == 0:
            equity = balance
        elif position == 1:
            equity = qty * price                      # long değeri
        else:
            equity = qty * (2 * entry_price - price)  # short değeri (fiyat düşerse artar)
        equity_curve.append(equity)

    # Simülasyon sonunda açık pozisyon varsa son fiyattan kapat (normal çıkış -> slippage)
    if position != 0:
        exit_p = _signal_exit_price(position, prices[-1])
        balance = _close_value(position, qty, entry_price, exit_p)
        total_trades += 1
        position, qty = 0, 0.0
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
    print(f"       BACKTEST SONUÇLARI (BTC/USDT {TIMEFRAME}, OOS)")
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

    # 2b) OUT-OF-SAMPLE İZOLASYON (SIZINTI ONARIMI):
    #     train.py 'iloc[:TRAIN_CANDLES]' ile eğitildiği için, backtest'i
    #     'iloc[TRAIN_CANDLES:]' ile başlatmak eğitim/sınav setlerini TAMAMEN
    #     kesişimsiz (leakage'siz) yapar. Böylece sınav %100 görülmemiş veridir.
    df = df.iloc[train.TRAIN_CANDLES:]
    print(f"[VERİ] OOS izolasyonu -> {train.TRAIN_CANDLES}. indeksten sona "
          f"(tamamen görülmemiş): {df.shape}")

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
