"""
data_pipeline.py — Veri Boru Hattı (Data Pipeline)

Görevi:
  1. Binance'ten geçmiş OHLCV verilerini çekmek (ccxt, sayfalama ile).
  2. `ta` ile teknik indikatörler (RSI, MACD, ATR) + yüzdelik getiri
     (returns = close.pct_change()) eklemek -> toplam 9 özellik.
  3. Girdi özelliklerini (X) MinMaxScaler ile ölçeklemek. Hedef (y) artık
     'close' değil, HAM (ölçeksiz) 'returns' sütunudur.
  4. Veriyi LSTM'in istediği 3B pencere yapısına dönüştürmek.

DURAĞANLIK (stationarity): Model artık non-stationary ham fiyatı değil,
durağan olan yüzdelik getiriyi (returns) tahmin eder.

NOT: Bu dosya SADECE veri hazırlığı yapar.
"""

import time

import ccxt
import numpy as np
import pandas as pd
import ta
from sklearn.preprocessing import MinMaxScaler


# Binance tek istekte en fazla bu kadar mum döndürür
MAX_CANDLES_PER_REQUEST = 1000


# Ham OHLCV sütunları (borsadan gelir; yardımcı — özellik türetmek ve
# simülasyon/fiyat için kullanılır)
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

# ===========================================================================
# STRATEJİ: MEAN-REVERSION (Ortalamaya Dönüş)
# ---------------------------------------------------------------------------
# Yön (momentum/trend) tahmini yerine AŞIRILIK ölçen özellikler kullanılır.
# Fiyat 20 mumluk ortalamadan çok saparsa (z-score |>2|) ortalamaya geri
# dönmesi beklenir. Girdiler durağan/aşırılık göstergeleridir.
# ===========================================================================
FEATURE_COLUMNS = [
    "price_zscore",      # (close - MA20) / STD20         -> fiyat aşırılığı (kaç std)
    "flow_imb_zscore",   # order-flow imbalance'ın 20-Z   -> agresif akış aşırılığı
    "rsi",               # RSI(14)                         -> aşırı alım/satım
    "atr",               # ATR(14)                         -> volatilite
    "volume",            # hacim                           -> katılım
    "vwap_distance",     # (close - VWAP) / close          -> hacim-ağırlıklı sapma
    "macd_hist",         # MACD histogram                  -> momentum tükenme
]

# Yardımcı (girdi olmayan) sütunlar — DataFrame'de bulunur, X'e girmez.
# close/high/low backtest & inference'ta fiyat/SL-TP için; bb_mid ortalama seviyesi;
# flow_imb & taker_buy z-score'un ham kaynağıdır.
HELPER_COLUMNS = [
    "open", "high", "low", "close", "taker_buy",
    "flow_imb", "bb_mid", "bb_high", "bb_low", "vwap",
]

# Hedef: SINIFLANDIRMA etiketi -> 0 (SAT), 1 (BEKLE), 2 (AL)
TARGET_COLUMN = "target_class"

# --- Mean-reversion parametreleri ---
ZSCORE_WINDOW = 20      # z-score / Bollinger penceresi
ZSCORE_THRESHOLD = 2.0  # |z| > 2 -> fiyat 'aşırı gerilmiş' sayılır
REVERSION_HORIZON = 3   # sonraki kaç mumda ortalamaya dönüş aranır

# Sınıf tanımları (okunabilirlik için)
CLASS_SELL = 0   # ortalamaya AŞAĞI dönecek (yukarı aşırılık)
CLASS_HOLD = 1   # aşırılık yok / dönüş yok
CLASS_BUY = 2    # ortalamaya YUKARI dönecek (aşağı aşırılık)


def fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=500, exchange_name="binance"):
    """
    Binance ham kline verisini (12 alan) sayfalama ile çeker. Standart OHLCV'ye
    ek olarak 'taker_buy' (agresif ALIŞ hacmi) alanını da saklar — order-flow
    imbalance özelliği bundan türetilir (bedava + geçmişe dönük).

    SAYFALAMA: Binance tek istekte en fazla 1000 mum döndürür; `startTime` ve
    bir `while` döngüsüyle parça parça birleştirilir, her sayfada time.sleep(1).

    NOT: Ham kline (publicGetKlines) Binance'e özgüdür; exchange_name yalnızca
    'binance' için geçerlidir (projede tek kullanılan borsa).

    Dönüş
    -----
    pandas.DataFrame  (sütunlar: open, high, low, close, volume, taker_buy)
    """
    exchange_class = getattr(ccxt, exchange_name)
    exchange = exchange_class({"enableRateLimit": True})

    timeframe_ms = exchange.parse_timeframe(timeframe) * 1000
    market_id = symbol.replace("/", "")  # 'BTC/USDT' -> 'BTCUSDT'
    since = exchange.milliseconds() - limit * timeframe_ms

    all_rows = []
    while len(all_rows) < limit:
        remaining = limit - len(all_rows)
        page_limit = min(MAX_CANDLES_PER_REQUEST, remaining)

        # Ham kline: [openTime,o,h,l,c,v,closeTime,quoteVol,nTrades,
        #             takerBuyBase(9), takerBuyQuote, ignore]
        batch = exchange.publicGetKlines({
            "symbol": market_id,
            "interval": timeframe,
            "startTime": int(since),
            "limit": page_limit,
        })
        if not batch:
            break

        all_rows += batch
        since = int(batch[-1][0]) + timeframe_ms

        if len(batch) < page_limit:
            break

        time.sleep(1)  # IP ban yememek için sayfalar arası bekleme

    records = [
        {
            "timestamp": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "taker_buy": float(k[9]),  # agresif alış hacmi (order-flow)
        }
        for k in all_rows
    ]

    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)

    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df.iloc[-limit:]


