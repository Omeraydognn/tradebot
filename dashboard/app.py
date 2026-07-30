"""
Flask dashboard backend.
Serves the web UI and provides API endpoints for real-time strategy data.
"""
import time
import logging
import threading
from flask import Flask, jsonify, render_template

logger = logging.getLogger(__name__)

# These will be set by main.py before starting
_data_service = None
_paper_trader = None
_agents = []
_online_model = None
_btc_price_history = []  # [(timestamp, price)]


def _persistence_snapshot() -> dict:
    """
    Kalıcılık teşhisi: "deploy'da geçmişim kaybolur mu?"
    `durable=False` ise KAYBOLUR (dosya sistemi ephemeral).
    """
    try:
        from persistence import HEALTH
        return {
            "backend": HEALTH.get("backend"),
            "durable": HEALTH.get("durable"),
            "loaded_on_boot": HEALTH.get("loaded"),
            "loaded_from": HEALTH.get("loaded_from"),
            "restored_trades": HEALTH.get("restored_trades"),
            "save_count": HEALTH.get("save_count"),
            "last_save_ago_sec": (
                round(time.time() - HEALTH["last_save_at"], 1)
                if HEALTH.get("last_save_at") else None
            ),
            "last_save_ok": HEALTH.get("last_save_ok"),
            "last_error": HEALTH.get("last_error"),
        }
    except Exception as e:
        return {"error": str(e)[:120]}


