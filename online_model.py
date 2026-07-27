"""
Online Öğrenen Model (Online Logistic Regression)

FİKİR:
  Stratejiler insan tarafından yazılmış sabit kurallardır ("RSI>52 ise AL").
  Bu model ise HİÇBİR KURAL VARSAYMAZ — sadece veriye bakar:

      "Şu mikroyapı koşullarında (CVD, emir defteri, funding, OI...)
       BTC 5 dakika sonra gerçekte yukarı mı gitti?"

  Her sonuçlanan pencereden öğrenir (stochastic gradient descent) ve
  zamanla hangi özelliklerin gerçekten öngörü taşıdığını kendi bulur.
  Ağırlıklar dashboard'da görünür — hangi sinyalin işe yaradığı şeffaftır.

NEDEN ONLINE?
  Piyasa rejimi değişir. Batch eğitim eskir; online model son dönemi
  ağırlıklandırarak uyum sağlar (öğrenme oranı + hafif ağırlık çürümesi).

DÜRÜSTLÜK:
  Model kendi doğruluğunu ve log-loss'unu raporlar. Rastgeleden (%50 /
  0.693) daha iyi değilse, bunu dashboard'da açıkça gösterir — sahte
  güven vermez.
"""
import math
import logging
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

# Modelin kullandığı özellikler ve normalizasyon ölçekleri.
# (değer / ölçek) yaklaşık -1..+1 bandına düşsün diye seçildi.
FEATURE_SPEC = [
    ("cvd_ratio_60s",       1.0),      # zaten -1..1
    ("cvd_ratio_300s",      1.0),
    ("book_imbalance",      1.0),      # zaten -1..1
    ("funding_rate",        0.0003),   # ±0.03% -> ±1
    ("basis_pct",           0.05),     # ±0.05% -> ±1
    ("oi_change_5m",        0.20),     # ±0.2% -> ±1
    ("momentum_1m",         0.05),     # ±0.05% -> ±1
    ("momentum_5m",         0.10),
    ("tf_alignment",        1.0),      # -1..1
    ("realized_vol",        0.02),
    ("poly_book_imbalance", 1.0),      # Polymarket'in kendi UP defteri, -1..1
    ("window_elapsed_frac", 1.0),      # pencerede ne kadar ilerlendiği, -1..1
    ("outcome_streak",      3.0),      # ardışık UP/DOWN sayısı, ±3 -> ±1
]

MIN_SAMPLES_TO_PREDICT = 25   # bu kadar sonuç görmeden tahmin vermez


