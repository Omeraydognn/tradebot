"""
Base strategy class.
All 5 strategies inherit from this and implement generate_signal().
The base handles:
  - Expected value calculation with dynamic Polymarket pricing
  - Bet sizing
  - Integration with paper trader
"""
import time
import logging
from abc import ABC, abstractmethod
from typing import Optional
from dataclasses import dataclass

from market_data import MarketDataService, PolymarketSnapshot
from paper_trader import PaperTrader
from config import (
    POLYMARKET_FEE_RATE, PRICE_MIN, PRICE_MAX, NO_TRADE_LAST_SECONDS,
)

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """Output of a strategy's analysis."""
    direction: str          # "UP" or "DOWN"
    confidence: float       # 0.0 to 1.0
    reasoning: str          # human-readable explanation
    indicators: dict        # raw indicator values for logging


class BaseStrategy(ABC):
    """
    Abstract base for all strategies.

    Key concept: Every strategy produces a signal with a confidence level.
    The base class then compares that confidence against the Polymarket
    share price (implied probability) to determine if there's positive
    expected value (EV).

    Example:
        - Strategy says UP with 65% confidence
        - Polymarket UP share costs $0.50 (implied 50% probability)
        - EV = (0.65 * $0.50_profit) - (0.35 * $0.50_cost) = +$0.15 ✅
        - But if UP share costs $0.75:
        - EV = (0.65 * $0.25_profit) - (0.35 * $0.75_cost) = -$0.10 ❌
    """

    def __init__(self, name: str, data: MarketDataService, paper: PaperTrader):
        self.name = name
        self.data = data
        self.paper = paper
        self.paper.register_strategy(name)
        self.is_active = True
        self.last_signal: Optional[Signal] = None
        self.last_ev: float = 0.0
        self.total_signals: int = 0
        self.ai_decisions: list[dict] = []  # log of AI-enhanced decisions
        # Adaptif AI bunları outcome'lara göre değiştirir:
        self.bet_size: float = 10.0   # işlem başına $ (2-50 arası ayarlanır)
        self.min_ev: float = 0.02     # işlem için gereken min EV (0.0-0.15 arası)

    @abstractmethod
    def generate_signal(self, snapshot: PolymarketSnapshot) -> Optional[Signal]:
        """
        Analyze market data and return a Signal, or None if no trade.
        Subclasses implement their specific strategy logic here.
        """
        ...

    def calculate_ev(self, signal: Signal, snapshot: PolymarketSnapshot) -> float:
        """
        Calculate expected value including Polymarket's dynamic pricing.

        EV = (confidence × profit_if_win) - ((1-confidence) × cost_if_lose) - fee

        Where:
            profit_if_win = 1.0 - share_price
            cost_if_lose  = share_price
            fee           = share_price × fee_rate
        """
        if signal.direction == "UP":
            share_price = snapshot.up_price
        else:
            share_price = snapshot.down_price

        # Clamp to valid range
        share_price = max(0.01, min(0.99, share_price))

        profit_if_win = 1.0 - share_price       # e.g. buy at $0.40 → win $0.60
        cost_if_lose = share_price               # e.g. buy at $0.40 → lose $0.40
        fee = share_price * POLYMARKET_FEE_RATE

        ev = (signal.confidence * profit_if_win) - ((1 - signal.confidence) * cost_if_lose) - fee
        return ev

    def evaluate_and_trade(self, snapshot: PolymarketSnapshot) -> Optional[dict]:
        """
        Full pipeline: generate signal → calculate EV → place paper trade if +EV.
        Returns a decision dict for logging.
        """
        if not self.is_active:
            return None

        signal = self.generate_signal(snapshot)
        if signal is None:
            return None

        self.last_signal = signal
        self.total_signals += 1

        ev = self.calculate_ev(signal, snapshot)
        self.last_ev = ev

        share_price = snapshot.up_price if signal.direction == "UP" else snapshot.down_price

        decision = {
            "timestamp": time.time(),
            "strategy": self.name,
            "direction": signal.direction,
            "confidence": round(signal.confidence, 3),
            "share_price": round(share_price, 3),
            "implied_prob": round(share_price, 3),
            "ev": round(ev, 4),
            "reasoning": signal.reasoning,
            "indicators": signal.indicators,
            "action": "SKIP",
            "trade": None,
        }

        # --- KORUMA BANTLARI (guardrails) ---
        # 1) Aşırı fiyat: $0.10-$0.90 dışında işlem yok (sonuç neredeyse belli).
        # 2) Pencere sonu: son NO_TRADE_LAST_SECONDS sn içinde yeni işlem yok.
        time_left = (snapshot.window_end - time.time()) if snapshot.window_end else 999.0
        price_ok = PRICE_MIN <= share_price <= PRICE_MAX
        time_ok = time_left >= NO_TRADE_LAST_SECONDS

        if ev > self.min_ev and price_ok and time_ok:
            trade = self.paper.place_paper_trade(
                strategy_name=self.name,
                side=signal.direction,
                share_price=share_price,
                bet_amount=self.bet_size,
                market_window=snapshot.window_start,
                resolve_at=snapshot.window_end,
            )
            if trade:
                decision["action"] = "TRADE"
                decision["trade"] = trade.to_dict()
                logger.info(
                    f"🎯 [{self.name}] {signal.direction} | Conf: {signal.confidence:.0%} | "
                    f"Share: ${share_price:.2f} | EV: ${ev:+.3f} | {signal.reasoning}"
                )
        else:
            if not price_ok:
                reason = f"fiyat uçta (${share_price:.2f})"
            elif not time_ok:
                reason = f"pencere sonu ({time_left:.0f}s kaldı)"
            else:
                reason = f"EV düşük (${ev:+.3f} < {self.min_ev:.2f})"
            decision["skip_reason"] = reason
            logger.debug(f"⏭️  [{self.name}] Skip {signal.direction} | {reason}")

        # Keep decision log (max 100)
        self.ai_decisions.append(decision)
        if len(self.ai_decisions) > 100:
            self.ai_decisions.pop(0)

        return decision

    def get_status(self) -> dict:
        """Return strategy status for dashboard."""
        portfolio = self.paper.portfolios.get(self.name)
        return {
            "name": self.name,
            "is_active": self.is_active,
            "total_signals": self.total_signals,
            "last_signal": {
                "direction": self.last_signal.direction,
                "confidence": round(self.last_signal.confidence, 3),
                "reasoning": self.last_signal.reasoning,
            } if self.last_signal else None,
            "last_ev": round(self.last_ev, 4),
            "bet_size": round(self.bet_size, 2),
            "min_ev": round(self.min_ev, 3),
            "portfolio": portfolio.to_dict() if portfolio else None,
            "recent_decisions": self.ai_decisions[-5:],
        }
