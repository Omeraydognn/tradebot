"""
Paper trading engine.
Tracks virtual portfolios, P&L, trade history, and win rates
for each strategy independently.
"""
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
from config import INITIAL_BALANCE, POLYMARKET_FEE_RATE

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    """A single paper trade record."""
    strategy_name: str
    timestamp: float
    side: str              # "UP" or "DOWN"
    entry_price: float     # share price paid (e.g. 0.60)
    amount: float          # USD spent
    shares: float          # shares bought = amount / entry_price
    fee: float             # fee paid
    payout_if_win: float   # shares * 1.0
    result: Optional[str] = None     # "WIN", "LOSE", or None (pending)
    pnl: Optional[float] = None      # profit/loss after resolution
    resolved_at: Optional[float] = None
    market_window: int = 0           # ait olduğu 5dk pencerenin başlangıcı (unix)
    resolve_at: float = 0.0          # bu trade'in çözüleceği zaman (pencere sonu, unix)
    raw_confidence: float = 0.0      # stratejinin HAM güveni (kalibrasyon için)
    calibrated_confidence: float = 0.0  # kalibre edilmiş güven (karşılaştırma)

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy_name,
            "time": self.timestamp,
            "side": self.side,
            "entry_price": round(self.entry_price, 4),
            "amount": round(self.amount, 2),
            "shares": round(self.shares, 4),
            "fee": round(self.fee, 4),
            "result": self.result,
            "pnl": round(self.pnl, 2) if self.pnl is not None else None,
        }


@dataclass
class StrategyPortfolio:
    """Virtual portfolio for one strategy."""
    name: str
    balance: float = INITIAL_BALANCE
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    peak_balance: float = INITIAL_BALANCE
    max_drawdown: float = 0.0
    trades: list = field(default_factory=list)
    pending_trades: list = field(default_factory=list)
    balance_history: list = field(default_factory=list)  # [(timestamp, balance), ...]

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades * 100.0

    @property
    def current_drawdown(self) -> float:
        if self.peak_balance == 0:
            return 0.0
        return (self.peak_balance - self.balance) / self.peak_balance * 100.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "balance": round(self.balance, 2),
            "initial_balance": INITIAL_BALANCE,
            "total_pnl": round(self.total_pnl, 2),
            "total_pnl_pct": round(self.total_pnl / INITIAL_BALANCE * 100, 2),
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 1),
            "max_drawdown": round(self.max_drawdown, 1),
            "last_10_trades": [t.to_dict() for t in self.trades[-10:]],
            "open_trades": [t.to_dict() for t in self.pending_trades],
            "balance_history": self.balance_history[-60:],  # P&L grafiği
        }


