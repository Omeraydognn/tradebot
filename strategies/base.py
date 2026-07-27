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
    FORCE_TRADE_MODE,
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
        self.bet_size: float = 10.0   # taban bahis $ (Kelly ile ölçeklenir)
        self.min_ev: float = 0.02     # işlem için gereken min EV (0.0-0.15 arası)
        self.kelly_fraction: float = 0.25  # fraksiyonel Kelly (0=kapalı, 1=tam Kelly)
        self.last_skip_reason: Optional[str] = None  # neden işlem yapmadı (UI)
        # Güven kalibrasyonu: ham güveni gerçek isabetle hizalar
        self.calibrator = Calibrator(name)

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
        }

    def set_tunables(self, values: dict):
        """Verilen parametreleri güvenli sınırlar içinde uygular."""
        spec = self.get_tunables()
        for key, val in (values or {}).items():
            if key not in spec:
                continue
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
        # 1) Aşırı fiyat: $0.10-$0.90 dışında işlem yok (sonuç neredeyse belli).
        # 2) Pencere sonu: son NO_TRADE_LAST_SECONDS sn içinde yeni işlem yok.
        # 3) Pencere başına tek pozisyon (aynı 5dk'da yığılma yok).
        time_left = (snapshot.window_end - time.time()) if snapshot.window_end else 999.0
        price_ok = PRICE_MIN <= share_price <= PRICE_MAX
        time_ok = time_left >= NO_TRADE_LAST_SECONDS
        window_free = not self.has_position_in_window(snapshot.window_start)

        # Kelly ile bahis boyutu (edge büyüdükçe artar)
        bet = self.kelly_bet_size(signal.confidence, share_price) if self.kelly_fraction > 0 else self.bet_size
        if bet <= 0:
            bet = self.bet_size
        bet = round(max(1.0, bet), 2)

        # Zorunlu modda EV eşiği işlemi ENGELLEMEZ — sadece gerçek koruma
        # bantları (fiyat aralığı, pencere sonu, tek pozisyon) geçerlidir.
        # Böylece her strateji sonuçlanan işleme sahip olur; kalibratör ve
        # adapt() gerçek kazanç/kayıptan öğrenebilir.
        ev_ok = FORCE_TRADE_MODE or (ev > self.min_ev)

        if ev_ok and price_ok and time_ok and window_free:
            trade = self.paper.place_paper_trade(
                strategy_name=self.name,
                side=signal.direction,
                share_price=share_price,
                bet_amount=bet,
                market_window=snapshot.window_start,
                resolve_at=snapshot.window_end,
                raw_confidence=raw_conf,
                calibrated_confidence=signal.confidence,
            )
            if trade:
                self.last_skip_reason = None
                decision["action"] = "TRADE"
                decision["trade"] = trade.to_dict()
                logger.info(
                    f"🎯 [{self.name}] {signal.direction} | Conf: {signal.confidence:.0%} | "
                    f"Share: ${share_price:.2f} | Bet: ${bet:.2f} | EV: ${ev:+.3f} | {signal.reasoning}"
                )
        else:
            if not window_free:
                reason = "bu pencerede zaten pozisyon var"
            elif not price_ok:
                reason = f"fiyat uçta (${share_price:.2f})"
            elif not time_ok:
                reason = f"pencere sonu ({time_left:.0f}s kaldı)"
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

    def get_status(self) -> dict:
        """Return strategy status for dashboard."""
        portfolio = self.paper.portfolios.get(self.name)
        return {
            "name": self.name,
            "is_active": self.is_active,
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
            "calibration": self.calibrator.to_dict(),
            "tunables": self.get_tunables(),
            "portfolio": portfolio.to_dict() if portfolio else None,
            "recent_decisions": self.ai_decisions[-5:],
        }