class OnlineLogisticModel:
    """Mikroyapı özelliklerinden P(UP) öğrenen artımlı lojistik regresyon."""

    def __init__(self, lr: float = 0.05, l2: float = 1e-4):
        self.lr = lr
        self.l2 = l2
        self.w: list[float] = [0.0] * len(FEATURE_SPEC)
        self.bias: float = 0.0
        self.n_updates: int = 0
        # Performans takibi
        self.history: deque = deque(maxlen=500)  # (p_pred, y)
        self.recent_correct: deque = deque(maxlen=100)

    # ---------------- özellik çıkarımı ----------------

    @staticmethod
    def extract_features(micro: dict) -> Optional[list[float]]:
        """
        Mikroyapı sözlüğünden normalize özellik vektörü.
        Kritik özellikler eksikse None döner (yarım veriyle öğrenme yok).
        """
        if not micro:
            return None
        feats = []
        missing = 0
        for key, scale in FEATURE_SPEC:
            v = micro.get(key)
            if v is None:
                feats.append(0.0)   # nötr
                missing += 1
            else:
                x = float(v) / scale if scale else float(v)
                feats.append(max(-3.0, min(3.0, x)))  # aykırı değer kırpma
        # Yarıdan fazlası eksikse güvenilmez
        if missing > len(FEATURE_SPEC) // 2:
            return None
        return feats

    # ---------------- tahmin ----------------

    def _raw_prob(self, feats: list[float]) -> float:
        z = self.bias + sum(w * x for w, x in zip(self.w, feats))
        z = max(-8.0, min(8.0, z))
        return 1.0 / (1.0 + math.exp(-z))

    def predict_proba(self, micro: dict) -> Optional[float]:
        """
        P(UP) tahmini. Yeterli örnek yoksa None (sahte güven vermez).
        Çıktı gerçekçi banda kırpılır.
        """
        if self.n_updates < MIN_SAMPLES_TO_PREDICT:
            return None
        feats = self.extract_features(micro)
        if feats is None:
            return None
        return min(max(self._raw_prob(feats), 0.15), 0.85)

    # ---------------- öğrenme ----------------

    def update(self, micro: dict, went_up: bool):
        """
        Bir pencere sonuçlandığında öğren.
        micro: pencere BAŞINDA kaydedilen mikroyapı (ileriye bakış yok!)
        """
        feats = self.extract_features(micro)
        if feats is None:
            return
        y = 1.0 if went_up else 0.0
        p = self._raw_prob(feats)
        err = p - y

        # SGD + L2
        for i, x in enumerate(feats):
            self.w[i] -= self.lr * (err * x + self.l2 * self.w[i])
        self.bias -= self.lr * err

        self.n_updates += 1
        self.history.append((p, y))
        self.recent_correct.append(1 if (p >= 0.5) == (y == 1.0) else 0)

    # ---------------- ölçüm ----------------

    @property
    def accuracy(self) -> Optional[float]:
        if not self.history:
            return None
        return sum(1 for p, y in self.history if (p >= 0.5) == (y == 1.0)) / len(self.history)

    @property
    def recent_accuracy(self) -> Optional[float]:
        if not self.recent_correct:
            return None
        return sum(self.recent_correct) / len(self.recent_correct)

    @property
    def log_loss(self) -> Optional[float]:
        """Rastgele = 0.693. Daha düşük = gerçek öngörü var."""
        if not self.history:
            return None
        tot = 0.0
        for p, y in self.history:
            p = min(max(p, 1e-6), 1 - 1e-6)
            tot += -(y * math.log(p) + (1 - y) * math.log(1 - p))
        return tot / len(self.history)

    @property
    def beats_random(self) -> Optional[bool]:
        """Model rastgeleden anlamlı ölçüde iyi mi? (dürüstlük göstergesi)"""
        ll = self.log_loss
        if ll is None or self.n_updates < MIN_SAMPLES_TO_PREDICT:
            return None
        return ll < 0.685

    def top_features(self, k: int = 5) -> list:
        """En etkili özellikler (|ağırlık| büyüklüğüne göre) — şeffaflık."""
        pairs = [(FEATURE_SPEC[i][0], self.w[i]) for i in range(len(self.w))]
        pairs.sort(key=lambda x: abs(x[1]), reverse=True)
        return [{"feature": n, "weight": round(w, 4)} for n, w in pairs[:k]]

    def to_dict(self) -> dict:
        return {
            "n_updates": self.n_updates,
            "ready": self.n_updates >= MIN_SAMPLES_TO_PREDICT,
            "accuracy": round(self.accuracy, 3) if self.accuracy is not None else None,
            "recent_accuracy": round(self.recent_accuracy, 3) if self.recent_accuracy is not None else None,
            "log_loss": round(self.log_loss, 4) if self.log_loss is not None else None,
            "beats_random": self.beats_random,
            "top_features": self.top_features(),
            "bias": round(self.bias, 4),
        }

    # ---------------- kalıcılık ----------------

    def dump(self) -> dict:
        return {
            "w": self.w,
            "bias": self.bias,
            "n_updates": self.n_updates,
            "history": [[p, y] for p, y in list(self.history)[-300:]],
        }

    def load(self, d: dict):
        try:
            w = d.get("w") or []
            if len(w) == len(self.w):
                self.w = [float(x) for x in w]
            self.bias = float(d.get("bias", 0.0))
            self.n_updates = int(d.get("n_updates", 0))
            for p, y in (d.get("history") or []):
                self.history.append((float(p), float(y)))
                self.recent_correct.append(1 if (p >= 0.5) == (y == 1.0) else 0)
        except Exception as e:
            logger.debug(f"Online model yüklenemedi: {e}")


