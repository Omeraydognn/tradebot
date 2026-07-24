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
_btc_price_history = []  # [(timestamp, price)]


def create_app(data_service, paper_trader, agents):
    """Create and configure the Flask app."""
    global _data_service, _paper_trader, _agents

    _data_service = data_service
    _paper_trader = paper_trader
    _agents = agents

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

        return jsonify({
            "btc_price": round(_data_service.latest_btc_price, 2),
            "timestamp": time.time(),
            "polymarket": polymarket_data,
            "strategies": strategies_data,
            "leaderboard": _paper_trader.get_leaderboard(),
            "recent_trades": _paper_trader.get_trade_log(20),
        })

    @app.route("/api/strategy/<name>/toggle")
    def toggle_strategy(name):
        """Toggle a strategy on/off."""
        for agent in _agents:
            if agent.strategy.name == name:
                agent.strategy.is_active = not agent.strategy.is_active
                return jsonify({
                    "name": name,
                    "is_active": agent.strategy.is_active,
                })
        return jsonify({"error": "Strategy not found"}), 404

    return app


def run_dashboard(data_service, paper_trader, agents, port=5050):
    """Run the Flask dashboard in a background thread."""
    app = create_app(data_service, paper_trader, agents)

    def _run():
        # Suppress Flask's default logging a bit
        wlog = logging.getLogger("werkzeug")
        wlog.setLevel(logging.WARNING)
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    logger.info(f"🌐 Dashboard running at http://localhost:{port}")
    return thread
