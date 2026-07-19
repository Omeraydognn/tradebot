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


# Ham OHLCV sütunları
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

# Teknik indikatör sütunları
INDICATOR_COLUMNS = ["rsi", "macd", "atr"]

# Yüzdelik getiri sütunu (durağan; hem girdi özelliği hem de etiketin kaynağı)
RETURNS_COLUMN = "returns"

# Modelin GİRDİ olarak gördüğü özellik seti (5 + 3 + 1 = 9 özellik)
FEATURE_COLUMNS = OHLCV_COLUMNS + INDICATOR_COLUMNS + [RETURNS_COLUMN]

# Hedef: SINIFLANDIRMA etiketi -> 0 (SAT), 1 (BEKLE), 2 (AL)
TARGET_COLUMN = "target_class"

# Etiketleme eşiği katsayısı: atr_threshold = (atr/close) * LABEL_ATR_MULTIPLIER
# (Eskiden inference/backtest'te olan eşik mantığı artık eğitim verisine gömülü.)
LABEL_ATR_MULTIPLIER = 0.5

# Sınıf tanımları (okunabilirlik için)
CLASS_SELL = 0   # AŞAĞI
CLASS_HOLD = 1   # YATAY
CLASS_BUY = 2    # YUKARI

# Simülasyonda gereken ham sütun indeksi (FEATURE_COLUMNS içinde)
CLOSE_COL_INDEX = FEATURE_COLUMNS.index("close")  # = 3


def fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=500, exchange_name="binance"):
    """
    Bir borsadan (varsayılan: Binance) geçmiş OHLCV verisi çeker.

    SAYFALAMA (pagination): Binance tek istekte en fazla 1000 mum döndürdüğü
    için, `limit` 1000'den büyükse `since` parametresi ve bir `while` döngüsü
    ile veriler parça parça çekilip birleştirilir. Her sayfadan sonra IP ban
    riskini azaltmak için `time.sleep(1)` beklenir.

    Dönüş
    -----
    pandas.DataFrame  (sütunlar: [open, high, low, close, volume], index: timestamp)
    """
    exchange_class = getattr(ccxt, exchange_name)
    exchange = exchange_class({"enableRateLimit": True})

    # timeframe'in milisaniye cinsinden süresi (ör. '1h' -> 3_600_000 ms)
    timeframe_ms = exchange.parse_timeframe(timeframe) * 1000

    # Başlangıç: en yeni `limit` mumu kapsayacak şekilde geçmişe git
    since = exchange.milliseconds() - limit * timeframe_ms

    all_candles = []
    while len(all_candles) < limit:
        remaining = limit - len(all_candles)
        page_limit = min(MAX_CANDLES_PER_REQUEST, remaining)

        batch = exchange.fetch_ohlcv(
            symbol, timeframe=timeframe, since=since, limit=page_limit
        )
        if not batch:
            break

        all_candles += batch
        since = batch[-1][0] + timeframe_ms

        if len(batch) < page_limit:
            break

        time.sleep(1)  # IP ban yememek için sayfalar arası bekleme

    df = pd.DataFrame(all_candles, columns=["timestamp"] + OHLCV_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)

    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df.iloc[-limit:]


def add_indicators(df):
    """
    DataFrame'e teknik indikatörleri + yüzdelik getiriyi ekler ve
    NaN satırlarını temizler.

    Eklenen sütunlar:
      - rsi          : RSI(14)   -> aşırı alım/satım
      - macd         : MACD(12, 26)  -> trend momentumu (MACD çizgisi)
      - atr          : ATR(14)   -> volatilite
      - returns      : close.pct_change()  -> DURAĞAN yüzdelik getiri (girdi + etiket kaynağı)
      - target_class : 0 (SAT) / 1 (BEKLE) / 2 (AL)  -> SINIFLANDIRMA hedefi

    Etiketleme mantığı (ATR bazlı dinamik eşik doğrudan veriye gömülür):
      atr_threshold = (atr / close) * LABEL_ATR_MULTIPLIER
        returns >  atr_threshold  -> 2 (AL/YUKARI)
        returns < -atr_threshold  -> 0 (SAT/AŞAĞI)
        aksi halde                -> 1 (BEKLE/YATAY)

    Dönüş
    -----
    pandas.DataFrame  (yeni sütunlar eklenmiş, NaN'ler atılmış)
    """
    df = df.copy()

    # RSI (14)
    df["rsi"] = ta.momentum.RSIIndicator(close=df["close"], window=14).rsi()

    # MACD (12, 26, 9) -> MACD çizgisi
    macd = ta.trend.MACD(close=df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["macd"] = macd.macd()

    # ATR (14)
    df["atr"] = ta.volatility.AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=14
    ).average_true_range()

    # Yüzdelik getiri: bir önceki muma göre % değişim (durağan seri)
    df["returns"] = df["close"].pct_change()

    # --- SINIFLANDIRMA HEDEFİ (target_class) ---
    # ATR bazlı dinamik eşik: fiyatın yüzdesi olarak volatilite bandı
    atr_threshold = (df["atr"] / df["close"]) * LABEL_ATR_MULTIPLIER
    df["target_class"] = np.select(
        [df["returns"] > atr_threshold, df["returns"] < -atr_threshold],
        [CLASS_BUY, CLASS_SELL],
        default=CLASS_HOLD,
    ).astype(int)

    # İndikatör ısınması + pct_change kaynaklı NaN'leri temizle
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

    print("2) İndikatör + returns + target_class ekleniyor...")
    df = add_indicators(df)
    print(f"   İşlenmiş şekil: {df.shape}  (özellikler: {FEATURE_COLUMNS})")

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
