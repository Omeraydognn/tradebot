"""
Strategy 3: Order Book Imbalance (OBI)

Finans Temeli:
  Microstructure teorisine dayalı. Order book'taki alış (bid) ve satış (ask)
  taraflarındaki hacim dengesizliği, bilgi sahibi trader'ların pozisyon
  aldığını gösterir ve kısa vadeli fiyat hareketini önceden tahmin edebilir.

  Polymarket'teki UP/DOWN token'larının order book derinliği karşılaştırılır.

Sinyal:
  - UP tarafında bid hacmi >> ask hacmi → UP (alıcılar güçlü)
  - DOWN tarafında bid hacmi >> ask hacmi → DOWN (satıcılar güçlü)
  - Confidence = dengesizlik oranına dayalı
"""
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal
from market_data import MarketDataService, PolymarketSnapshot
from paper_trader import PaperTrader

logger = logging.getLogger(__name__)


class OrderBookImbalanceStrategy(BaseStrategy):
    def __init__(self, data: MarketDataService, paper: PaperTrader):
        super().__init__(name="Order Book Imbalance", data=data, paper=paper)
        self.imbalance_threshold = 0.10  # 10% imbalance to trigger (daha sık)

    def _calc_side_pressure(self, bids: list, asks: list) -> float:
        """
        Calculate buy pressure ratio.
        Returns 0-1 where >0.5 means more buying pressure.
        """
        bid_vol = sum(size for _, size in bids[:5])   # top 5 levels
        ask_vol = sum(size for _, size in asks[:5])
        total = bid_vol + ask_vol
        if total == 0:
            return 0.5
        return bid_vol / total

    def generate_signal(self, snapshot: PolymarketSnapshot) -> Optional[Signal]:
        # Need order book data
        if not snapshot.up_book_bids and not snapshot.down_book_bids:
            return None

        up_pressure = self._calc_side_pressure(
            snapshot.up_book_bids, snapshot.up_book_asks
        )
        down_pressure = self._calc_side_pressure(
            snapshot.down_book_bids, snapshot.down_book_asks
        )

        # Net imbalance: positive = market favors UP
        # UP pressure high = people want to BUY up tokens = bullish
        # DOWN pressure high = people want to BUY down tokens = bearish
        net_imbalance = up_pressure - down_pressure

        indicators = {
            "up_bid_pressure": round(up_pressure, 3),
            "down_bid_pressure": round(down_pressure, 3),
            "net_imbalance": round(net_imbalance, 3),
            "up_book_depth": len(snapshot.up_book_bids) + len(snapshot.up_book_asks),
            "down_book_depth": len(snapshot.down_book_bids) + len(snapshot.down_book_asks),
        }

        # Strong buying pressure on UP side
        if net_imbalance > self.imbalance_threshold:
            strength = min(net_imbalance / 0.5, 1.0)
            confidence = 0.52 + (strength * 0.18)
            confidence = min(confidence, 0.78)

            return Signal(
                direction="UP",
                confidence=confidence,
                reasoning=f"Order book favors UP: imbalance={net_imbalance:.2f}",
                indicators=indicators,
            )

        # Strong buying pressure on DOWN side
        elif net_imbalance < -self.imbalance_threshold:
            strength = min(abs(net_imbalance) / 0.5, 1.0)
            confidence = 0.52 + (strength * 0.18)
            confidence = min(confidence, 0.78)

            return Signal(
                direction="DOWN",
                confidence=confidence,
                reasoning=f"Order book favors DOWN: imbalance={net_imbalance:.2f}",
                indicators=indicators,
            )

        return None
