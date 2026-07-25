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
from persistence import load_state, save_state

# Import strategies
from strategies.momentum import MomentumStrategy
from strategies.vwap_reversion import VWAPReversionStrategy
from strategies.order_book_imbalance import OrderBookImbalanceStrategy
from strategies.bollinger_breakout import BollingerBreakoutStrategy
from strategies.implied_arb import ImpliedArbStrategy
from strategies.microstructure import MicrostructureStrategy
from strategies.learned import LearnedStrategy
from online_model import OnlineLogisticModel, bootstrap_from_history

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)-25s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


async def strategy_loop(agents: list[AIAgent], data: MarketDataService, paper: PaperTrader,
                        online_model):
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

        # ÖĞRENME her zaman çalışır — Polymarket erişilemese bile model
        # BTC pencerelerinden öğrenmeye devam eder (veri kaybı olmasın).
        try:
            train_online_model(data, online_model)
        except Exception as e:
            logger.debug(f"Model eğitim hatası: {e}")

        if snapshot is None:
            if cycle % 12 == 0:  # ~2 dakikada bir bilgilendir
                logger.warning(
                    "⚠️  Polymarket piyasası yok (erişim/veri sorunu) — "
                    "işlem duraklatıldı, model öğrenmeye devam ediyor."
                )
            if cycle % 3 == 0:
                save_state(paper, agents, online_model=online_model)
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
        resolved = resolve_due_trades(data, paper, agents, online_model)

        # İşlem sonuçlandıysa: ajanlar öğrenip stratejilerini adapte etsin
        if resolved:
            for agent in agents:
                try:
                    agent.maybe_adapt()
                except Exception as e:
                    logger.error(f"Adapt error in {agent.name}: {e}")

        # Her ~10 dakikada bir DERİN AI ayarı (Gemini strateji eşiklerini optimize eder)
        if cycle % 60 == 0:
            for agent in agents:
                try:
                    await agent.ai_tune()
                except Exception as e:
                    logger.debug(f"AI tune error in {agent.name}: {e}")

        # Durumu periyodik kaydet (restart'ta kaybolmasın)
        if cycle % 3 == 0:
            save_state(paper, agents, online_model=online_model)

        await asyncio.sleep(10)


def resolve_due_trades(data: MarketDataService, paper: PaperTrader,
                       agents: list, online_model) -> int:
    """
    Zamanı gelen trade'leri gerçek sonuçla çözer; ayrıca:
      - Her stratejinin KALİBRATÖRÜNÜ besler (ham güven vs gerçek sonuç)
      - ONLINE MODELİ eğitir (pencere başı mikroyapı -> gerçek yön)

    Çözülen trade sayısını döndürür.
    """
    now = time.time()
    resolved = 0
    agent_by_strategy = {a.strategy.name: a for a in agents}

    for portfolio in paper.portfolios.values():
        for trade in list(portfolio.pending_trades):
            if not trade.resolve_at or now < trade.resolve_at:
                continue

            outcome = None
            source = ""

            # 1) ÖNCELİK: Piyasanın kendi kararı. Pencere kapanırken UP fiyatı
            #    1'e yakınsa UP, 0'a yakınsa DOWN kazanmıştır.
            last_up = data.window_last_up_price.get(trade.market_window)
            if last_up is not None and (last_up >= 0.85 or last_up <= 0.15):
                outcome = "UP" if last_up >= 0.85 else "DOWN"
                source = f"piyasa (UP={last_up:.2f})"

            # 2) YEDEK: Pencere açılış/kapanış BTC fiyatı kıyası
            if outcome is None:
                open_price = data.window_open_price.get(trade.market_window)
                close_price = data.kline_open_at(trade.market_window + 300) or data.latest_btc_price
                if not open_price or not close_price:
                    continue  # veri yok, bir sonraki turda tekrar dene
                outcome = "UP" if close_price >= open_price else "DOWN"
                source = f"fiyat ${open_price:,.0f}→${close_price:,.0f}"

            paper.resolve_trade(trade, outcome)
            resolved += 1

            # KALİBRASYON: stratejinin HAM güveni gerçekle eşleşti mi?
            agent = agent_by_strategy.get(trade.strategy_name)
            if agent is not None and trade.raw_confidence > 0:
                agent.strategy.calibrator.record(
                    raw_conf=trade.raw_confidence,
                    won=(trade.result == "WIN"),
                )

            logger.info(f"🔔 [{trade.strategy_name}] sonuç: {outcome} | {source}")

    return resolved


