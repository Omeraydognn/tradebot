"""
Strategy 3: Order Book Imbalance (OBI) — Binance spot mikroyapı

Finans Temeli:
  Market microstructure teorisi: emir defterindeki alış/satış derinliği
  dengesizliği kısa vadeli fiyat yönünü öngörür. Kalın bid tarafı = destek
  (yukarı baskı), kalın ask tarafı = direnç (aşağı baskı).

  ÖNEMLİ DÜZELTME: Eskiden Polymarket'in UP/DOWN kitapları kullanılıyordu;
  ancak bu iki token tümleyendir (biri diğerinin aynası) — net dengesizlik
  daima ~0 çıkıyor ve sinyal üretilemiyordu. Artık BINANCE SPOT emir defteri
  (gerçek BTC arz/talebi) kullanılıyor.

Sinyal:
  - Bid derinliği >> ask derinliği  → UP
  - Ask derinliği >> bid derinliği  → DOWN
  - CVD (agresif akış) aynı yöne bakıyorsa güven artar (teyit)
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
        self.imbalance_threshold = 0.15  # |imbalance| bu değeri aşarsa sinyal
        self.depth_levels = 10           # kaç seviye derinlik bakılacak

    def get_tunables(self) -> dict:
        t = super().get_tunables()
        t["imbalance_threshold"] = {
            "value": self.imbalance_threshold, "min": 0.05, "max": 0.60,
            "selectivity": True,
            "desc": "sinyal için gereken emir defteri dengesizliği",
        }
        return t

    def generate_signal(self, snapshot: PolymarketSnapshot) -> Optional[Signal]:
        imbalance = self.data.calc_book_imbalance(self.depth_levels)
        if imbalance is None:
            return None

        cvd_ratio = self.data.calc_cvd_ratio(60)   # agresif akış teyidi
        spread = self.data.calc_spread_bps()

        indicators = {
            "book_imbalance": round(imbalance, 3),
            "cvd_ratio_60s": round(cvd_ratio, 3) if cvd_ratio is not None else None,
            "spread_bps": round(spread, 4) if spread is not None else None,
            "depth_levels": self.depth_levels,
        }

        if abs(imbalance) < self.imbalance_threshold:
            return None

        direction = "UP" if imbalance > 0 else "DOWN"

        # Temel güven: dengesizlik büyüklüğüyle orantılı
        strength = min(abs(imbalance) / 0.6, 1.0)
        confidence = 0.50 + strength * 0.14

        # TEYİT: agresif akış (CVD) aynı yöne bakıyorsa güven artar, tersse azalır
        if cvd_ratio is not None:
            same_way = (imbalance > 0 and cvd_ratio > 0) or (imbalance < 0 and cvd_ratio < 0)
            confidence += 0.06 if same_way else -0.08
            indicators["flow_confirms"] = same_way

        confidence = max(0.35, min(confidence, 0.78))

        return Signal(
            direction=direction,
            confidence=confidence,
            reasoning=(
                f"Binance defteri {'ALIŞ' if imbalance > 0 else 'SATIŞ'} ağırlıklı "
                f"(imb={imbalance:+.2f}"
                + (f", CVD={cvd_ratio:+.2f}" if cvd_ratio is not None else "")
                + ")"
            ),
            indicators=indicators,
        )
