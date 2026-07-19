"""
data_pipeline.py — Veri Boru Hattı (Data Pipeline)

Görevi:
  1. Binance'ten geçmiş OHLCV verilerini çekmek (ccxt).
  2. `ta` kütüphanesiyle teknik indikatörler eklemek (RSI, MACD, ATR)
     -> toplam 8 özellik.
  3. Veriyi 0-1 arasına ölçeklendirmek (MinMaxScaler) ve scaler'ı saklamak.
  4. Ölçeklenmiş veriyi LSTM'in istediği 3B pencere yapısına dönüştürmek.

NOT: Bu dosya SADECE veri hazırlığı yapar. Eğitim, backtest ve
al-sat işlemleri kapsam dışıdır.
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

# Modelin gördüğü nihai özellik seti (5 + 3 = 8 özellik)
FEATURE_COLUMNS = OHLCV_COLUMNS + INDICATOR_COLUMNS

# Hedef (tahmin edilecek) sütun ve indeksi -> 'close'
TARGET_COLUMN = "close"
TARGET_COL_INDEX = FEATURE_COLUMNS.index(TARGET_COLUMN)  # = 3


def fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=500, exchange_name="binance"):
    """
    Bir borsadan (varsayılan: Binance) geçmiş OHLCV verisi çeker.

    SAYFALAMA (pagination): Binance tek istekte en fazla 1000 mum döndürdüğü
    için, `limit` 1000'den büyükse `since` parametresi ve bir `while` döngüsü
    ile veriler parça parça (her parça 1000 mum) çekilip birleştirilir.
    Her sayfadan sonra IP ban riskini azaltmak için `time.sleep(1)` beklenir.

    Parametreler
    ----------
    symbol : str
        İşlem çifti, örn. 'BTC/USDT'.
    timeframe : str
        Mum periyodu, örn. '1m', '5m', '1h', '1d'.
    limit : int
        Çekilecek toplam mum sayısı (örn. 5000, 10000 desteklenir).
    exchange_name : str
        ccxt borsa kimliği (varsayılan 'binance').

    Dönüş
    -----
    pandas.DataFrame
        Sütunlar: [open, high, low, close, volume], index: timestamp.
    """
    exchange_class = getattr(ccxt, exchange_name)
    exchange = exchange_class({"enableRateLimit": True})

    # timeframe'in milisaniye cinsinden süresi (ör. '1h' -> 3_600_000 ms)
    timeframe_ms = exchange.parse_timeframe(timeframe) * 1000

    # Başlangıç: en yeni `limit` mumu kapsayacak şekilde geçmişe git
    since = exchange.milliseconds() - limit * timeframe_ms

    all_candles = []
    while len(all_candles) < limit:
        # Kalan miktara göre bu sayfada kaç mum isteneceğini belirle
        remaining = limit - len(all_candles)
        page_limit = min(MAX_CANDLES_PER_REQUEST, remaining)

        batch = exchange.fetch_ohlcv(
            symbol, timeframe=timeframe, since=since, limit=page_limit
        )
        if not batch:
            break  # Borsa daha fazla veri döndürmüyor

        all_candles += batch

        # Bir sonraki sayfa: son mumdan bir periyot sonrasından devam et
        since = batch[-1][0] + timeframe_ms

        # Son sayfa tam dolmadıysa elde edilebilecek her şey alınmış demektir
        if len(batch) < page_limit:
            break

        time.sleep(1)  # IP ban yememek için sayfalar arası bekleme

    df = pd.DataFrame(all_candles, columns=["timestamp"] + OHLCV_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)

    # Olası çakışan/tekrarlı zaman damgalarını temizle, kronolojik sırala
    df = df[~df.index.duplicated(keep="first")].sort_index()

    # İstenen son `limit` mumu döndür
    return df.iloc[-limit:]


def add_indicators(df):
    """
    DataFrame'e teknik indikatörleri ekler ve NaN satırlarını temizler.

    Eklenen indikatörler:
      - rsi  : RSI(14)  -> aşırı alım/satım
      - macd : MACD(12, 26)  -> trend momentumu (MACD çizgisi)
      - atr  : ATR(14)  -> volatilite

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

    # İndikatörlerin ısınma (warm-up) döneminde oluşan NaN'leri temizle
    df.dropna(inplace=True)

    return df


def scale_data(data, feature_columns=FEATURE_COLUMNS):
    """
    Özellikleri MinMaxScaler ile 0-1 aralığına ölçeklendirir.

    `data` bir DataFrame veya NumPy dizisi olabilir. DataFrame ise
    `feature_columns` seçilir; dizi ise olduğu gibi ölçeklenir.

    Dönüş
    -----
    scaled : numpy.ndarray, şekil (n_samples, n_features)
    scaler : MinMaxScaler  (ters dönüşüm için saklanmalı)
    """
    if isinstance(data, pd.DataFrame):
        values = data[feature_columns].values
    else:
        values = np.asarray(data)

    scaler = MinMaxScaler(feature_range=(0, 1))
    scaled = scaler.fit_transform(values)
    return scaled, scaler


def create_sequences(scaled_data, sequence_length=60, target_col_index=TARGET_COL_INDEX):
    """
    Ölçeklenmiş veriyi kayan pencere (sliding window) yöntemiyle
    LSTM'in istediği 3B yapıya dönüştürür.

    Her örnek:
        X -> son `sequence_length` mum, tüm özelliklerle
        y -> bir sonraki mumun hedef sütunu (varsayılan: 'close')

    Dönüş
    -----
    X : numpy.ndarray, şekil (batch_size, sequence_length, n_features)
    y : numpy.ndarray, şekil (batch_size, 1)
    """
    X, y = [], []

    for i in range(sequence_length, len(scaled_data)):
        X.append(scaled_data[i - sequence_length:i, :])
        y.append(scaled_data[i, target_col_index])

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

    print("2) İndikatörler ekleniyor (RSI, MACD, ATR)...")
    df = add_indicators(df)
    print(f"   İndikatörlü şekil: {df.shape}  (özellikler: {FEATURE_COLUMNS})")

    print("3) Ölçeklendiriliyor (MinMaxScaler, dinamik özellik sayısı)...")
    scaled, scaler = scale_data(df)
    print(f"   Ölçeklenmiş veri şekli: {scaled.shape}")

    print(f"4) Pencereleme (sequence_length={SEQUENCE_LENGTH})...")
    X, y = create_sequences(scaled, sequence_length=SEQUENCE_LENGTH)

    print("\n--- Sonuç Boyutları ---")
    print(f"X shape: {X.shape}   # (batch_size, sequence_length, {len(FEATURE_COLUMNS)})")
    print(f"y shape: {y.shape}   # (batch_size, 1)")
