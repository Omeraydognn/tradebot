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

    def executable_price(self, side: str) -> float:
        """
        GERÇEKÇİ yürütme fiyatı: alırken midpoint değil, en iyi ASK ödenir.

        Polymarket'te açık bir "işlem ücreti" yoktur; asıl maliyet spread'dir
        (mid 0.50 iken ask 0.52 ise gerçek maliyetin %4'ü buradan gelir).
        Kitap yoksa midpoint'e düşer.
        """
        asks = self.up_book_asks if side == "UP" else self.down_book_asks
        mid = self.up_price if side == "UP" else self.down_price
        if asks:
            best_ask = min(p for p, _ in asks)
            if 0.0 < best_ask < 1.0:
                return best_ask
        return mid


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
        # 5dk pencere -> o pencerenin açılış BTC fiyatı (resolution için)
        self.window_open_price: dict[int, float] = {}
        # 5dk pencere -> son görülen Polymarket UP fiyatı (piyasanın kendi kararı)
        self.window_last_up_price: dict[int, float] = {}
        # 5dk pencere -> Polymarket'in RESMİ sonucu ("UP"/"DOWN"). Tek doğru kaynak.
        self.window_official_outcome: dict[int, str] = {}
        # 5dk pencere -> pencere BAŞINDAKİ mikroyapı (online model bunu öğrenir;
        # ileriye bakış yok: sadece pencere başında bilinen bilgi)
        self.window_micro: dict[int, dict] = {}

        # Internal state for kline building
        self._current_kline_start: float = 0.0
        self._current_kline: Optional[Kline] = None
        self._trade_volume_acc: float = 0.0

        # ---------------- MİKROYAPI / TÜREV VERİLERİ ----------------
        # Bunlar 5dk yön tahmininde ham fiyattan daha bilgilendiricidir.

        # CVD (Cumulative Volume Delta): agresif alış - agresif satış hacmi.
        # (ts, signed_qty) — pozitif = agresif alış, negatif = agresif satış
        self.trade_flow: deque = deque(maxlen=6000)

        # Emir defteri (Binance spot depth20) — anlık arz/talep dengesi
        self.book_bids: list = []   # [(price, qty), ...] en iyi 20
        self.book_asks: list = []
        self.book_ts: float = 0.0

        # Perp türev verileri (Binance futures)
        self.funding_rate: float = 0.0        # anlık funding (8h)
        self.mark_price: float = 0.0          # perp mark price
        self.open_interest: float = 0.0       # açık pozisyon (BTC)
        self.oi_history: deque = deque(maxlen=120)   # (ts, oi)
        self.basis: float = 0.0               # (mark - spot)/spot -> perp primi

        # Likidasyonlar (forceOrder) — zorunlu akış, kısa vadeli itici güç
        self.liquidations: deque = deque(maxlen=400)  # (ts, side, qty_usd)

    # ------------------------------------------------------------------ #
    #  Binance WebSocket — real-time BTC/USDT trades + kline building     #
    # ------------------------------------------------------------------ #

    # Bazı bulut sağlayıcıların (Render, AWS, GCP…) IP aralıkları Binance'in
    # ana domainlerinde (stream.binance.com, api.binance.com) HTTP 451 ile
    # engellenebiliyor — coğrafi bölgeden bağımsız, IP itibarına dayalı bir
    # blok. Binance bu durum için RESMİ mirror domainler sağlıyor; birincisi
    # engellenirse otomatik olarak ikinciye geçilir.
    SPOT_WS_HOSTS = ["stream.binance.com:9443", "data-stream.binance.vision"]
    SPOT_REST_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]

    def bootstrap_klines(self, limit: int = 60):
        """
        Başlangıçta Binance REST'ten geçmiş 1dk mumları çekip klines_1m'i doldurur.
        Böylece TA stratejileri (RSI/EMA/VWAP/Bollinger) ~20 dk beklemeden ANINDA
        çalışır. Son (tamamlanmamış) mum atlanır.

        Ana domain (api.binance.com) engelliyse otomatik olarak Binance'in
        resmi mirror'ına (data-api.binance.vision) düşer.
        """
        import urllib.request

        for i, base in enumerate(self.SPOT_REST_HOSTS):
            try:
                url = f"{base}/api/v3/klines?symbol=BTCUSDT&interval=1m&limit={limit}"
                with urllib.request.urlopen(url, timeout=10) as r:
                    raw = json.loads(r.read().decode())
                for row in raw[:-1]:  # son mum henüz kapanmadı -> atla
                    ts, o, h, l, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
                    self.klines_1m.append(Kline(
                        timestamp=ts / 1000.0, open=float(o), high=float(h),
                        low=float(l), close=float(c), volume=float(v),
                    ))
                if raw:
                    self.latest_btc_price = float(raw[-1][4])
                    self._current_kline_start = int(raw[-1][0] / 1000.0) // 60 * 60
                tag = "" if i == 0 else " (mirror üzerinden)"
                logger.info(f"✅ {len(self.klines_1m)} geçmiş 1dk mum yüklendi{tag}")
                return
            except Exception as e:
                logger.warning(f"Kline bootstrap ({base}) başarısız: {e}")
        logger.error("Kline bootstrap: tüm domainler başarısız oldu.")

    async def _connect_spot_ws(self, path: str, label: str):
        """
        Spot WS'e bağlanır; ana domain engelliyse (451 vb.) otomatik olarak
        Binance'in resmi mirror domainine (data-stream.binance.vision) geçer.
        Hangi host'un çalıştığını hatırlayıp bir sonraki bağlantıda önce onu
        dener (gereksiz 451 denemesiyle zaman kaybetmemek için).
        """
        host_order = list(self.SPOT_WS_HOSTS)
        while True:
            for host in host_order:
                url = f"wss://{host}/ws/{path}"
                try:
                    async with websockets.connect(url, ping_interval=20) as ws:
                        logger.info(f"✅ {label} bağlandı ({host})")
                        # Bu host çalıştı -> sıradaki denemede önce bunu dene
                        if host_order[0] != host:
                            host_order.remove(host)
                            host_order.insert(0, host)
                        async for raw in ws:
                            yield raw
                except Exception as e:
                    logger.error(f"{label} hatası ({host}): {e}")
                    await asyncio.sleep(2)
            await asyncio.sleep(3)  # tüm hostlar denendi, biraz bekleyip tekrar dene

    async def binance_ws_loop(self):
        """
        Binance spot trade akışı: fiyat + 1dk mum + CVD (order flow).

        `m` alanı = "alıcı maker mı?".  m=True -> agresif taraf SATICI,
        m=False -> agresif taraf ALICI. Bu, gerçek order-flow sinyalidir.
        """
        async for raw in self._connect_spot_ws("btcusdt@trade", "Binance WS (trade+CVD)"):
            try:
                data = json.loads(raw)
                price = float(data["p"])
                qty = float(data["q"])
                ts = data["T"] / 1000.0  # ms → sec
                is_buyer_maker = bool(data.get("m", False))

                self.latest_btc_price = price
                self.price_history.append((ts, price))
                self._update_kline(ts, price, qty)

                # CVD: agresif alış (+) / agresif satış (-)
                signed = -qty if is_buyer_maker else qty
                self.trade_flow.append((ts, signed))
            except Exception as e:
                logger.debug(f"trade mesajı işlenemedi: {e}")

    async def binance_depth_loop(self):
        """Binance spot emir defteri (top-20, 100ms) — anlık arz/talep dengesi."""
        async for raw in self._connect_spot_ws("btcusdt@depth20@100ms", "Binance depth WS"):
            try:
                d = json.loads(raw)
                bids = d.get("bids") or d.get("b") or []
                asks = d.get("asks") or d.get("a") or []
                self.book_bids = [(float(p), float(q)) for p, q in bids]
                self.book_asks = [(float(p), float(q)) for p, q in asks]
                self.book_ts = time.time()
            except Exception as e:
                logger.debug(f"depth mesajı işlenemedi: {e}")

    async def binance_futures_loop(self):
        """
        Perp türev verileri: funding rate + mark price (dolayısıyla basis).

        NOT: fstream WebSocket bazı ağlardan/bölgelerden erişilemiyor (timeout),
        bu yüzden REST premiumIndex kullanılıyor — daha dayanıklı.
        Funding 8 saatte bir değiştiği için 20s poll fazlasıyla yeterli.
        """
        import urllib.request
        url = "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"
        logged = False
        while True:
            try:
                def _fetch():
                    with urllib.request.urlopen(url, timeout=10) as r:
                        return json.loads(r.read().decode())
                d = await asyncio.to_thread(_fetch)
                self.mark_price = float(d.get("markPrice", 0) or 0)
                self.funding_rate = float(d.get("lastFundingRate", 0) or 0)
                if self.latest_btc_price > 0 and self.mark_price > 0:
                    self.basis = (self.mark_price - self.latest_btc_price) / self.latest_btc_price
                if not logged:
                    logger.info("✅ Perp verisi bağlandı (funding + mark price)")
                    logged = True
            except Exception as e:
                logger.debug(f"premiumIndex hatası: {e}")
            await asyncio.sleep(20)

    async def liquidation_loop(self):
        """
        Likidasyon akışı (forceOrder WS). Bazı ağlarda engelli olabilir;
        erişilemezse sessizce devre dışı kalır (sistem çalışmaya devam eder).
        """
        url = "wss://fstream.binance.com/ws/btcusdt@forceOrder"
        fails = 0
        while fails < 3:  # 3 denemede olmazsa vazgeç (opsiyonel veri)
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info("✅ Likidasyon akışı bağlandı")
                    fails = 0
                    async for raw in ws:
                        d = json.loads(raw)
                        o = d.get("o", {})
                        side = o.get("S", "")          # SELL = long likidasyonu
                        qty = float(o.get("q", 0) or 0)
                        px = float(o.get("p", 0) or 0)
                        self.liquidations.append((time.time(), side, qty * px))
            except Exception:
                fails += 1
                await asyncio.sleep(5)
        logger.info("ℹ️  Likidasyon akışı erişilemiyor — bu veri olmadan devam ediliyor.")

    async def open_interest_loop(self):
        """Açık pozisyon (open interest) — kaldıraç birikimini gösterir (REST, 30s)."""
        import urllib.request
        url = "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"
        while True:
            try:
                def _fetch():
                    with urllib.request.urlopen(url, timeout=10) as r:
                        return json.loads(r.read().decode())
                d = await asyncio.to_thread(_fetch)
                oi = float(d.get("openInterest", 0) or 0)
                if oi > 0:
                    self.open_interest = oi
                    self.oi_history.append((time.time(), oi))
            except Exception as e:
                logger.debug(f"Open interest fetch hatası: {e}")
            await asyncio.sleep(30)

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

    def kline_open_at(self, ts: int) -> Optional[float]:
        """
        Verilen unix zamanına denk gelen 1dk mumun AÇILIŞ fiyatı.
        5dk pencerenin gerçek başlangıç fiyatını hassas belirlemek için kullanılır.
        """
        minute = int(ts) // 60 * 60
        for k in self.klines_1m:
            if int(k.timestamp) == minute:
                return float(k.open)
        # Oluşmakta olan mum da olabilir
        if self._current_kline and int(self._current_kline.timestamp) == minute:
            return float(self._current_kline.open)
        return None

    def recent_price_changes(self, seconds: int = 60) -> list[float]:
        """Get price samples from the last N seconds."""
        now = time.time()
        cutoff = now - seconds
        return [p for t, p in self.price_history if t >= cutoff]

    # ------------------------------------------------------------------ #
    #  MİKROYAPI GÖSTERGELERİ (5dk yön tahmini için asıl bilgi taşıyanlar) #
    # ------------------------------------------------------------------ #

    def calc_cvd(self, seconds: int = 60) -> Optional[float]:
        """
        Cumulative Volume Delta: son N saniyedeki (agresif alış - agresif satış).
        Pozitif = alıcılar agresif (yukarı baskı). Order-flow'un özü.
        """
        if not self.trade_flow:
            return None
        cutoff = time.time() - seconds
        vals = [q for t, q in self.trade_flow if t >= cutoff]
        if len(vals) < 5:
            return None
        return float(sum(vals))

    def calc_cvd_ratio(self, seconds: int = 60) -> Optional[float]:
        """
        CVD'nin toplam hacme oranı: -1 (tamamen satış) … +1 (tamamen alış).
        Ölçekten bağımsız olduğu için eşik koymak kolaydır.
        """
        if not self.trade_flow:
            return None
        cutoff = time.time() - seconds
        vals = [q for t, q in self.trade_flow if t >= cutoff]
        if len(vals) < 5:
            return None
        total = sum(abs(v) for v in vals)
        if total == 0:
            return None
        return float(sum(vals) / total)

    def calc_book_imbalance(self, levels: int = 10) -> Optional[float]:
        """
        Emir defteri dengesizliği: (bid_hacim - ask_hacim) / toplam.
        Pozitif = alış tarafı kalın (destek), negatif = satış baskısı.
        """
        if not self.book_bids or not self.book_asks:
            return None
        if time.time() - self.book_ts > 30:  # bayat veri
            return None
        bid_vol = sum(q for _, q in self.book_bids[:levels])
        ask_vol = sum(q for _, q in self.book_asks[:levels])
        total = bid_vol + ask_vol
        if total == 0:
            return None
        return float((bid_vol - ask_vol) / total)

    def calc_spread_bps(self) -> Optional[float]:
        """Bid-ask spread (baz puan). Yüksek spread = likidite düşük/oynak."""
        if not self.book_bids or not self.book_asks:
            return None
        best_bid = self.book_bids[0][0]
        best_ask = self.book_asks[0][0]
        mid = (best_bid + best_ask) / 2
        if mid <= 0:
            return None
        return float((best_ask - best_bid) / mid * 10000)

    def calc_liquidation_flow(self, seconds: int = 300) -> Optional[float]:
        """
        Net likidasyon akışı ($). SELL emri = LONG likidasyonu (aşağı baskı),
        BUY emri = SHORT likidasyonu (yukarı baskı).
        Pozitif dönüş = short'lar likide oluyor (yukarı itiş).
        """
        if not self.liquidations:
            return None
        cutoff = time.time() - seconds
        net = 0.0
        count = 0
        for t, side, usd in self.liquidations:
            if t < cutoff:
                continue
            count += 1
            net += usd if side == "BUY" else -usd
        return float(net) if count else None

    def calc_oi_change(self, seconds: int = 300) -> Optional[float]:
        """
        Açık pozisyon değişimi (%). Fiyatla birlikte yorumlanır:
          OI↑ + fiyat↑ = yeni long'lar (trend güçlü)
          OI↓ + fiyat↑ = short kapanışı (short squeeze)
        """
        if len(self.oi_history) < 2:
            return None
        cutoff = time.time() - seconds
        old = None
        for t, oi in self.oi_history:
            if t >= cutoff:
                old = oi
                break
        if old is None or old == 0:
            return None
        return float((self.open_interest - old) / old * 100)

    def calc_momentum_pct(self, seconds: int = 60) -> Optional[float]:
        """Son N saniyedeki yüzde fiyat değişimi."""
        prices = self.recent_price_changes(seconds)
        if len(prices) < 5 or prices[0] <= 0:
            return None
        return float((prices[-1] - prices[0]) / prices[0] * 100)

    def calc_multi_tf_alignment(self) -> Optional[float]:
        """
        Çoklu zaman dilimi momentum uyumu: 1dk/3dk/5dk aynı yöne bakıyor mu?
        -1 (hepsi aşağı) … +1 (hepsi yukarı). Uyum, tek dilimden güvenilirdir.
        """
        m1 = self.calc_momentum_pct(60)
        m3 = self.calc_momentum_pct(180)
        m5 = self.calc_momentum_pct(300)
        vals = [m for m in (m1, m3, m5) if m is not None]
        if len(vals) < 2:
            return None
        score = sum(1 if v > 0 else -1 for v in vals) / len(vals)
        return float(score)

    def calc_realized_vol(self, seconds: int = 300) -> Optional[float]:
        """Gerçekleşen oynaklık (%): fiyat örneklerinin std/ortalama."""
        prices = self.recent_price_changes(seconds)
        if len(prices) < 10:
            return None
        arr = np.array(prices, dtype=float)
        mean = float(arr.mean())
        if mean <= 0:
            return None
        return float(arr.std() / mean * 100)

    def microstructure_snapshot(self) -> dict:
        """Tüm mikroyapı göstergelerini tek sözlükte döndürür (AI + dashboard)."""
        return {
            "cvd_60s": self.calc_cvd(60),
            "cvd_ratio_60s": self.calc_cvd_ratio(60),
            "cvd_ratio_300s": self.calc_cvd_ratio(300),
            "book_imbalance": self.calc_book_imbalance(10),
            "spread_bps": self.calc_spread_bps(),
            "funding_rate": self.funding_rate,
            "basis_pct": self.basis * 100 if self.basis else 0.0,
            "oi_change_5m": self.calc_oi_change(300),
            "liquidation_flow_5m": self.calc_liquidation_flow(300),
            "momentum_1m": self.calc_momentum_pct(60),
            "momentum_5m": self.calc_momentum_pct(300),
            "tf_alignment": self.calc_multi_tf_alignment(),
            "realized_vol": self.calc_realized_vol(300),
        }

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

    async def fetch_official_outcome(self, window_start: int) -> Optional[str]:
        """
        Bir pencerenin RESMİ sonucunu Polymarket'ten çeker: "UP" / "DOWN" / None.

        NEDEN KRİTİK:
          Bu piyasalar Binance ile DEĞİL, Chainlink BTC/USD data stream ile
          çözülür (piyasa açıklamasında açıkça yazıyor). Ölçtük: Binance fiyat
          kıyasıyla karar vermek pencerelerin ~%8'inde YANLIŞ sonuç veriyor —
          çünkü 5 dakikalık hareket çoğu zaman birkaç dolar ve iki kaynak
          arasındaki küçük fark sonucu ters çeviriyor.

          Yanlış sonuç = yanlış P&L + kalibrasyona yanlış sinyal + online
          modelin bozuk etiketle öğrenmesi. Bu yüzden tek doğru kaynak
          piyasanın kendi resmi çözümüdür.

        Sonuç henüz belli değilse None döner (çağıran tekrar denemeli).
        Sonuçlar cache'lenir; aynı pencere iki kez sorgulanmaz.
        """
        cached = self.window_official_outcome.get(window_start)
        if cached:
            return cached

        slug = f"{POLY_SLUG_PREFIX}{window_start}"
        try:
            mk = await self.client.get_market(slug=slug)
            d = mk.model_dump()

            status = (d.get("resolution") or {}).get("uma_resolution_status")
            if status is None:
                return None  # henüz çözülmemiş

            yes = (d.get("outcomes") or {}).get("yes") or {}
            price = yes.get("price")
            if price is None:
                return None
            price = float(price)
            # Çözülmüş piyasada kazanan taraf 1, kaybeden 0 olur
            if price >= 0.9:
                outcome = "UP"
            elif price <= 0.1:
                outcome = "DOWN"
            else:
                return None  # kesinleşmemiş, bekle

            self.window_official_outcome[window_start] = outcome
            # Bellek: 2 saatten eski kayıtları at
            cutoff = int(time.time()) - 7200
            self.window_official_outcome = {
                w: o for w, o in self.window_official_outcome.items() if w >= cutoff
            }
            return outcome

        except Exception as e:
            logger.debug(f"Resmi sonuç alınamadı ({slug}): {e}")
            return None

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
            # Pencerenin GERÇEK açılış fiyatı: pencere başlangıcındaki 1dk mumun
            # open değeri (ilk görüldüğü andaki fiyat değil — ~15s sapma olurdu).
            if window_start not in self.window_open_price:
                open_px = self.kline_open_at(window_start)
                if open_px is None and self.latest_btc_price > 0:
                    open_px = self.latest_btc_price  # yedek
                if open_px:
                    self.window_open_price[window_start] = open_px
                # Pencere başındaki mikroyapıyı dondur (online modelin girdisi)
                self.window_micro[window_start] = self.microstructure_snapshot()
            # Eski pencereleri buda (bellek): 1 saatten eski
            cutoff = int(time.time()) - 3600
            self.window_open_price = {
                w: p for w, p in self.window_open_price.items() if w >= cutoff
            }
            self.window_last_up_price = {
                w: p for w, p in self.window_last_up_price.items() if w >= cutoff
            }
            self.window_micro = {
                w: m for w, m in self.window_micro.items() if w >= cutoff
            }

            # Midpoint (implied probability)
            up_price, down_price = 0.50, 0.50
            try:
                up_price = float(await self.client.get_midpoint(token_id=up_token))
                down_price = float(await self.client.get_midpoint(token_id=down_token))
            except Exception as e:
                logger.debug(f"midpoint fetch failed: {e}")

            # Piyasanın kendi kararı: pencerenin son UP fiyatı (1'e yakın = UP kazandı).
            # Fiyat alındıktan SONRA kaydedilmeli.
            self.window_last_up_price[window_start] = up_price

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

    async def window_tracker_loop(self):
        """
        BTC 5dk pencerelerini POLYMARKET'TEN BAĞIMSIZ takip eder.

        Neden ayrı: online model "şu mikroyapıda BTC 5dk sonra yukarı mı
        gitti?" sorusunu öğrenir — bunun Polymarket ile ilgisi yoktur.
        Polymarket erişilemezken (bakım, ağ, piyasa arası boşluk) bile
        model öğrenmeye DEVAM etmeli. Aksi halde değerli veri kaybedilir.
        """
        while True:
            try:
                now = int(time.time())
                w = now - (now % 300)   # şu anki 5dk penceresi
                if w not in self.window_micro:
                    open_px = self.kline_open_at(w) or self.latest_btc_price
                    if open_px:
                        self.window_open_price.setdefault(w, open_px)
                        self.window_micro[w] = self.microstructure_snapshot()
            except Exception as e:
                logger.debug(f"window tracker hatası: {e}")
            await asyncio.sleep(10)

    async def start(self):
        """Tüm veri akışlarını başlatır (spot, depth, futures, OI, Polymarket)."""
        # Önce geçmiş mumları yükle -> stratejiler beklemeden çalışsın
        await asyncio.to_thread(self.bootstrap_klines, 60)
        asyncio.create_task(self.binance_ws_loop())        # fiyat + CVD
        asyncio.create_task(self.binance_depth_loop())     # emir defteri
        asyncio.create_task(self.binance_futures_loop())   # funding + mark (REST)
        asyncio.create_task(self.liquidation_loop())       # likidasyonlar (opsiyonel)
        asyncio.create_task(self.open_interest_loop())     # açık pozisyon
        asyncio.create_task(self.window_tracker_loop())    # 5dk pencere takibi (öğrenme için)
        while True:
            await self.fetch_polymarket_data()
            await asyncio.sleep(15)  # refresh Polymarket data every 15s
