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
import time
import json
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal
from market_data import MarketDataService, PolymarketSnapshot
from paper_trader import PaperTrader
from config import GEMINI_API_KEY

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

        # If AI is enabled and we got a signal, ask the AI to review
        if self.ai_enabled and decision.get("action") == "TRADE":
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
- Balance: ${portfolio.balance:.2f if portfolio else 0}
- Win Rate: {portfolio.win_rate:.0f}% if portfolio else 'N/A'
- Total P&L: ${portfolio.total_pnl:+.2f if portfolio else 0}

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
                model="gemini-2.0-flash",
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

    def get_status(self) -> dict:
        """Return agent status for dashboard."""
        base_status = self.strategy.get_status()
        base_status["ai_enabled"] = self.ai_enabled
        base_status["ai_calls"] = self.total_ai_calls
        base_status["ai_overrides"] = self.ai_overrides
        return base_status
