"""
AI Kota Yöneticisi (Budget Manager)

GERÇEK KISIT:
  Gemini ücretsiz katmanı model başına GÜNDE ~20 istek veriyor
  (quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier, quotaValue: 20).
  Sistem 7 ajanla her işlemde LLM çağırınca ilk dakikada kota bitiyor ve
  24 saat boyunca TÜM AI ölüyor — üretimde tam olarak bu oldu.

TASARIM DEĞİŞİKLİĞİ:
  LLM'i her karara çağırmak yerine, LLM'e POLİTİKA yazdırırız; kararları
  yerel kod anında ve bedava uygular. Gerçek trading sistemleri de böyle
  çalışır — her tick'te model çağrılmaz.

  - Yerel "persona brain": her kararda çalışır, ücretsiz, anlık
  - LLM (nadir): ajanın tezini/parametrelerini günceller

BÜTÇE:
  Günlük kota ajanlar arasında adil paylaştırılır ve en değerli iş olan
  ÖZ-DEĞERLENDİRME'ye (reflection) ayrılır. Kota biterse sistem çalışmaya
  devam eder — sadece yerel beyinle.
"""
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class AIBudget:
    """
    Günlük LLM çağrı bütçesini yöneten paylaşılan sayaç + MODEL ROTASYONU.

    Ücretsiz katman model başına ~20 istek/gün verir. Tek modelle kota
    dakikalar içinde biter. Birden fazla modeli sırayla kullanmak kotayı
    aşmak DEĞİLDİR — her modelin kendi ayrı kotası vardır. Biri dolunca
    (429) o model gün sonuna kadar işaretlenir ve sıradakine geçilir.

    5 model x 20 = ~100 istek/gün kapasite.
    """

    def __init__(self, models: Optional[list] = None,
                 per_model_limit: int = 18, reserve_ratio: float = 0.75):
        from config import GEMINI_MODELS
        self.models = list(models or GEMINI_MODELS)
        self.per_model_limit = per_model_limit          # 20'nin biraz altı (güvenlik payı)
        self.reserve_ratio = reserve_ratio              # kotanın %75'i öz-değerlendirmeye
        self.day_start: float = self._today_start()
        # Model başına durum
        self.used_by_model: dict = {m: 0 for m in self.models}
        self.blocked_until: dict = {m: 0.0 for m in self.models}
        self.total_calls: int = 0
        self.total_429: int = 0

    @property
    def daily_limit(self) -> int:
        return self.per_model_limit * max(1, len(self.models))

    @property
    def used_today(self) -> int:
        self._roll_day_if_needed()
        return sum(self.used_by_model.values())

    def current_model(self) -> Optional[str]:
        """Kotası kalan ilk modeli döndürür (yoksa None)."""
        self._roll_day_if_needed()
        now = time.time()
        for m in self.models:
            if now < self.blocked_until.get(m, 0.0):
                continue
            if self.used_by_model.get(m, 0) < self.per_model_limit:
                return m
        return None

    @staticmethod
    def _today_start() -> float:
        t = time.gmtime()
        return time.time() - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec)

    def _roll_day_if_needed(self):
        if time.time() - self.day_start >= 86400:
            self.day_start = self._today_start()
            self.used_by_model = {m: 0 for m in self.models}
            self.blocked_until = {m: 0.0 for m in self.models}
            logger.info("🔄 AI günlük kotası yenilendi (tüm modeller).")

    def can_spend(self, purpose: str = "general") -> bool:
        """
        Bu amaç için kota var mı?

        purpose="reflection" -> tüm kalan kotayı kullanabilir (öncelikli iş)
        purpose="general"    -> yalnızca rezerv dışındaki payı kullanır
        """
        if self.current_model() is None:
            return False
        if purpose == "reflection":
            return True
        # Genel amaçlı çağrılar öz-değerlendirme rezervini yiyemez
        general_cap = int(self.daily_limit * (1 - self.reserve_ratio))
        return self.used_today < general_cap

    def spend(self, model: str):
        self._roll_day_if_needed()
        self.used_by_model[model] = self.used_by_model.get(model, 0) + 1
        self.total_calls += 1

    def mark_model_exhausted(self, model: str, retry_after_sec: float = 3600.0):
        """Bu modelin kotası doldu — sıradakine geç."""
        self.total_429 += 1
        self.blocked_until[model] = time.time() + retry_after_sec
        self.used_by_model[model] = self.per_model_limit
        nxt = self.current_model()
        if nxt:
            logger.info(f"🔀 '{model}' kotası doldu → '{nxt}' modeline geçiliyor.")
        else:
            logger.warning(
                "⚠️  Tüm modellerin günlük kotası doldu. "
                "Sistem yerel persona beyniyle tam çalışmaya devam ediyor."
            )

    def to_dict(self) -> dict:
        self._roll_day_if_needed()
        cur = self.current_model()
        return {
            "used_today": self.used_today,
            "daily_limit": self.daily_limit,
            "remaining": max(0, self.daily_limit - self.used_today),
            "current_model": cur,
            "exhausted": cur is None,
            "models": len(self.models),
            "per_model": {m: self.used_by_model.get(m, 0) for m in self.models},
            "total_calls": self.total_calls,
            "total_429": self.total_429,
        }


# Tüm ajanların paylaştığı tek bütçe
BUDGET = AIBudget()