class PaperTrader:
    """
    Manages paper trading for all strategies.
    Each strategy has its own isolated portfolio.
    """

    def __init__(self):
        self.portfolios: dict[str, StrategyPortfolio] = {}
        self.global_trade_log: list[Trade] = []

    def register_strategy(self, name: str):
        """Create a portfolio for a strategy."""
        if name not in self.portfolios:
            self.portfolios[name] = StrategyPortfolio(name=name)
            logger.info(f"📝 Registered paper portfolio for '{name}' with ${INITIAL_BALANCE}")

    def place_paper_trade(
        self,
        strategy_name: str,
        side: str,
        share_price: float,
        bet_amount: float,
        market_window: int = 0,
        resolve_at: float = 0.0,
        raw_confidence: float = 0.0,
        calibrated_confidence: float = 0.0,
    ) -> Optional[Trade]:
        """
        Place a paper trade.

        Args:
            strategy_name: Which strategy is trading
            side: "UP" or "DOWN"
            share_price: Current Polymarket share price (0.01-0.99)
            bet_amount: How much USD to bet

        Returns:
            Trade object or None if insufficient balance
        """
        portfolio = self.portfolios.get(strategy_name)
        if portfolio is None:
            logger.error(f"Unknown strategy: {strategy_name}")
            return None

        # Calculate fee
        fee = bet_amount * POLYMARKET_FEE_RATE
        total_cost = bet_amount + fee

        if portfolio.balance < total_cost:
            logger.warning(
                f"[{strategy_name}] Insufficient balance: "
                f"${portfolio.balance:.2f} < ${total_cost:.2f}"
            )
            return None

        # Calculate shares
        shares = bet_amount / share_price
        payout_if_win = shares * 1.0  # each share pays $1 if correct

        trade = Trade(
            strategy_name=strategy_name,
            timestamp=time.time(),
            side=side,
            entry_price=share_price,
            amount=bet_amount,
            shares=shares,
            fee=fee,
            payout_if_win=payout_if_win,
            market_window=market_window,
            resolve_at=resolve_at,
            raw_confidence=raw_confidence,
            calibrated_confidence=calibrated_confidence,
        )

        # Deduct cost from balance
        portfolio.balance -= total_cost
        portfolio.pending_trades.append(trade)
        self.global_trade_log.append(trade)

        logger.info(
            f"📈 [{strategy_name}] Paper {side} @ ${share_price:.2f} | "
            f"Bet: ${bet_amount:.2f} | Shares: {shares:.2f} | "
            f"Potential payout: ${payout_if_win:.2f} | Fee: ${fee:.2f}"
        )
        return trade

    def resolve_trade(self, trade: Trade, actual_outcome: str):
        """
        Resolve a pending trade.

        Args:
            trade: The trade to resolve
            actual_outcome: "UP" or "DOWN" — what actually happened
        """
        portfolio = self.portfolios.get(trade.strategy_name)
        if portfolio is None:
            return

        trade.resolved_at = time.time()

        if trade.side == actual_outcome:
            # WIN — receive payout
            trade.result = "WIN"
            trade.pnl = trade.payout_if_win - trade.amount - trade.fee
            portfolio.balance += trade.payout_if_win
            portfolio.wins += 1
        else:
            # LOSE — already paid, get nothing
            trade.result = "LOSE"
            trade.pnl = -(trade.amount + trade.fee)
            portfolio.losses += 1

        portfolio.total_trades += 1
        portfolio.total_pnl += trade.pnl
        portfolio.trades.append(trade)
        # Bakiye geçmişi (P&L grafiği için) — son 200 nokta tut
        portfolio.balance_history.append((round(trade.resolved_at, 1), round(portfolio.balance, 2)))
        if len(portfolio.balance_history) > 200:
            portfolio.balance_history.pop(0)

        # Update peak/drawdown
        if portfolio.balance > portfolio.peak_balance:
            portfolio.peak_balance = portfolio.balance
        dd = portfolio.current_drawdown
        if dd > portfolio.max_drawdown:
            portfolio.max_drawdown = dd

        # Remove from pending
        if trade in portfolio.pending_trades:
            portfolio.pending_trades.remove(trade)

        emoji = "✅" if trade.result == "WIN" else "❌"
        logger.info(
            f"{emoji} [{trade.strategy_name}] {trade.result} | "
            f"Side: {trade.side} | P&L: ${trade.pnl:+.2f} | "
            f"Balance: ${portfolio.balance:.2f}"
        )

    def resolve_all_pending(self, actual_outcome: str):
        """Resolve all pending trades across all strategies."""
        for portfolio in self.portfolios.values():
            for trade in list(portfolio.pending_trades):
                self.resolve_trade(trade, actual_outcome)

    def get_all_stats(self) -> dict:
        """Get stats for all strategies (used by dashboard)."""
        return {
            name: portfolio.to_dict()
            for name, portfolio in self.portfolios.items()
        }

    def get_leaderboard(self) -> list[dict]:
        """Return strategies sorted by P&L."""
        stats = list(self.portfolios.values())
        stats.sort(key=lambda p: p.total_pnl, reverse=True)
        return [p.to_dict() for p in stats]

    def get_trade_log(self, limit: int = 50) -> list[dict]:
        """Return recent global trade log."""
        return [t.to_dict() for t in self.global_trade_log[-limit:]]
