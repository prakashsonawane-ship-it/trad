"""
Liquidity Flow — Stream B: Order flow approximation from tick data.

Since Upstox WebSocket provides LTP ticks (not L2 order book), this uses a
tick-classification approach — the best achievable without exchange co-location.

Components:
- Tick classifier: uptick + volume > avg = aggressive buy, downtick = aggressive sell
- Per-candle delta: buy_volume - sell_volume per 1m candle
- Cumulative delta: rolling sum from 9:15 AM, resets each session
- Delta divergence: price new high/low but delta diverges
- Stop-hunt detector: pierce swing level > 0.1%, close back, delta negative
- Volume spike detector: volume > 2.5x 20-candle average

IMPORTANT: Stream B is weighted at 0.8x in the confluence scorer
because tick-based delta is an approximation, not true order flow.
"""

import logging
from datetime import datetime, time
from typing import Optional
from collections import deque

from data.state import MarketState, Candle
import config

logger = logging.getLogger(__name__)

# Stream B weight factor (used by confluence scorer)
STREAM_B_WEIGHT = 0.8


class LiquidityFlow:
    """
    Stream B — Order flow and liquidity analysis.

    Approximates institutional order flow from tick data.
    """

    def __init__(self, state: MarketState):
        self._state = state

        # Per-index tracking
        self._last_tick_price: dict[str, float] = {"NIFTY": 0, "SENSEX": 0}
        self._tick_deltas: dict[str, list[int]] = {"NIFTY": [], "SENSEX": []}

        # Rolling tick volume for classification
        self._tick_volumes: dict[str, deque] = {
            "NIFTY": deque(maxlen=20),
            "SENSEX": deque(maxlen=20),
        }

        # Per-candle delta accumulation (reset on candle close)
        self._current_buy_vol: dict[str, int] = {"NIFTY": 0, "SENSEX": 0}
        self._current_sell_vol: dict[str, int] = {"NIFTY": 0, "SENSEX": 0}

        # Candle-level delta history
        self._candle_deltas: dict[str, list[dict]] = {"NIFTY": [], "SENSEX": []}
        self._cumulative_delta: dict[str, int] = {"NIFTY": 0, "SENSEX": 0}

        # Volume history for spike detection
        self._candle_volumes: dict[str, deque] = {
            "NIFTY": deque(maxlen=20),
            "SENSEX": deque(maxlen=20),
        }

        logger.info("LiquidityFlow (Stream B) initialized (weight=0.8x)")

    # ─── Tick Processing (called on every tick) ────────────────────

    async def process_tick(self, index: str, ltp: float, volume: int, tick_time: datetime) -> None:
        """
        Classify each tick as aggressive buy or sell.

        Called on every WebSocket tick — must be fast.
        """
        last_price = self._last_tick_price[index]

        if last_price == 0:
            self._last_tick_price[index] = ltp
            return

        # Track volume for rolling average
        self._tick_volumes[index].append(volume)

        # Calculate rolling average volume
        vol_deque = self._tick_volumes[index]
        avg_vol = sum(vol_deque) / len(vol_deque) if vol_deque else 1

        # Classify tick direction
        if ltp > last_price:
            # Uptick
            if volume > avg_vol:
                # Aggressive buy: uptick + above-average volume
                self._current_buy_vol[index] += volume
            else:
                # Passive buy
                self._current_buy_vol[index] += volume // 2
        elif ltp < last_price:
            # Downtick
            if volume > avg_vol:
                # Aggressive sell: downtick + above-average volume
                self._current_sell_vol[index] += volume
            else:
                # Passive sell
                self._current_sell_vol[index] += volume // 2
        # Equal price: split volume
        else:
            self._current_buy_vol[index] += volume // 2
            self._current_sell_vol[index] += volume // 2

        self._last_tick_price[index] = ltp

    # ─── Candle Close Processing ───────────────────────────────────

    async def on_candle_close(self, index: str, candle: Candle) -> Optional[dict]:
        """
        Finalize delta for the closed candle and run analysis.

        Called after each 1m candle closes.
        """
        # Calculate per-candle delta
        buy_vol = self._current_buy_vol[index]
        sell_vol = self._current_sell_vol[index]
        delta = buy_vol - sell_vol

        # Store candle delta
        candle_delta = {
            "timestamp": candle.timestamp,
            "delta": delta,
            "buy_volume": buy_vol,
            "sell_volume": sell_vol,
            "candle_close": candle.close,
            "candle_high": candle.high,
            "candle_low": candle.low,
            "total_volume": candle.volume,
        }
        self._candle_deltas[index].append(candle_delta)

        # Keep last 200 candle deltas
        if len(self._candle_deltas[index]) > 200:
            self._candle_deltas[index] = self._candle_deltas[index][-200:]

        # Update cumulative delta
        self._cumulative_delta[index] += delta

        # Track volume for spike detection
        self._candle_volumes[index].append(candle.volume)

        # Reset per-candle accumulators
        self._current_buy_vol[index] = 0
        self._current_sell_vol[index] = 0

        # Run analysis
        candles = self._state.get_candles(index, "1m")
        if len(candles) < 5:
            return None

        # Build flow analysis output
        flow = {
            "delta_current": delta,
            "delta_2m": self._get_delta_sum(index, 2),
            "delta_5m": self._get_delta_sum(index, 5),
            "cumulative_delta": self._cumulative_delta[index],
            "delta_trend": self._delta_trend(index),
            "divergence": self._detect_divergence(index, candles),
            "stop_hunt": self._detect_stop_hunt(index, candle),
            "volume_spike": self._detect_volume_spike(index, candle),
            "buy_pressure_pct": round(
                buy_vol / max(buy_vol + sell_vol, 1) * 100, 1
            ),
            "stream_weight": STREAM_B_WEIGHT,
            "computed_at": datetime.now().isoformat(),
        }

        # Store in state
        await self._state.update("analysis", "flow", value=flow)

        return flow

    # ─── Delta Analysis ────────────────────────────────────────────

    def _get_delta_sum(self, index: str, candles_back: int) -> int:
        """Sum of delta over the last N candles."""
        deltas = self._candle_deltas[index]
        if len(deltas) < candles_back:
            return sum(d["delta"] for d in deltas)
        return sum(d["delta"] for d in deltas[-candles_back:])

    def _delta_trend(self, index: str) -> str:
        """
        Determine if delta is trending up, down, or flat.
        Based on last 5 candle deltas.
        """
        deltas = self._candle_deltas[index]
        if len(deltas) < 5:
            return "INSUFFICIENT_DATA"

        recent = [d["delta"] for d in deltas[-5:]]
        positive_count = sum(1 for d in recent if d > 0)
        negative_count = sum(1 for d in recent if d < 0)

        if positive_count >= 4:
            return "STRONG_BUYING"
        elif positive_count >= 3:
            return "BUYING"
        elif negative_count >= 4:
            return "STRONG_SELLING"
        elif negative_count >= 3:
            return "SELLING"
        else:
            return "MIXED"

    # ─── Divergence Detection ──────────────────────────────────────

    def _detect_divergence(self, index: str, candles: list[Candle]) -> Optional[dict]:
        """
        Detect delta divergence — price makes new high/low but delta diverges.

        Bullish divergence: price makes lower low, but delta makes higher low
        Bearish divergence: price makes higher high, but delta makes lower high
        """
        deltas = self._candle_deltas[index]
        if len(deltas) < 10 or len(candles) < 10:
            return None

        recent_candles = candles[-10:]
        recent_deltas = deltas[-10:]

        # Check for bearish divergence (price up, delta down)
        price_highs = [c.high for c in recent_candles]
        delta_at_highs = []
        for i, c in enumerate(recent_candles):
            if c.high == max(price_highs[:i + 1]):
                delta_at_highs.append(recent_deltas[i]["delta"] if i < len(recent_deltas) else 0)

        if len(delta_at_highs) >= 2:
            if price_highs[-1] >= max(price_highs[:-1]) and delta_at_highs[-1] < delta_at_highs[-2]:
                return {
                    "type": "BEARISH_DIVERGENCE",
                    "description": "Price making new highs but delta declining — distribution",
                }

        # Check for bullish divergence (price down, delta up)
        price_lows = [c.low for c in recent_candles]
        delta_at_lows = []
        for i, c in enumerate(recent_candles):
            if c.low == min(price_lows[:i + 1]):
                delta_at_lows.append(recent_deltas[i]["delta"] if i < len(recent_deltas) else 0)

        if len(delta_at_lows) >= 2:
            if price_lows[-1] <= min(price_lows[:-1]) and delta_at_lows[-1] > delta_at_lows[-2]:
                return {
                    "type": "BULLISH_DIVERGENCE",
                    "description": "Price making new lows but delta rising — accumulation",
                }

        return None

    # ─── Stop-Hunt Detection ──────────────────────────────────────

    def _detect_stop_hunt(self, index: str, candle: Candle) -> Optional[dict]:
        """
        Detect stop-hunt patterns:
        1. Candle pierces a known swing level by > 0.1%
        2. Closes back above/below the level
        3. Delta on the candle opposes the pierce direction

        A bullish stop-hunt: price pierces below a swing low then closes above it
        with negative delta (sellers trapped).
        """
        structure = self._state.get("analysis", "structure")
        if not structure:
            return None

        swing_lows = structure.get("swing_lows", [])
        swing_highs = structure.get("swing_highs", [])

        # Check for bullish stop-hunt (pierce below swing low, close above)
        for swing in swing_lows:
            level = swing["price"]
            pierce_threshold = level * 0.001  # 0.1%

            if (candle.low < level - pierce_threshold and
                    candle.close > level):
                # Delta should be negative (sellers got trapped)
                deltas = self._candle_deltas[index]
                if deltas and deltas[-1]["delta"] < 0:
                    return {
                        "type": "BULLISH_STOP_HUNT",
                        "level": level,
                        "pierce_low": candle.low,
                        "close": candle.close,
                        "description": f"Pierced swing low {level:.2f}, closed above — sellers trapped",
                    }

        # Check for bearish stop-hunt (pierce above swing high, close below)
        for swing in swing_highs:
            level = swing["price"]
            pierce_threshold = level * 0.001

            if (candle.high > level + pierce_threshold and
                    candle.close < level):
                deltas = self._candle_deltas[index]
                if deltas and deltas[-1]["delta"] > 0:
                    return {
                        "type": "BEARISH_STOP_HUNT",
                        "level": level,
                        "pierce_high": candle.high,
                        "close": candle.close,
                        "description": f"Pierced swing high {level:.2f}, closed below — buyers trapped",
                    }

        return None

    # ─── Volume Spike Detection ────────────────────────────────────

    def _detect_volume_spike(self, index: str, candle: Candle) -> Optional[dict]:
        """
        Flag candles where volume > 2.5x the 20-candle average.
        Marks institutional entry or exhaustion.
        """
        vol_deque = self._candle_volumes[index]
        if len(vol_deque) < 5:
            return None

        avg_volume = sum(vol_deque) / len(vol_deque)
        if avg_volume == 0:
            return None

        ratio = candle.volume / avg_volume

        if ratio >= 2.5:
            # Determine if this is accumulation or distribution
            deltas = self._candle_deltas[index]
            delta = deltas[-1]["delta"] if deltas else 0

            spike_type = "ACCUMULATION" if delta > 0 else "DISTRIBUTION"

            return {
                "type": spike_type,
                "volume": candle.volume,
                "average": int(avg_volume),
                "ratio": round(ratio, 1),
                "description": f"Volume spike {ratio:.1f}x average — {spike_type.lower()}",
            }

        return None

    def reset_session(self) -> None:
        """Reset all session-level tracking for a new trading day."""
        for index in ("NIFTY", "SENSEX"):
            self._current_buy_vol[index] = 0
            self._current_sell_vol[index] = 0
            self._cumulative_delta[index] = 0
            self._candle_deltas[index] = []
            self._candle_volumes[index].clear()
            self._tick_volumes[index].clear()
            self._last_tick_price[index] = 0
        logger.info("LiquidityFlow session reset")
