"""
Güven Kalibrasyonu (Confidence Calibration)

SORUN:
  Stratejiler güveni uydurma formüllerle üretir (ör. 0.52 + strength*0.15).
  Bu sayının gerçekle ilgisi olmayabilir. Strateji "%60 eminim" derken
  gerçekte %48 tutturuyorsa, EV hesabı yanlıştır ve sistem yapısal olarak
  kaybeder — çünkü tüm işlem kararı EV'ye dayanır.

ÇÖZÜM:
  Her stratejinin (tahmin, gerçek sonuç) çiftlerini biriktir ve ham güveni
  AMPİRİK olarak düzelt. Platt scaling (tek özellikli lojistik regresyon)
  kullanılır:

      p_kalibre = sigmoid(a * logit(p_ham) + b)

  Az veri varken (a=1, b=0) yani kimliğe yakın kalır; veri biriktikçe
  gerçek performansa göre eğrilir. Ayrıca güven, kanıtlanmış isabet oranına
  doğru "shrink" edilir (Bayesçi yumuşatma) — böylece 3 işlemlik şans
  serisi modeli aşırı agresif yapmaz.

ÖLÇÜM:
  - Brier skoru: ortalama (tahmin - sonuç)^2. Düşük = iyi. 0.25 = rastgele.
  - Kalibrasyon eğrisi: tahmin kovaları vs gerçek isabet.
"""
import math
import logging
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

# Kalibrasyonun devreye girmesi için gereken minimum sonuçlanmış işlem
MIN_SAMPLES_FOR_FIT = 15
# Bu sayıya ulaşınca kalibrasyona tam güvenilir (öncesinde kısmi harmanlanır)
FULL_TRUST_SAMPLES = 60


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


