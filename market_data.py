"""
Shared market data service.
Streams real-time BTC price from Binance, collects 1-minute klines,
and fetches Polymarket order book data for active BTC 5m markets.
"""
import asyncio
import json
import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import websockets
from polymarket import AsyncPublicClient, PRODUCTION

logger = logging.getLogger(__name__)

# Polymarket 5 dakikalık BTC piyasa slug öneki: btc-updown-5m-<5dk-hizalı-unix>
POLY_SLUG_PREFIX = "btc-updown-5m-"


@dataclass
class Kline:
    """One candlestick."""
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class PolymarketSnapshot:
    """Current state of a Polymarket 5m BTC market."""
    market_id: str
    question: str
    up_token_id: str
    down_token_id: str
    up_price: float          # 0.01-0.99 — implied probability for UP
    down_price: float         # 0.01-0.99 — implied probability for DOWN
    up_book_bids: list = field(default_factory=list)   # [(price, size), ...]
    up_book_asks: list = field(default_factory=list)
    down_book_bids: list = field(default_factory=list)
    down_book_asks: list = field(default_factory=list)
    timestamp: float = 0.0
    window_start: int = 0    # 5dk pencerenin başlangıcı (unix) — resolution için
    window_end: int = 0      # 5dk pencerenin sonu (unix) — trade bu anda çözülür

    @property
    def up_payout(self) -> float:
        """Profit if you buy UP at current price and win."""
        return 1.0 - self.up_price

    @property
    def down_payout(self) -> float:
        """Profit if you buy DOWN at current price and win."""
        return 1.0 - self.down_price


