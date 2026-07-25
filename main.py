"""
Polymarket BTC 5m — Multi-Strategy AI Trading System
Main orchestrator.

Starts:
  1. Market data service (Binance WS + Polymarket API)
  2. 5 strategies, each wrapped by an AI agent
  3. Paper trading engine
  4. Web dashboard (Flask)
  5. Strategy evaluation loop

Usage:
  source venv/bin/activate && python main.py
"""
import asyncio
import time
import logging
import sys

from polymarket import AsyncPublicClient, PRODUCTION

from config import POLYMARKET_PRIVATE_KEY, DASHBOARD_PORT
from market_data import MarketDataService
from paper_trader import PaperTrader
from ai_agent import AIAgent
from dashboard.app import run_dashboard

# Import strategies
from strategies.momentum import MomentumStrategy
from strategies.vwap_reversion import VWAPReversionStrategy
from strategies.order_book_imbalance import OrderBookImbalanceStrategy
from strategies.bollinger_breakout import BollingerBreakoutStrategy
from strategies.implied_arb import ImpliedArbStrategy

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)-25s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


async def strategy_loop(agents: list[AIAgent], data: MarketDataService, paper: PaperTrader):
    """
    Main strategy evaluation loop.
    Runs every 10 seconds, evaluates all strategies against
    current market data.
    """
    # Wait for initial data
    logger.info("⏳ Waiting for market data…")
    while data.latest_btc_price == 0 or len(data.klines_1m) < 3:
        await asyncio.sleep(2)
    logger.info(f"✅ Data ready. BTC: ${data.latest_btc_price:,.2f}, Klines: {len(data.klines_1m)}")

    cycle = 0
    while True:
        cycle += 1
        snapshot = data.active_snapshot

        if snapshot is None:
            logger.debug("No Polymarket snapshot yet, waiting…")
            await asyncio.sleep(10)
            continue

        # Evaluate all strategies
        for agent in agents:
            try:
                await agent.evaluate(snapshot)
            except Exception as e:
                logger.error(f"Error in {agent.name}: {e}")

        # Resolution: her trade KENDİ 5dk penceresinin sonunda, o penceredeki
        # gerçek BTC hareketine (açılış vs kapanış) göre çözülür.
        resolved = resolve_due_trades(data, paper)

        # İşlem sonuçlandıysa: ajanlar öğrenip stratejilerini adapte etsin
        if resolved:
            for agent in agents:
                try:
                    agent.maybe_adapt()
                except Exception as e:
                    logger.error(f"Adapt error in {agent.name}: {e}")

        await asyncio.sleep(10)


def resolve_due_trades(data: MarketDataService, paper: PaperTrader) -> int:
    """
    Zamanı gelen (pencere sonu geçmiş) bekleyen trade'leri gerçek sonuçla çözer.
    Çözülen trade sayısını döndürür (adaptasyon tetiklemek için).
    """
    now = time.time()
    resolved = 0
    for portfolio in paper.portfolios.values():
        for trade in list(portfolio.pending_trades):
            if not trade.resolve_at or now < trade.resolve_at:
                continue
            open_price = data.window_open_price.get(trade.market_window)
            close_price = data.latest_btc_price
            if not open_price or not close_price:
                continue
            outcome = "UP" if close_price >= open_price else "DOWN"
            paper.resolve_trade(trade, outcome)
            resolved += 1
            logger.info(
                f"🔔 [{trade.strategy_name}] resolution: {outcome} | "
                f"pencere ${open_price:,.2f} → ${close_price:,.2f}"
            )
    return resolved


async def main():
    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║  ⚡ Polymarket BTC 5m — Multi-Strategy AI Trading System ⚡  ║
    ║                     PAPER TRADING MODE                      ║
    ╚══════════════════════════════════════════════════════════════╝
    """)

    if not POLYMARKET_PRIVATE_KEY:
        logger.warning("No wallet key configured. Running in read-only mode.")

    # 1. Initialize Polymarket client
    client = AsyncPublicClient(environment=PRODUCTION)
    logger.info("📡 Polymarket client initialized")

    # 2. Initialize shared services
    data_service = MarketDataService(client)
    paper_trader = PaperTrader()
    logger.info("📊 Data service and paper trader ready")

    # 3. Create strategies
    strategies = [
        MomentumStrategy(data_service, paper_trader),
        VWAPReversionStrategy(data_service, paper_trader),
        OrderBookImbalanceStrategy(data_service, paper_trader),
        BollingerBreakoutStrategy(data_service, paper_trader),
        ImpliedArbStrategy(data_service, paper_trader),
    ]

    # 4. Wrap each strategy with an AI agent
    agents = [AIAgent(strategy) for strategy in strategies]
    logger.info(f"🤖 {len(agents)} AI agents initialized")

    # 5. Start dashboard
    run_dashboard(data_service, paper_trader, agents, port=DASHBOARD_PORT)

    # 6. Start data collection
    asyncio.create_task(data_service.start())
    logger.info("📡 Data streams starting…")

    # 7. Start strategy loop
    logger.info(f"🌐 Dashboard: http://localhost:{DASHBOARD_PORT}")
    logger.info("🚀 System is live! Press Ctrl+C to stop.")
    await strategy_loop(agents, data_service, paper_trader)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 System shutting down.")
        sys.exit(0)