def add_indicators(df):
    """
    MEAN-REVERSION veri hazırlığı: aşırılık/z-score özellikleri + ortalamaya
    dönüş hedefi ekler, NaN satırlarını temizler.

    GİRDİ özellikleri (FEATURE_COLUMNS):
      - price_zscore     : (close - MA20) / STD20       -> fiyat aşırılığı
      - flow_imb_zscore  : order-flow imbalance'ın 20-Z  -> akış aşırılığı
      - rsi              : RSI(14)                         -> aşırı alım/satım
      - atr              : ATR(14)                         -> volatilite
      - volume           : hacim                           -> katılım

    Yardımcı sütunlar (df'de kalır, girdi değil): open/high/low/close,
    taker_buy, flow_imb, bb_mid/bb_high/bb_low.

    HEDEF (mean-reversion):
      MA20 = close.rolling(20).mean()  (ortalama; dönüş hedefi)
      - price_zscore < -2.0  VE  sonraki 3 mumun EN YÜKSEĞİ >= MA20  -> 2 (AL)
      - price_zscore > +2.0  VE  sonraki 3 mumun EN DÜŞÜĞÜ  <= MA20  -> 0 (SAT)
      - aksi halde                                                    -> 1 (BEKLE)

    NOT: Hedef ileriye bakar (sonraki H mum) — bu bir ETİKET; sızıntı değildir.
    Girdiler (X) yalnızca geçmişe bakar. Verinin son H satırının etiketi tam
    belirlenemez (gelecek yok) ve BEKLE'ye düşer; bu satırlar train hedefinde
    kullanılmaz (OOS kuyruğunda kalır), backtest/inference hedefi kullanmaz.

    Dönüş
    -----
    pandas.DataFrame  (yeni sütunlar eklenmiş, NaN'ler atılmış)
    """
    df = df.copy()
    W = ZSCORE_WINDOW
    H = REVERSION_HORIZON

    # --- Girdi indikatörleri ---
    df["rsi"] = ta.momentum.RSIIndicator(close=df["close"], window=14).rsi()
    df["atr"] = ta.volatility.AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=14
    ).average_true_range()

    # Bollinger Bands (20, 2) — fiyatın durağan bandı (yardımcı)
    bb = ta.volatility.BollingerBands(close=df["close"], window=W, window_dev=2)
    df["bb_mid"] = bb.bollinger_mavg()   # = MA20 (ortalama; dönüş hedefi)
    df["bb_high"] = bb.bollinger_hband()
    df["bb_low"] = bb.bollinger_lband()

    # Fiyat Z-Score (aşırılık): kaç standart sapma uzakta
    ma20 = df["close"].rolling(W).mean()
    std20 = df["close"].rolling(W).std()
    df["price_zscore"] = (df["close"] - ma20) / std20

    # VWAP (Hacim Ağırlıklı Ortalama Fiyat) — kümülatif
    cumvol = df["volume"].cumsum()
    cumtp = (df["close"] * df["volume"]).cumsum()
    df["vwap"] = cumtp / cumvol
    df["vwap_distance"] = (df["close"] - df["vwap"]) / df["close"]

    # MACD Histogram — momentum tükenme sinyali
    macd_indicator = ta.trend.MACD(close=df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["macd_hist"] = macd_indicator.macd_diff()

    # Order-flow imbalance [-1,1] + 20 mumluk rolling Z-Score (akış aşırılığı)
    df["flow_imb"] = (2 * df["taker_buy"] - df["volume"]) / df["volume"]
    df["flow_imb"] = df["flow_imb"].replace([np.inf, -np.inf], 0.0).fillna(0.0)
    f_mean = df["flow_imb"].rolling(W).mean()
    f_std = df["flow_imb"].rolling(W).std()
    df["flow_imb_zscore"] = (df["flow_imb"] - f_mean) / f_std

    # Bölme kaynaklı sonsuzları temizle (std=0 gibi dejenere durumlar)
    df.replace([np.inf, -np.inf], np.nan, inplace=True)

    # --- MEAN-REVERSION HEDEFİ (target_class) ---
    # Sonraki H mumun en yükseği / en düşüğü (ileriye bakan pencere = ETİKET):
    #   high.rolling(H).max().shift(-H) -> satır i için max(high[i+1..i+H])
    future_max_high = df["high"].rolling(H).max().shift(-H)
    future_min_low = df["low"].rolling(H).min().shift(-H)

    cond_buy = (df["price_zscore"] < -ZSCORE_THRESHOLD) & (future_max_high >= ma20)
    cond_sell = (df["price_zscore"] > ZSCORE_THRESHOLD) & (future_min_low <= ma20)
    df["target_class"] = np.select(
        [cond_buy, cond_sell], [CLASS_BUY, CLASS_SELL], default=CLASS_HOLD
    ).astype(int)

    # Isınma (rolling) kaynaklı NaN'leri temizle
    df.dropna(inplace=True)

    return df


def scale_features(df, scaler=None, feature_columns=FEATURE_COLUMNS):
    """
    SADECE girdi özelliklerini (X) MinMaxScaler ile 0-1 aralığına ölçekler.
    Hedef (returns) burada ÖLÇEKLENMEZ; ham haliyle ayrıca kullanılır.

    `scaler` verilirse yalnızca transform edilir (leakage'siz test/inference);
    verilmezse yeni bir scaler fit edilir.

    Dönüş
    -----
    scaled  : numpy.ndarray, şekil (n_samples, n_features)
    scaler  : MinMaxScaler
    """
    values = df[feature_columns].values
    if scaler is None:
        scaler = MinMaxScaler(feature_range=(0, 1))
        scaled = scaler.fit_transform(values)
    else:
        scaled = scaler.transform(values)
    return scaled, scaler


def create_sequences(scaled_features, raw_target, sequence_length=60):
    """
    Kayan pencere (sliding window) ile 3B girdi tensörünü ve HAM hedef
    vektörünü üretir.

    Her örnek:
        X -> son `sequence_length` mumun ÖLÇEKLİ özellikleri
        y -> bir sonraki mumun HAM (ölçeksiz) returns değeri

    Parametreler
    ----------
    scaled_features : numpy.ndarray, şekil (n_samples, n_features)
        Ölçeklenmiş girdi özellikleri.
    raw_target : numpy.ndarray, şekil (n_samples,)
        Ham (ölçeksiz) returns serisi.
    sequence_length : int

    Dönüş
    -----
    X : numpy.ndarray, şekil (batch_size, sequence_length, n_features)
    y : numpy.ndarray, şekil (batch_size, 1)  -> ham returns
    """
    raw_target = np.asarray(raw_target).reshape(-1)
    X, y = [], []

    for i in range(sequence_length, len(scaled_features)):
        X.append(scaled_features[i - sequence_length:i, :])
        y.append(raw_target[i])  # bir sonraki mumun hedefi (sınıf: 0/1/2)

    X = np.array(X)
    y = np.array(y).reshape(-1, 1)

    return X, y


if __name__ == "__main__":
    # --- Basit test senaryosu ---
    SYMBOL = "BTC/USDT"
    TIMEFRAME = "1h"
    LIMIT = 500
    SEQUENCE_LENGTH = 60

    print(f"1) {SYMBOL} verisi çekiliyor ({TIMEFRAME}, limit={LIMIT})...")
    df = fetch_ohlcv(symbol=SYMBOL, timeframe=TIMEFRAME, limit=LIMIT)
    print(f"   Ham DataFrame şekli: {df.shape}")

    print("2) Mean-reversion özellikleri + target_class ekleniyor...")
    df = add_indicators(df)
    print(f"   İşlenmiş şekil: {df.shape}  (özellikler: {FEATURE_COLUMNS})")
    print(f"   price_zscore aralığı: [{df['price_zscore'].min():.2f}, "
          f"{df['price_zscore'].max():.2f}] | |z|>2 oranı: "
          f"{(df['price_zscore'].abs() > ZSCORE_THRESHOLD).mean()*100:.1f}%")

    print("3) Girdi özellikleri ölçekleniyor (hedef sınıf ölçeklenmez)...")
    scaled, scaler = scale_features(df)
    raw_target = df[TARGET_COLUMN].values
    import numpy as _np
    classes, counts = _np.unique(raw_target, return_counts=True)
    print(f"   Ölçekli X şekli: {scaled.shape} | Sınıf dağılımı "
          f"(0=SAT,1=BEKLE,2=AL): {dict(zip(classes.tolist(), counts.tolist()))}")

    print(f"4) Pencereleme (sequence_length={SEQUENCE_LENGTH})...")
    X, y = create_sequences(scaled, raw_target, sequence_length=SEQUENCE_LENGTH)

    print("\n--- Sonuç Boyutları ---")
    print(f"X shape: {X.shape}   # (batch, seq, {len(FEATURE_COLUMNS)})")
    print(f"y shape: {y.shape}   # (batch, 1) -> sınıf etiketi (0/1/2)")
    print(f"y örnek : {y[:5].reshape(-1)}")
