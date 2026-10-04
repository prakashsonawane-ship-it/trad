"""
Context Builder — Assembles the final JSON context for the Claude agent.

Target: under 4K tokens. Pulls from all streams + macro + memory.
Output is the exact JSON that gets sent to the agent.
"""

import json
import logging
from datetime import datetime
from typing import Optional

from data.state import MarketState
from news.news_bias import get_bias_instruction
import config

logger = logging.getLogger(__name__)


class ContextBuilder:
    """
    Builds the JSON context packet for agent calls.

    Keeps context lean (~3-4K tokens) for cost and speed.
    """

    def __init__(self, state: MarketState):
        self._state = state
        logger.info("ContextBuilder initialized")

    def build(
        self,
        index: str,
        triggers: list[dict],
        scenario: dict,
        confluence: dict,
        memory: list[dict],
    ) -> dict:
        """
        Assemble final context JSON for the agent.

        Args:
            index: "NIFTY" or "SENSEX"
            triggers: Active triggers
            scenario: Classified scenario
            confluence: Confluence score output
            memory: Last N decisions from memory

        Returns:
            Dict ready for JSON serialization to agent prompt.
        """
        indicators = self._state.get("indicators", index) or {}
        structure = self._state.get("analysis", "structure") or {}
        flow = self._state.get("analysis", "flow") or {}
        sentiment = self._state.get("analysis", "sentiment") or {}
        macro = self._state.macro_context

        # Get index data
        idx_data = self._state.get("index", index) or {}
        ltp = idx_data.get("ltp", 0)

        # Build lean context
        context = {
            "macro_context": self._build_macro_section(macro),
            "market_state": {
                "index": index,
                "ltp": round(ltp, 2),
                "day_high": round(idx_data.get("high", 0), 2),
                "day_low": round(idx_data.get("low", 0), 2),
                "day_open": round(idx_data.get("open", 0), 2),
                "time": datetime.now().strftime("%H:%M"),
                "candle_count": indicators.get("candle_count", 0),
            },
            "analysis": {
                "structure": self._build_structure_section(structure, indicators),
                "flow": self._build_flow_section(flow),
                "sentiment": self._build_sentiment_section(sentiment),
            },
            "scenario": {
                "type": scenario.get("type", "GENERIC"),
                "description": scenario.get("description", ""),
                "confluence_score": confluence.get("total_score", 0),
                "stream_scores": confluence.get("stream_scores", {}),
                "key_factors": scenario.get("key_factors", []),
            },
            "triggers": [
                {
                    "type": t["type"],
                    "direction": t["direction"],
                    "strength": t["strength"],
                    "detail": t["detail"],
                }
                for t in triggers[:5]  # Cap at 5 most relevant
            ],
            "memory": self._build_memory_section(memory),
            "constraints": {
                "lot_size": config.NIFTY_LOT if index == "NIFTY" else config.SENSEX_LOT,
                "max_premium": config.NIFTY_MAX_PREMIUM if index == "NIFTY" else config.SENSEX_MAX_PREMIUM,
                "max_trade_cost": config.MAX_TRADE_COST,
                "positions_open": len(self._state.positions),
                "max_positions": config.MAX_OPEN_POSITIONS,
                "daily_pnl": round(self._state.daily_pnl, 2),
                "daily_loss_limit": config.DAILY_LOSS_LIMIT,
            },
            "question": self._build_question(index, scenario, confluence),
        }

        # Log token estimate
        json_str = json.dumps(context)
        token_est = len(json_str) // 4  # Rough ~4 chars per token
        logger.info(f"Context built: ~{token_est} tokens ({len(json_str)} chars)")

        if token_est > config.AGENT_MAX_CONTEXT_TOKENS:
            logger.warning(
                f"Context exceeds target ({token_est} > {config.AGENT_MAX_CONTEXT_TOKENS}). "
                "Consider trimming."
            )

        return context

    # ─── Section Builders ──────────────────────────────────────────

    @staticmethod
    def _build_macro_section(macro: dict) -> dict:
        """Extract key macro fields — keep it lean."""
        return {
            "bias": macro.get("agent_bias_note", "Normal conditions"),
            "global_sentiment": macro.get("global_sentiment", "NEUTRAL"),
            "sgx_gap": macro.get("sgx_nifty_gap", "0.0%"),
            "events": macro.get("calendar_events", []),
            "impact": macro.get("impact_level", "LOW"),
        }

    @staticmethod
    def _build_structure_section(structure: dict, indicators: dict) -> dict:
        """Build lean structure summary."""
        return {
            "phase": structure.get("phase", "UNKNOWN"),
            "nearest_swing_high": structure.get("nearest_swing_high"),
            "nearest_swing_low": structure.get("nearest_swing_low"),
            "pdh": structure.get("pdh", 0),
            "pdl": structure.get("pdl", 0),
            "volume_poc": structure.get("volume_profile", {}).get("poc", 0),
            "ema_alignment": indicators.get("ema_alignment", "UNKNOWN"),
            "ema_9": indicators.get("ema_9", 0),
            "ema_21": indicators.get("ema_21", 0),
            "rsi": indicators.get("rsi", 50),
            "rsi_zone": indicators.get("rsi_zone", "NEUTRAL"),
            "macd_cross": indicators.get("macd_cross", "NONE"),
            "vwap": indicators.get("vwap", 0),
            "vwap_position": indicators.get("vwap_position", "UNKNOWN"),
            "bb_position": indicators.get("bb_position", "UNKNOWN"),
            "atr": indicators.get("atr", 0),
        }

    @staticmethod
    def _build_flow_section(flow: dict) -> dict:
        """Build lean flow summary."""
        return {
            "delta_current": flow.get("delta_current", 0),
            "delta_2m": flow.get("delta_2m", 0),
            "cumulative_delta": flow.get("cumulative_delta", 0),
            "delta_trend": flow.get("delta_trend", "UNKNOWN"),
            "stop_hunt": bool(flow.get("stop_hunt")),
            "volume_spike": bool(flow.get("volume_spike")),
            "buy_pressure_pct": flow.get("buy_pressure_pct", 50),
            "note": "Stream B weight=0.8x (tick-based approximation)",
        }

    @staticmethod
    def _build_sentiment_section(sentiment: dict) -> dict:
        """Build lean sentiment summary."""
        return {
            "pcr": sentiment.get("pcr", 1.0),
            "pcr_trend": sentiment.get("pcr_trend", "UNKNOWN"),
            "max_pain": sentiment.get("max_pain", 0),
            "max_pain_distance": sentiment.get("max_pain_distance", 0),
            "atm_iv": sentiment.get("atm_iv", 0),
            "iv_percentile": sentiment.get("iv_percentile", 50),
            "highest_ce_oi_strike": sentiment.get("highest_ce_oi_strike"),
            "highest_pe_oi_strike": sentiment.get("highest_pe_oi_strike"),
        }

    @staticmethod
    def _build_memory_section(memory: list[dict]) -> list[dict]:
        """Build memory section — last 5 decisions with outcomes."""
        return [
            {
                "time": m.get("time", ""),
                "decision": m.get("decision", ""),
                "confidence": m.get("confidence", 0),
                "outcome": m.get("outcome", "pending"),
                "pnl": m.get("pnl", 0),
            }
            for m in memory[-5:]
        ]

    @staticmethod
    def _build_question(index: str, scenario: dict, confluence: dict) -> str:
        """Build the specific question for the agent based on scenario type."""
        scenario_type = scenario.get("type", "GENERIC")
        score = confluence.get("total_score", 0)
        direction = confluence.get("direction", "unknown")

        questions = {
            "LIQUIDITY_TRAP": (
                f"A liquidity trap / stop-hunt has been detected on {index} expiry. "
                f"Confluence score: {score}/9. Indicated direction: {direction}. "
                "Should we trade the reversal? If yes, what's your entry zone and invalidation?"
            ),
            "BREAKOUT": (
                f"A trend breakout setup is forming on {index} expiry. "
                f"Confluence score: {score}/9. Direction: {direction}. "
                "Is this a genuine breakout or a false move? Entry zone and targets?"
            ),
            "MEAN_REVERSION": (
                f"A mean-reversion setup is forming on {index} expiry. "
                f"Confluence score: {score}/9. Direction: {direction}. "
                "Is the extreme overextended enough for a snap-back? Entry zone?"
            ),
            "MAX_PAIN_DRIFT": (
                f"Price is away from Max Pain on {index} expiry day. "
                f"Confluence score: {score}/9. Expected drift: {direction}. "
                "Should we trade the expected drift to Max Pain? Timing and targets?"
            ),
            "OPENING_RANGE_BREAK": (
                f"Opening range breakout detected on {index} expiry. "
                f"Confluence score: {score}/9. Direction: {direction}. "
                "Is this a valid ORB with conviction, or a fake-out? Entry?"
            ),
            "GENERIC": (
                f"Multiple signals are active on {index} expiry. "
                f"Confluence score: {score}/9. Dominant direction: {direction}. "
                "Evaluate the setup and recommend a trade or WAIT."
            ),
        }

        return questions.get(scenario_type, questions["GENERIC"])
