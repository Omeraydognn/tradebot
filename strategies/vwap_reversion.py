"""
Strategy 2: VWAP Mean Reversion

Finans Temeli:
  Fiyatlar kısa vadede Volume-Weighted Average Price (VWAP) etrafında
  hareket eder. Institutional trader'lar VWAP'ı benchmark olarak kullanır.
  Fiyat VWAP'tan aşırı uzaklaştığında, geri dönme (mean reversion)
  olasılığı artar.

Sinyal:
  - Fiyat VWAP'ın %0.15+ üstünde → DOWN (geri dönecek)
  - Fiyat VWAP'ın %0.15+ altında → UP (geri dönecek)
  - Confidence = sapma büyüklüğüyle orantılı
"""
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal
from market_data import MarketDataService, PolymarketSnapshot
from paper_trader import PaperTrader

logger = logging.getLogger(__name__)


class VWAPReversionStrategy(BaseStrategy):
    def __init__(self, data: MarketDataService, paper: PaperTrader):
        super().__init__(name="VWAP Mean Reversion", data=data, paper=paper)
        self.deviation_threshold = 0.0005  # 0.05% deviation triggers signal (daha sık)

    def get_tunables(self) -> dict:
        t = super().get_tunables()
        t["deviation_threshold"] = {
            "value": self.deviation_threshold, "min": 0.0002, "max": 0.003,
            "selectivity": True,
            "desc": "VWAP'tan gereken min sapma (düşük=sık sinyal, yüksek=seçici)",
        }
        return t

    def generate_signal(self, snapshot: PolymarketSnapshot) -> Optional[Signal]:
        vwap = self.data.calc_vwap(20)
        price = self.data.latest_btc_price

        if vwap is None or price == 0:
            return None

        deviation = (price - vwap) / vwap  # positive = above VWAP

        indicators = {
            "btc_price": round(price, 2),
            "vwap": round(vwap, 2),
            "deviation_pct": round(deviation * 100, 4),
        }

        # Price is significantly ABOVE VWAP → expect reversion DOWN
        if deviation > self.deviation_threshold:
            strength = min(deviation / (self.deviation_threshold * 4), 1.0)
            confidence = 0.52 + (strength * 0.20)
            confidence = min(confidence, 0.80)

            return Signal(
                direction="DOWN",
                confidence=confidence,
                reasoning=f"Price {deviation*100:.3f}% above VWAP — mean reversion expected",
                indicators=indicators,
            )

        # Price is significantly BELOW VWAP → expect reversion UP
        elif deviation < -self.deviation_threshold:
            strength = min(abs(deviation) / (self.deviation_threshold * 4), 1.0)
            confidence = 0.52 + (strength * 0.20)
            confidence = min(confidence, 0.80)

            return Signal(
                direction="UP",
                confidence=confidence,
                reasoning=f"Price {abs(deviation)*100:.3f}% below VWAP — mean reversion expected",
                indicators=indicators,
            )

        return None
