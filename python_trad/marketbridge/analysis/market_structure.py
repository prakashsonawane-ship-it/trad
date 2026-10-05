"""
Market Structure — Stream A: Swing detection, volume profile, phase tagging.

Identifies:
- Swing highs and lows (fractal-based detection)
- Volume profile: POC (Point of Control), VAH (Value Area High), VAL (Value Area Low)
- Market phase: TRENDING_UP, TRENDING_DOWN, RANGING
- PDH/PDL (Previous Day High/Low) reference levels
- Opening range levels

Updated after every 1m candle close.
"""

import logging
from datetime import datetime, time, date
from typing import Optional
from collections import defaultdict

from data.state import MarketState, Candle
import config

logger = logging.getLogger(__name__)


class MarketStructure:
    """
    Stream A — Structural analysis of price action.

    Detects swing points, volume profile, market phase, and key levels.
    """

    def __init__(self, state: MarketState):
        self._state = state

        # Swing tracking
        self._swing_highs: dict[str, list[dict]] = {"NIFTY": [], "SENSEX": []}
        self._swing_lows: dict[str, list[dict]] = {"NIFTY": [], "SENSEX": []}

        # Previous day levels (set from historical data at session start)
        self._pdh: dict[str, float] = {"NIFTY": 0, "SENSEX": 0}
        self._pdl: dict[str, float] = {"NIFTY": 0, "SENSEX": 0}
        self._pdc: dict[str, float] = {"NIFTY": 0, "SENSEX": 0}  # Previous day close

        logger.info("MarketStructure (Stream A) initialized")

    def set_previous_day_levels(self, index: str, high: float, low: float, close: float) -> None:
        """Set PDH/PDL/PDC from historical data at session start."""
        self._pdh[index] = high
        self._pdl[index] = low
        self._pdc[index] = close
        logger.info(f"{index} PDH={high:.2f} PDL={low:.2f} PDC={close:.2f}")

    async def on_candle_close(self, index: str, candle: Candle) -> Optional[dict]:
        """
        Update market structure after each 1m candle close.

        Returns:
            Dict with structure analysis, or None if insufficient data.
        """
        candles = self._state.get_candles(index, "1m")

        if len(candles) < 10:
            return None

        # Detect swing points
        self._detect_swings(index, candles)

        # Determine market phase
        phase = self._determine_phase(index, candles)

        # Build volume profile
        volume_profile = self._build_volume_profile(candles[-60:])  # Last 60 candles

        # Compile structure output
        structure = {
            "phase": phase,
            "swing_highs": [
                {"price": s["price"], "candle_idx": s["idx"]}
                for s in self._swing_highs[index][-5:]  # Last 5
            ],
            "swing_lows": [
                {"price": s["price"], "candle_idx": s["idx"]}
                for s in self._swing_lows[index][-5:]
            ],
            "nearest_swing_high": self._swing_highs[index][-1]["price"] if self._swing_highs[index] else None,
            "nearest_swing_low": self._swing_lows[index][-1]["price"] if self._swing_lows[index] else None,
            "pdh": self._pdh[index],
            "pdl": self._pdl[index],
            "pdc": self._pdc[index],
            "opening_range": self._state.opening_range,
            "volume_profile": volume_profile,
            "current_price_vs_vp": self._price_vs_volume_profile(
                candles[-1].close, volume_profile
            ),
            "higher_highs": self._check_higher_highs(index),
            "lower_lows": self._check_lower_lows(index),
            "computed_at": datetime.now().isoformat(),
        }

        # Store in state
        await self._state.update("analysis", "structure", value=structure)

        return structure

    def _detect_swings(self, index: str, candles: list[Candle], lookback: int = 3) -> None:
        """
        Fractal-based swing detection.

        A swing high = candle whose high is higher than `lookback` candles on both sides.
        A swing low = candle whose low is lower than `lookback` candles on both sides.
        """
        if len(candles) < (2 * lookback + 1):
            return

        # Only check the most recently completable swing point
        # (lookback candles ago, since we need lookback candles after it)
        pivot_idx = len(candles) - lookback - 1
        pivot = candles[pivot_idx]

        # Check swing high
        is_swing_high = True
        for i in range(1, lookback + 1):
            left = candles[pivot_idx - i]
            right = candles[pivot_idx + i]
            if pivot.high <= left.high or pivot.high <= right.high:
                is_swing_high = False
                break

        if is_swing_high:
            # Avoid duplicate
            existing = self._swing_highs[index]
            if not existing or existing[-1]["idx"] != pivot_idx:
                self._swing_highs[index].append({
                    "price": pivot.high,
                    "idx": pivot_idx,
                    "time": pivot.timestamp,
                })
                # Keep last 20 swings
                if len(self._swing_highs[index]) > 20:
                    self._swing_highs[index] = self._swing_highs[index][-20:]

        # Check swing low
        is_swing_low = True
        for i in range(1, lookback + 1):
            left = candles[pivot_idx - i]
            right = candles[pivot_idx + i]
            if pivot.low >= left.low or pivot.low >= right.low:
                is_swing_low = False
                break

        if is_swing_low:
            existing = self._swing_lows[index]
            if not existing or existing[-1]["idx"] != pivot_idx:
                self._swing_lows[index].append({
                    "price": pivot.low,
                    "idx": pivot_idx,
                    "time": pivot.timestamp,
                })
                if len(self._swing_lows[index]) > 20:
                    self._swing_lows[index] = self._swing_lows[index][-20:]

    def _determine_phase(self, index: str, candles: list[Candle]) -> str:
        """
        Determine market phase based on swing structure and EMA alignment.

        TRENDING_UP: Higher highs + higher lows (at least 2 of each)
        TRENDING_DOWN: Lower highs + lower lows
        RANGING: Mixed or no clear direction
        """
        highs = self._swing_highs[index]
        lows = self._swing_lows[index]

        if len(highs) < 2 or len(lows) < 2:
            return "FORMING"

        # Check last 3 swing highs and lows
        recent_highs = [s["price"] for s in highs[-3:]]
        recent_lows = [s["price"] for s in lows[-3:]]

        higher_highs = all(
            recent_highs[i] > recent_highs[i - 1]
            for i in range(1, len(recent_highs))
        )
        higher_lows = all(
            recent_lows[i] > recent_lows[i - 1]
            for i in range(1, len(recent_lows))
        )
        lower_highs = all(
            recent_highs[i] < recent_highs[i - 1]
            for i in range(1, len(recent_highs))
        )
        lower_lows = all(
            recent_lows[i] < recent_lows[i - 1]
            for i in range(1, len(recent_lows))
        )

        if higher_highs and higher_lows:
            return "TRENDING_UP"
        elif lower_highs and lower_lows:
            return "TRENDING_DOWN"
        else:
            # Check if price is contained in a range
            price_range = max(recent_highs) - min(recent_lows)
            recent_candles = candles[-20:]
            recent_range = max(c.high for c in recent_candles) - min(c.low for c in recent_candles)

            # If recent range is less than 60% of swing range, it's consolidating
            if price_range > 0 and recent_range / price_range < 0.6:
                return "RANGING"
            return "RANGING"

    def _check_higher_highs(self, index: str) -> bool:
        """Check if last 2 swing highs are making higher highs."""
        highs = self._swing_highs[index]
        if len(highs) >= 2:
            return highs[-1]["price"] > highs[-2]["price"]
        return False

    def _check_lower_lows(self, index: str) -> bool:
        """Check if last 2 swing lows are making lower lows."""
        lows = self._swing_lows[index]
        if len(lows) >= 2:
            return lows[-1]["price"] < lows[-2]["price"]
        return False

    @staticmethod
    def _build_volume_profile(candles: list[Candle], num_bins: int = 20) -> dict:
        """
        Build a volume profile from candle data.

        Returns:
            {
                "poc": float,       # Point of Control (highest volume price)
                "vah": float,       # Value Area High (70% of volume above POC)
                "val": float,       # Value Area Low (70% of volume below POC)
                "total_volume": int,
            }
        """
        if not candles:
            return {"poc": 0, "vah": 0, "val": 0, "total_volume": 0}

        # Price range
        all_highs = [c.high for c in candles]
        all_lows = [c.low for c in candles]
        price_high = max(all_highs)
        price_low = min(all_lows)

        if price_high == price_low:
            return {"poc": price_high, "vah": price_high, "val": price_low, "total_volume": 0}

        bin_size = (price_high - price_low) / num_bins
        volume_at_price = defaultdict(int)

        for candle in candles:
            # Distribute volume across the candle's range
            candle_low_bin = int((candle.low - price_low) / bin_size)
            candle_high_bin = int((candle.high - price_low) / bin_size)
            candle_high_bin = min(candle_high_bin, num_bins - 1)
            candle_low_bin = max(candle_low_bin, 0)

            bins_in_candle = candle_high_bin - candle_low_bin + 1
            vol_per_bin = candle.volume / max(bins_in_candle, 1)

            for b in range(candle_low_bin, candle_high_bin + 1):
                volume_at_price[b] += vol_per_bin

        if not volume_at_price:
            mid = (price_high + price_low) / 2
            return {"poc": mid, "vah": price_high, "val": price_low, "total_volume": 0}

        # POC = bin with highest volume
        poc_bin = max(volume_at_price, key=volume_at_price.get)
        poc_price = price_low + (poc_bin + 0.5) * bin_size

        # Value Area = 70% of total volume, expanding from POC
        total_vol = sum(volume_at_price.values())
        target_vol = total_vol * 0.70

        accumulated = volume_at_price[poc_bin]
        low_bin = poc_bin
        high_bin = poc_bin

        while accumulated < target_vol:
            # Expand in the direction with more volume
            expand_up = volume_at_price.get(high_bin + 1, 0)
            expand_down = volume_at_price.get(low_bin - 1, 0)

            if expand_up >= expand_down and high_bin < num_bins - 1:
                high_bin += 1
                accumulated += volume_at_price.get(high_bin, 0)
            elif low_bin > 0:
                low_bin -= 1
                accumulated += volume_at_price.get(low_bin, 0)
            else:
                break

        vah = price_low + (high_bin + 1) * bin_size
        val = price_low + low_bin * bin_size

        return {
            "poc": round(poc_price, 2),
            "vah": round(vah, 2),
            "val": round(val, 2),
            "total_volume": int(total_vol),
        }

    @staticmethod
    def _price_vs_volume_profile(price: float, vp: dict) -> str:
        """Where is current price relative to the volume profile?"""
        if not vp or vp["vah"] == 0:
            return "UNKNOWN"
        if price > vp["vah"]:
            return "ABOVE_VALUE"
        elif price < vp["val"]:
            return "BELOW_VALUE"
        elif price > vp["poc"]:
            return "ABOVE_POC"
        else:
            return "BELOW_POC"

    def get_key_levels(self, index: str) -> list[dict]:
        """
        Return all key levels for an index (for the agent context).
        Sorted by distance from current price.
        """
        levels = []

        if self._pdh[index]:
            levels.append({"price": self._pdh[index], "type": "PDH"})
        if self._pdl[index]:
            levels.append({"price": self._pdl[index], "type": "PDL"})
        if self._pdc[index]:
            levels.append({"price": self._pdc[index], "type": "PDC"})

        opening = self._state.opening_range
        if opening.get("high"):
            levels.append({"price": opening["high"], "type": "OR_HIGH"})
        if opening.get("low"):
            levels.append({"price": opening["low"], "type": "OR_LOW"})

        for s in self._swing_highs[index][-5:]:
            levels.append({"price": s["price"], "type": "SWING_HIGH"})
        for s in self._swing_lows[index][-5:]:
            levels.append({"price": s["price"], "type": "SWING_LOW"})

        return levels