def create_app(data_service, paper_trader, agents, online_model=None):
    """Create and configure the Flask app."""
    global _data_service, _paper_trader, _agents, _online_model

    _data_service = data_service
    _paper_trader = paper_trader
    _agents = agents
    _online_model = online_model

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/status")
    def api_status():
        """Main API endpoint — returns all system state."""
        snapshot = _data_service.active_snapshot

        polymarket_data = None
        if snapshot:
            polymarket_data = {
                "question": snapshot.question,
                "up_price": snapshot.up_price,
                "down_price": snapshot.down_price,
                "up_payout": snapshot.up_payout,
                "down_payout": snapshot.down_payout,
                "market_id": snapshot.market_id,
                "timestamp": snapshot.timestamp,
                # Hangi 5dk penceresine bakıyoruz — ileri pencere mi?
                "window_start": snapshot.window_start,
                "window_end": snapshot.window_end,
                "window_label": snapshot.window_label,
                "window_offset": snapshot.window_offset,
                "is_current_window": snapshot.is_current_window,
                "seconds_left": max(0, round(snapshot.window_end - time.time())),
                "up_book_depth_usd": round(
                    sum(p * q for p, q in (snapshot.up_book_asks or [])), 2),
                "down_book_depth_usd": round(
                    sum(p * q for p, q in (snapshot.down_book_asks or [])), 2),
            }

        strategies_data = []
        for agent in _agents:
            strategies_data.append(agent.get_status())

        # Mikroyapı göstergeleri (CVD, emir defteri, funding, OI, likidasyon…)
        try:
            micro = _data_service.microstructure_snapshot()
        except Exception:
            micro = {}

        try:
            from ai_budget import BUDGET
            ai_budget = BUDGET.to_dict()
        except Exception:
            ai_budget = {}

        # VERİ BÜTÜNLÜĞÜ: hangi fiyat kaynağı canlı, kaynaklar ne kadar
        # ayrışıyor, pencere doğru mu. Sorun varsa gizlenmez.
        cl = _data_service.chainlink
        integrity = {
            "chainlink": cl.to_dict(),
            "binance_price": round(_data_service.latest_btc_price, 2),
            "divergence_bps": (
                round(d, 1)
                if (d := _data_service.calc_chainlink_divergence_bps()) is not None
                else None
            ),
            "divergence_usd": (
                round(_data_service.latest_btc_price - cl.price, 2)
                if cl.is_live and _data_service.latest_btc_price > 0 else None
            ),
            "resolution_source": "Polymarket resmi çözümü (Chainlink BTC/USD)",
            "decision_price_source": "Chainlink" if cl.is_live else "YALNIZCA Binance (Chainlink yok!)",
            "book_live": bool(snapshot and snapshot.up_book_asks),
            "window_ok": bool(snapshot and snapshot.window_offset == 0
                              and snapshot.is_current_window),
        }

        # KALICILIK SAĞLIĞI: deploy'dan sağ çıkacak mıyız?
        try:
            from persistence import HEALTH as _PH
            persistence_health = dict(_PH)
            if persistence_health.get("last_save_at"):
                persistence_health["last_save_ago_sec"] = round(
                    time.time() - persistence_health["last_save_at"], 1)
        except Exception:
            persistence_health = {}

        return jsonify({
            "ai_budget": ai_budget,
            "btc_price": round(_data_service.latest_btc_price, 2),
            "chainlink_price": round(cl.price, 2) if cl.is_live else None,
            "timestamp": time.time(),
            "polymarket": polymarket_data,
            "microstructure": micro,
            "integrity": integrity,
            "persistence": persistence_health,
            "online_model": _online_model.to_dict() if _online_model else None,
            "strategies": strategies_data,
            "leaderboard": _paper_trader.get_leaderboard(),
            "recent_trades": _paper_trader.get_trade_log(20),
        })

    @app.route("/api/trades/<path:name>")
    def api_strategy_trades(name):
        """
        Tek bir botun TÜM işlemleri — tam detayla.
        Arayüzdeki bot seçicisi bunu kullanır.
        """
        portfolio = _paper_trader.portfolios.get(name)
        if portfolio is None:
            return jsonify({"error": "Strateji bulunamadı", "name": name}), 404

        rows = portfolio.full_history()
        wins = [t for t in rows if t["result"] == "WIN"]
        losses = [t for t in rows if t["result"] == "LOSE"]
        closed = wins + losses

        return jsonify({
            "name": name,
            "trades": rows,
            "summary": {
                "total": len(rows),
                "open": sum(1 for t in rows if t["result"] is None),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
                "balance": round(portfolio.balance, 2),
                "total_pnl": round(portfolio.total_pnl, 2),
                "gross_win": round(sum(t["pnl"] for t in wins), 2) if wins else 0.0,
                "gross_loss": round(sum(t["pnl"] for t in losses), 2) if losses else 0.0,
                "avg_entry_price": (
                    round(sum(t["entry_price"] for t in closed) / len(closed), 4)
                    if closed else None
                ),
                "avg_slippage_bps": (
                    round(sum(t["slippage_bps"] for t in closed) / len(closed), 1)
                    if closed else None
                ),
                # Piyasanın kote ettiği ortalama oran (zımni olasılık)
                "avg_mid_price": (
                    round(sum(t["mid_price"] for t in closed) / len(closed), 4)
                    if closed and any(t["mid_price"] for t in closed) else None
                ),
                # Midpoint'e göre toplam işlem maliyeti (spread + defter kayması).
                # Polymarket'te komisyon yoktur; kârı yiyen gerçek maliyet budur.
                "avg_total_cost_bps": (
                    round(sum(
                        (t["entry_price"] - t["mid_price"]) / t["mid_price"] * 10000
                        for t in closed if t["mid_price"] > 0
                    ) / n, 1)
                    if closed and (n := sum(1 for t in closed if t["mid_price"] > 0)) else None
                ),
                "future_window_trades": sum(1 for t in rows if t["is_future_window"]),
                "liquidity_capped_trades": sum(1 for t in rows if t["liquidity_capped"]),
            },
        })

    @app.route("/api/bots")
    def api_bots():
        """Bot seçicisi için isim listesi."""
        return jsonify({
            "bots": [
                {
                    "name": a.strategy.name,
                    "is_active": a.strategy.is_active,
                    "total_trades": (
                        p.total_trades
                        if (p := _paper_trader.portfolios.get(a.strategy.name)) else 0
                    ),
                }
                for a in _agents
            ]
        })

    @app.route("/api/strategy/<path:name>/toggle", methods=["GET", "POST"])
    def toggle_strategy(name):
        """Toggle a strategy on/off."""
        for agent in _agents:
            if agent.strategy.name == name:
                agent.strategy.is_active = not agent.strategy.is_active
                logger.info(
                    f"{'▶️ ' if agent.strategy.is_active else '⏸️ '} "
                    f"'{name}' -> {'AKTİF' if agent.strategy.is_active else 'DURAKLATILDI'}"
                )
                return jsonify({"name": name, "is_active": agent.strategy.is_active})
        return jsonify({"error": "Strategy not found"}), 404

    @app.route("/healthz")
    def healthz():
        """Keep-alive / sağlık kontrolü (uptime ping'leri için)."""
        import time as _t
        now = int(_t.time())
        cw = now - (now % 300)
        return jsonify({
            "ok": True,
            "btc_price": round(_data_service.latest_btc_price, 2),
            "has_market": _data_service.active_snapshot is not None,
            "strategies": len(_agents),
            # Öğrenme teşhisi
            "current_window": cw,
            "tracked_windows": sorted(_data_service.window_micro.keys()),
            "window_open_prices": len(_data_service.window_open_price),
            "klines": len(_data_service.klines_1m),
            "kline_at_current": _data_service.kline_open_at(cw),
            "model_updates": _online_model.n_updates if _online_model else None,
            # KALICILIK TEŞHİSİ — "deploy'da geçmişim kaybolur mu?" sorusunun
            # tek bakışta cevabı. durable=false ise KAYBOLUR.
            "persistence": _persistence_snapshot(),
        })

    return app


def run_dashboard(data_service, paper_trader, agents, port=5050, online_model=None):
    """Run the Flask dashboard in a background thread."""
    app = create_app(data_service, paper_trader, agents, online_model)

    def _run():
        # Suppress Flask's default logging a bit
        wlog = logging.getLogger("werkzeug")
        wlog.setLevel(logging.WARNING)
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    logger.info(f"🌐 Dashboard running at http://localhost:{port}")
    return thread
