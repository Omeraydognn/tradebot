"""
Strategy 5: Implied Probability Arbitrage

Finans Temeli:
  Kelly Criterion tabanlı edge hesaplaması. Polymarket'teki hisse fiyatı
  (implied probability) ile birden fazla teknik göstergenin birleşik
  tahminini karşılaştırır. Aralarındaki fark (edge) kâr fırsatı yaratır.

  Bu strateji diğer 4 stratejinin aksine "kendi modeli" ile Polymarket
  arasındaki fiyat farkını istismar etmeye çalışır.

Sinyal:
  - Birleşik model UP olasılığını %60 tahmin ediyor, Polymarket UP $0.45
    → Edge = +15% → BUY UP
  - Birleşik model DOWN olasılığını %65 tahmin ediyor, Polymarket DOWN $0.50
    → Edge = +15% → BUY DOWN
  - Minimum edge threshold: %8
"""
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal
from market_data import MarketDataService, PolymarketSnapshot
from paper_trader import PaperTrader

logger = logging.getLogger(__name__)


class ImpliedArbStrategy(BaseStrategy):
    def __init__(self, data: MarketDataService, paper: PaperTrader):
        super().__init__(name="Implied Prob. Arbitrage", data=data, paper=paper)
        self.min_edge = 0.08  # minimum 8% edge to trade

    def _build_composite_probability(self) -> Optional[float]:
        """
        Build a composite UP probability from multiple indicators.
        Returns estimated P(UP) between 0 and 1.
        """
        scores = []
        weights = []

        # 1. RSI signal
        rsi = self.data.calc_rsi(14)
        if rsi is not None:
            if rsi > 50:
                scores.append(0.5 + (rsi - 50) / 100)  # 50-100 → 0.5-1.0
            else:
                scores.append(rsi / 100)  # 0-50 → 0.0-0.5
            weights.append(0.25)

        # 2. EMA trend
        ema9 = self.data.calc_ema(9)
        ema21 = self.data.calc_ema(21)
        if ema9 is not None and ema21 is not None:
            if ema9 > ema21:
                gap = min((ema9 - ema21) / ema21 * 200, 1.0)
                scores.append(0.5 + gap * 0.3)
            else:
                gap = min((ema21 - ema9) / ema21 * 200, 1.0)
                scores.append(0.5 - gap * 0.3)
            weights.append(0.25)

        # 3. VWAP position
        vwap = self.data.calc_vwap(20)
        price = self.data.latest_btc_price
        if vwap is not None and price > 0:
            dev = (price - vwap) / vwap
            # Above VWAP = slightly bullish short-term (momentum)
            vwap_score = 0.5 + min(max(dev * 50, -0.3), 0.3)
            scores.append(vwap_score)
            weights.append(0.20)

        # 4. Recent price momentum (last 60s)
        recent = self.data.recent_price_changes(60)
        if len(recent) >= 10:
            first = recent[0]
            last = recent[-1]
            change_pct = (last - first) / first if first > 0 else 0
            mom_score = 0.5 + min(max(change_pct * 100, -0.3), 0.3)
            scores.append(mom_score)
            weights.append(0.30)

        if not scores:
            return None

        # Weighted average
        total_weight = sum(weights)
        composite = sum(s * w for s, w in zip(scores, weights)) / total_weight
        return max(0.05, min(0.95, composite))

    def generate_signal(self, snapshot: PolymarketSnapshot) -> Optional[Signal]:
        model_p_up = self._build_composite_probability()
        if model_p_up is None:
            return None

        model_p_down = 1.0 - model_p_up
        market_p_up = snapshot.up_price       # what Polymarket thinks
        market_p_down = snapshot.down_price

        up_edge = model_p_up - market_p_up    # positive = UP is underpriced
        down_edge = model_p_down - market_p_down

        indicators = {
            "model_p_up": round(model_p_up, 3),
            "model_p_down": round(model_p_down, 3),
            "market_p_up": round(market_p_up, 3),
            "market_p_down": round(market_p_down, 3),
            "up_edge": round(up_edge, 3),
            "down_edge": round(down_edge, 3),
        }

        # Find the side with the largest edge
        if up_edge > self.min_edge and up_edge >= down_edge:
            return Signal(
                direction="UP",
                confidence=model_p_up,
                reasoning=f"UP underpriced: model={model_p_up:.0%} vs market={market_p_up:.0%}, edge={up_edge:.0%}",
                indicators=indicators,
            )
        elif down_edge > self.min_edge and down_edge > up_edge:
            return Signal(
                direction="DOWN",
                confidence=model_p_down,
                reasoning=f"DOWN underpriced: model={model_p_down:.0%} vs market={market_p_down:.0%}, edge={down_edge:.0%}",
                indicators=indicators,
            )

        return None
