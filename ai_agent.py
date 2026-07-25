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
from config import GEMINI_API_KEY, GEMINI_MODEL, AI_MIN_INTERVAL_SEC, ADAPT_EVERY_N_TRADES

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

        if self.ai_enabled:
            try:
                self.client = genai.Client(api_key=GEMINI_API_KEY)
                logger.info(f"🤖 AI Agent initialized for '{strategy.name}'")
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

        # First, get the base strategy's decision
        decision = self.strategy.evaluate_and_trade(snapshot)

        if decision is None:
            return None

        # If AI is enabled and we got a signal, ask the AI to review.
        # Kota dostu: ajan başına en az AI_MIN_INTERVAL_SEC aralıkla çağır.
        if self.ai_enabled and decision.get("action") == "TRADE":
            if time.time() - self._last_ai_call >= AI_MIN_INTERVAL_SEC:
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

        prompt = f"""You are an AI trading advisor for a Polymarket BTC 5-minute prediction market.

CURRENT MARKET STATE:
- BTC Price: ${self.strategy.data.latest_btc_price:,.2f}
- Polymarket UP share price: ${snapshot.up_price:.2f} (buy UP for ${snapshot.up_price:.2f}, win $1.00 if correct)
- Polymarket DOWN share price: ${snapshot.down_price:.2f} (buy DOWN for ${snapshot.down_price:.2f}, win $1.00 if correct)

STRATEGY SIGNAL ({self.strategy.name}):
- Direction: {decision['direction']}
- Confidence: {decision['confidence']:.0%}
- Expected Value: ${decision['ev']:+.4f}
- Reasoning: {decision['reasoning']}

RECENT TRADE OUTCOMES:
{chr(10).join(recent_outcomes) if recent_outcomes else 'No trades yet'}

PORTFOLIO:
- Balance: ${bal:.2f}
- Win Rate: {wr:.0f}%
- Total P&L: ${pnl:+.2f}

Should this trade be executed? Consider:
1. Is the expected value genuinely positive after fees?
2. Does the recent trade history suggest this strategy is working?
3. Is the share price favorable (risk/reward ratio)?

Respond in JSON format:
{{"approve": true/false, "confidence_adjustment": 0.0, "reasoning": "brief explanation"}}"""

        try:
            import asyncio
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=GEMINI_MODEL,
                contents=prompt,
            )
            text = response.text.strip()
            # Try to parse JSON from response
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()

            review = json.loads(text)

            if not review.get("approve", True):
                self.ai_overrides += 1
                logger.info(
                    f"🤖 [{self.name}] AI OVERRIDE: Blocked trade. "
                    f"Reason: {review.get('reasoning', 'N/A')}"
                )

            return review

        except Exception as e:
            logger.debug(f"AI review failed: {e}")
            return None

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
        Kural tabanlı hızlı adaptasyon (her zaman çalışır, ücretsiz):
          - Kazanıyorsa -> daha AGRESİF (bet↑, minEV↓)
          - Kaybediyorsa -> daha SEÇİCİ (bet↓, minEV↑)
        AI açıksa ayrıca `ai_tune()` ile derin parametre ayarı denenir.
        """
        port = self.strategy.paper.portfolios.get(self.strategy.name)
        if not port or port.total_trades < 1:
            return

        recent = port.trades[-6:]
        wins = sum(1 for t in recent if t.result == "WIN")
        recent_wr = wins / len(recent) if recent else 0.0
        recent_pnl = sum((t.pnl or 0.0) for t in recent)

        old_bet, old_ev = self.strategy.bet_size, self.strategy.min_ev

        if recent_wr >= 0.55 and recent_pnl > 0:
            self.strategy.bet_size = min(self.strategy.bet_size * 1.25, 50.0)
            self.strategy.min_ev = max(self.strategy.min_ev - 0.01, 0.0)
            mode = "AGRESİF"
        elif recent_wr < 0.40 or recent_pnl < 0:
            self.strategy.bet_size = max(self.strategy.bet_size * 0.70, 2.0)
            self.strategy.min_ev = min(self.strategy.min_ev + 0.02, 0.15)
            mode = "SEÇİCİ"
        else:
            mode = "STABİL"

        note = (
            f"{mode}: son {len(recent)} işlem WR={recent_wr:.0%}, PnL=${recent_pnl:+.2f} → "
            f"bet ${old_bet:.0f}→${self.strategy.bet_size:.0f}, "
            f"minEV {old_ev:.2f}→{self.strategy.min_ev:.2f}"
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
            self.total_ai_calls += 1
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=GEMINI_MODEL,
                contents=prompt,
            )
            text = (response.text or "").strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()

            data = json.loads(text)
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
        return base_status
