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
    """
    Tek bir kâğıt işlemin TAM kaydı.

    Buradaki her alan, işlemin sonradan denetlenebilmesi içindir: hangi
    saatte, hangi 5dk penceresine, hangi fiyattan, defterin kaç seviyesini
    yiyerek, hangi gerekçeyle açıldı ve gerçekte ne oldu. Gerçek parayla
    çalışmaya geçildiğinde tek değişecek şey paranın gerçek olması;
    kayıtların doğruluğu şimdiden gerçek olmalı.
    """
    strategy_name: str
    timestamp: float
    side: str              # "UP" or "DOWN"
    entry_price: float     # gerçekte ödenen AĞIRLIKLI ORTALAMA hisse fiyatı
    amount: float          # USD spent
    shares: float          # defter yürütülerek alınan gerçek hisse adedi
    fee: float             # fee paid
    payout_if_win: float   # shares * 1.0
    result: Optional[str] = None     # "WIN", "LOSE", or None (pending)
    pnl: Optional[float] = None      # profit/loss after resolution
    resolved_at: Optional[float] = None
    market_window: int = 0           # ait olduğu 5dk pencerenin başlangıcı (unix)
    resolve_at: float = 0.0          # bu trade'in çözüleceği zaman (pencere sonu, unix)
    raw_confidence: float = 0.0      # stratejinin HAM güveni (kalibrasyon için)
    calibrated_confidence: float = 0.0  # kalibre edilmiş güven (karşılaştırma)

    # --- DENETİM ALANLARI (gerçeklik kontrolü için) ---
    window_offset: int = 0            # 0 = şu anki pencere, >0 = İLERİ pencere
    top_ask: float = 0.0              # defterin en iyi ask'i (kayma ölçümü)
    mid_price: float = 0.0            # midpoint (implied probability)
    slippage_bps: float = 0.0         # ortalama fiyatın en iyi ask'ten sapması
    levels_used: int = 0              # kaç defter seviyesi tüketildi
    requested_amount: float = 0.0     # istenen bahis (likidite kısarsa amount<bu)
    liquidity_capped: bool = False    # defter yetmediği için bahis küçüldü mü
    ev: float = 0.0                   # işlem anındaki beklenen değer
    reasoning: str = ""               # stratejinin gerekçesi
    ai_initiated: bool = False        # AI kendi inisiyatifiyle mi açtı
    btc_price_at_entry: float = 0.0   # giriş anındaki Binance BTC fiyatı
    chainlink_at_entry: float = 0.0   # giriş anındaki Chainlink BTC (çözüm kaynağı)
    cl_divergence_bps: float = 0.0    # iki kaynak arasındaki fark (bps)
    outcome: Optional[str] = None     # pencerenin resmi sonucu ("UP"/"DOWN")

    # --- ERKEN ÇIKIŞ ALANLARI ---
    # Pozisyon pencere sonunu beklemeden satıldıysa burası dolar.
    exit_type: str = "RESOLUTION"     # "RESOLUTION" | "EARLY"
    exit_price: float = 0.0           # satışta gerçekleşen ağırlıklı ort. bid
    exit_top_bid: float = 0.0         # satış anındaki en iyi bid
    exit_proceeds: float = 0.0        # satıştan eline geçen USD
    exit_reason: str = ""             # "stop-loss" / "kâr kilitleme" vb.
    exit_at: Optional[float] = None   # satış zamanı
    exit_slippage_bps: float = 0.0    # bid defterini yürütme maliyeti
    exit_levels_used: int = 0
    exit_blocked_reason: str = ""     # çıkmak istedik ama olmadıysa nedeni

    @property
    def is_early_exit(self) -> bool:
        return self.exit_type == "EARLY"

    @property
    def is_future_window(self) -> bool:
        """İşlem, açıldığı anda henüz başlamamış bir pencereye mi yapıldı?"""
        return self.window_offset > 0

    @property
    def window_label(self) -> str:
        """Pencerenin insan okunur saat aralığı: '21:45–21:50'."""
        if not self.market_window:
            return "?"
        s = time.strftime("%H:%M", time.localtime(self.market_window))
        e = time.strftime("%H:%M", time.localtime(self.market_window + 300))
        return f"{s}–{e}"

    def to_dict(self) -> dict:
        """Arayüzün gösterdiği TAM kayıt — hiçbir alan gizlenmez."""
        return {
            "strategy": self.strategy_name,
            "time": self.timestamp,
            "time_str": time.strftime("%H:%M:%S", time.localtime(self.timestamp)),
            "date_str": time.strftime("%d.%m.%Y", time.localtime(self.timestamp)),
            "side": self.side,
            "entry_price": round(self.entry_price, 4),
            "top_ask": round(self.top_ask, 4),
            "mid_price": round(self.mid_price, 4),
            "slippage_bps": round(self.slippage_bps, 1),
            "levels_used": self.levels_used,
            "amount": round(self.amount, 2),
            "requested_amount": round(self.requested_amount, 2),
            "liquidity_capped": self.liquidity_capped,
            "shares": round(self.shares, 4),
            "fee": round(self.fee, 4),
            "payout_if_win": round(self.payout_if_win, 2),
            "result": self.result,
            "pnl": round(self.pnl, 2) if self.pnl is not None else None,
            "outcome": self.outcome,
            # Pencere bilgisi — hangi 5dk'ya oynandı
            "market_window": self.market_window,
            "window_label": self.window_label,
            "window_offset": self.window_offset,
            "is_future_window": self.is_future_window,
            "resolve_at": self.resolve_at,
            "resolve_at_str": (time.strftime("%H:%M:%S", time.localtime(self.resolve_at))
                               if self.resolve_at else None),
            "resolved_at": self.resolved_at,
            "resolved_at_str": (time.strftime("%H:%M:%S", time.localtime(self.resolved_at))
                                if self.resolved_at else None),
            # Karar bilgisi
            "raw_confidence": round(self.raw_confidence, 3),
            "calibrated_confidence": round(self.calibrated_confidence, 3),
            "ev": round(self.ev, 4),
            "reasoning": self.reasoning,
            "ai_initiated": self.ai_initiated,
            # Fiyat kaynakları (gerçeklik denetimi)
            "btc_price_at_entry": round(self.btc_price_at_entry, 2),
            "chainlink_at_entry": round(self.chainlink_at_entry, 2),
            "cl_divergence_bps": round(self.cl_divergence_bps, 1),
            # Erken çıkış
            "exit_type": self.exit_type,
            "is_early_exit": self.is_early_exit,
            "exit_price": round(self.exit_price, 4),
            "exit_top_bid": round(self.exit_top_bid, 4),
            "exit_proceeds": round(self.exit_proceeds, 2),
            "exit_reason": self.exit_reason,
            "exit_at": self.exit_at,
            "exit_at_str": (time.strftime("%H:%M:%S", time.localtime(self.exit_at))
                            if self.exit_at else None),
            "exit_slippage_bps": round(self.exit_slippage_bps, 1),
            "exit_levels_used": self.exit_levels_used,
            "exit_blocked_reason": self.exit_blocked_reason,
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
            "last_10_trades": [t.to_dict() for t in self.trades[-25:]],
            "open_trades": [t.to_dict() for t in self.pending_trades],
            "balance_history": self.balance_history[-60:],  # P&L grafiği
            # Gerçeklik göstergeleri — arayüzde uyarı olarak gösterilir
            "future_window_trades": sum(1 for t in self.trades if t.is_future_window),
            "liquidity_capped_trades": sum(1 for t in self.trades if t.liquidity_capped),
            "avg_slippage_bps": (
                round(sum(t.slippage_bps for t in self.trades) / len(self.trades), 1)
                if self.trades else 0.0
            ),
        }

    def full_history(self) -> list[dict]:
        """Bu stratejinin TÜM işlemleri (açık + kapalı), en yeni önce."""
        rows = [t.to_dict() for t in self.pending_trades]
        rows += [t.to_dict() for t in reversed(self.trades)]
        return rows


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
        bet_amount: float,
        fill: dict,
        market_window: int = 0,
        resolve_at: float = 0.0,
        raw_confidence: float = 0.0,
        calibrated_confidence: float = 0.0,
        window_offset: int = 0,
        mid_price: float = 0.0,
        ev: float = 0.0,
        reasoning: str = "",
        ai_initiated: bool = False,
        btc_price_at_entry: float = 0.0,
        chainlink_at_entry: float = 0.0,
        cl_divergence_bps: float = 0.0,
    ) -> Optional[Trade]:
        """
        Kâğıt işlem açar — GERÇEK DEFTER DOLUMU ile.

        `fill`, snapshot.simulate_fill() çıktısıdır: emir defteri seviye
        seviye yürütülerek hesaplanmış gerçek hisse adedi ve ağırlıklı
        ortalama fiyat. Artık "en iyi ask'ten sınırsız hisse" yok.

        Likidite istenen bahsi karşılamıyorsa işlem İPTAL EDİLMEZ; gerçekte
        ne kadar dolarsa o kadarıyla açılır ve `liquidity_capped=True`
        işaretlenir — böylece arayüzde defterin dar olduğu görülür.

        Returns:
            Trade veya None (bakiye yetersiz / dolum imkânsız).
        """
        portfolio = self.portfolios.get(strategy_name)
        if portfolio is None:
            logger.error(f"Unknown strategy: {strategy_name}")
            return None

        # Defter yoksa veya hiç dolum olmadıysa işlem açma.
        # (Uydurma bir fiyattan işlem açmaktansa hiç açmamak doğrudur.)
        shares = float(fill.get("shares") or 0.0)
        avg_price = fill.get("avg_price")
        filled_usd = float(fill.get("filled_usd") or 0.0)
        if shares <= 0 or not avg_price or filled_usd <= 0:
            logger.debug(f"[{strategy_name}] Dolum yok (defter boş/yetersiz) — işlem atlandı.")
            return None

        # GERÇEKTE dolan tutar bahis olur (istenen değil)
        actual_amount = filled_usd
        fee = actual_amount * POLYMARKET_FEE_RATE
        total_cost = actual_amount + fee

        if portfolio.balance < total_cost:
            logger.warning(
                f"[{strategy_name}] Insufficient balance: "
                f"${portfolio.balance:.2f} < ${total_cost:.2f}"
            )
            return None

        payout_if_win = shares * 1.0  # her hisse kazanırsa $1 öder

        trade = Trade(
            strategy_name=strategy_name,
            timestamp=time.time(),
            side=side,
            entry_price=float(avg_price),
            amount=actual_amount,
            shares=shares,
            fee=fee,
            payout_if_win=payout_if_win,
            market_window=market_window,
            resolve_at=resolve_at,
            raw_confidence=raw_confidence,
            calibrated_confidence=calibrated_confidence,
            window_offset=window_offset,
            top_ask=float(fill.get("top_price") or 0.0),
            mid_price=mid_price,
            slippage_bps=float(fill.get("slippage_bps") or 0.0),
            levels_used=int(fill.get("levels_used") or 0),
            requested_amount=float(bet_amount),
            liquidity_capped=bool(fill.get("insufficient")),
            ev=ev,
            reasoning=reasoning,
            ai_initiated=ai_initiated,
            btc_price_at_entry=btc_price_at_entry,
            chainlink_at_entry=chainlink_at_entry,
            cl_divergence_bps=cl_divergence_bps,
        )

        # Deduct cost from balance
        portfolio.balance -= total_cost
        portfolio.pending_trades.append(trade)
        self.global_trade_log.append(trade)

        cap_note = ""
        if trade.liquidity_capped:
            cap_note = f" | ⚠️ likidite sınırladı (istenen ${bet_amount:.2f})"
        win_note = ""
        if trade.window_offset > 0:
            win_note = f" | ⏭️ İLERİ PENCERE (+{trade.window_offset})"

        logger.info(
            f"📈 [{strategy_name}] {side} {trade.window_label} @ ${avg_price:.4f} "
            f"(top ${trade.top_ask:.4f}, kayma {trade.slippage_bps:.0f}bps, "
            f"{trade.levels_used} seviye) | Bahis: ${actual_amount:.2f} | "
            f"Hisse: {shares:.1f} | Kazanırsa: ${payout_if_win:.2f}"
            f"{cap_note}{win_note}"
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
        trade.outcome = actual_outcome

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

    def close_trade_early(self, trade: Trade, sell: dict, reason: str) -> bool:
        """
        POZİSYONU ERKEN KAPAT — pencere sonunu beklemeden bid'e satarak.

        `sell`, snapshot.simulate_sell() çıktısıdır: bid defteri yürütülerek
        hesaplanmış gerçek satış hasılatı.

        NEDEN ÖNEMLİ:
          Kaybeden bir pozisyonda pencere sonunu beklemek = %100 kayıp.
          Erken satışta hisse başına ne kadar bid varsa o kurtarılır.
          Örn: $0.55'ten alınan hisse şu an $0.18'den alıcı buluyorsa,
          beklersek $0 olur; şimdi satarsak maliyetin ~%33'ünü kurtarırız.

        DEFTER YETMEZSE: tüm pozisyon satılamıyorsa çıkış YAPILMAZ ve neden
        kaydedilir. Bu gerçek bir risktir — dar piyasada kaybeden pozisyondan
        çıkamamak sık görülür ve sistem bunu gizlememelidir.

        Dönüş: çıkış gerçekleştiyse True.
        """
        portfolio = self.portfolios.get(trade.strategy_name)
        if portfolio is None or trade not in portfolio.pending_trades:
            return False

        sold = float(sell.get("shares_sold") or 0.0)
        avg = sell.get("avg_price")
        proceeds = float(sell.get("proceeds_usd") or 0.0)

        if sold <= 0 or not avg:
            trade.exit_blocked_reason = "bid defteri boş — alıcı yok"
            return False

        # Kısmi çıkış modellemiyoruz: ya tamamı satılır ya da çıkılmaz.
        # (Kısmi pozisyon taşımak gerçekte mümkün ama burada modellenmesi
        #  yanlış P&L'e yol açardı; çıkamamak dürüst bir sonuçtur.)
        if sell.get("insufficient"):
            trade.exit_blocked_reason = (
                f"bid derinliği yetersiz ({sold:.0f}/{trade.shares:.0f} hisse) "
                f"— pozisyondan çıkılamıyor"
            )
            return False

        exit_fee = proceeds * POLYMARKET_FEE_RATE

        trade.exit_type = "EARLY"
        trade.exit_price = float(avg)
        trade.exit_top_bid = float(sell.get("top_bid") or 0.0)
        trade.exit_proceeds = proceeds
        trade.exit_reason = reason
        trade.exit_at = time.time()
        trade.exit_slippage_bps = float(sell.get("slippage_bps") or 0.0)
        trade.exit_levels_used = int(sell.get("levels_used") or 0)
        trade.exit_blocked_reason = ""
        trade.resolved_at = trade.exit_at
        trade.fee += exit_fee

        # P&L: eline geçen - ödediğin (giriş + çıkış ücretleri dahil)
        trade.pnl = proceeds - trade.amount - trade.fee
        # Sonuç finansal gerçeğe göre etiketlenir: kârla çıktıysak WIN.
        trade.result = "WIN" if trade.pnl > 0 else "LOSE"

        portfolio.balance += proceeds
        if trade.pnl > 0:
            portfolio.wins += 1
        else:
            portfolio.losses += 1
        portfolio.total_trades += 1
        portfolio.total_pnl += trade.pnl
        portfolio.trades.append(trade)
        portfolio.balance_history.append(
            (round(trade.exit_at, 1), round(portfolio.balance, 2))
        )
        if len(portfolio.balance_history) > 200:
            portfolio.balance_history.pop(0)

        if portfolio.balance > portfolio.peak_balance:
            portfolio.peak_balance = portfolio.balance
        dd = portfolio.current_drawdown
        if dd > portfolio.max_drawdown:
            portfolio.max_drawdown = dd

        portfolio.pending_trades.remove(trade)

        salvage_pct = proceeds / trade.amount * 100 if trade.amount else 0.0
        logger.info(
            f"🚪 [{trade.strategy_name}] ERKEN ÇIKIŞ {trade.side} {trade.window_label} | "
            f"giriş ${trade.entry_price:.4f} → çıkış ${trade.exit_price:.4f} "
            f"(en iyi bid ${trade.exit_top_bid:.4f}, {trade.exit_levels_used} seviye) | "
            f"${trade.amount:.2f} → ${proceeds:.2f} (%{salvage_pct:.0f} kurtarıldı) | "
            f"P&L ${trade.pnl:+.2f} | {reason}"
        )
        return True

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
