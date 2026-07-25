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
            }

        strategies_data = []
        for agent in _agents:
            strategies_data.append(agent.get_status())

        # Mikroyapı göstergeleri (CVD, emir defteri, funding, OI, likidasyon…)
        try:
            micro = _data_service.microstructure_snapshot()
        except Exception:
            micro = {}

        return jsonify({
            "btc_price": round(_data_service.latest_btc_price, 2),
            "timestamp": time.time(),
            "polymarket": polymarket_data,
            "microstructure": micro,
            "online_model": _online_model.to_dict() if _online_model else None,
            "strategies": strategies_data,
            "leaderboard": _paper_trader.get_leaderboard(),
            "recent_trades": _paper_trader.get_trade_log(20),
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
