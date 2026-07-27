"""
Strategy 1: Momentum (RSI + EMA Crossover)

Finans Temeli:
  Trend-following stratejisi. RSI(14) momentum gücünü ölçer,
  EMA(9)/EMA(21) crossover ise trendin yönünü belirler.
  Güçlü yönlü hareketlerde fiyatın aynı yöne devam etme
  olasılığı istatistiksel olarak daha yüksektir.

Sinyal:
  - RSI > 55 AND EMA9 > EMA21 → UP (bullish momentum)
  - RSI < 45 AND EMA9 < EMA21 → DOWN (bearish momentum)
  - Confidence = RSI'ın 50'den sapması ile orantılı
"""
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal
from market_data import MarketDataService, PolymarketSnapshot
from paper_trader import PaperTrader

logger = logging.getLogger(__name__)


class MomentumStrategy(BaseStrategy):
    def __init__(self, data: MarketDataService, paper: PaperTrader):
        super().__init__(name="Momentum RSI+EMA", data=data, paper=paper)
        self.rsi_band = 2.0  # RSI 50'den bu kadar sapınca sinyal (52/48)

    def get_tunables(self) -> dict:
        t = super().get_tunables()
        t["rsi_band"] = {
            "value": self.rsi_band, "min": 1.0, "max": 10.0,
            "selectivity": True,
            "desc": "RSI 50'den gereken min sapma (düşük=sık sinyal, yüksek=seçici)",
        }
        return t

    def generate_signal(self, snapshot: PolymarketSnapshot) -> Optional[Signal]:
        rsi = self.data.calc_rsi(14)
        ema9 = self.data.calc_ema(9)
        ema21 = self.data.calc_ema(21)
        price = self.data.latest_btc_price

        if rsi is None or ema9 is None or ema21 is None or price == 0:
            return None

        indicators = {
            "rsi": round(rsi, 2),
            "ema9": round(ema9, 2),
            "ema21": round(ema21, 2),
            "btc_price": round(price, 2),
        }

        band = self.rsi_band

        # Bullish momentum (eşik AI/kural tarafından ayarlanabilir: 50+band)
        if rsi > 50 + band and ema9 > ema21:
            # Confidence scales with RSI distance from neutral
            rsi_strength = min((rsi - 50) / 30, 1.0)  # 0-1 scale
            ema_gap = (ema9 - ema21) / ema21 * 100      # percentage gap
            ema_strength = min(abs(ema_gap) / 0.5, 1.0)

            confidence = 0.50 + (rsi_strength * 0.15) + (ema_strength * 0.10)
            confidence = min(confidence, 0.85)

            return Signal(
                direction="UP",
                confidence=confidence,
                reasoning=f"Bullish momentum: RSI={rsi:.0f}, EMA9>EMA21 gap={ema_gap:.3f}%",
                indicators=indicators,
            )

        # Bearish momentum (eşik AI/kural tarafından ayarlanabilir: 50-band)
        elif rsi < 50 - band and ema9 < ema21:
            rsi_strength = min((50 - rsi) / 30, 1.0)
            ema_gap = (ema21 - ema9) / ema21 * 100
            ema_strength = min(abs(ema_gap) / 0.5, 1.0)

            confidence = 0.50 + (rsi_strength * 0.15) + (ema_strength * 0.10)
            confidence = min(confidence, 0.85)

            return Signal(
                direction="DOWN",
                confidence=confidence,
                reasoning=f"Bearish momentum: RSI={rsi:.0f}, EMA9<EMA21 gap={ema_gap:.3f}%",
                indicators=indicators,
            )

        return None
