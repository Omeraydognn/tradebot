"""
Strategy 7: Learned Alpha (Online Öğrenen Model)

Diğer 6 stratejiden temel farkı: HİÇBİR KURAL YAZILMAMIŞTIR.

  Diğerleri: "RSI > 52 ve EMA9 > EMA21 ise AL" (insan varsayımı)
  Bu:        "Geçmişte şu mikroyapı koşullarında ne oldu?" (veriden öğrenme)

Her 5 dakikalık pencere kapandığında model:
  girdi  = pencere BAŞINDAKİ mikroyapı (CVD, defter, funding, OI, momentum…)
  hedef  = pencere gerçekte yukarı mı kapandı?
çiftiyle güncellenir (online lojistik regresyon). Zamanla hangi sinyalin
gerçekten öngörü taşıdığını kendi keşfeder; ağırlıkları dashboard'da görünür.

DÜRÜSTLÜK KURALI:
  Model yeterli veri görmeden (MIN_SAMPLES_TO_PREDICT) hiç sinyal üretmez.
  Ayrıca `beats_random` False ise — yani log-loss rastgeleden iyi değilse —
  işlem açmaz. Sahte güvenle para yakmaz.
"""
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal
from market_data import MarketDataService, PolymarketSnapshot
from paper_trader import PaperTrader
from online_model import OnlineLogisticModel, MIN_SAMPLES_TO_PREDICT

logger = logging.getLogger(__name__)


class LearnedStrategy(BaseStrategy):
    def __init__(self, data: MarketDataService, paper: PaperTrader,
                 model: OnlineLogisticModel):
        super().__init__(name="Learned Alpha", data=data, paper=paper)
        self.model = model
        self.min_conviction = 0.03      # 0.50'den min sapma
        self.require_beats_random = 1.0  # 1=sadece model iyiyken işlem yap
        # Neden sinyal üretmedi (arayüzde gösterilir — "bot dondu mu?" sorusunun cevabı)
        self.silence_reason: Optional[str] = None
        self.last_p_up: Optional[float] = None

    def get_tunables(self) -> dict:
        t = super().get_tunables()
        t.update({
            "min_conviction": {"value": self.min_conviction, "min": 0.01, "max": 0.20,
                               "selectivity": True,
                               "desc": "sinyal için 0.50'den min sapma"},
            "require_beats_random": {"value": self.require_beats_random, "min": 0.0, "max": 1.0,
                                     "desc": "1=model rastgeleyi yenmeden işlem yapma"},
        })
        return t

    def generate_signal(self, snapshot: PolymarketSnapshot) -> Optional[Signal]:
        micro = self.data.microstructure_snapshot()
        p_up = self.model.predict_proba(micro)

        # SESSİZLİK TEŞHİSİ: bu strateji günlerce hiç işlem açmayabilir ve
        # dışarıdan "bot dondu mu?" diye görünür. Neden sustuğunu her
        # döngüde kaydediyoruz; arayüz bunu gösterir.
        if p_up is None:
            if self.model.n_updates < MIN_SAMPLES_TO_PREDICT:
                self.silence_reason = (
                    f"model henüz öğreniyor ({self.model.n_updates}/"
                    f"{MIN_SAMPLES_TO_PREDICT} pencere)"
                )
            else:
                self.silence_reason = "mikroyapı verisi eksik (özellik çıkarılamadı)"
            self.last_p_up = None
            return None

        self.last_p_up = p_up

        # Model henüz rastgeleden iyi değilse işlem açma (dürüstlük kuralı)
        if self.require_beats_random >= 0.5 and self.model.beats_random is False:
            ll = self.model.log_loss
            self.silence_reason = (
                f"model rastgeleyi yenemiyor (log-loss {ll:.4f} ≥ 0.685) — "
                f"dürüstlük kuralı işlemi durduruyor"
            )
            return None

        conviction = abs(p_up - 0.50)
        if conviction < self.min_conviction:
            self.silence_reason = (
                f"kanaat zayıf (|{p_up:.3f}-0.50|={conviction:.3f} "
                f"< {self.min_conviction:.3f})"
            )
            return None

        self.silence_reason = None

        direction = "UP" if p_up > 0.50 else "DOWN"
        confidence = p_up if direction == "UP" else (1.0 - p_up)

        top = self.model.top_features(3)
        top_txt = ", ".join(f"{f['feature']}={f['weight']:+.2f}" for f in top)

        return Signal(
            direction=direction,
            confidence=confidence,
            reasoning=(
                f"Öğrenilmiş model: P(UP)={p_up:.0%} "
                f"({self.model.n_updates} pencereden öğrendi; "
                f"isabet={self.model.accuracy:.0%} | etkili: {top_txt})"
                if self.model.accuracy is not None else
                f"Öğrenilmiş model: P(UP)={p_up:.0%}"
            ),
            indicators={
                "model_p_up": round(p_up, 3),
                "n_updates": self.model.n_updates,
                "accuracy": round(self.model.accuracy, 3) if self.model.accuracy is not None else None,
                "log_loss": round(self.model.log_loss, 4) if self.model.log_loss is not None else None,
                "beats_random": self.model.beats_random,
                "top_features": top,
            },
        )
