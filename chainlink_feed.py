"""
Chainlink BTC/USD fiyat akışı — POLYMARKET'İN ÇÖZÜM KAYNAĞI.

NEDEN BU DOSYA VAR (kritik):
  Polymarket'in "Bitcoin Up or Down — 5m" piyasaları Binance ile DEĞİL,
  Chainlink BTC/USD ile çözülür. Sistem şimdiye kadar Binance fiyatına
  bakıp karar veriyordu. Canlı ölçüm (29 Tem 2026):

      Binance spot        : $64,414.08
      Chainlink (Polygon) : $64,337.58   -> fark $76.50 (~12 bps)

  5 dakikalık bir BTC hareketi tipik olarak $20-60'tır. Yani iki kaynak
  arasındaki fark, TAHMİN ETMEYE ÇALIŞTIĞIMIZ HAREKETTEN BÜYÜK. Sabit bir
  offset olsa sorun olmazdı (pencere içinde sadeleşirdi) ama değil:
  Chainlink kesikli güncellenir (sapma eşiği + heartbeat), yani offset
  sürekli oynar. Pencerelerin bir kısmında iki kaynak TERS yön gösterir.

  Bu yüzden burada gerçek Chainlink verisi okunur. Uydurma/mock yok:
  veri alınamazsa None döner ve sistem bunu açıkça bildirir.

NEDEN POLYGON:
  Polymarket Polygon üzerinde çalışır ve Polygon'daki BTC/USD aggregator'ı
  Ethereum mainnet'e göre çok daha sık güncellenir (ölçüm: 49s vs 256s
  yaş). Taze olan, resolution'a yakın olandır.

NOT (dürüstlük):
  Polymarket bu hızlı piyasalarda Chainlink'in "Data Streams" ürününü
  kullanır; buradaki on-chain aggregator aynı fiyat ailesidir ama birebir
  aynı güncelleme anları değildir. Yani bu, Binance'e göre ÇOK daha doğru
  bir vekildir — mükemmel bir kopya değil. Sistem bu farkı `age_sec` ve
  `binance_divergence_bps` olarak açıkça raporlar; gizlemez.
"""
import json
import time
import asyncio
import logging
import urllib.request
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

# AggregatorV3Interface.latestRoundData() fonksiyon seçicisi
_SELECTOR_LATEST_ROUND_DATA = "0xfeaf968c"

# BTC/USD aggregator proxy adresleri (Chainlink resmi dokümantasyonu)
FEEDS = [
    {
        "name": "polygon",
        "address": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
        "rpcs": [
            "https://polygon-bor-rpc.publicnode.com",
            "https://1rpc.io/matic",
            "https://polygon.drpc.org",
        ],
    },
    {
        "name": "ethereum",
        "address": "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c",
        "rpcs": [
            "https://ethereum-rpc.publicnode.com",
            "https://eth.drpc.org",
        ],
    },
]

DECIMALS = 8          # BTC/USD feed'leri 8 ondalık kullanır
STALE_AFTER_SEC = 180  # bu yaştan sonra veri "bayat" sayılır


