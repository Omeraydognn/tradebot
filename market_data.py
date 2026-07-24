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

        # Internal state for kline building
        self._current_kline_start: float = 0.0
        self._current_kline: Optional[Kline] = None
        self._trade_volume_acc: float = 0.0

    # ------------------------------------------------------------------ #
    #  Binance WebSocket — real-time BTC/USDT trades + kline building     #
    # ------------------------------------------------------------------ #

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

    async def fetch_polymarket_data(self):
        """Fetch the currently active BTC 5m market and its order book from Polymarket."""
        try:
            target = None

            # Method 1: Try to get the event directly by URL (the user's link)
            try:
                event = await self.client.get_event(
                    url="https://polymarket.com/event/btc-updown-5m"
                )
                if event and hasattr(event, 'markets') and event.markets:
                    for m in event.markets:
                        if not getattr(m, 'closed', True):
                            target = m
                            break
                    if target is None:
                        target = event.markets[0]
            except Exception:
                pass

            # Method 2: Search for BTC Up/Down markets
            if target is None:
                try:
                    search_pag = self.client.search(q="BTC Up/Down 5m", page_size=10)
                    async for page in search_pag:
                        if hasattr(page, 'events'):
                            for ev in page.events:
                                title = (getattr(ev, 'title', '') or '').lower()
                                if ('btc' in title or 'bitcoin' in title) and ('5m' in title or '5 min' in title or 'up' in title):
                                    if hasattr(ev, 'markets') and ev.markets:
                                        target = ev.markets[0]
                                        break
                        if hasattr(page, 'markets'):
                            for m in page.markets:
                                q = (getattr(m, 'question', '') or '').lower()
                                if ('btc' in q or 'bitcoin' in q) and ('5m' in q or '5 min' in q or 'up' in q):
                                    target = m
                                    break
                        break
                except Exception:
                    pass

            # Method 3: List events with BTC filter
            if target is None:
                try:
                    paginator = self.client.list_events(
                        title_search="BTC Up", live=True, page_size=20
                    )
                    async for page in paginator:
                        for event in page.items:
                            title = (event.title or "").lower()
                            if "5m" in title or "up/down" in title or "up or down" in title:
                                if hasattr(event, 'markets') and event.markets:
                                    target = event.markets[0]
                                else:
                                    target = event
                                break
                        break
                except Exception:
                    pass

            if target is None:
                logger.debug("No active BTC 5m markets found this cycle.")
                return


            # Get order book data for the target market
            market_id = str(target.id)
            question = getattr(target, "question", getattr(target, "title", "BTC 5m"))

            # Get token IDs — markets have two outcomes
            tokens = getattr(target, "clob_token_ids", None)
            if tokens and len(tokens) >= 2:
                up_token = tokens[0]
                down_token = tokens[1]
            else:
                # Try to get from market directly
                up_token = getattr(target, "token_id", "")
                down_token = ""

            # Get midpoint prices (implied probabilities)
            up_price = 0.50
            down_price = 0.50
            try:
                if up_token:
                    from decimal import Decimal
                    mid = await self.client.get_midpoint(token_id=up_token)
                    up_price = float(mid)
                    down_price = 1.0 - up_price
            except Exception as e:
                logger.debug(f"Could not fetch midpoint: {e}")

            # Get order books
            up_bids, up_asks = [], []
            down_bids, down_asks = [], []
            try:
                if up_token:
                    book = await self.client.get_order_book(token_id=up_token)
                    up_bids = [(float(l.price), float(l.size)) for l in book.bids]
                    up_asks = [(float(l.price), float(l.size)) for l in book.asks]
                if down_token:
                    book = await self.client.get_order_book(token_id=down_token)
                    down_bids = [(float(l.price), float(l.size)) for l in book.bids]
                    down_asks = [(float(l.price), float(l.size)) for l in book.asks]
            except Exception as e:
                logger.debug(f"Could not fetch order book: {e}")

            self.active_snapshot = PolymarketSnapshot(
                market_id=market_id,
                question=question,
                up_token_id=up_token,
                down_token_id=down_token,
                up_price=up_price,
                down_price=down_price,
                up_book_bids=up_bids,
                up_book_asks=up_asks,
                down_book_bids=down_bids,
                down_book_asks=down_asks,
                timestamp=time.time(),
            )
            logger.info(
                f"📊 Polymarket snapshot: UP=${up_price:.2f} DOWN=${down_price:.2f} | "
                f"Q: {question}"
            )

        except Exception as e:
            logger.error(f"Polymarket fetch error: {e}")

    # ------------------------------------------------------------------ #
    #  Main loop                                                          #
    # ------------------------------------------------------------------ #

    async def start(self):
        """Start all data collection loops."""
        asyncio.create_task(self.binance_ws_loop())
        while True:
            await self.fetch_polymarket_data()
            await asyncio.sleep(15)  # refresh Polymarket data every 15s