def bootstrap_from_history(model: "OnlineLogisticModel", limit: int = 500) -> int:
    """
    Modeli GEÇMİŞ Binance verisiyle önceden eğitir.

    Neden mümkün: Binance kline verisi `taker_buy` (agresif alış hacmi)
    içerir — yani CVD/order-flow geçmişe dönük hesaplanabilir. Momentum,
    oynaklık ve zaman dilimi uyumu da geçmişten türetilebilir.
    Emir defteri/funding/OI geçmişi olmadığı için o özellikler nötr (0)
    bırakılır; `extract_features` yarıya kadar eksiğe toleranslıdır.

    SIZINTI YOK: pencere i'nin etiketi kendi açılış/kapanışından gelir;
    özellikleri ise YALNIZCA i'den ÖNCEKİ pencerelerden hesaplanır.

    Dönüş: öğrenilen pencere sayısı.
    """
    # Binance'in ana REST domaini (api.binance.com) bazı bulut sağlayıcı IP'lerinde
    # (Render, AWS, GCP…) HTTP 451 ile engellenir. Resmi mirror'a (data-api.binance.vision)
    # otomatik düşülür — market_data.py'deki SPOT_REST_HOSTS ile aynı strateji.
    hosts = ["https://api.binance.com", "https://data-api.binance.vision"]
    raw = None
    for base in hosts:
        try:
            import urllib.request, json as _json
            url = f"{base}/api/v3/klines?symbol=BTCUSDT&interval=5m&limit={min(limit, 1000)}"
            with urllib.request.urlopen(url, timeout=15) as r:
                raw = _json.loads(r.read().decode())
            break
        except Exception as e:
            logger.debug(f"Geçmiş veri ({base}) başarısız: {e}")
    if raw is None:
        logger.warning("Geçmiş veri çekilemedi (model bootstrap atlandı) — tüm domainler başarısız.")
        return 0

    rows = []
    for k in raw:
        try:
            rows.append({
                "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                "close": float(k[4]), "volume": float(k[5]), "taker_buy": float(k[9]),
            })
        except (IndexError, ValueError, TypeError):
            continue

    def flow_ratio(r):
        v = r["volume"]
        return ((2 * r["taker_buy"] - v) / v) if v > 0 else 0.0

    learned = 0
    # i-3'e kadar geçmiş gerektiği için 3'ten başla; son mum kapanmamış olabilir
    for i in range(3, len(rows) - 1):
        prev1, prev2, prev3 = rows[i - 1], rows[i - 2], rows[i - 3]
        cur = rows[i]

        # --- Özellikler: SADECE i'den önceki pencereler ---
        cvd1 = flow_ratio(prev1)
        cvd3 = (flow_ratio(prev1) + flow_ratio(prev2) + flow_ratio(prev3)) / 3.0
        mom1 = (prev1["close"] - prev1["open"]) / prev1["open"] * 100 if prev1["open"] else 0.0
        mom5 = (prev1["close"] - prev3["open"]) / prev3["open"] * 100 if prev3["open"] else 0.0
        signs = [1 if m > 0 else -1 for m in (mom1, mom5)]
        tf = sum(signs) / len(signs)
        closes = [prev3["close"], prev2["close"], prev1["close"]]
        mean_c = sum(closes) / 3
        var = sum((c - mean_c) ** 2 for c in closes) / 3
        rvol = (var ** 0.5) / mean_c * 100 if mean_c else 0.0

        micro = {
            "cvd_ratio_60s": cvd1,
            "cvd_ratio_300s": cvd3,
            "momentum_1m": mom1,
            "momentum_5m": mom5,
            "tf_alignment": tf,
            "realized_vol": rvol,
            # Geçmişi olmayan özellikler nötr bırakılır:
            "book_imbalance": None, "funding_rate": None,
            "basis_pct": None, "oi_change_5m": None,
        }

        went_up = cur["close"] >= cur["open"]
        before = model.n_updates
        model.update(micro, went_up)
        if model.n_updates > before:
            learned += 1

    if learned:
        logger.info(
            f"🎓 Model geçmiş veriden başlatıldı: {learned} pencere | "
            f"isabet={model.accuracy:.1%} | log-loss={model.log_loss:.4f}"
        )
    return learned
