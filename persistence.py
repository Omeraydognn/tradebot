"""
Kalıcılık (persistence) katmanı.

Sistem yeniden başladığında portföyler, işlem geçmişi ve AI ajanlarının
öğrendiği parametreler kaybolmasın diye periyodik olarak diske yazar ve
açılışta geri yükler.

Basit ve dayanıklı: tek JSON dosyası + atomik yazma (önce .tmp, sonra rename).
Bozuk/eksik dosya durumunda sessizce temiz başlar (asla çökmez).
"""
import json
import os
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

STATE_FILE = os.getenv("STATE_FILE", "state.json")
SAVE_INTERVAL_SEC = float(os.getenv("SAVE_INTERVAL_SEC", "30"))


def _trade_to_row(t) -> dict:
    """Trade dataclass -> JSON satırı (tüm alanlar)."""
    return {
        "strategy_name": t.strategy_name,
        "timestamp": t.timestamp,
        "side": t.side,
        "entry_price": t.entry_price,
        "amount": t.amount,
        "shares": t.shares,
        "fee": t.fee,
        "payout_if_win": t.payout_if_win,
        "result": t.result,
        "pnl": t.pnl,
        "resolved_at": t.resolved_at,
        "market_window": t.market_window,
        "resolve_at": t.resolve_at,
    }


def _row_to_trade(row: dict):
    """JSON satırı -> Trade dataclass."""
    from paper_trader import Trade
    return Trade(
        strategy_name=row.get("strategy_name", ""),
        timestamp=row.get("timestamp", 0.0),
        side=row.get("side", "UP"),
        entry_price=row.get("entry_price", 0.5),
        amount=row.get("amount", 0.0),
        shares=row.get("shares", 0.0),
        fee=row.get("fee", 0.0),
        payout_if_win=row.get("payout_if_win", 0.0),
        result=row.get("result"),
        pnl=row.get("pnl"),
        resolved_at=row.get("resolved_at"),
        market_window=row.get("market_window", 0),
        resolve_at=row.get("resolve_at", 0.0),
    )


def save_state(paper_trader, agents, path: str = STATE_FILE, online_model=None) -> bool:
    """
    Tüm sistem durumunu atomik olarak diske yazar.
    Hata durumunda False döner ama ASLA exception fırlatmaz (trading durmasın).
    """
    try:
        portfolios = {}
        for name, p in paper_trader.portfolios.items():
            portfolios[name] = {
                "balance": p.balance,
                "total_trades": p.total_trades,
                "wins": p.wins,
                "losses": p.losses,
                "total_pnl": p.total_pnl,
                "peak_balance": p.peak_balance,
                "max_drawdown": p.max_drawdown,
                "trades": [_trade_to_row(t) for t in p.trades[-200:]],
                "pending_trades": [_trade_to_row(t) for t in p.pending_trades],
                "balance_history": p.balance_history[-200:],
            }

        agents_state = {}
        for a in agents:
            agents_state[a.strategy.name] = {
                "bet_size": a.strategy.bet_size,
                "min_ev": a.strategy.min_ev,
                "is_active": a.strategy.is_active,
                "total_signals": a.strategy.total_signals,
                "adaptations": a.adaptations[-20:],
                "trades_at_last_adapt": a._trades_at_last_adapt,
                "total_ai_calls": a.total_ai_calls,
                "ai_overrides": a.ai_overrides,
                # Strateji-özel ayarlanabilir parametreler (AI değiştirebilir)
                "tunables": a.strategy.get_tunables() if hasattr(a.strategy, "get_tunables") else {},
                # Güven kalibratörü (öğrenilmiş a, b + ham örnekler)
                "calibrator": a.strategy.calibrator.dump() if hasattr(a.strategy, "calibrator") else {},
            }

        state = {
            "version": 2,
            "saved_at": time.time(),
            "portfolios": portfolios,
            "agents": agents_state,
            "online_model": online_model.dump() if online_model else None,
        }

        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)  # atomik
        return True
    except Exception as e:
        logger.error(f"Durum kaydedilemedi: {e}")
        return False


def load_state(paper_trader, agents, path: str = STATE_FILE, online_model=None) -> bool:
    """
    Diskteki durumu yükler. Dosya yoksa/bozuksa sessizce False döner
    (sistem temiz başlar).
    """
    if not os.path.exists(path):
        logger.info("Kayıtlı durum yok — temiz başlanıyor.")
        return False

    try:
        with open(path) as f:
            state = json.load(f)

        # Portföyler
        for name, pd_ in (state.get("portfolios") or {}).items():
            p = paper_trader.portfolios.get(name)
            if p is None:
                continue
            p.balance = pd_.get("balance", p.balance)
            p.total_trades = pd_.get("total_trades", 0)
            p.wins = pd_.get("wins", 0)
            p.losses = pd_.get("losses", 0)
            p.total_pnl = pd_.get("total_pnl", 0.0)
            p.peak_balance = pd_.get("peak_balance", p.balance)
            p.max_drawdown = pd_.get("max_drawdown", 0.0)
            p.trades = [_row_to_trade(r) for r in (pd_.get("trades") or [])]
            p.pending_trades = [_row_to_trade(r) for r in (pd_.get("pending_trades") or [])]
            p.balance_history = [tuple(x) for x in (pd_.get("balance_history") or [])]
            # Global log'u da doldur ki dashboard boş görünmesin
            paper_trader.global_trade_log.extend(p.trades[-20:])

        # Ajanlar (öğrenilmiş parametreler)
        for a in agents:
            ad = (state.get("agents") or {}).get(a.strategy.name)
            if not ad:
                continue
            a.strategy.bet_size = ad.get("bet_size", a.strategy.bet_size)
            a.strategy.min_ev = ad.get("min_ev", a.strategy.min_ev)
            a.strategy.is_active = ad.get("is_active", True)
            a.strategy.total_signals = ad.get("total_signals", 0)
            a.adaptations = ad.get("adaptations") or []
            a._trades_at_last_adapt = ad.get("trades_at_last_adapt", 0)
            a.total_ai_calls = ad.get("total_ai_calls", 0)
            a.ai_overrides = ad.get("ai_overrides", 0)
            tun = ad.get("tunables") or {}
            if tun and hasattr(a.strategy, "set_tunables"):
                a.strategy.set_tunables(tun)
            cal = ad.get("calibrator") or {}
            if cal and hasattr(a.strategy, "calibrator"):
                a.strategy.calibrator.load(cal)

        # Online model (öğrenilmiş ağırlıklar)
        if online_model is not None and state.get("online_model"):
            online_model.load(state["online_model"])
            logger.info(f"📚 Online model geri yüklendi: {online_model.n_updates} pencere")

        age = time.time() - state.get("saved_at", 0)
        logger.info(f"✅ Kayıtlı durum yüklendi ({age/60:.0f} dk önce kaydedilmiş)")
        return True

    except Exception as e:
        logger.error(f"Durum yüklenemedi ({e}) — temiz başlanıyor.")
        return False
