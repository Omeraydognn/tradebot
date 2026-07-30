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
import os
import certifi
os.environ["SSL_CERT_FILE"] = certifi.where()

import asyncio
import time
import logging
import signal
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
            await train_online_model(data, online_model)
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

        # Mekanik stratejileri değerlendir (AI her birini kendi kimliğiyle inceler)
        silent_agents = []
        for agent in agents:
            try:
                decision = await agent.evaluate(snapshot)
                if decision is None:
                    silent_agents.append(agent)
            except Exception as e:
                logger.error(f"Error in {agent.name}: {e}")

        # ZORUNLU MOD YEDEĞİ: sessiz kalan HER ajan için (kota gerektirmez,
        # anında çalışır) mikroyapı yönünden basit bir sinyal üretip normal
        # koruma bantlarından geçirir. Pencere başına tek pozisyon kuralı
        # zaten fazla işlem açılmasını engeller.
        for agent in silent_agents:
            try:
                agent.force_local_signal(snapshot)
            except Exception as e:
                logger.debug(f"Force local signal error in {agent.name}: {e}")

        # AI İNİSİYATİFİ: stratejisi sessiz kalan ajanlar piyasaya KENDİ
        # gözleriyle bakar; kimliğine uyan bir fırsat görürse işlemi kendisi
        # başlatır (LLM destekli, daha nüanslı gerekçe). Kota dostu olsun
        # diye ~1 dakikada bir ve sırayla.
        if cycle % 6 == 0 and silent_agents:
            idx = (cycle // 6) % len(silent_agents)
            try:
                await silent_agents[idx].ai_initiate(snapshot)
            except Exception as e:
                logger.debug(f"AI initiate error: {e}")

        # ERKEN ÇIKIŞ: açık pozisyonlar her döngüde gözden geçirilir.
        # Kaybeden pozisyonda bid'e satıp ne kurtarılırsa kurtarılır;
        # kazananda kâr kilitlenir. Resolution'dan ÖNCE çalışır.
        early_closed = 0
        for agent in agents:
            try:
                early_closed += agent.strategy.check_early_exits(snapshot)
            except Exception as e:
                logger.debug(f"Erken çıkış hatası ({agent.name}): {e}")

        # Resolution: her trade, penceresinin RESMİ (Chainlink tabanlı)
        # Polymarket çözümüyle kapatılır — Binance tahminiyle değil.
        resolved = await resolve_due_trades(data, paper, agents, online_model)
        resolved += early_closed

        # İşlem sonuçlandıysa: ajanlar öğrenip stratejilerini adapte etsin
        if resolved:
            leaderboard = paper.get_leaderboard()
            for agent in agents:
                try:
                    agent.maybe_adapt()
                    # ÖZ-DEĞERLENDİRME: ajan kendi tezini yeniden yazar ve
                    # rakiplerinin sonuçlarını görür (rekabetten öğrenme)
                    if agent.maybe_reflect():
                        await agent.reflect(leaderboard)
                        agent._trades_at_last_reflect = (
                            paper.portfolios[agent.strategy.name].total_trades
                        )
                except Exception as e:
                    logger.error(f"Adapt/reflect error in {agent.name}: {e}")

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


async def resolve_due_trades(data: MarketDataService, paper: PaperTrader,
                             agents: list, online_model) -> int:
    """
    Zamanı gelen trade'leri RESMİ sonuçla çözer; ayrıca:
      - Her stratejinin KALİBRATÖRÜNÜ besler (ham güven vs gerçek sonuç)
      - ONLINE MODELİ eğitir (pencere başı mikroyapı -> gerçek yön)

    FİYAT KAYNAĞI (kritik):
      Bu piyasalar Chainlink BTC/USD ile çözülür, Binance ile değil. Ölçtük:
      Binance kıyası pencerelerin ~%8'inde yanlış sonuç veriyor. Bu yüzden
      SADECE Polymarket'in resmi çözümü kullanılır. Sonuç henüz gelmediyse
      trade bekletilir (yanlış sonuçla kapatmaktansa beklemek yeğdir).

    Çözülen trade sayısını döndürür.
    """
    now = time.time()
    resolved = 0
    agent_by_strategy = {a.strategy.name: a for a in agents}

    for portfolio in paper.portfolios.values():
        for trade in list(portfolio.pending_trades):
            if not trade.resolve_at or now < trade.resolve_at:
                continue

            # TEK DOĞRU KAYNAK: Polymarket'in resmi (Chainlink tabanlı) çözümü
            outcome = await data.fetch_official_outcome(trade.market_window)
            if outcome is None:
                # Henüz çözülmemiş. Çok uzun sürerse (>30dk) vazgeçip iptal et,
                # ama ASLA tahminî bir sonuçla kapatma.
                if now - trade.resolve_at > 1800:
                    portfolio.pending_trades.remove(trade)
                    logger.warning(
                        f"⚠️  [{trade.strategy_name}] pencere {trade.market_window} "
                        f"30dk+ çözülmedi — işlem iptal edildi (bakiye iade)."
                    )
                    portfolio.balance += trade.amount + trade.fee
                continue

            source = "resmi (Chainlink)"
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


async def train_online_model(data: MarketDataService, online_model) -> int:
    """
    ONLINE MODEL EĞİTİMİ.

    Kapanmış her 5dk penceresi için:
        girdi  = pencere İÇİNDE alınmış mikroyapı örnekleri (ileriye bakış yok)
        hedef  = piyasanın RESMİ sonucu (Chainlink tabanlı)

    ÇOKLU ÖRNEK (train/serve uyumu):
      Pencere boyunca ~30sn'de bir örnek alınır ve hepsi AYNI sonuçla
      etiketlenir. Canlı strateji pencerenin ortasında tahmin istediği için
      model de tam olarak o durumu görmelidir; sadece pencere başıyla
      eğitmek modeli hiç karşılaşmayacağı bir dünyaya hazırlardı.
      Yan fayda: pencere başına 1 yerine ~10 örnek -> 10x öğrenme hızı.

    Etiket kalitesi kritik: Binance kıyasıyla üretilen etiketlerin ~%8'i
    yanlıştı; model bozuk etiketle öğrenirse hiçbir zaman gerçek sinyali
    bulamaz. Bu yüzden yalnızca resmi sonuç kullanılır.
    """
    now = int(time.time())
    current_window = now - (now % 300)
    learned = 0

    for w in sorted(list(data.window_micro.keys())):
        if w >= current_window:
            continue  # pencere henüz kapanmadı
        samples = data.window_micro.get(w)
        if not samples:
            data.window_micro.pop(w, None)
            continue

        outcome = await data.fetch_official_outcome(w)
        if outcome is None:
            # Henüz çözülmedi; 30dk'dan eskiyse artık gelmeyecek, at.
            if now - (w + 300) > 1800:
                data.window_micro.pop(w, None)
            continue
        went_up = (outcome == "UP")

        data.record_window_outcome(went_up)
        for micro in samples:
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


def _save_on_shutdown(why: str):
    """
    Kapanışta son durumu kaydeder.

    NEDEN KRİTİK: Render (ve çoğu PaaS) deploy sırasında SIGTERM gönderir.
    Eskiden yalnızca KeyboardInterrupt (SIGINT) yakalanıyordu; yani her
    deploy'da son 30 saniyenin işlemleri ve AI öğrenmesi kaydedilmeden
    yok oluyordu. Artık SIGTERM de yakalanıyor.
    """
    logger.info(f"👋 Kapatılıyor ({why}) — durum kaydediliyor…")
    try:
        from dashboard.app import _paper_trader, _agents, _online_model
        if _paper_trader and _agents:
            ok = save_state(_paper_trader, _agents, online_model=_online_model)
            logger.info("💾 Durum kaydedildi." if ok else "⚠️  Durum KAYDEDİLEMEDİ!")
    except Exception as e:
        logger.error(f"Kapanışta kayıt hatası: {e}")


def _handle_sigterm(signum, frame):
    _save_on_shutdown(f"sinyal {signum}")
    sys.exit(0)


if __name__ == "__main__":
    # Deploy/ölçekleme sırasında gelen SIGTERM'de de durumu kaydet
    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _save_on_shutdown("Ctrl+C")
        sys.exit(0)
