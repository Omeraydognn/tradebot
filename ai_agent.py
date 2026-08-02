"""
AI Agent wrapper.
Each strategy is wrapped by an AI agent that can:
  1. Observe the strategy's signals and market conditions
  2. Override, adjust, or confirm the strategy's decisions
  3. Learn from trade outcomes over time
  4. Provide human-readable reasoning

Uses Gemini API for intelligence. Falls back to passthrough mode
if no API key is configured.
"""
import asyncio
import time
import json
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal
from market_data import MarketDataService, PolymarketSnapshot
from paper_trader import PaperTrader
from config import (
    GEMINI_API_KEY, GEMINI_MODEL, AI_MIN_INTERVAL_SEC, ADAPT_EVERY_N_TRADES,
    FORCE_TRADE_MODE, ALWAYS_TRADE_MODE,
)

# "Her pencereye gir" modu, zorunlu modu da kapsar: persona vetosu ve EV
# eşiği işlemi durduramaz. Tek fark: bu mod fiyat bandını da kaldırır.
_FORCED = FORCE_TRADE_MODE or ALWAYS_TRADE_MODE
from personas import get_persona, local_persona_judgment
from ai_budget import BUDGET

logger = logging.getLogger(__name__)

# Try to import google.genai
try:
    from google import genai
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False
    logger.warning("google-genai not installed. AI agents will run in passthrough mode.")


