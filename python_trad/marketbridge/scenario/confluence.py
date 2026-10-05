"""
Confluence Scorer — Scores 3 analysis streams and gates agent calls.

Scoring: each stream scores 0-3, total 0-9.
Stream B (liquidity flow) weighted at 0.8x.

Gate: score < threshold → suppress (no agent call).
Threshold varies by time of day:
  - Primary (9:30-11:30):   5
  - Quiet (11:30-13:30):    7
  - Secondary (13:30-14:15): 6

Additional adjustment from macro context (news_bias).
"""

import logging
from datetime import datetime, time
from typing import Optional

from data.state import MarketState
from news.news_bias import get_confluence_adjustment
import config

logger = logging.getLogger(__name__)

# Stream B weight (liquidity flow is approximate, not true order flow)
STREAM_B_WEIGHT = 0.8


class ConfluenceScorer:
    """
    Scores triggered setups across three analysis streams.

    Only setups meeting the time-adjusted threshold pass to the agent.
    """

    def __init__(self, state: MarketState):
        self._state = state
        logger.info("ConfluenceScorer initialized (Stream B weight=0.8x)")

    def score(self, triggers: list[dict], direction: str) -> dict:
        """
        Score a triggered setup for confluence.

        Args:
            triggers: List of trigger dicts from TriggerEngine
            direction: Primary direction "BUY" or "SELL"

        Returns:
            {
                "total_score": float,
                "passes_gate": bool,
                "threshold": int,
                "stream_scores": {"structure": int, "flow": float, "sentiment": int},
                "direction": str,
                "supporting_triggers": list,
                "opposing_triggers": list,
            }
        """
        indicators = self._state.get("indicators", self._state.today_index) or {}
        structure = self._state.get("analysis", "structure") or {}
        flow = self._state.get("analysis", "flow") or {}
        sentiment = self._state.get("analysis", "sentiment") or {}

        # Score each stream
        structure_score = self._score_structure(structure, indicators, direction)
        flow_score = self._score_flow(flow, direction)
        sentiment_score = self._score_sentiment(sentiment, direction)

        # Apply Stream B weight
        weighted_flow = flow_score * STREAM_B_WEIGHT

        # Total score
        total = structure_score + weighted_flow + sentiment_score

        # Determine threshold based on time of day
        threshold = self._get_threshold()

        # Apply macro adjustment
        macro_ctx = self._state.macro_context
        macro_adj = get_confluence_adjustment(macro_ctx)
        effective_threshold = min(threshold + macro_adj, 9)  # Cap at 9

        passes = total >= effective_threshold

        # Separate supporting vs opposing triggers
        supporting = [t for t in triggers if t["direction"] == direction]
        opposing = [t for t in triggers if t["direction"] != direction]

        result = {
            "total_score": round(total, 1),
            "passes_gate": passes,
            "threshold": effective_threshold,
            "base_threshold": threshold,
            "macro_adjustment": macro_adj,
            "stream_scores": {
                "structure": structure_score,
                "flow": flow_score,
                "flow_weighted": round(weighted_flow, 1),
                "sentiment": sentiment_score,
            },
            "direction": direction,
            "supporting_triggers": [t["type"] for t in supporting],
            "opposing_triggers": [t["type"] for t in opposing],
            "trigger_count": len(triggers),
        }

        if passes:
            logger.info(
                f"Confluence PASS: score={total:.1f} >= {effective_threshold} | "
                f"S={structure_score} F={weighted_flow:.1f} Se={sentiment_score} | "
                f"direction={direction}"
            )
        else:
            logger.debug(
                f"Confluence SUPPRESSED: score={total:.1f} < {effective_threshold} | "
                f"direction={direction}"
            )

        return result

    # ─── Stream Scoring ────────────────────────────────────────────

    def _score_structure(self, structure: dict, indicators: dict, direction: str) -> int:
        """
        Score Stream A (Market Structure): 0-3 points.

        +1: Phase agrees with direction
        +1: EMA alignment agrees
        +1: Price at favorable level (near swing support/resistance)
        """
        score = 0
        phase = structure.get("phase", "")
        ema_alignment = indicators.get("ema_alignment", "")

        # Phase alignment
        if direction == "BUY" and phase == "TRENDING_UP":
            score += 1
        elif direction == "SELL" and phase == "TRENDING_DOWN":
            score += 1
        elif phase == "RANGING":
            # Ranging supports mean reversion
            score += 0  # Neutral

        # EMA alignment
        if direction == "BUY" and ema_alignment == "BULLISH":
            score += 1
        elif direction == "SELL" and ema_alignment == "BEARISH":
            score += 1

        # Price near key level
        ltp = indicators.get("last_close", 0)
        if ltp > 0:
            # Near swing low for buys
            nearest_low = structure.get("nearest_swing_low")
            nearest_high = structure.get("nearest_swing_high")

            if direction == "BUY" and nearest_low:
                distance_pct = abs(ltp - nearest_low) / ltp * 100
                if distance_pct < 0.3:  # Within 0.3% of swing low
                    score += 1
            elif direction == "SELL" and nearest_high:
                distance_pct = abs(ltp - nearest_high) / ltp * 100
                if distance_pct < 0.3:
                    score += 1

        return min(score, 3)

    def _score_flow(self, flow: dict, direction: str) -> int:
        """
        Score Stream B (Liquidity Flow): 0-3 points.
        (Will be weighted by 0.8x in the total)

        +1: Delta trend agrees with direction
        +1: Cumulative delta supports direction
        +1: Stop-hunt or volume spike confirms
        """
        score = 0
        delta_trend = flow.get("delta_trend", "")
        cum_delta = flow.get("cumulative_delta", 0)

        # Delta trend alignment
        if direction == "BUY" and delta_trend in ("BUYING", "STRONG_BUYING"):
            score += 1
        elif direction == "SELL" and delta_trend in ("SELLING", "STRONG_SELLING"):
            score += 1

        # Cumulative delta direction
        if direction == "BUY" and cum_delta > 0:
            score += 1
        elif direction == "SELL" and cum_delta < 0:
            score += 1

        # Stop-hunt or volume spike confirmation
        stop_hunt = flow.get("stop_hunt")
        volume_spike = flow.get("volume_spike")

        if stop_hunt:
            hunt_type = stop_hunt.get("type", "")
            if direction == "BUY" and hunt_type == "BULLISH_STOP_HUNT":
                score += 1
            elif direction == "SELL" and hunt_type == "BEARISH_STOP_HUNT":
                score += 1

        if volume_spike:
            spike_type = volume_spike.get("type", "")
            if direction == "BUY" and spike_type == "ACCUMULATION":
                score += 1
            elif direction == "SELL" and spike_type == "DISTRIBUTION":
                score += 1

        return min(score, 3)

    def _score_sentiment(self, sentiment: dict, direction: str) -> int:
        """
        Score Stream C (Sentiment): 0-3 points.

        +1: PCR supports direction
        +1: Max Pain pull agrees with direction
        +1: IV percentile favorable (not overpriced)
        """
        score = 0
        pcr = sentiment.get("pcr", 1.0)
        max_pain_dir = sentiment.get("max_pain_direction", "")
        iv_pct = sentiment.get("iv_percentile", 50)

        # PCR alignment
        # High PCR (>1.0) = more puts = contrarian bullish
        # Low PCR (<0.7) = more calls = contrarian bearish
        if direction == "BUY" and pcr > 1.0:
            score += 1  # Heavy put writing = support below
        elif direction == "SELL" and pcr < 0.7:
            score += 1  # Heavy call writing = resistance above

        # Max Pain direction
        if direction == "BUY" and max_pain_dir == "ABOVE":
            score += 1  # Price below max pain, expect drift up
        elif direction == "SELL" and max_pain_dir == "BELOW":
            score += 1  # Price above max pain, expect drift down

        # IV percentile (prefer buying when IV is not extreme)
        if iv_pct < 70:
            score += 1  # Options not overpriced
        # If IV > 80, options are expensive — penalize

        return min(score, 3)

    # ─── Threshold by Time of Day ──────────────────────────────────

    def _get_threshold(self) -> int:
        """Get base confluence threshold based on current time."""
        now = datetime.now().time()

        if now < config.OPENING_RANGE_END:
            return 99  # No trades during opening range formation

        if now < config.PRIMARY_WINDOW_END:
            return config.CONFLUENCE_THRESHOLDS["primary"]   # 5

        if now < config.QUIET_PERIOD_END:
            return config.CONFLUENCE_THRESHOLDS["quiet"]     # 7

        if now < config.NO_ENTRY_AFTER:
            return config.CONFLUENCE_THRESHOLDS["secondary"]  # 6

        return 99  # No entries after 2:15 PM