def train_online_model(data: MarketDataService, online_model) -> int:
    """
    ONLINE MODEL EĞİTİMİ — Polymarket'ten BAĞIMSIZ çalışır.

    Kapanmış her 5dk penceresi için:
        girdi  = pencere BAŞINDA dondurulmuş mikroyapı (ileriye bakış yok)
        hedef  = BTC gerçekte yukarı mı kapandı
    Böylece Polymarket erişilemezken bile model öğrenmeye devam eder.
    """
    now = int(time.time())
    current_window = now - (now % 300)
    learned = 0

    for w in sorted(list(data.window_micro.keys())):
        if w >= current_window:
            continue  # pencere henüz kapanmadı
        micro = data.window_micro.get(w)
        went_up = _window_went_up(data, w)
        if not micro or went_up is None:
            data.window_micro.pop(w, None)   # etiketlenemiyor, at
            continue

        online_model.update(micro, went_up)
        learned += 1
        data.window_micro.pop(w, None)       # tekrar öğrenmeyi önle

        if online_model.n_updates % 10 == 0:
            acc = online_model.accuracy
            ll = online_model.log_loss
            logger.info(
                f"📚 Online model: {online_model.n_updates} pencere öğrenildi | "
                f"isabet={acc:.0%} | log-loss={ll:.4f} "
                f"({'rastgeleden İYİ' if online_model.beats_random else 'henüz rastgele seviyesinde'})"
            )
    return learned


def _window_went_up(data: MarketDataService, window_start: int):
    """Bir pencere yukarı mı kapandı? (model etiketi) — bilinemiyorsa None."""
    last_up = data.window_last_up_price.get(window_start)
    if last_up is not None and (last_up >= 0.85 or last_up <= 0.15):
        return last_up >= 0.85
    open_p = data.window_open_price.get(window_start)
    close_p = data.kline_open_at(window_start + 300)
    if open_p and close_p:
        return close_p >= open_p
    return None


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
    online_model = OnlineLogisticModel()   # veriden öğrenen model (7. strateji kullanır)
    logger.info("📊 Data service, paper trader ve online model hazır")

    # 3. Create strategies
    strategies = [
        MomentumStrategy(data_service, paper_trader),
        VWAPReversionStrategy(data_service, paper_trader),
        OrderBookImbalanceStrategy(data_service, paper_trader),
        BollingerBreakoutStrategy(data_service, paper_trader),
        ImpliedArbStrategy(data_service, paper_trader),
        MicrostructureStrategy(data_service, paper_trader),
        LearnedStrategy(data_service, paper_trader, online_model),
    ]

    # 4. Wrap each strategy with an AI agent
    agents = [AIAgent(strategy) for strategy in strategies]
    logger.info(f"🤖 {len(agents)} AI agents initialized")

    # 4b. Önceki oturumun durumunu yükle (bakiye, işlemler, AI öğrenmesi)
    load_state(paper_trader, agents, online_model=online_model)

    # 4c. Model boşsa geçmiş Binance verisiyle önceden eğit (saatlerce
    #     beklemeden anlamlı bir başlangıç noktası verir)
    if online_model.n_updates == 0:
        await asyncio.to_thread(bootstrap_from_history, online_model, 500)

    # 5. Start dashboard
    run_dashboard(data_service, paper_trader, agents, port=DASHBOARD_PORT, online_model=online_model)

    # 6. Start data collection
    asyncio.create_task(data_service.start())
    logger.info("📡 Data streams starting…")

    # 7. Start strategy loop
    logger.info(f"🌐 Dashboard: http://localhost:{DASHBOARD_PORT}")
    logger.info("🚀 System is live! Press Ctrl+C to stop.")
    await strategy_loop(agents, data_service, paper_trader, online_model)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Kapatılıyor — durum kaydediliyor…")
        try:
            from dashboard.app import _paper_trader, _agents
            if _paper_trader and _agents:
                save_state(_paper_trader, _agents)
                logger.info("💾 Durum kaydedildi.")
        except Exception as e:
            logger.error(f"Kapanışta kayıt hatası: {e}")
        sys.exit(0)