class Calibrator:
    """
    Tek bir strateji için güven kalibratörü.

    Kullanım:
      cal.record(raw_conf=0.58, won=True)     # sonuç geldiğinde
      p = cal.calibrate(0.58)                 # işlem öncesi düzeltilmiş güven
    """

    def __init__(self, name: str, maxlen: int = 500):
        self.name = name
        self.samples: deque = deque(maxlen=maxlen)  # (raw_conf, won 0/1)
        self.a: float = 1.0   # Platt eğim
        self.b: float = 0.0   # Platt kayma
        self._fitted_at: int = 0

    # ---------------- kayıt ----------------

    def record(self, raw_conf: float, won: bool):
        """Sonuçlanmış bir tahmini kaydet ve gerekirse yeniden fit et."""
        self.samples.append((float(raw_conf), 1.0 if won else 0.0))
        # Her 5 yeni örnekte bir yeniden fit (ucuz)
        if len(self.samples) >= MIN_SAMPLES_FOR_FIT and len(self.samples) - self._fitted_at >= 5:
            self._fit()
            self._fitted_at = len(self.samples)

    # ---------------- fit ----------------

    def _fit(self, iters: int = 300, lr: float = 0.08):
        """
        Platt scaling: log-loss üzerinden gradient descent ile a, b bulunur.
        Basit, hızlı ve az veriyle stabil (a düzenlileştirmesi ile).
        """
        xs = [_logit(c) for c, _ in self.samples]
        ys = [y for _, y in self.samples]
        n = len(xs)
        if n < MIN_SAMPLES_FOR_FIT:
            return

        a, b = self.a, self.b
        for _ in range(iters):
            ga = gb = 0.0
            for x, y in zip(xs, ys):
                p = _sigmoid(a * x + b)
                err = p - y
                ga += err * x
                gb += err
            ga /= n
            gb /= n
            # a'yı 1'e doğru hafif düzenlileştir (aşırı uçlara kaçmasın)
            ga += 0.02 * (a - 1.0)
            a -= lr * ga
            b -= lr * gb
            a = max(0.05, min(3.0, a))
            b = max(-2.0, min(2.0, b))

        self.a, self.b = a, b

    # ---------------- uygulama ----------------

    def calibrate(self, raw_conf: float) -> float:
        """
        Ham güveni ampirik olarak düzeltilmiş güvene çevirir.

        - Az veri: ham değere yakın kalır (güvenli).
        - Veri arttıkça: Platt dönüşümüne tam geçer.
        - Her hâlükârda gerçekçi bantta tutulur (0.20–0.80).
        """
        raw = min(max(float(raw_conf), 0.01), 0.99)
        n = len(self.samples)
        if n < MIN_SAMPLES_FOR_FIT:
            return raw

        p = _sigmoid(self.a * _logit(raw) + self.b)

        # Veri az iken ham ile harmanla (kademeli güven)
        w = min(1.0, (n - MIN_SAMPLES_FOR_FIT) / max(1, FULL_TRUST_SAMPLES - MIN_SAMPLES_FOR_FIT))
        p = w * p + (1 - w) * raw

        return min(max(p, 0.20), 0.80)

    # ---------------- ölçüm ----------------

    @property
    def brier(self) -> Optional[float]:
        """Brier skoru: ortalama (tahmin - sonuç)^2. 0.25 = rastgele, düşük iyi."""
        if not self.samples:
            return None
        return sum((c - y) ** 2 for c, y in self.samples) / len(self.samples)

    @property
    def brier_calibrated(self) -> Optional[float]:
        """Kalibrasyon sonrası Brier — düzeltmenin işe yarayıp yaramadığını gösterir."""
        if len(self.samples) < MIN_SAMPLES_FOR_FIT:
            return None
        tot = 0.0
        for c, y in self.samples:
            p = _sigmoid(self.a * _logit(c) + self.b)
            tot += (p - y) ** 2
        return tot / len(self.samples)

    @property
    def empirical_accuracy(self) -> Optional[float]:
        """Gerçek isabet oranı (tüm tahminlerde)."""
        if not self.samples:
            return None
        return sum(y for _, y in self.samples) / len(self.samples)

    @property
    def mean_predicted(self) -> Optional[float]:
        """Ortalama tahmin edilen güven — isabetle karşılaştırılır."""
        if not self.samples:
            return None
        return sum(c for c, _ in self.samples) / len(self.samples)

    def reliability_buckets(self, n_buckets: int = 4) -> list:
        """
        Kalibrasyon eğrisi: her güven kovasında tahmin vs gerçek.
        Dashboard'da 'model dürüst mü?' sorusunu görsel yanıtlar.
        """
        if not self.samples:
            return []
        lo, hi = 0.35, 0.75
        width = (hi - lo) / n_buckets
        out = []
        for i in range(n_buckets):
            b0 = lo + i * width
            b1 = b0 + width
            sel = [(c, y) for c, y in self.samples if (b0 <= c < b1 or (i == n_buckets - 1 and c >= b1))]
            if not sel:
                continue
            out.append({
                "range": f"{b0:.2f}-{b1:.2f}",
                "n": len(sel),
                "predicted": round(sum(c for c, _ in sel) / len(sel), 3),
                "actual": round(sum(y for _, y in sel) / len(sel), 3),
            })
        return out

    def to_dict(self) -> dict:
        return {
            "samples": len(self.samples),
            "a": round(self.a, 3),
            "b": round(self.b, 3),
            "brier": round(self.brier, 4) if self.brier is not None else None,
            "brier_calibrated": round(self.brier_calibrated, 4) if self.brier_calibrated is not None else None,
            "empirical_accuracy": round(self.empirical_accuracy, 3) if self.empirical_accuracy is not None else None,
            "mean_predicted": round(self.mean_predicted, 3) if self.mean_predicted is not None else None,
            "active": len(self.samples) >= MIN_SAMPLES_FOR_FIT,
            "buckets": self.reliability_buckets(),
        }

    def load(self, d: dict):
        """Kalıcılıktan geri yükle."""
        try:
            self.a = float(d.get("a", 1.0))
            self.b = float(d.get("b", 0.0))
            for c, y in (d.get("raw_samples") or []):
                self.samples.append((float(c), float(y)))
            self._fitted_at = len(self.samples)
        except Exception as e:
            logger.debug(f"Kalibratör yüklenemedi: {e}")

    def dump(self) -> dict:
        """Kalıcılık için (ham örnekler dahil)."""
        return {
            "a": self.a,
            "b": self.b,
            "raw_samples": [[c, y] for c, y in list(self.samples)[-300:]],
        }
