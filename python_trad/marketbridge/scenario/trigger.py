"""
Trigger Engine — Evaluates all trigger conditions after each 1m candle close.

Checks for actionable setups across all three analysis streams:
- EMA crossovers (9/21, 21/50)
- VWAP reclaim / rejection
- RSI divergence / extreme zones
- Delta flip (order flow reversal)
- OI spike (sudden buildup at specific strikes)
- Max Pain proximity (price near max pain on expiry)
- Opening range breakout

A trigger firing does NOT mean a trade — it means the confluence
scorer should evaluate whether the setup is strong enough.
"""

import logging
from datetime import datetime, time
from typing import Optional

from data.state import MarketState
import config

logger = logging.getLogger(__name__)


class TriggerEngine:
    """
    Evaluates trigger conditions after each 1m candle close.

    Returns a list of active triggers (if any) for the confluence scorer.
    """

    def __init__(self, state: MarketState):
        self._state = state
        self._prev_indicators: dict[str, dict] = {}
        logger.info("TriggerEngine initialized")

    async def evaluate(self, index: str) -> list[dict]:
        """
        Check all trigger conditions for the given index.

        Returns:
            List of fired trigger dicts, each with:
            {"type": str, "direction": "BUY"|"SELL", "strength": 1-3, "detail": str}
        """
        indicators = self._state.get("indicators", index) or {}
        structure = self._state.get("analysis", "structure") or {}
        flow = self._state.get("analysis", "flow") or {}
        sentiment = self._state.get("analysis", "sentiment") or {}
        prev = self._prev_indicators.get(index, {})

        if not indicators:
            return []

        triggers = []

        # ── EMA Crossovers ─────────────────────────────────────────
        ema_trigger = self._check_ema_cross(indicators, prev)
        if ema_trigger:
            triggers.append(ema_trigger)

        # ── VWAP Reclaim / Rejection ───────────────────────────────
        vwap_trigger = self._check_vwap(indicators, prev)
        if vwap_trigger:
            triggers.append(vwap_trigger)

        # ── RSI Extremes / Divergence ──────────────────────────────
        rsi_trigger = self._check_rsi(indicators, structure)
        if rsi_trigger:
            triggers.append(rsi_trigger)

        # ── Delta Flip ─────────────────────────────────────────────
        delta_trigger = self._check_delta_flip(flow)
        if delta_trigger:
            triggers.append(delta_trigger)

        # ── OI Spike ──────────────────────────────────────────────
        oi_trigger = self._check_oi_spike(sentiment)
        if oi_trigger:
            triggers.append(oi_trigger)

        # ── Max Pain Proximity ─────────────────────────────────────
        mp_trigger = self._check_max_pain(indicators, sentiment)
        if mp_trigger:
            triggers.append(mp_trigger)

        # ── Opening Range Break ────────────────────────────────────
        or_trigger = self._check_opening_range_break(index, indicators)
        if or_trigger:
            triggers.append(or_trigger)

        # Save current indicators for next comparison
        self._prev_indicators[index] = dict(indicators)

        if triggers:
            logger.info(
                f"{index}: {len(triggers)} trigger(s) fired: "
                f"{[t['type'] for t in triggers]}"
            )

        return triggers

    # ─── Individual Trigger Checks ─────────────────────────────────

    def _check_ema_cross(self, curr: dict, prev: dict) -> Optional[dict]:
        """Detect EMA 9/21 crossover."""
        if not prev or "ema_9" not in prev or "ema_21" not in prev:
            return None

        curr_9, curr_21 = curr.get("ema_9", 0), curr.get("ema_21", 0)
        prev_9, prev_21 = prev.get("ema_9", 0), prev.get("ema_21", 0)

        if curr_9 == 0 or prev_9 == 0:
            return None

        # Bullish cross: 9 crosses above 21
        if prev_9 <= prev_21 and curr_9 > curr_21:
            return {
                "type": "EMA_CROSS",
                "direction": "BUY",
                "strength": 2,
                "detail": f"EMA 9 crossed above EMA 21 ({curr_9:.0f} > {curr_21:.0f})",
            }

        # Bearish cross: 9 crosses below 21
        if prev_9 >= prev_21 and curr_9 < curr_21:
            return {
                "type": "EMA_CROSS",
                "direction": "SELL",
                "strength": 2,
                "detail": f"EMA 9 crossed below EMA 21 ({curr_9:.0f} < {curr_21:.0f})",
            }

        return None

    def _check_vwap(self, curr: dict, prev: dict) -> Optional[dict]:
        """Detect VWAP reclaim or rejection."""
        if not prev:
            return None

        curr_pos = curr.get("vwap_position", "")
        prev_pos = prev.get("vwap_position", "")
        vwap = curr.get("vwap", 0)

        if not curr_pos or not prev_pos or vwap == 0:
            return None

        # Reclaim: was below, now above
        if prev_pos == "BELOW" and curr_pos == "ABOVE":
            return {
                "type": "VWAP_RECLAIM",
                "direction": "BUY",
                "strength": 2,
                "detail": f"Price reclaimed VWAP ({vwap:.0f})",
            }

        # Rejection: was above, now below
        if prev_pos == "ABOVE" and curr_pos == "BELOW":
            return {
                "type": "VWAP_REJECTION",
                "direction": "SELL",
                "strength": 2,
                "detail": f"Price rejected from VWAP ({vwap:.0f})",
            }

        return None

    def _check_rsi(self, curr: dict, structure: dict) -> Optional[dict]:
        """Detect RSI at extreme zones with structural context."""
        rsi = curr.get("rsi", 50)
        phase = structure.get("phase", "")

        # RSI oversold in non-downtrend = potential bounce
        if rsi < 30 and phase != "TRENDING_DOWN":
            return {
                "type": "RSI_OVERSOLD",
                "direction": "BUY",
                "strength": 2,
                "detail": f"RSI oversold at {rsi:.1f} (phase: {phase})",
            }

        # RSI overbought in non-uptrend = potential rejection
        if rsi > 70 and phase != "TRENDING_UP":
            return {
                "type": "RSI_OVERBOUGHT",
                "direction": "SELL",
                "strength": 2,
                "detail": f"RSI overbought at {rsi:.1f} (phase: {phase})",
            }

        return None

    def _check_delta_flip(self, flow: dict) -> Optional[dict]:
        """Detect significant delta reversal."""
        delta_trend = flow.get("delta_trend", "")
        cum_delta = flow.get("cumulative_delta", 0)
        delta_5m = flow.get("delta_5m", 0)

        if not delta_trend:
            return None

        # Strong buying after extended selling
        if delta_trend == "STRONG_BUYING" and cum_delta < 0:
            return {
                "type": "DELTA_FLIP_BULLISH",
                "direction": "BUY",
                "strength": 2,
                "detail": f"Delta flipped to strong buying (cum: {cum_delta}, 5m: {delta_5m})",
            }

        # Strong selling after extended buying
        if delta_trend == "STRONG_SELLING" and cum_delta > 0:
            return {
                "type": "DELTA_FLIP_BEARISH",
                "direction": "SELL",
                "strength": 2,
                "detail": f"Delta flipped to strong selling (cum: {cum_delta}, 5m: {delta_5m})",
            }

        return None

    def _check_oi_spike(self, sentiment: dict) -> Optional[dict]:
        """Detect significant OI buildup at specific strikes."""
        oi_velocity = sentiment.get("oi_velocity", [])

        for entry in oi_velocity[:3]:  # Top 3 by change
            signal = entry.get("signal", "")
            total_change = entry.get("total_change", 0)

            if total_change < 1000:  # Minimum significance threshold
                continue

            if signal == "CE_UNWIND_PE_BUILDUP":
                return {
                    "type": "OI_BULLISH_SIGNAL",
                    "direction": "BUY",
                    "strength": 1,
                    "detail": f"CE unwinding + PE buildup at {entry['strike']} (change: {total_change})",
                }

            if signal == "CE_BUILDUP_PE_UNWIND":
                return {
                    "type": "OI_BEARISH_SIGNAL",
                    "direction": "SELL",
                    "strength": 1,
                    "detail": f"CE buildup + PE unwinding at {entry['strike']} (change: {total_change})",
                }

        return None

    def _check_max_pain(self, curr: dict, sentiment: dict) -> Optional[dict]:
        """Detect when price is near max pain on expiry day."""
        max_pain = sentiment.get("max_pain", 0)
        ltp = curr.get("last_close", 0)

        if max_pain == 0 or ltp == 0:
            return None

        distance_pct = abs(ltp - max_pain) / ltp * 100

        # Price within 0.3% of max pain = strong gravitational pull
        if distance_pct < 0.3:
            return {
                "type": "MAX_PAIN_PROXIMITY",
                "direction": "BUY" if ltp < max_pain else "SELL",
                "strength": 1,
                "detail": f"Price near Max Pain ({max_pain:.0f}), distance {distance_pct:.2f}%",
            }

        # Price > 0.5% away = drift expected toward max pain
        now = datetime.now().time()
        if distance_pct > 0.5 and now >= time(13, 30):
            direction = "BUY" if ltp < max_pain else "SELL"
            return {
                "type": "MAX_PAIN_DRIFT",
                "direction": direction,
                "strength": 2,
                "detail": f"Afternoon Max Pain drift expected: {ltp:.0f} → {max_pain:.0f} ({distance_pct:.1f}% away)",
            }

        return None

    def _check_opening_range_break(self, index: str, curr: dict) -> Optional[dict]:
        """Detect opening range breakout/breakdown."""
        opening = self._state.opening_range
        if not opening.get("formed"):
            return None

        ltp = curr.get("last_close", 0)
        or_high = opening.get("high", 0)
        or_low = opening.get("low", 0)

        if not ltp or not or_high or not or_low:
            return None

        # Breakout above opening range
        if ltp > or_high:
            return {
                "type": "OR_BREAKOUT",
                "direction": "BUY",
                "strength": 3,
                "detail": f"Opening range breakout: {ltp:.0f} > OR high {or_high:.0f}",
            }

        # Breakdown below opening range
        if ltp < or_low:
            return {
                "type": "OR_BREAKDOWN",
                "direction": "SELL",
                "strength": 3,
                "detail": f"Opening range breakdown: {ltp:.0f} < OR low {or_low:.0f}",
            }

        return None
