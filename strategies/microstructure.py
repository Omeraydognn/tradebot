"""
Strategy 6: Microstructure Alpha (Order Flow + Türev Konumlanması)

Finans Temeli:
  5 dakikalık BTC yönü, teknik indikatörlerden çok MİKROYAPI ve
  KONUMLANMA verisiyle açıklanır. Bu strateji dört bağımsız kanaldan
  kanıt toplayıp ağırlıklı bir olasılık üretir:

  1) CVD / Agresif Akış (en yüksek ağırlık)
     Piyasa emirleriyle gelen alış-satış dengesizliği. Bilgi sahibi akış
     burada görünür; fiyat çoğu zaman akışı takip eder.

  2) Emir Defteri Dengesizliği
     Pasif likidite nerede kalın? Kalın bid = destek, kalın ask = direnç.

  3) Türev Konumlanması (funding + basis)
     Aşırı pozitif funding = long'lar kalabalık ve kaldıraçlı → yukarı
     hareket yakıtsız, aşağı squeeze riski (KONTRARİAN sinyal).
     Aşırı negatif funding = short'lar kalabalık → yukarı squeeze riski.

  4) Açık Pozisyon (OI) × Fiyat kombinasyonu
     OI↑ + fiyat↑ = yeni long'lar (trend sağlam, DEVAM)
     OI↓ + fiyat↑ = short kapanışı (squeeze, sürdürülemez)
     OI↑ + fiyat↓ = yeni short'lar (aşağı trend sağlam)

  Her kanal bir P(UP) tahmini üretir; ağırlıklı ortalama nihai olasılıktır.
  Sinyal ancak yeterli sayıda kanal veri sağladığında üretilir.
"""
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal
from market_data import MarketDataService, PolymarketSnapshot
from paper_trader import PaperTrader

logger = logging.getLogger(__name__)


class MicrostructureStrategy(BaseStrategy):
    def __init__(self, data: MarketDataService, paper: PaperTrader):
        super().__init__(name="Microstructure Alpha", data=data, paper=paper)
        # Kanal ağırlıkları (AI ajanı bunları ayarlayabilir)
        self.w_flow = 0.40       # CVD / agresif akış
        self.w_book = 0.25       # emir defteri dengesizliği
        self.w_deriv = 0.20      # funding + basis (kontrarian)
        self.w_oi = 0.15         # açık pozisyon davranışı
        self.min_conviction = 0.04   # 0.50'den bu kadar sapma gerekir

    def get_tunables(self) -> dict:
        t = super().get_tunables()
        t.update({
            "w_flow": {"value": self.w_flow, "min": 0.0, "max": 0.7,
                       "desc": "CVD/agresif akış ağırlığı"},
            "w_book": {"value": self.w_book, "min": 0.0, "max": 0.5,
                       "desc": "emir defteri ağırlığı"},
            "w_deriv": {"value": self.w_deriv, "min": 0.0, "max": 0.5,
                        "desc": "funding/basis (kontrarian) ağırlığı"},
            "w_oi": {"value": self.w_oi, "min": 0.0, "max": 0.5,
                     "desc": "açık pozisyon ağırlığı"},
            "min_conviction": {"value": self.min_conviction, "min": 0.01, "max": 0.20,
                               "desc": "sinyal için 0.50'den min sapma"},
        })
        return t

    def generate_signal(self, snapshot: PolymarketSnapshot) -> Optional[Signal]:
        m = self.data.microstructure_snapshot()
        scores: list[float] = []   # her kanalın P(UP) tahmini
        weights: list[float] = []
        ind: dict = {}

        # --- 1) CVD / agresif akış ---
        cvd_r = m.get("cvd_ratio_60s")
        if cvd_r is not None:
            # -1..+1 -> 0.30..0.70 (akış ne kadar tek taraflıysa o kadar güçlü)
            scores.append(0.50 + max(-1.0, min(1.0, cvd_r)) * 0.20)
            weights.append(self.w_flow)
            ind["cvd_ratio_60s"] = round(cvd_r, 3)

        # --- 2) Emir defteri dengesizliği ---
        book = m.get("book_imbalance")
        if book is not None:
            scores.append(0.50 + max(-1.0, min(1.0, book)) * 0.15)
            weights.append(self.w_book)
            ind["book_imbalance"] = round(book, 3)

        # --- 3) Türev konumlanması (KONTRARİAN) ---
        # Funding tipik bant: ±0.01% (0.0001). Aşırı pozitif -> long kalabalık.
        funding = m.get("funding_rate")
        if funding is not None:
            f_norm = max(-1.0, min(1.0, funding / 0.0003))  # ±0.03% -> ±1
            scores.append(0.50 - f_norm * 0.10)             # ters yönlü
            weights.append(self.w_deriv)
            ind["funding_rate"] = round(funding, 6)
            basis = m.get("basis_pct")
            if basis is not None:
                ind["basis_pct"] = round(basis, 4)

        # --- 4) Açık pozisyon × fiyat davranışı ---
        oi_chg = m.get("oi_change_5m")
        mom5 = m.get("momentum_5m")
        if oi_chg is not None and mom5 is not None:
            if oi_chg > 0.05 and mom5 > 0:
                oi_score = 0.60      # yeni long'lar: trend sağlam
                oi_label = "yeni long (trend sağlam)"
            elif oi_chg > 0.05 and mom5 < 0:
                oi_score = 0.40      # yeni short'lar: aşağı sağlam
                oi_label = "yeni short (aşağı sağlam)"
            elif oi_chg < -0.05 and mom5 > 0:
                oi_score = 0.45      # short kapanışı: squeeze, sürdürülemez
                oi_label = "short squeeze (sürdürülemez)"
            elif oi_chg < -0.05 and mom5 < 0:
                oi_score = 0.55      # long kapanışı: düşüş yorulmuş
                oi_label = "long kapanışı (düşüş yorgun)"
            else:
                oi_score = 0.50
                oi_label = "nötr"
            scores.append(oi_score)
            weights.append(self.w_oi)
            ind["oi_change_5m"] = round(oi_chg, 4)
            ind["oi_regime"] = oi_label

        # --- Likidasyon akışı (varsa bonus kanıt) ---
        liq = m.get("liquidation_flow_5m")
        if liq is not None and abs(liq) > 100_000:
            scores.append(0.50 + (0.06 if liq > 0 else -0.06))
            weights.append(0.10)
            ind["liquidation_flow_5m"] = round(liq, 0)

        # En az 2 kanal veri vermeli
        if len(scores) < 2 or sum(weights) <= 0:
            return None

        p_up = sum(s * w for s, w in zip(scores, weights)) / sum(weights)
        p_up = max(0.05, min(0.95, p_up))
        ind["composite_p_up"] = round(p_up, 3)
        ind["channels_used"] = len(scores)

        conviction = abs(p_up - 0.50)
        if conviction < self.min_conviction:
            return None

        direction = "UP" if p_up > 0.50 else "DOWN"
        confidence = p_up if direction == "UP" else (1.0 - p_up)

        return Signal(
            direction=direction,
            confidence=confidence,
            reasoning=(
                f"Mikroyapı {len(scores)} kanal: P(UP)={p_up:.0%} "
                f"(CVD={ind.get('cvd_ratio_60s', 'n/a')}, "
                f"defter={ind.get('book_imbalance', 'n/a')}, "
                f"OI={ind.get('oi_regime', 'n/a')})"
            ),
            indicators=ind,
        )