class AIAgent:
    """
    AI wrapper around a strategy.
    Enhances strategy decisions with LLM reasoning.
    """

    def __init__(self, strategy: BaseStrategy):
        self.strategy = strategy
        self.name = f"AI-{strategy.name}"
        self.ai_enabled = bool(GEMINI_API_KEY) and HAS_GENAI
        self.client = None
        self.decision_history: list[dict] = []
        self.total_ai_calls: int = 0
        self.ai_overrides: int = 0
        self._last_ai_call: float = 0.0  # kota-dostu cooldown için
        # Adaptasyon (outcome'lardan öğrenme)
        self.adaptations: list[dict] = []
        self._trades_at_last_adapt: int = 0

        # --- ZİHİN: kişilik + kendi yazdığı tez ---
        # Her ajan farklı bir ticaret felsefesine sahiptir; aynı veriye bakıp
        # farklı sonuçlara varırlar. `thesis` ajanın kendi deneyiminden
        # çıkardığı, zamanla güncellediği kişisel dersidir.
        self.persona = get_persona(strategy.name)
        self.thesis: str = ""              # AI kendi yazar (reflect ile güncellenir)
        self.thesis_updated_at: float = 0.0
        self.ai_initiated: int = 0          # AI'ın kendi başlattığı işlem sayısı
        self.reflections: list[dict] = []   # öz-değerlendirme geçmişi
        self._trades_at_last_reflect: int = 0

        if self.ai_enabled:
            try:
                self.client = genai.Client(api_key=GEMINI_API_KEY)
                logger.info(
                    f"🤖 AI Ajan hazır: '{strategy.name}' — "
                    f"kişilik: {self.persona['title']} (risk: {self.persona['risk']})"
                )
            except Exception as e:
                logger.error(f"Failed to init Gemini client: {e}")
                self.ai_enabled = False
        else:
            logger.info(f"🔧 '{strategy.name}' running in passthrough mode (no AI)")

    async def evaluate(self, snapshot: PolymarketSnapshot) -> Optional[dict]:
        """
        Run the strategy, optionally enhanced by AI.
        Returns a decision dict or None.
        """
        if not self.strategy.is_active:
            return None

        # 1) Mekanik strateji bir sinyal üretir (henüz işlem AÇILMAZ)
        signal = self.strategy.generate_signal(snapshot)
        if signal is None:
            return None

        # 2) YEREL PERSONA BEYNİ — her kararda çalışır, bedava, anlık.
        #    Kişilik bu işlemi kendi felsefesiyle değerlendirir; beğenmezse
        #    VETO eder. LLM kotası bittiğinde bile zihin çalışmaya devam eder.
        micro = self.strategy.data.microstructure_snapshot()
        market_prob = (snapshot.up_price if signal.direction == "UP"
                       else snapshot.down_price)
        delta, reason = local_persona_judgment(
            self.strategy.name, signal.direction, micro, market_prob
        )

        if delta is None:
            if not _FORCED:
                # Kişilik işlemi reddetti
                self.ai_overrides += 1
                self.strategy.last_skip_reason = f"{self.persona['title']}: {reason}"
                self.strategy.last_signal = signal
                logger.info(f"🚫 [{self.name}] {self.persona['title']} REDDETTİ: {reason}")
                veto = {
                    "timestamp": time.time(),
                    "strategy": self.strategy.name,
                    "direction": signal.direction,
                    "confidence": round(signal.confidence, 3),
                    "action": "PERSONA_VETO",
                    "reasoning": signal.reasoning,
                    "persona_reason": reason,
                    "trade": None,
                }
                self.decision_history.append(veto)
                if len(self.decision_history) > 50:
                    self.decision_history.pop(0)
                return veto
            # ZORUNLU MOD: kişilik itiraz ediyor ama veto işlemi durduramaz —
            # şüpheciliği güven kırılımına yansıt, karar normal hattan geçsin.
            delta = -0.08
            reason = f"(itiraz etti ama zorunlu modda izin veriyor) {reason}"
            logger.info(f"⚠️  [{self.name}] {self.persona['title']} itiraz etti (zorunlu): {reason}")

        # Kişilik onayladı — kendi görüşüyle güveni ayarlar
        if delta:
            signal.confidence = max(0.05, min(0.95, signal.confidence + delta))
            signal.reasoning = f"{signal.reasoning} | {self.persona['title']}: {reason}"

        # 3) İşlemi normal hattan geçir (koruma bantları + Kelly burada)
        decision = self.strategy._process_signal(signal, snapshot, ai_initiated=False)
        if decision is None:
            return None
        decision["persona_note"] = reason

        # 4) LLM incelemesi — SADECE kota varsa (günde ~20 istek sınırı).
        #    Kota yoksa sistem yerel beyinle sorunsuz devam eder.
        if (self.ai_enabled and decision.get("action") == "TRADE"
                and BUDGET.can_spend("general")
                and time.time() - self._last_ai_call >= AI_MIN_INTERVAL_SEC):
            self._last_ai_call = time.time()
            ai_review = await self._ai_review(decision, snapshot)
            if ai_review:
                decision["ai_review"] = ai_review

        self.decision_history.append(decision)
        if len(self.decision_history) > 50:
            self.decision_history.pop(0)

        return decision

    async def _ai_review(self, decision: dict, snapshot: PolymarketSnapshot) -> Optional[dict]:
        """Ask AI to review and potentially override a trading decision."""
        if not self.client:
            return None

        self.total_ai_calls += 1

        # Build context for the AI
        recent_outcomes = []
        portfolio = self.strategy.paper.portfolios.get(self.strategy.name)
        if portfolio:
            for t in portfolio.trades[-5:]:
                recent_outcomes.append(f"{t.side}: {t.result} (${t.pnl:+.2f})")

        # Portföy değerlerini önceden hesapla (f-string format spec'inde koşul olmaz)
        bal = portfolio.balance if portfolio else 0.0
        wr = portfolio.win_rate if portfolio else 0.0
        pnl = portfolio.total_pnl if portfolio else 0.0

        micro = self.strategy.data.microstructure_snapshot()
        micro_txt = "\n".join(
            f"- {k}: {v:.5f}" if isinstance(v, (int, float)) else f"- {k}: {v}"
            for k, v in micro.items() if v is not None
        )
        p = self.persona

        prompt = f"""Sen bir Polymarket "BTC 5 dakikada yukarı mı aşağı mı" piyasasında
işlem yapan bağımsız bir trader'sın. Bir bot değil, KENDİ GÖRÜŞÜ OLAN bir zihinsin.

SENİN KİMLİĞİN: {p['title']}
{p['philosophy']}
Risk iştahın: {p['risk']}
Eğilimin: {p['override_bias']}
Odaklandığın sinyaller: {p['edge_focus']}

SENİN KENDİ TEZİN (geçmiş deneyimlerinden çıkardığın ders):
{self.thesis or '(henüz yeterli deneyim yok — ilk izlenimlerinle karar ver)'}

PİYASA DURUMU:
- BTC: ${self.strategy.data.latest_btc_price:,.2f}
- UP hissesi: ${snapshot.up_price:.2f} | DOWN hissesi: ${snapshot.down_price:.2f}
  (aldığın hisse doğru çıkarsa $1.00 öder; yani $0.40'a alıp kazanırsan $0.60 kâr)

MİKROYAPI VERİSİ:
{micro_txt or '(veri yok)'}

MEKANİK STRATEJİNİN ÖNERİSİ ({self.strategy.name}):
- Yön: {decision['direction']}
- Güven: {decision['confidence']:.0%}
- Beklenen değer (EV): ${decision['ev']:+.4f}
- Gerekçe: {decision['reasoning']}

SENİN PERFORMANSIN:
- Bakiye: ${bal:.2f} | Kazanma oranı: {wr:.0f}% | Toplam P&L: ${pnl:+.2f}
- Son işlemler: {' | '.join(recent_outcomes) if recent_outcomes else 'henüz yok'}

Mekanik strateji sadece bir ÖNERİDİR. Sen kendi kimliğin, tezin ve piyasa
verisiyle KENDİ kararını ver. Katılmıyorsan reddet. Kimliğine uygun bir
fırsat görüyorsan güveni artır.

SADECE JSON döndür:
{{"approve": true/false, "confidence_adjustment": -0.15..+0.15, "reasoning": "tek cümle, Türkçe, kendi sesinle"}}"""

        review = await self._llm_json(prompt, purpose="general")
        if not review:
            return None

        if not review.get("approve", True):
            self.ai_overrides += 1
            logger.info(
                f"🤖 [{self.name}] AI itiraz etti: {review.get('reasoning', 'N/A')}"
            )
        return review

    async def _llm_json(self, prompt: str, purpose: str = "general") -> Optional[dict]:
        """
        Tek LLM çağrı noktası: bütçe kontrolü + 429 yakalama + JSON ayrıştırma.

        Ücretsiz katman günde ~20 istek verdiği için her çağrı bütçeden
        geçer. Kota dolarsa (429) tüm ajanlar için bir süre çağrı durdurulur
        ve sistem yerel persona beyniyle çalışmaya devam eder.
        """
        if not self.client or not BUDGET.can_spend(purpose):
            return None

        # Kotası dolan model olursa sıradakine geç (her modelin kotası ayrı)
        for _ in range(len(BUDGET.models)):
            model = BUDGET.current_model()
            if model is None:
                return None
            try:
                BUDGET.spend(model)
                self.total_ai_calls += 1
                response = await asyncio.to_thread(
                    self.client.models.generate_content,
                    model=model,
                    contents=prompt,
                )
                text = (response.text or "").strip()
                if "```json" in text:
                    text = text.split("```json")[1].split("```")[0].strip()
                elif "```" in text:
                    text = text.split("```")[1].split("```")[0].strip()
                return json.loads(text)
            except Exception as e:
                msg = str(e)
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    BUDGET.mark_model_exhausted(model)
                    continue  # sıradaki modeli dene
                logger.debug(f"LLM çağrısı başarısız ({purpose}, {model}): {e}")
                return None
        return None

    def force_local_signal(self, snapshot: PolymarketSnapshot) -> Optional[dict]:
        """
        ZORUNLU MOD YEDEĞİ: mekanik strateji bu pencerede hiç sinyal
        üretmediyse (generate_signal None döndüyse), LLM'e ihtiyaç duymadan
        mikroyapının ham yönünden (momentum) basit bir sinyal üretip normal
        işlem hattından geçirir. Koruma bantları (fiyat, zaman, tek pozisyon)
        burada da aynen geçerlidir — sadece "hiç bakmama" durumunu ortadan
        kaldırır.
        """
        if not _FORCED or not self.strategy.is_active:
            return None
        if self.strategy.has_position_in_window(snapshot.window_start):
            return None

        micro = self.strategy.data.microstructure_snapshot()
        mom = micro.get("momentum_5m")
        if mom is None:
            mom = micro.get("momentum_1m") or 0.0
        direction = "UP" if mom >= 0 else "DOWN"

        decision = self.strategy.execute_ai_signal(
            snapshot, direction, 0.55,
            "🔒 Zorunlu işlem: mekanik strateji sessizdi, mikroyapı yönü kullanıldı",
        )
        if decision and decision.get("action") == "TRADE":
            self.ai_initiated += 1
        return decision

    async def ai_initiate(self, snapshot: PolymarketSnapshot) -> Optional[dict]:
        """
        AI'IN KENDİ İNİSİYATİFİ.

        Mekanik strateji sessiz kaldığında bile, ajan piyasaya kendi
        kimliğiyle bakar ve bir FIRSAT görürse işlemi kendisi başlatır.
        Kullanıcının istediği "AI fırsatı gördüğünde alışılmışın dışına
        çıkar" davranışı burada gerçekleşir.

        Güvenlik: aynı koruma bantları (fiyat aralığı, pencere sonu, tek
        pozisyon) burada da geçerlidir — AI bunları AŞAMAZ.
        """
        if not self.ai_enabled or not self.client:
            return None
        if not self.strategy.is_active:
            return None
        # Zaten bu pencerede pozisyon varsa yeni fırsat aramaya gerek yok
        if self.strategy.has_position_in_window(snapshot.window_start):
            return None

        micro = self.strategy.data.microstructure_snapshot()
        micro_txt = "\n".join(
            f"- {k}: {v:.5f}" if isinstance(v, (int, float)) else f"- {k}: {v}"
            for k, v in micro.items() if v is not None
        )
        portfolio = self.strategy.paper.portfolios.get(self.strategy.name)
        wr = portfolio.win_rate if portfolio else 0.0
        pnl = portfolio.total_pnl if portfolio else 0.0
        p = self.persona

        prompt = f"""Sen "{p['title']}" kimliğine sahip bağımsız bir trader'sın.
{p['philosophy']}

SENİN TEZİN:
{self.thesis or '(henüz deneyim birikmedi)'}

Şu an mekanik stratejin SESSİZ (sinyal üretmedi). Ama sen kendi gözünle
bakıyorsun: burada senin kimliğine uygun bir FIRSAT var mı?

PİYASA:
- BTC: ${self.strategy.data.latest_btc_price:,.2f}
- UP: ${snapshot.up_price:.2f} | DOWN: ${snapshot.down_price:.2f}

MİKROYAPI:
{micro_txt or '(veri yok)'}

PERFORMANSIN: kazanma {wr:.0f}%, P&L ${pnl:+.2f}

ÖNEMLİ: İşlem yapmamak da bir karardır ve çoğu zaman DOĞRU karardır.
Sadece kimliğine gerçekten uyan, net bir fırsat varsa işlem aç.
Kararsızsan veya sinyal zayıfsa "trade": false de.

SADECE JSON:
{{"trade": true/false, "direction": "UP" veya "DOWN", "confidence": 0.50-0.75,
  "reasoning": "tek cümle Türkçe gerekçe"}}"""

        try:
            d = await self._llm_json(prompt, purpose="general")
            if not d:
                return None

            if not d.get("trade"):
                return None
            direction = d.get("direction")
            if direction not in ("UP", "DOWN"):
                return None
            conf = float(d.get("confidence", 0.55))
            conf = max(0.50, min(0.75, conf))
            reasoning = d.get("reasoning", "AI inisiyatifi")

            # Stratejinin normal işlem hattından geçir — koruma bantları korunur
            decision = self.strategy.execute_ai_signal(
                snapshot, direction, conf, f"🤖 AI inisiyatifi: {reasoning}"
            )
            if decision and decision.get("action") == "TRADE":
                self.ai_initiated += 1
                logger.info(
                    f"💡 [{self.name}] AI KENDİ BAŞLATTI: {direction} "
                    f"(güven {conf:.0%}) — {reasoning}"
                )
            return decision

        except Exception as e:
            logger.debug(f"AI inisiyatif başarısız: {e}")
            return None

    async def reflect(self, leaderboard: Optional[list] = None):
        """
        ÖZ-DEĞERLENDİRME: Ajan kendi işlem geçmişine bakıp KENDİ TEZİNİ yazar.

        Bu, sistemin gerçek öğrenme döngüsüdür: ajan neyin işe yarayıp
        yaramadığını doğal dille kendi kelimeleriyle çıkarır ve bu tez
        sonraki tüm kararlarına girdi olur. Ayrıca liderlik tablosunu
        görür — rakiplerinden ders çıkarabilir (rekabet).
        """
        if not self.ai_enabled or not self.client:
            return
        portfolio = self.strategy.paper.portfolios.get(self.strategy.name)
        if not portfolio or portfolio.total_trades < 5:
            return

        trades = portfolio.trades[-15:]
        hist = "\n".join(
            f"- {t.side} @{t.entry_price:.2f} ${t.amount:.0f} -> {t.result} (${(t.pnl or 0):+.2f})"
            for t in trades
        )
        cal = self.strategy.calibrator.to_dict()
        lb_txt = ""
        if leaderboard:
            lb_txt = "\n".join(
                f"- {r['name']}: P&L ${r['total_pnl']:+.2f}, kazanma {r['win_rate']:.0f}%"
                for r in leaderboard[:5]
            )
        p = self.persona

        prompt = f"""Sen "{p['title']}" kimliğine sahip bir trader'sın.
{p['philosophy']}

Şimdi kendi performansını dürüstçe değerlendireceksin.

İŞLEM GEÇMİŞİN (son {len(trades)}):
{hist}

GENEL: {portfolio.total_trades} işlem, kazanma {portfolio.win_rate:.0f}%, P&L ${portfolio.total_pnl:+.2f}

KALİBRASYON (dürüstlük ölçümü):
- Ortalama tahmin ettiğin güven: {cal.get('mean_predicted')}
- Gerçek isabet oranın: {cal.get('empirical_accuracy')}
  (tahminin gerçekten yüksekse FAZLA İYİMSERSİN — daha temkinli ol)

RAKİPLERİN (aynı piyasada yarışan diğer zihinler):
{lb_txt or '(veri yok)'}

Görev: Kendi deneyiminden bir DERS çıkar ve tezini yaz. Bu tez sonraki
kararlarında sana rehber olacak. Somut ol — "daha dikkatli olacağım" gibi
boş laf değil, "X koşulunda işlem açmam çünkü Y" gibi işe yarar bir kural.
Kaybediyorsan bunu kabul et ve yaklaşımını değiştir. Rakibin daha iyiyse
ondan ne öğrenebileceğini düşün ama kendi kimliğini terk etme.

SADECE JSON:
{{"thesis": "2-3 cümle, Türkçe, kendi sesinle yazılmış kişisel tezin",
  "key_lesson": "tek cümle en önemli ders"}}"""

        try:
            # Öz-değerlendirme kotanın ayrılmış payını kullanır (en değerli iş)
            d = await self._llm_json(prompt, purpose="reflection")
            if not d:
                return

            new_thesis = (d.get("thesis") or "").strip()
            if new_thesis:
                self.thesis = new_thesis
                self.thesis_updated_at = time.time()
                self.reflections.append({
                    "time": time.time(),
                    "thesis": new_thesis,
                    "lesson": d.get("key_lesson", ""),
                    "trades_at": portfolio.total_trades,
                    "pnl_at": round(portfolio.total_pnl, 2),
                })
                if len(self.reflections) > 10:
                    self.reflections.pop(0)
                logger.info(f"📝 [{self.name}] tezini güncelledi: {new_thesis[:110]}")

        except Exception as e:
            logger.debug(f"Reflect başarısız: {e}")

    def maybe_reflect(self, leaderboard=None):
        """Yeterli yeni işlem biriktiyse öz-değerlendirme gerekir mi?"""
        portfolio = self.strategy.paper.portfolios.get(self.strategy.name)
        if not portfolio:
            return False
        return portfolio.total_trades - self._trades_at_last_reflect >= 8

    def maybe_adapt(self):
        """Yeterli sayıda yeni işlem sonuçlandıysa stratejiyi yeniden ayarlar."""
        port = self.strategy.paper.portfolios.get(self.strategy.name)
        if not port:
            return
        if port.total_trades - self._trades_at_last_adapt >= ADAPT_EVERY_N_TRADES:
            self._trades_at_last_adapt = port.total_trades
            self.adapt()

    def adapt(self):
        """
        Kural tabanlı hızlı adaptasyon (her zaman çalışır, ücretsiz, LLM'siz):
          - Kazanıyorsa -> daha AGRESİF (bet↑, minEV↓, sinyal eşikleri gevşer)
          - Kaybediyorsa -> daha SEÇİCİ (bet↓, minEV↑, sinyal eşikleri sıkılaşır)

        ÖNEMLİ: sadece bet_size/min_ev değil, stratejinin KENDİ sinyal
        üretim eşikleri de (RSI bandı, VWAP sapması, Bollinger genişliği,
        emir defteri dengesizliği, min conviction, arb edge — get_tunables()
        içinde "selectivity": True ile işaretli olanlar) buradan ayarlanır.
        Böylece hatalardan gerçekten ders çıkarılır: strateji sürekli
        kaybediyorsa mekaniği daha seçici hale gelir, kazanıyorsa daha çok
        fırsat yakalamak için gevşer. AI açıksa ayrıca `ai_tune()` ile
        LLM'in kendi muhakemesiyle daha ince ayar denenir.
        """
        port = self.strategy.paper.portfolios.get(self.strategy.name)
        if not port or port.total_trades < 1:
            return

        recent = port.trades[-6:]
        wins = sum(1 for t in recent if t.result == "WIN")
        recent_wr = wins / len(recent) if recent else 0.0
        recent_pnl = sum((t.pnl or 0.0) for t in recent)

        old_bet, old_ev = self.strategy.bet_size, self.strategy.min_ev
        winning = recent_wr >= 0.55 and recent_pnl > 0
        losing = recent_wr < 0.40 or recent_pnl < 0

        if winning:
            self.strategy.bet_size = min(self.strategy.bet_size * 1.25, 50.0)
            self.strategy.min_ev = max(self.strategy.min_ev - 0.01, 0.0)
            mode = "AGRESİF"
        elif losing:
            self.strategy.bet_size = max(self.strategy.bet_size * 0.70, 2.0)
            self.strategy.min_ev = min(self.strategy.min_ev + 0.02, 0.15)
            mode = "SEÇİCİ"
        else:
            mode = "STABİL"

        # --- Stratejinin kendi sinyal eşiklerini ayarla (gerçek "hatadan ders") ---
        selectivity_note = ""
        if winning or losing:
            tun = self.strategy.get_tunables()
            changes = {}
            for key, spec in tun.items():
                if key in ("bet_size", "min_ev", "kelly_fraction") or not spec.get("selectivity"):
                    continue
                lo, hi = spec["min"], spec["max"]
                step = (hi - lo) * 0.08
                new_val = spec["value"] - step if winning else spec["value"] + step
                changes[key] = max(lo, min(hi, new_val))
            if changes:
                before = {k: tun[k]["value"] for k in changes}
                self.strategy.set_tunables(changes)
                selectivity_note = " | eşikler: " + ", ".join(
                    f"{k} {before[k]:.4f}→{v:.4f}" for k, v in changes.items()
                )

        note = (
            f"{mode}: son {len(recent)} işlem WR={recent_wr:.0%}, PnL=${recent_pnl:+.2f} → "
            f"bet ${old_bet:.0f}→${self.strategy.bet_size:.0f}, "
            f"minEV {old_ev:.2f}→{self.strategy.min_ev:.2f}{selectivity_note}"
        )
        self.adaptations.append({"time": time.time(), "mode": mode, "note": note, "by": "kural"})
        if len(self.adaptations) > 20:
            self.adaptations.pop(0)
        logger.info(f"🧠 [{self.name}] ADAPT {note}")

    async def ai_tune(self):
        """
        DERİN ADAPTASYON: Gemini, stratejinin işlem geçmişini + mikroyapı
        bağlamını inceleyip STRATEJİYE ÖZEL eşikleri (RSI seviyeleri, kanal
        ağırlıkları, dengesizlik eşiği vb.) gerekçeyle değiştirir.

        Model yalnızca `get_tunables()` ile açılan parametreleri, kendi
        min/max sınırları içinde değiştirebilir — güvenli.
        """
        if not self.ai_enabled or not self.client:
            return
        port = self.strategy.paper.portfolios.get(self.strategy.name)
        if not port or port.total_trades < 4:
            return

        tun = self.strategy.get_tunables()
        recent = port.trades[-10:]
        hist = "\n".join(
            f"- {t.side} @{t.entry_price:.2f} ${t.amount:.0f} -> {t.result} (${(t.pnl or 0):+.2f})"
            for t in recent
        )
        micro = self.strategy.data.microstructure_snapshot()
        micro_txt = "\n".join(
            f"- {k}: {v:.5f}" if isinstance(v, (int, float)) else f"- {k}: {v}"
            for k, v in micro.items() if v is not None
        )
        params_txt = "\n".join(
            f'- {k}: mevcut={v["value"]:.4f}, izin={v["min"]}..{v["max"]} ({v["desc"]})'
            for k, v in tun.items()
        )

        prompt = f"""Sen bir kantitatif trading stratejisini optimize eden AI'sın.
Strateji: {self.strategy.name}
Piyasa: Polymarket "BTC 5 dakikada yukarı mı aşağı mı" (binary, $1 ödeme).

PERFORMANS:
- Toplam işlem: {port.total_trades}, kazanma oranı: {port.win_rate:.0f}%
- Toplam P&L: ${port.total_pnl:+.2f} (başlangıç ${port.balance + abs(port.total_pnl):.0f})

SON İŞLEMLER:
{hist or 'yok'}

ANLIK PİYASA MİKROYAPISI:
{micro_txt or 'yok'}

AYARLAYABİLECEĞİN PARAMETRELER:
{params_txt}

Görev: Kazanma oranını ve P&L'i artırmak için parametreleri ayarla.
Kurallar:
- Sadece yukarıdaki parametreleri, izin verilen aralıkta değiştir.
- Kaybediyorsa daha seçici ol (daha yüksek eşik, daha düşük bahis).
- Kazanıyorsa dikkatli şekilde daha agresif ol.
- Değişiklik gereksizse boş bırak.

SADECE JSON döndür:
{{"changes": {{"param_adi": deger}}, "reasoning": "tek cümle Türkçe gerekçe"}}"""

        try:
            data = await self._llm_json(prompt, purpose="reflection")
            if not data:
                return
            changes = data.get("changes") or {}
            if not changes:
                return

            before = {k: v["value"] for k, v in tun.items()}
            self.strategy.set_tunables(changes)
            after = {k: v["value"] for k, v in self.strategy.get_tunables().items()}
            diff = {k: (before[k], after[k]) for k in after if abs(after[k] - before.get(k, 0)) > 1e-9}
            if not diff:
                return

            note = "AI ayarı: " + ", ".join(f"{k} {a:.3f}→{b:.3f}" for k, (a, b) in diff.items())
            reason = data.get("reasoning", "")
            self.adaptations.append({
                "time": time.time(), "mode": "AI", "note": f"{note} — {reason}", "by": "gemini",
            })
            if len(self.adaptations) > 20:
                self.adaptations.pop(0)
            logger.info(f"🤖 [{self.name}] AI-TUNE {note} | {reason}")

        except Exception as e:
            logger.debug(f"AI tune başarısız: {e}")

    def get_status(self) -> dict:
        """Return agent status for dashboard."""
        base_status = self.strategy.get_status()
        base_status["ai_enabled"] = self.ai_enabled
        base_status["ai_calls"] = self.total_ai_calls
        base_status["ai_overrides"] = self.ai_overrides
        base_status["adaptations"] = self.adaptations[-5:]
        base_status["adaptation_count"] = len(self.adaptations)
        # Zihin: kişilik + kendi yazdığı tez (dashboard'da görünür)
        base_status["persona"] = {
            "title": self.persona.get("title"),
            "risk": self.persona.get("risk"),
        }
        base_status["thesis"] = self.thesis
        base_status["ai_initiated"] = self.ai_initiated
        base_status["reflections"] = self.reflections[-3:]
        return base_status