def _rpc_call(rpc_url: str, address: str, timeout: float = 8.0) -> Optional[bytes]:
    """Tek bir eth_call. Başarısızsa None (istisna fırlatmaz)."""
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": address, "data": _SELECTOR_LATEST_ROUND_DATA}, "latest"],
    }).encode()
    # Public RPC'lerin çoğu urllib'in varsayılan User-Agent'ını 403 ile reddeder.
    req = urllib.request.Request(
        rpc_url, data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "tradebot/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
    except Exception as e:
        logger.debug(f"Chainlink RPC hatası ({rpc_url}): {e}")
        return None

    if "error" in body or not body.get("result"):
        logger.debug(f"Chainlink RPC yanıt hatası ({rpc_url}): {body.get('error')}")
        return None
    raw = body["result"]
    if not isinstance(raw, str) or not raw.startswith("0x"):
        return None
    try:
        return bytes.fromhex(raw[2:])
    except ValueError:
        return None


def _decode_round(data: bytes) -> Optional[dict]:
    """
    latestRoundData() dönüşünü çözer:
      (uint80 roundId, int256 answer, uint256 startedAt,
       uint256 updatedAt, uint80 answeredInRound)
    Her alan 32 bayt.
    """
    if data is None or len(data) < 160:
        return None
    def word(i: int) -> int:
        return int.from_bytes(data[i * 32:(i + 1) * 32], "big")

    answer = word(1)
    # int256 negatif kontrolü (fiyat negatif olmamalı ama sözleşme int256 döner)
    if answer >= 1 << 255:
        answer -= 1 << 256
    if answer <= 0:
        return None

    updated_at = word(3)
    if updated_at <= 0:
        return None

    return {
        "round_id": word(0),
        "price": answer / (10 ** DECIMALS),
        "updated_at": float(updated_at),
    }


def fetch_chainlink_price() -> Optional[dict]:
    """
    Chainlink BTC/USD'yi okur. Feed'ler ve RPC'ler sırayla denenir;
    ilk BAŞARILI ve TAZE yanıt döner.

    Dönüş: {price, updated_at, age_sec, feed, round_id} veya None.
    Asla uydurma değer döndürmez.
    """
    best = None
    for feed in FEEDS:
        for rpc in feed["rpcs"]:
            decoded = _decode_round(_rpc_call(rpc, feed["address"]))
            if decoded is None:
                continue
            decoded["feed"] = feed["name"]
            decoded["rpc"] = rpc
            decoded["age_sec"] = max(0.0, time.time() - decoded["updated_at"])
            # En taze olanı tercih et; ilk feed (polygon) zaten öncelikli
            if best is None or decoded["age_sec"] < best["age_sec"]:
                best = decoded
            break  # bu feed için çalışan RPC bulundu, sıradaki feed'e geç
        if best is not None and best["age_sec"] < STALE_AFTER_SEC:
            break  # taze veri var, diğer zincire bakmaya gerek yok
    return best


class ChainlinkPriceFeed:
    """
    Chainlink BTC/USD'yi periyodik okuyup geçmişini tutar.

    Polymarket'in resolution'ı bu fiyat ailesine dayandığı için,
    stratejilerin "pencere içinde fiyat yukarı mı gitti?" sorusunu
    BU seriyle sorması gerekir — Binance ile değil.
    """

    def __init__(self, poll_interval: float = 10.0):
        self.poll_interval = poll_interval
        self.price: float = 0.0
        self.updated_at: float = 0.0      # zincir üstündeki güncelleme anı
        self.fetched_at: float = 0.0      # bizim okuduğumuz an
        self.feed_name: str = ""
        self.round_id: int = 0
        self.history: deque = deque(maxlen=600)   # (fetched_at, price)
        self.consecutive_failures: int = 0
        self.total_reads: int = 0
        # 5dk pencere -> o pencere başındaki Chainlink fiyatı
        self.window_open_price: dict[int, float] = {}

    # ---------------- durum ----------------

    @property
    def is_live(self) -> bool:
        """Elimizde kullanılabilir, bayat olmayan bir fiyat var mı?"""
        if self.price <= 0 or self.fetched_at <= 0:
            return False
        return (time.time() - self.fetched_at) < STALE_AFTER_SEC

    @property
    def age_sec(self) -> Optional[float]:
        """Zincirdeki son güncellemenin üzerinden geçen süre."""
        if self.updated_at <= 0:
            return None
        return time.time() - self.updated_at

    def price_at_or_before(self, ts: float) -> Optional[float]:
        """Verilen ana kadar bilinen son Chainlink fiyatı (ileriye bakış yok)."""
        best = None
        for t, p in self.history:
            if t <= ts:
                best = p
            else:
                break
        return best

    def momentum_pct(self, seconds: float = 300.0) -> Optional[float]:
        """Chainlink serisi üzerinde yüzde değişim — resolution ile aynı ölçü."""
        if len(self.history) < 2:
            return None
        cutoff = time.time() - seconds
        old = None
        for t, p in self.history:
            if t >= cutoff:
                old = p
                break
        if old is None or old <= 0:
            return None
        return (self.price - old) / old * 100.0

    def window_change_pct(self, window_start: int) -> Optional[float]:
        """
        Pencere açılışından ŞU ANA kadar Chainlink'e göre yüzde değişim.
        Piyasanın gerçekte neye göre çözüleceğinin en yakın canlı vekili.
        """
        open_px = self.window_open_price.get(window_start)
        if not open_px or self.price <= 0:
            return None
        return (self.price - open_px) / open_px * 100.0

    def divergence_bps(self, binance_price: float) -> Optional[float]:
        """
        Binance ile Chainlink arasındaki fark (baz puan).
        Pozitif = Binance daha yüksek. Bu değer 5dk'lık tipik hareketten
        büyükse, Binance'e bakarak karar vermek kumar demektir.
        """
        if self.price <= 0 or binance_price <= 0:
            return None
        return (binance_price - self.price) / self.price * 10000.0

    # ---------------- toplama ----------------

    def _record(self, data: dict):
        self.price = data["price"]
        self.updated_at = data["updated_at"]
        self.fetched_at = time.time()
        self.feed_name = data.get("feed", "")
        self.round_id = data.get("round_id", 0)
        self.history.append((self.fetched_at, self.price))
        self.total_reads += 1
        self.consecutive_failures = 0

        # Pencere açılış fiyatını dondur (ilk okuma o pencerenin referansı olur)
        now = int(self.fetched_at)
        w = now - (now % 300)
        self.window_open_price.setdefault(w, self.price)
        cutoff = now - 7200
        self.window_open_price = {
            k: v for k, v in self.window_open_price.items() if k >= cutoff
        }

    async def poll_loop(self):
        """Arka planda Chainlink'i okumaya devam eder."""
        while True:
            try:
                data = await asyncio.to_thread(fetch_chainlink_price)
                if data is None:
                    self.consecutive_failures += 1
                    if self.consecutive_failures in (3, 10, 30):
                        logger.warning(
                            f"⚠️  Chainlink okunamıyor ({self.consecutive_failures} deneme) — "
                            f"resolution kaynağıyla hizalı fiyat YOK."
                        )
                else:
                    first = self.total_reads == 0
                    self._record(data)
                    if first:
                        logger.info(
                            f"🔗 Chainlink BTC/USD bağlandı: ${self.price:,.2f} "
                            f"({self.feed_name}, yaş {self.age_sec:.0f}s) — "
                            f"Polymarket'in çözüm kaynağı"
                        )
            except Exception as e:
                logger.debug(f"Chainlink poll hatası: {e}")
            await asyncio.sleep(self.poll_interval)

    def to_dict(self) -> dict:
        """Dashboard için şeffaf durum — sorun varsa gizlemez."""
        return {
            "price": round(self.price, 2) if self.price else None,
            "feed": self.feed_name or None,
            "is_live": self.is_live,
            "age_sec": round(self.age_sec, 1) if self.age_sec is not None else None,
            "last_read_ago": round(time.time() - self.fetched_at, 1) if self.fetched_at else None,
            "total_reads": self.total_reads,
            "consecutive_failures": self.consecutive_failures,
            "history_points": len(self.history),
        }
