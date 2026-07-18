"""
data_pipeline.py — Veri Boru Hattı (Data Pipeline)

Görevi:
  1. Binance'ten geçmiş OHLCV verilerini çekmek (ccxt).
  2. Veriyi 0-1 arasına ölçeklendirmek (MinMaxScaler) ve scaler'ı saklamak.
  3. Ölçeklenmiş veriyi LSTM'in istediği 3B pencere yapısına dönüştürmek.

NOT: Bu dosya SADECE veri hazırlığı yapar. Eğitim, backtest ve
al-sat işlemleri kapsam dışıdır.
"""

import ccxt
import numpy as np
import pandas as pd
from sklearn.preprocessing import MinMaxScaler


# OHLCV DataFrame'inde kullanılacak sütun sırası
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


def fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=500, exchange_name="binance"):
    """
    Bir borsadan (varsayılan: Binance) geçmiş OHLCV verisi çeker.

    Parametreler
    ----------
    symbol : str
        İşlem çifti, örn. 'BTC/USDT'.
    timeframe : str
        Mum periyodu, örn. '1m', '5m', '1h', '1d'.
    limit : int
        Çekilecek mum sayısı.
    exchange_name : str
        ccxt borsa kimliği (varsayılan 'binance').

    Dönüş
    -----
    pandas.DataFrame
        Sütunlar: [timestamp, open, high, low, close, volume]
        Index: timestamp (datetime).
    """
    exchange_class = getattr(ccxt, exchange_name)
    exchange = exchange_class({"enableRateLimit": True})

    # ccxt: [[timestamp, open, high, low, close, volume], ...]
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    df = pd.DataFrame(raw, columns=["timestamp"] + OHLCV_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)

    return df


def scale_data(df, feature_columns=OHLCV_COLUMNS):
    """
    OHLCV verilerini MinMaxScaler ile 0-1 aralığına ölçeklendirir.

    Parametreler
    ----------
    df : pandas.DataFrame
        Ham OHLCV verisi.
    feature_columns : list[str]
        Ölçeklenecek sütunlar.

    Dönüş
    -----
    scaled : numpy.ndarray, şekil (n_samples, n_features)
        Ölçeklenmiş veri.
    scaler : MinMaxScaler
        Ters dönüşüm (inverse_transform) için saklanacak scaler objesi.
    """
    scaler = MinMaxScaler(feature_range=(0, 1))
    scaled = scaler.fit_transform(df[feature_columns].values)
    return scaled, scaler


def create_sequences(scaled_data, sequence_length=60, target_col_index=3):
    """
    Ölçeklenmiş veriyi kayan pencere (sliding window) yöntemiyle
    LSTM'in istediği 3B yapıya dönüştürür.

    Her örnek:
        X -> son `sequence_length` mum, tüm özelliklerle
        y -> bir sonraki mumun hedef sütunu (varsayılan: 'close')

    Parametreler
    ----------
    scaled_data : numpy.ndarray, şekil (n_samples, n_features)
        Ölçeklenmiş OHLCV verisi.
    sequence_length : int
        Girdi penceresindeki mum sayısı (örn. 60).
    target_col_index : int
        Hedef sütunun indeksi (OHLCV_COLUMNS içinde 'close' = 3).

    Dönüş
    -----
    X : numpy.ndarray, şekil (batch_size, sequence_length, n_features)
    y : numpy.ndarray, şekil (batch_size, 1)
    """
    X, y = [], []

    for i in range(sequence_length, len(scaled_data)):
        # Son `sequence_length` mum -> girdi
        X.append(scaled_data[i - sequence_length:i, :])
        # Bir sonraki mumun hedef değeri (kapanış) -> çıktı
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
    print(f"   DataFrame şekli: {df.shape}")

    print("2) Veri ölçeklendiriliyor (MinMaxScaler)...")
    scaled, scaler = scale_data(df)
    print(f"   Ölçeklenmiş veri şekli: {scaled.shape}")

    print(f"3) Pencereleme yapılıyor (sequence_length={SEQUENCE_LENGTH})...")
    X, y = create_sequences(scaled, sequence_length=SEQUENCE_LENGTH)

    print("\n--- Sonuç Boyutları ---")
    print(f"X shape: {X.shape}   # (batch_size, sequence_length, features)")
    print(f"y shape: {y.shape}   # (batch_size, 1)")
