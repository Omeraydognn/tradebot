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
    Günlük LLM çağrı bütçesini yöneten paylaşılan sayaç.

    Tüm ajanlar aynı bütçeyi paylaşır. 429 (kota doldu) görülürse gün
    sonuna kadar tüm çağrılar durdurulur — boşuna denenmez.
    """

    def __init__(self, daily_limit: int = 18, reserve_for_reflection: int = 14):
        # Gerçek limit 20; küçük bir güvenlik payı bırakıyoruz
        self.daily_limit = daily_limit
        # Kotanın çoğu öz-değerlendirmeye ayrılır (en değerli kullanım)
        self.reserve_for_reflection = reserve_for_reflection
        self.used_today: int = 0
        self.day_start: float = self._today_start()
        self.exhausted_until: float = 0.0   # 429 sonrası bekleme
        self.total_calls: int = 0
        self.total_429: int = 0

    @staticmethod
    def _today_start() -> float:
        t = time.gmtime()
        return time.time() - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec)

    def _roll_day_if_needed(self):
        if time.time() - self.day_start >= 86400:
            self.day_start = self._today_start()
            self.used_today = 0
            self.exhausted_until = 0.0
            logger.info("🔄 AI günlük kotası yenilendi.")

    def can_spend(self, purpose: str = "general") -> bool:
        """
        Bu amaç için kota var mı?

        purpose="reflection" -> ayrılmış payı kullanabilir (öncelikli)
        purpose="general"    -> yalnızca rezerv dışındaki payı kullanır
        """
        self._roll_day_if_needed()
        if time.time() < self.exhausted_until:
            return False
        if purpose == "reflection":
            return self.used_today < self.daily_limit
        # Genel amaçlı çağrılar rezervi yiyemez
        general_cap = max(0, self.daily_limit - self.reserve_for_reflection)
        return self.used_today < general_cap

    def spend(self):
        self._roll_day_if_needed()
        self.used_today += 1
        self.total_calls += 1

    def mark_exhausted(self, retry_after_sec: float = 3600.0):
        """429 alındı — bir süre hiç deneme (boşuna istek atma)."""
        self.total_429 += 1
        self.exhausted_until = time.time() + retry_after_sec
        self.used_today = self.daily_limit  # bugünlük bitti say
        logger.warning(
            f"⚠️  AI günlük kotası doldu (429). "
            f"{retry_after_sec/60:.0f} dk boyunca yerel beyinle devam edilecek."
        )

    def to_dict(self) -> dict:
        self._roll_day_if_needed()
        return {
            "used_today": self.used_today,
            "daily_limit": self.daily_limit,
            "remaining": max(0, self.daily_limit - self.used_today),
            "exhausted": time.time() < self.exhausted_until,
            "total_calls": self.total_calls,
            "total_429": self.total_429,
        }


# Tüm ajanların paylaştığı tek bütçe
BUDGET = AIBudget()
