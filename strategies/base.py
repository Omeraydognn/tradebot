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
    FORCE_TRADE_MODE, ALLOW_FUTURE_WINDOW_TRADES,
    ALWAYS_TRADE_MODE, ALWAYS_TRADE_BET, ALWAYS_TRADE_MIN_BALANCE,
    ALWAYS_TRADE_LAST_SECONDS,
)
from calibration import Calibrator

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
        self.bet_size: float = 50.0   # taban bahis $ (Kelly ile ölçeklenir)
        self.min_ev: float = 0.02     # işlem için gereken min EV (0.0-0.15 arası)
        self.kelly_fraction: float = 0.25  # fraksiyonel Kelly (0=kapalı, 1=tam Kelly)
        self.last_skip_reason: Optional[str] = None  # neden işlem yapmadı (UI)
        # Güven kalibrasyonu: ham güveni gerçek isabetle hizalar
        self.calibrator = Calibrator(name)

        # --- ERKEN ÇIKIŞ POLİTİKASI ---
        # Pozisyonun anlık değeri (en iyi bid) girişin bu oranının ALTINA
        # düşerse sat: "ne kurtarırsak kâr". 0.55'ten alıp bid 0.28'e
        # düştüyse (%50) beklemek yerine maliyetin yarısını kurtarırız.
        self.stop_loss_ratio: float = 0.50
        # Bid girişin bu katına ÇIKTIYSA kârı kilitle. Pencere sonuna kadar
        # ters dönme riskini almaktansa kesinleşmiş kârı al.
        self.take_profit_ratio: float = 1.60
        # Çıkışın anlamlı olması için pencerede en az bu kadar saniye kalmalı
        # (son saniyelerde satmak zaten çözülmeyi beklemekle aynı şey).
        self.min_seconds_to_exit: float = 45.0
        self.early_exits: int = 0
        self.exit_blocked_count: int = 0

        # --- HER İŞLEMDEN ÖĞRENME ---
        # Kalibratör tek bir "güven -> isabet" eğrisi öğrenir. Bu tablo ise
        # KOŞULA göre öğrenir: "hangi giriş fiyatı diliminde gerçekten
        # kazanıyorum?". Uçtaki fiyatlardan da işlem açılan modda bu ayrım
        # kritik — $0.05'ten alınan işlemlerle $0.50'den alınanlar aynı
        # dünyada değildir ve tek bir ortalama ikisini de gizler.
        # {dilim: {"n": adet, "wins": adet, "pnl": toplam}}
        self.price_bucket_stats: dict = {}

    @abstractmethod
    def generate_signal(self, snapshot: PolymarketSnapshot) -> Optional[Signal]:
        """
        Analyze market data and return a Signal, or None if no trade.
        Subclasses implement their specific strategy logic here.
        """
        ...

    # ------------------------------------------------------------------ #
    #  AI-ayarlanabilir parametreler (tunables)                           #
    #  Alt sınıflar kendi eşiklerini bu API üzerinden açar; AI ajanı      #
    #  outcome'lara göre bunları değiştirebilir.                          #
    # ------------------------------------------------------------------ #

    def get_tunables(self) -> dict:
        """Ayarlanabilir parametreler: {isim: {value, min, max, desc}}"""
        return {
            "bet_size": {"value": self.bet_size, "min": 2.0, "max": 50.0,
                         "desc": "taban bahis ($)"},
            "min_ev": {"value": self.min_ev, "min": 0.0, "max": 0.15,
                       "desc": "işlem için gereken minimum EV"},
            "kelly_fraction": {"value": self.kelly_fraction, "min": 0.0, "max": 0.5,
                               "desc": "fraksiyonel Kelly katsayısı"},
            "stop_loss_ratio": {"value": self.stop_loss_ratio, "min": 0.0, "max": 0.90,
                                "desc": "bid girişin bu oranına düşerse sat (0=kapalı)"},
            "take_profit_ratio": {"value": self.take_profit_ratio, "min": 1.0, "max": 3.0,
                                  "desc": "bid girişin bu katına çıkarsa kârı kilitle"},
        }

    # "Her pencereye gir" modunda AI'ın DOKUNAMAYACAĞI parametreler.
    #
    # Gözlenen sorun: az işlem -> zarar -> adapt() eşikleri sıkar -> daha az
    # işlem. Ölüm sarmalı. Canlıda AI `min_ev`'i izin verilen tavana (0.15)
    # ve `min_conviction`'ı 0.03'ten 0.14'e çekmişti; sistem neredeyse hiç
    # işlem açmaz olmuştu. Bu modun amacı veri toplamak olduğu için
    # seçiciliği artıran parametreler kilitlenir.
    _SELECTIVITY_KEYS = ("min_ev", "min_conviction")

    def set_tunables(self, values: dict):
        """Verilen parametreleri güvenli sınırlar içinde uygular."""
        spec = self.get_tunables()
        for key, val in (values or {}).items():
            if key not in spec:
                continue
            if ALWAYS_TRADE_MODE and key in self._SELECTIVITY_KEYS:
                continue  # bu modda seçicilik artırılamaz
            try:
                v = float(val["value"] if isinstance(val, dict) else val)
            except (TypeError, ValueError):
                continue
            lo, hi = spec[key]["min"], spec[key]["max"]
            setattr(self, key, max(lo, min(hi, v)))

    def kelly_bet_size(self, confidence: float, share_price: float) -> float:
        """
        Fraksiyonel Kelly ile bahis boyutu.

        Binary market: b = (1-p)/p  (kazanç oranı), win olasılığı = confidence
            f* = (b·q_win - q_lose) / b   →   sadeleşince: f* = (conf - price) / (1 - price)

        Güvenlik: fraksiyonel Kelly (varsayılan 1/4) + bakiyenin %20'si tavan +
        taban bahsin 3 katı tavan (tek işlemde patlamayı önler).
        """
        price = max(0.01, min(0.99, share_price))
        edge = confidence - price          # pozitif = avantaj var
        if edge <= 0:
            return 0.0

        f_star = edge / (1.0 - price)      # tam Kelly oranı (0-1)
        f = f_star * self.kelly_fraction   # fraksiyonel

        portfolio = self.paper.portfolios.get(self.name)
        balance = portfolio.balance if portfolio else 0.0

        size = balance * f
        size = min(size, balance * 0.20, self.bet_size * 3.0)  # tavanlar
        return max(0.0, size)

    def calculate_ev(self, signal: Signal, snapshot: PolymarketSnapshot) -> float:
        """
        Calculate expected value including Polymarket's dynamic pricing.

        EV = (confidence × profit_if_win) - ((1-confidence) × cost_if_lose) - fee

        Where:
            profit_if_win = 1.0 - share_price
            cost_if_lose  = share_price
            fee           = share_price × fee_rate
        """
        # GERÇEKÇİ maliyet: midpoint değil, gerçekten ödenecek ASK fiyatı
        share_price = snapshot.executable_price(signal.direction)

        # Clamp to valid range
        share_price = max(0.01, min(0.99, share_price))

        profit_if_win = 1.0 - share_price       # e.g. buy at $0.40 → win $0.60
        cost_if_lose = share_price               # e.g. buy at $0.40 → lose $0.40
        fee = share_price * POLYMARKET_FEE_RATE

        ev = (signal.confidence * profit_if_win) - ((1 - signal.confidence) * cost_if_lose) - fee
        return ev

    def _blend_with_market(self, our_conf: float, direction: str,
                           snapshot: PolymarketSnapshot) -> float:
        """
        Güveni piyasa olasılığına doğru büzer (Bayesçi shrinkage).

        NEDEN GEREKLİ (üretimde gerçek zarar gözlendi):
          Piyasa UP için $0.07 derken (=%7 ihtimal) stratejilerimiz "%70
          eminim" deyip alıyordu. 60+ puanlık bu fark neredeyse hiçbir zaman
          gerçek avantaj değildir — bizim uydurma güvenimizin hatasıdır.
          EV formülü bu sahte güvenle beslenince her seferinde "muhteşem
          fırsat" gösteriyor ve sistem sistematik olarak para kaybediyordu.

        MANTIK:
          Piyasa fiyatı, para koymuş binlerce katılımcının bilgisidir; güçlü
          bir önseldir. Kendi görüşümüze ancak KANITLADIĞIMIZ kadar ağırlık
          veririz. Kalibratörde veri yokken piyasaya çok, veri biriktikçe
          kendimize daha fazla güveniriz.
        """
        market_prob = snapshot.up_price if direction == "UP" else snapshot.down_price
        market_prob = max(0.01, min(0.99, market_prob))

        # Kalibrasyon kanıtı arttıkça kendi görüşümüzün ağırlığı artar (max %50)
        n = len(self.calibrator.samples)
        own_weight = min(0.50, 0.15 + 0.35 * min(1.0, n / 60.0))

        blended = own_weight * our_conf + (1 - own_weight) * market_prob
        return max(0.05, min(0.95, blended))

    def has_position_in_window(self, window_start: int) -> bool:
        """Bu 5dk penceresinde zaten açık pozisyon var mı? (yığılmayı önler)"""
        if not window_start:
            return False
        portfolio = self.paper.portfolios.get(self.name)
        if not portfolio:
            return False
        return any(t.market_window == window_start for t in portfolio.pending_trades)

    def execute_ai_signal(self, snapshot: PolymarketSnapshot, direction: str,
                          confidence: float, reasoning: str) -> Optional[dict]:
        """
        AI'ın KENDİ inisiyatifiyle ürettiği sinyali işleme sokar.

        Mekanik strateji sessizken ajan bir fırsat gördüğünde buradan geçer.
        ÖNEMLİ: normal işlem hattının aynısını kullanır — yani koruma bantları
        (fiyat aralığı, pencere sonu, pencere başına tek pozisyon, EV eşiği,
        Kelly boyutlandırma) AI için de aynen geçerlidir. AI bunları aşamaz;
        sadece "ne zaman bakılacağına" karar verir, risk kurallarına değil.
        """
        signal = Signal(
            direction=direction,
            confidence=confidence,
            reasoning=reasoning,
            indicators={"source": "ai_initiative"},
        )
        return self._process_signal(signal, snapshot, ai_initiated=True)

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

        return self._process_signal(signal, snapshot, ai_initiated=False)

    def _process_signal(self, signal, snapshot: PolymarketSnapshot,
                        ai_initiated: bool = False) -> Optional[dict]:
        """Sinyali kalibre eder, EV hesaplar, koruma bantlarını uygular, işlem açar."""

        self.last_signal = signal
        self.total_signals += 1

        # --- KALİBRASYON ---
        # Stratejinin ham güveni uydurma bir formülden gelir. Geçmiş
        # (tahmin, sonuç) çiftlerine bakıp gerçeğe hizalanmış güveni kullan.
        raw_conf = signal.confidence
        signal.confidence = self.calibrator.calibrate(raw_conf)

        # --- PİYASA ÖNSELİ (market prior) ---
        # Piyasa fiyatı, binlerce katılımcının parasını koyduğu bir olasılık
        # tahminidir ve güçlü bir önseldir. Bizim güvenimiz henüz KANITLANMADI.
        # Bu yüzden güven, piyasa olasılığına doğru büzülür (Bayesçi shrinkage).
        # Kalibrasyon verisi biriktikçe kendi görüşümüze daha çok ağırlık verilir.
        signal.confidence = self._blend_with_market(signal.confidence, signal.direction, snapshot)

        ev = self.calculate_ev(signal, snapshot)
        self.last_ev = ev

        # İşlem, gerçekten ödenecek fiyattan (ask) yapılır — midpoint'ten değil
        share_price = snapshot.executable_price(signal.direction)
        mid_price = snapshot.up_price if signal.direction == "UP" else snapshot.down_price

        decision = {
            "timestamp": time.time(),
            "strategy": self.name,
            "direction": signal.direction,
            "confidence": round(signal.confidence, 3),
            "raw_confidence": round(raw_conf, 3),
            "share_price": round(share_price, 3),
            "mid_price": round(mid_price, 3),
            "spread_cost": round(share_price - mid_price, 4),
            "implied_prob": round(mid_price, 3),
            "ev": round(ev, 4),
            "ai_initiated": ai_initiated,
            "reasoning": signal.reasoning,
            "indicators": signal.indicators,
            "action": "SKIP",
            "trade": None,
        }

        # --- KORUMA BANTLARI (guardrails) ---
        # 1) Aşırı fiyat: PRICE_MIN-PRICE_MAX dışında işlem yok.
        # 2) Pencere sonu: son NO_TRADE_LAST_SECONDS sn içinde yeni işlem yok.
        # 3) Pencere başına tek pozisyon (aynı 5dk'da yığılma yok).
        # 4) PENCERE DOĞRULUĞU: varsayılan olarak SADECE şu an oynanan 5dk
        #    penceresine işlem açılır. Polymarket'in aktif piyasası
        #    bulunamayıp bir SONRAKİ pencereye düşüldüyse (window_offset>0),
        #    o pencere henüz başlamamıştır: mikroyapı verimiz o pencereye ait
        #    değildir, yani sinyalimizin hiçbir geçerliliği yoktur.
        time_left = (snapshot.window_end - time.time()) if snapshot.window_end else 999.0
        cutoff = ALWAYS_TRADE_LAST_SECONDS if ALWAYS_TRADE_MODE else NO_TRADE_LAST_SECONDS
        # HER PENCEREYE GİR modunda fiyat bandı uygulanmaz (uçtaki fiyatlardan
        # da işlem açılır) — amaç maksimum sonuçlanmış işlem, yani maksimum
        # kalibrasyon verisi.
        price_ok = ALWAYS_TRADE_MODE or (PRICE_MIN <= share_price <= PRICE_MAX)
        time_ok = time_left >= cutoff
        window_free = not self.has_position_in_window(snapshot.window_start)
        window_current = (snapshot.window_offset == 0) and snapshot.is_current_window
        window_ok = window_current or ALLOW_FUTURE_WINDOW_TRADES

        if ALWAYS_TRADE_MODE:
            # KÜÇÜK ama ÖĞRENMEYE DUYARLI bahis.
            #
            # Sabit bahis, öğrenmeyi kısırlaştırır: kalibratör ve adapt()
            # veriden bir avantaj çıkarsa bile onu kâra çevirecek hiçbir kol
            # kalmaz (bet_size/kelly yok sayılırdı). O yüzden taban bahis
            # küçük tutulur ama KANITLANMIŞ avantajla ölçeklenir.
            #
            # Ölçek = kalibre edilmiş güven ile piyasa fiyatı arasındaki fark
            # (Kelly'nin özü). Avantaj yoksa taban bahsin yarısı, güçlü ve
            # KANITLANMIŞ avantaj varsa en fazla 3 katı oynanır. Böylece
            # hem her pencereye girilir hem de öğrenmenin bir karşılığı olur.
            portfolio = self.paper.portfolios.get(self.name)
            balance = portfolio.balance if portfolio else 0.0

            edge = signal.confidence - share_price      # pozitif = avantaj
            # Kalibratör kanıt biriktirdikçe avantaja daha çok itibar edilir
            n_cal = len(self.calibrator.samples)
            trust = min(1.0, n_cal / 40.0)
            mult = 1.0 + (edge / max(0.05, 1.0 - share_price)) * 2.0 * trust
            mult = max(0.5, min(3.0, mult))

            # adapt()'ın öğrendiği bet_size da etkili olsun (yoksa AI ölü bir
            # parametreyi ayarlar). Varsayılana ORANLA uygulanır ve dar bir
            # banda kısılır: AI risk iştahını değiştirebilsin ama bu modun
            # "küçük bahis, uzun ömür" ilkesini bozamasın.
            size_ratio = max(0.5, min(2.0, self.bet_size / 50.0))

            bet = ALWAYS_TRADE_BET * mult * size_ratio
            if balance < ALWAYS_TRADE_MIN_BALANCE:
                bet = max(1.0, balance * 0.02)   # kalanın %2'si — asla bitmesin
            bet = round(min(bet, max(1.0, balance)), 2)
            decision["bet_multiplier"] = round(mult, 2)
        else:
            # Kelly ile bahis boyutu (edge büyüdükçe artar)
            bet = self.kelly_bet_size(signal.confidence, share_price) if self.kelly_fraction > 0 else self.bet_size
            if bet <= 0:
                bet = self.bet_size
            bet = round(max(10.0, bet), 2)

        # GERÇEK DOLUM: emir defterini seviye seviye yürüterek gerçekten
        # ödenecek ortalama fiyatı ve alınabilecek hisse adedini bul.
        fill = snapshot.simulate_fill(signal.direction, bet)
        fill_ok = bool(fill.get("shares", 0) > 0 and fill.get("avg_price"))
        decision["fill"] = {
            "avg_price": round(fill["avg_price"], 4) if fill.get("avg_price") else None,
            "shares": round(fill.get("shares") or 0.0, 2),
            "levels_used": fill.get("levels_used", 0),
            "slippage_bps": round(fill["slippage_bps"], 1) if fill.get("slippage_bps") is not None else None,
            "liquidity_capped": bool(fill.get("insufficient")),
            "book_depth_usd": round(fill.get("book_depth_usd") or 0.0, 2),
        }
        decision["window_label"] = snapshot.window_label
        decision["window_offset"] = snapshot.window_offset
        decision["is_current_window"] = window_current

        # Zorunlu modda EV eşiği işlemi ENGELLEMEZ — sadece gerçek koruma
        # bantları (fiyat aralığı, pencere sonu, tek pozisyon, pencere
        # doğruluğu) geçerlidir. Bunlar gerçeklikle ilgilidir, iştahla değil.
        ev_ok = FORCE_TRADE_MODE or ALWAYS_TRADE_MODE or (ev > self.min_ev)

        if ev_ok and price_ok and time_ok and window_free and window_ok and fill_ok:
            cl = self.data.chainlink
            trade = self.paper.place_paper_trade(
                strategy_name=self.name,
                side=signal.direction,
                bet_amount=bet,
                fill=fill,
                market_window=snapshot.window_start,
                resolve_at=snapshot.window_end,
                raw_confidence=raw_conf,
                calibrated_confidence=signal.confidence,
                window_offset=snapshot.window_offset,
                mid_price=mid_price,
                ev=ev,
                reasoning=signal.reasoning,
                ai_initiated=ai_initiated,
                btc_price_at_entry=self.data.latest_btc_price,
                chainlink_at_entry=cl.price if cl.is_live else 0.0,
                cl_divergence_bps=self.data.calc_chainlink_divergence_bps() or 0.0,
            )
            if trade:
                self.last_skip_reason = None
                decision["action"] = "TRADE"
                decision["trade"] = trade.to_dict()
                logger.info(
                    f"🎯 [{self.name}] {signal.direction} {trade.window_label} | "
                    f"Conf: {signal.confidence:.0%} | Dolum: ${trade.entry_price:.4f} | "
                    f"Bahis: ${trade.amount:.2f} | EV: ${ev:+.3f} | {signal.reasoning}"
                )
        else:
            if not window_ok:
                reason = (f"ileri pencereye işlem yok "
                          f"({snapshot.window_label}, offset +{snapshot.window_offset})")
            elif not window_free:
                reason = "bu pencerede zaten pozisyon var"
            elif not price_ok:
                reason = f"fiyat uçta (${share_price:.2f})"
            elif not time_ok:
                reason = f"pencere sonu ({time_left:.0f}s kaldı)"
            elif not fill_ok:
                reason = "emir defteri boş — gerçek dolum simüle edilemiyor"
            else:
                reason = f"EV düşük (${ev:+.3f} < {self.min_ev:.2f})"
            self.last_skip_reason = reason
            decision["skip_reason"] = reason
            logger.debug(f"⏭️  [{self.name}] Skip {signal.direction} | {reason}")

        # Keep decision log (max 100)
        self.ai_decisions.append(decision)
        if len(self.ai_decisions) > 100:
            self.ai_decisions.pop(0)

        return decision

    @staticmethod
    def _price_bucket(price: float) -> str:
        """Giriş fiyatını 0.10'luk dilimlere ayırır: '0.00-0.10', '0.50-0.60'…"""
        p = max(0.0, min(0.999, float(price)))
        lo = int(p * 10) / 10.0
        return f"{lo:.2f}-{lo + 0.10:.2f}"

    def learn_from_trade(self, trade) -> dict:
        """
        SONUÇLANAN HER İŞLEMDEN ÖĞREN.

        Kalibratörden farkı: kalibratör "%60 dediğimde %60 tutuyor muyum?"
        sorusunu tek eğriyle cevaplar. Bu tablo koşullu öğrenir — hangi
        FİYAT DİLİMİNDE gerçekten para kazanıldığını ayrı ayrı biriktirir.

        Uçtaki fiyatlardan da işlem açılan modda bu şart: $0.05'ten alınan
        yüzlerce işlem, $0.50'den alınanların istatistiğini bozmamalı.

        Dönüş: bu işlemin dilimine ait güncel özet (arayüz için).
        """
        b = self._price_bucket(trade.entry_price)
        st = self.price_bucket_stats.setdefault(b, {"n": 0, "wins": 0, "pnl": 0.0})
        st["n"] += 1
        if trade.result == "WIN":
            st["wins"] += 1
        st["pnl"] += float(trade.pnl or 0.0)
        return {"bucket": b, **st}

    def price_bucket_table(self) -> list:
        """Fiyat dilimi bazında öğrenilen tablo (arayüzde gösterilir)."""
        rows = []
        for b, st in sorted(self.price_bucket_stats.items()):
            n = st["n"]
            rows.append({
                "bucket": b,
                "n": n,
                "wins": st["wins"],
                "win_rate": round(st["wins"] / n * 100, 1) if n else None,
                "pnl": round(st["pnl"], 2),
                "avg_pnl": round(st["pnl"] / n, 3) if n else None,
            })
        return rows

    def check_early_exits(self, snapshot: PolymarketSnapshot) -> int:
        """
        AÇIK POZİSYONLARI GÖZDEN GEÇİR — erken çıkış gerekiyor mu?

        Pozisyonun anlık değeri, onu ŞU AN satabileceğimiz fiyattır: en iyi
        BID. Midpoint kullanmak iyimserlik olurdu, çünkü satarken midpoint'i
        alamayız.

        İki kural:
          1) STOP-LOSS: bid, girişin `stop_loss_ratio` katının altına düştüyse
             sat. Kaybeden pozisyonda pencere sonunu beklemek %100 kayıptır;
             şimdi satmak ne kurtarırsak onu kâr yazar.
          2) KÂR KİLİTLEME: bid, girişin `take_profit_ratio` katına çıktıysa
             sat. Kesinleşmiş kârı, ters dönme riskine tercih ederiz.

        Yalnızca bu snapshot'ın penceresine ait pozisyonlara bakar — başka
        pencerenin defteriyle fiyatlama yapmak yanlış olurdu.

        Dönüş: kapatılan pozisyon sayısı.
        """
        portfolio = self.paper.portfolios.get(self.name)
        if not portfolio or not portfolio.pending_trades:
            return 0
        if not snapshot or not snapshot.window_start:
            return 0

        time_left = (snapshot.window_end - time.time()) if snapshot.window_end else 0.0
        if time_left < self.min_seconds_to_exit:
            return 0   # bu kadar az kalmışken satmak ile beklemek aynı şey

        closed = 0
        for trade in list(portfolio.pending_trades):
            # Sadece AYNI pencerenin pozisyonları bu defterle fiyatlanabilir
            if trade.market_window != snapshot.window_start:
                continue

            bid = snapshot.mark_price(trade.side)
            if bid is None or trade.entry_price <= 0:
                continue

            ratio = bid / trade.entry_price
            reason = None
            if self.stop_loss_ratio > 0 and ratio <= self.stop_loss_ratio:
                reason = (f"stop-loss: bid ${bid:.4f} girişin %{ratio*100:.0f}'ine "
                          f"düştü (eşik %{self.stop_loss_ratio*100:.0f})")
            elif self.take_profit_ratio > 1.0 and ratio >= self.take_profit_ratio:
                reason = (f"kâr kilitleme: bid ${bid:.4f} girişin %{ratio*100:.0f}'i "
                          f"(eşik %{self.take_profit_ratio*100:.0f})")

            if reason is None:
                continue

            sell = snapshot.simulate_sell(trade.side, trade.shares)
            if self.paper.close_trade_early(trade, sell, reason):
                self.early_exits += 1
                closed += 1
                # Erken çıkan işlem de bir derstir (kalibratöre gitmez —
                # yön doğru muydu bilmiyoruz — ama fiyat dilimi tablosuna girer)
                try:
                    self.learn_from_trade(trade)
                except Exception as e:
                    logger.debug(f"learn_from_trade (erken çıkış) hatası: {e}")
            else:
                # Çıkamadık — dar defterde sıkıştık. Bu gerçek bir risktir.
                self.exit_blocked_count += 1
                logger.warning(
                    f"🔒 [{self.name}] çıkış ENGELLENDİ {trade.side} "
                    f"{trade.window_label}: {trade.exit_blocked_reason}"
                )
        return closed

    def get_status(self) -> dict:
        """Return strategy status for dashboard."""
        portfolio = self.paper.portfolios.get(self.name)
        return {
            "name": self.name,
            "is_active": self.is_active,
            # Strateji HİÇ sinyal üretmiyorsa nedeni ("bot dondu mu?" cevabı)
            "silence_reason": getattr(self, "silence_reason", None),
            "total_signals": self.total_signals,
            "last_signal": {
                "direction": self.last_signal.direction,
                "confidence": round(self.last_signal.confidence, 3),
                "reasoning": self.last_signal.reasoning,
            } if self.last_signal else None,
            "last_ev": round(self.last_ev, 4),
            "bet_size": round(self.bet_size, 2),
            "min_ev": round(self.min_ev, 3),
            "kelly_fraction": round(self.kelly_fraction, 3),
            "last_skip_reason": self.last_skip_reason,
            "early_exits": self.early_exits,
            "exit_blocked_count": self.exit_blocked_count,
            # Her işlemden öğrenilen koşullu tablo
            "price_buckets": self.price_bucket_table(),
            "stop_loss_ratio": round(self.stop_loss_ratio, 3),
            "take_profit_ratio": round(self.take_profit_ratio, 3),
            "calibration": self.calibrator.to_dict(),
            "tunables": self.get_tunables(),
            "portfolio": portfolio.to_dict() if portfolio else None,
            "recent_decisions": self.ai_decisions[-5:],
        }