class MarketDataService:
    """
    Central data hub for the entire system.
    All strategies read from this shared service.
    """

    def __init__(self, client: AsyncPublicClient):
        self.client = client

        # BTC price data
        self.latest_btc_price: float = 0.0
        self.price_history: deque = deque(maxlen=500)  # (timestamp, price) pairs
        self.klines_1m: deque = deque(maxlen=100)      # last 100 one-minute candles

        # Polymarket data
        self.active_snapshot: Optional[PolymarketSnapshot] = None
        # 5dk pencere -> o pencere ilk görüldüğündeki BTC fiyatı (resolution için)
        self.window_open_price: dict[int, float] = {}

        # Internal state for kline building
        self._current_kline_start: float = 0.0
        self._current_kline: Optional[Kline] = None
        self._trade_volume_acc: float = 0.0

    # ------------------------------------------------------------------ #
    #  Binance WebSocket — real-time BTC/USDT trades + kline building     #
    # ------------------------------------------------------------------ #

    def bootstrap_klines(self, limit: int = 60):
        """
        Başlangıçta Binance REST'ten geçmiş 1dk mumları çekip klines_1m'i doldurur.
        Böylece TA stratejileri (RSI/EMA/VWAP/Bollinger) ~20 dk beklemeden ANINDA
        çalışır. Son (tamamlanmamış) mum atlanır.
        """
        try:
            import ccxt
            ex = ccxt.binance({"enableRateLimit": True})
            raw = ex.fetch_ohlcv("BTC/USDT", timeframe="1m", limit=limit)
            for ts, o, h, l, c, v in raw[:-1]:  # son mum henüz kapanmadı -> atla
                self.klines_1m.append(Kline(
                    timestamp=ts / 1000.0, open=float(o), high=float(h),
                    low=float(l), close=float(c), volume=float(v),
                ))
            if raw:
                self.latest_btc_price = float(raw[-1][4])
                self._current_kline_start = int(raw[-1][0] / 1000.0) // 60 * 60
            logger.info(f"✅ {len(self.klines_1m)} geçmiş 1dk mum yüklendi (Binance REST)")
        except Exception as e:
            logger.error(f"Kline bootstrap hatası: {e}")

    async def binance_ws_loop(self):
        """Stream BTC/USDT trades from Binance and build 1-min klines."""
        url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
        while True:
            try:
                async with websockets.connect(url) as ws:
                    logger.info("✅ Connected to Binance WebSocket (btcusdt@trade)")
                    async for raw in ws:
                        data = json.loads(raw)
                        price = float(data["p"])
                        qty = float(data["q"])
                        ts = data["T"] / 1000.0  # ms → sec

                        self.latest_btc_price = price
                        self.price_history.append((ts, price))
                        self._update_kline(ts, price, qty)
            except Exception as e:
                logger.error(f"Binance WS error: {e}. Reconnecting in 3s…")
                await asyncio.sleep(3)

    def _update_kline(self, ts: float, price: float, volume: float):
        """Accumulate trades into 1-minute candles."""
        minute_start = int(ts) // 60 * 60

        if self._current_kline is None or minute_start != self._current_kline_start:
            # Save previous kline
            if self._current_kline is not None:
                self._current_kline.volume = self._trade_volume_acc
                self.klines_1m.append(self._current_kline)
            # Start new kline
            self._current_kline_start = minute_start
            self._current_kline = Kline(
                timestamp=minute_start,
                open=price, high=price, low=price, close=price,
                volume=0.0,
            )
            self._trade_volume_acc = volume
        else:
            k = self._current_kline
            k.high = max(k.high, price)
            k.low = min(k.low, price)
            k.close = price
            self._trade_volume_acc += volume

    # ------------------------------------------------------------------ #
    #  Technical indicator helpers                                        #
    # ------------------------------------------------------------------ #

    def get_closes(self, n: int = 50) -> list[float]:
        """Return the last N close prices from completed klines."""
        klines = list(self.klines_1m)
        return [k.close for k in klines[-n:]]

    def get_volumes(self, n: int = 50) -> list[float]:
        """Return the last N volumes from completed klines."""
        klines = list(self.klines_1m)
        return [k.volume for k in klines[-n:]]

    def calc_rsi(self, period: int = 14) -> Optional[float]:
        closes = self.get_closes(period + 1)
        if len(closes) < period + 1:
            return None
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def calc_ema(self, period: int) -> Optional[float]:
        closes = self.get_closes(period * 2)
        if len(closes) < period:
            return None
        arr = np.array(closes, dtype=float)
        multiplier = 2.0 / (period + 1)
        ema = arr[0]
        for price in arr[1:]:
            ema = (price - ema) * multiplier + ema
        return float(ema)

    def calc_vwap(self, n: int = 20) -> Optional[float]:
        klines = list(self.klines_1m)[-n:]
        if len(klines) < 3:
            return None
        cum_tp_vol = 0.0
        cum_vol = 0.0
        for k in klines:
            tp = (k.high + k.low + k.close) / 3.0
            cum_tp_vol += tp * k.volume
            cum_vol += k.volume
        if cum_vol == 0:
            return None
        return cum_tp_vol / cum_vol

    def calc_bollinger(self, period: int = 20, std_dev: float = 2.0):
        """Return (middle, upper, lower) bands or None."""
        closes = self.get_closes(period)
        if len(closes) < period:
            return None
        arr = np.array(closes[-period:])
        middle = float(np.mean(arr))
        std = float(np.std(arr))
        return (middle, middle + std_dev * std, middle - std_dev * std)

    def recent_price_changes(self, seconds: int = 60) -> list[float]:
        """Get price samples from the last N seconds."""
        now = time.time()
        cutoff = now - seconds
        return [p for t, p in self.price_history if t >= cutoff]

    # ------------------------------------------------------------------ #
    #  Polymarket data                                                    #
    # ------------------------------------------------------------------ #

    def _current_market_slugs(self) -> list[str]:
        """
        Aktif 5 dakikalık BTC piyasasının slug'ını HESAPLAR (sayfa sayfa tarama yok).

        Polymarket her 5 dakikada 'btc-updown-5m-<timestamp>' açar; timestamp,
        pencerenin başlangıcı olan 5-dakikaya hizalı Unix zamanıdır. Şu anki
        pencereyi (canlı) ve bir sonrakini (yedek) döndürür.
        """
        now = int(time.time())
        base = now - (now % 300)  # şu anki 5-dk sınırı
        return [f"{POLY_SLUG_PREFIX}{base}", f"{POLY_SLUG_PREFIX}{base + 300}"]

    async def fetch_polymarket_data(self):
        """Hesaplanan slug ile aktif BTC 5m piyasasını bulur ve fiyat/order book çeker."""
        try:
            slugs = self._current_market_slugs()

            # 1) Hesaplanan slug ile doğrudan bul (anlık)
            events: list = []
            try:
                pag = self.client.list_events(slug=slugs, closed=False)
                async for page in pag:
                    events = list(page.items or [])
                    break
            except Exception as e:
                logger.debug(f"list_events(slug) failed: {e}")

            # 2) Yedek: arama motoru (slug eşleşmesiyle)
            if not events:
                try:
                    pag = self.client.search(q="Bitcoin Up or Down 5m", page_size=10)
                    async for page in pag:
                        for sr in (page.items or []):
                            for ev in (getattr(sr, "events", None) or []):
                                if (getattr(ev, "slug", "") or "") in slugs:
                                    events.append(ev)
                        break
                except Exception as e:
                    logger.debug(f"search fallback failed: {e}")

            if not events:
                logger.debug("Aktif BTC 5m piyasası bulunamadı (bu döngü).")
                return

            # Şu anki pencereyi tercih et (slugs[0]); yoksa ilk bulunanı al
            by_slug = {getattr(e, "slug", ""): e for e in events}
            target_ev = next((by_slug[s] for s in slugs if s in by_slug), events[0])

            market = (getattr(target_ev, "markets", None) or [None])[0]
            if market is None:
                logger.debug("Event'te market yok.")
                return

            # Token ID'ler event listesinde gelmez -> tam market'i çek (outcomes.yes/no)
            full = await self.client.get_market(id=market.id)
            outs = getattr(full, "outcomes", None)
            if outs is None or not (hasattr(outs, "yes") and hasattr(outs, "no")):
                logger.debug("Market outcomes eksik.")
                return
            up_token = outs.yes.token_id    # 'Up'
            down_token = outs.no.token_id   # 'Down'

            # Pencereyi slug'dan türet (btc-updown-5m-<W>) -> resolution için
            slug = getattr(target_ev, "slug", "") or ""
            try:
                window_start = int(slug.rsplit("-", 1)[-1])
            except (ValueError, IndexError):
                now_i = int(time.time())
                window_start = now_i - (now_i % 300)
            window_end = window_start + 300
            # Pencere ilk görüldüğündeki BTC fiyatını kaydet (dönüş kıyası için)
            if window_start not in self.window_open_price and self.latest_btc_price > 0:
                self.window_open_price[window_start] = self.latest_btc_price
            # Eski pencereleri buda (bellek): 1 saatten eski
            cutoff = int(time.time()) - 3600
            self.window_open_price = {
                w: p for w, p in self.window_open_price.items() if w >= cutoff
            }

            # Midpoint (implied probability)
            up_price, down_price = 0.50, 0.50
            try:
                up_price = float(await self.client.get_midpoint(token_id=up_token))
                down_price = float(await self.client.get_midpoint(token_id=down_token))
            except Exception as e:
                logger.debug(f"midpoint fetch failed: {e}")

            # Order book'lar (her iki outcome için)
            up_bids, up_asks, down_bids, down_asks = [], [], [], []
            try:
                ob = await self.client.get_order_book(token_id=up_token)
                up_bids = [(float(l.price), float(l.size)) for l in ob.bids]
                up_asks = [(float(l.price), float(l.size)) for l in ob.asks]
                ob2 = await self.client.get_order_book(token_id=down_token)
                down_bids = [(float(l.price), float(l.size)) for l in ob2.bids]
                down_asks = [(float(l.price), float(l.size)) for l in ob2.asks]
            except Exception as e:
                logger.debug(f"order book fetch failed: {e}")

            self.active_snapshot = PolymarketSnapshot(
                market_id=str(market.id),
                question=getattr(target_ev, "title", "BTC 5m"),
                up_token_id=up_token,
                down_token_id=down_token,
                up_price=up_price,
                down_price=down_price,
                up_book_bids=up_bids,
                up_book_asks=up_asks,
                down_book_bids=down_bids,
                down_book_asks=down_asks,
                timestamp=time.time(),
                window_start=window_start,
                window_end=window_end,
            )
            logger.info(
                f"📊 Polymarket: {getattr(target_ev, 'slug', '?')} | "
                f"UP=${up_price:.2f} DOWN=${down_price:.2f}"
            )

        except Exception as e:
            logger.error(f"Polymarket fetch error: {e}")

    # ------------------------------------------------------------------ #
    #  Main loop                                                          #
    # ------------------------------------------------------------------ #

    async def start(self):
        """Start all data collection loops."""
        # Önce geçmiş mumları yükle -> stratejiler beklemeden çalışsın
        await asyncio.to_thread(self.bootstrap_klines, 60)
        asyncio.create_task(self.binance_ws_loop())
        while True:
            await self.fetch_polymarket_data()
            await asyncio.sleep(15)  # refresh Polymarket data every 15s
