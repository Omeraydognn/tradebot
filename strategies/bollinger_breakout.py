"""
Strategy 4: Volatility Breakout (Bollinger Bands)

Finans Temeli:
  Bollinger Bands(20, 2σ) volatilite sıkışması ve patlama noktalarını
  yakalar. Band genişliği daralıp (squeeze) aniden genişlediğinde,
  güçlü bir yönlü hareket başladığı anlamına gelir.

  Fiyat üst banda dokunursa momentum yukarı, alt banda dokunursa aşağı.

Sinyal:
  - Close > upper band → UP (breakout yukarı)
  - Close < lower band → DOWN (breakout aşağı)
  - Band width squeeze sonrası breakout daha yüksek confidence
"""
import logging
from typing import Optional

import numpy as np
from strategies.base import BaseStrategy, Signal
from market_data import MarketDataService, PolymarketSnapshot
from paper_trader import PaperTrader

logger = logging.getLogger(__name__)


class BollingerBreakoutStrategy(BaseStrategy):
    def __init__(self, data: MarketDataService, paper: PaperTrader):
        super().__init__(name="Bollinger Breakout", data=data, paper=paper)
        self.period = 20
        self.std_dev = 2.0

    def _band_width_percentile(self) -> Optional[float]:
        """
        Calculate current band width relative to recent history.
        Low percentile = squeeze, high = expansion.
        """
        closes = self.data.get_closes(self.period + 10)
        if len(closes) < self.period + 5:
            return None

        widths = []
        for i in range(5, len(closes)):
            window = closes[i - self.period : i]
            if len(window) < self.period:
                continue
            arr = np.array(window)
            mean = float(np.mean(arr))
            std = float(np.std(arr))
            if mean > 0:
                widths.append((std * 2) / mean)

        if len(widths) < 3:
            return None

        current = widths[-1]
        rank = sum(1 for w in widths if w <= current) / len(widths)
        return rank

    def generate_signal(self, snapshot: PolymarketSnapshot) -> Optional[Signal]:
        bands = self.data.calc_bollinger(self.period, self.std_dev)
        price = self.data.latest_btc_price

        if bands is None or price == 0:
            return None

        middle, upper, lower = bands
        band_width = (upper - lower) / middle if middle > 0 else 0
        bw_percentile = self._band_width_percentile()

        indicators = {
            "btc_price": round(price, 2),
            "bb_upper": round(upper, 2),
            "bb_middle": round(middle, 2),
            "bb_lower": round(lower, 2),
            "band_width_pct": round(band_width * 100, 4),
            "bw_percentile": round(bw_percentile * 100, 1) if bw_percentile else None,
        }

        # Squeeze bonus: if bands were tight and now expanding, higher confidence
        squeeze_bonus = 0.0
        if bw_percentile is not None and bw_percentile < 0.3:
            squeeze_bonus = 0.08  # bands are tight → potential explosive move

        # Breakout above upper band → UP
        if price > upper:
            distance = (price - upper) / (upper - middle) if (upper - middle) > 0 else 0
            strength = min(distance, 1.0)
            confidence = 0.53 + (strength * 0.15) + squeeze_bonus
            confidence = min(confidence, 0.82)

            return Signal(
                direction="UP",
                confidence=confidence,
                reasoning=f"Breakout above upper BB (${upper:.0f}), squeeze={bw_percentile:.0%}" if bw_percentile else f"Breakout above upper BB (${upper:.0f})",
                indicators=indicators,
            )

        # Breakout below lower band → DOWN
        elif price < lower:
            distance = (lower - price) / (middle - lower) if (middle - lower) > 0 else 0
            strength = min(distance, 1.0)
            confidence = 0.53 + (strength * 0.15) + squeeze_bonus
            confidence = min(confidence, 0.82)

            return Signal(
                direction="DOWN",
                confidence=confidence,
                reasoning=f"Breakout below lower BB (${lower:.0f}), squeeze={bw_percentile:.0%}" if bw_percentile else f"Breakout below lower BB (${lower:.0f})",
                indicators=indicators,
            )

        return None
