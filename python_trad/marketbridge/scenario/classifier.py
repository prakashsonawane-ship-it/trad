"""
Scenario Classifier — Maps triggers + stream signals to scenario types.

Scenario types:
- LIQUIDITY_TRAP: Stop-hunt detected + delta divergence
- BREAKOUT: Opening range break + volume spike + EMA alignment
- MEAN_REVERSION: RSI extreme + price at BB band + phase RANGING
- MAX_PAIN_DRIFT: Afternoon + price far from max pain + PCR supports
- OPENING_RANGE_BREAK: OR breakout/breakdown with volume confirmation
"""

import logging
from datetime import datetime, time
from typing import Optional

from data.state import MarketState

logger = logging.getLogger(__name__)

# Scenario types
LIQUIDITY_TRAP = "LIQUIDITY_TRAP"
BREAKOUT = "BREAKOUT"
MEAN_REVERSION = "MEAN_REVERSION"
MAX_PAIN_DRIFT = "MAX_PAIN_DRIFT"
OPENING_RANGE_BREAK = "OPENING_RANGE_BREAK"
GENERIC = "GENERIC"


class ScenarioClassifier:
    """
    Maps trigger combinations + analysis signals to scenario types.

    Each scenario type has a specific question template for the agent.
    """

    def __init__(self, state: MarketState):
        self._state = state
        logger.info("ScenarioClassifier initialized")

    def classify(self, triggers: list[dict], direction: str) -> dict:
        """
        Classify the current setup into a scenario type.

        Args:
            triggers: Active triggers from TriggerEngine
            direction: Primary direction

        Returns:
            {"type": str, "description": str, "key_factors": list}
        """
        trigger_types = {t["type"] for t in triggers}
        flow = self._state.get("analysis", "flow") or {}
        structure = self._state.get("analysis", "structure") or {}
        sentiment = self._state.get("analysis", "sentiment") or {}
        indicators = self._state.get("indicators", self._state.today_index) or {}

        # ── LIQUIDITY_TRAP ─────────────────────────────────────────
        has_stop_hunt = flow.get("stop_hunt") is not None
        has_divergence = flow.get("divergence") is not None

        if has_stop_hunt or (has_divergence and any("DELTA" in t for t in trigger_types)):
            return {
                "type": LIQUIDITY_TRAP,
                "description": (
                    "Stop-hunt / liquidity trap detected. Smart money swept stops "
                    "and price reversed. Trade WITH the reversal."
                ),
                "key_factors": [
                    f"Stop-hunt: {flow.get('stop_hunt', {}).get('description', 'N/A')}",
                    f"Delta divergence: {flow.get('divergence', {}).get('description', 'N/A')}",
                    f"Delta trend: {flow.get('delta_trend', 'N/A')}",
                ],
            }

        # ── OPENING_RANGE_BREAK ────────────────────────────────────
        if "OR_BREAKOUT" in trigger_types or "OR_BREAKDOWN" in trigger_types:
            volume_spike = flow.get("volume_spike")
            return {
                "type": OPENING_RANGE_BREAK,
                "description": (
                    "Opening range has been broken with conviction. "
                    "Trade in the direction of the break if volume confirms."
                ),
                "key_factors": [
                    f"OR break: {'BREAKOUT' if 'OR_BREAKOUT' in trigger_types else 'BREAKDOWN'}",
                    f"Volume spike: {volume_spike['description'] if volume_spike else 'No spike'}",
                    f"Phase: {structure.get('phase', 'N/A')}",
                ],
            }

        # ── MEAN_REVERSION ─────────────────────────────────────────
        rsi = indicators.get("rsi", 50)
        bb_pos = indicators.get("bb_position", "")
        phase = structure.get("phase", "")

        if (("RSI_OVERSOLD" in trigger_types or "RSI_OVERBOUGHT" in trigger_types) and
                (bb_pos in ("ABOVE_UPPER", "BELOW_LOWER") or phase == "RANGING")):
            return {
                "type": MEAN_REVERSION,
                "description": (
                    "Price at extremes with mean-reversion conditions. "
                    "Expect snap-back to VWAP/middle BB."
                ),
                "key_factors": [
                    f"RSI: {rsi:.1f}",
                    f"BB position: {bb_pos}",
                    f"Phase: {phase}",
                    f"VWAP position: {indicators.get('vwap_position', 'N/A')}",
                ],
            }

        # ── MAX_PAIN_DRIFT ─────────────────────────────────────────
        now = datetime.now().time()
        if ("MAX_PAIN_DRIFT" in trigger_types or "MAX_PAIN_PROXIMITY" in trigger_types):
            return {
                "type": MAX_PAIN_DRIFT,
                "description": (
                    "Price expected to drift toward Max Pain level. "
                    "Writers defend max pain, especially in last 2 hours."
                ),
                "key_factors": [
                    f"Max Pain: {sentiment.get('max_pain', 0):.0f}",
                    f"Current price distance: {sentiment.get('max_pain_distance', 0):.0f}",
                    f"PCR: {sentiment.get('pcr', 0):.3f}",
                    f"Time: {'afternoon' if now >= time(13, 0) else 'morning'}",
                ],
            }

        # ── BREAKOUT ───────────────────────────────────────────────
        if ("EMA_CROSS" in trigger_types and
                indicators.get("ema_alignment") in ("BULLISH", "BEARISH")):
            has_volume = flow.get("volume_spike") is not None
            return {
                "type": BREAKOUT,
                "description": (
                    "Trend breakout setup with EMA alignment and momentum. "
                    "Trade with the trend."
                ),
                "key_factors": [
                    f"EMA alignment: {indicators.get('ema_alignment', 'N/A')}",
                    f"MACD cross: {indicators.get('macd_cross', 'N/A')}",
                    f"Volume confirmation: {'Yes' if has_volume else 'No'}",
                    f"Phase: {phase}",
                ],
            }

        # ── GENERIC ────────────────────────────────────────────────
        return {
            "type": GENERIC,
            "description": (
                "Multiple signals firing but no clear scenario pattern. "
                "Use full analysis context for decision."
            ),
            "key_factors": [
                f"Triggers: {[t['type'] for t in triggers]}",
                f"Phase: {phase}",
                f"RSI: {rsi:.1f}",
            ],
        }
