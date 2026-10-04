"""
Candle Builder — Tick-to-OHLCV aggregation for multiple timeframes.

Receives raw ticks from the WebSocket, builds 1-minute candles,
then derives higher timeframes (3m, 5m, 15m, 30m, 1h) from 1m data.

Fires a callback on every candle close — this is the heartbeat of
the analysis pipeline.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional, Awaitable
from collections import defaultdict

from data.state import Candle, MarketState

logger = logging.getLogger(__name__)

# How many 1m candles make up each higher timeframe
TIMEFRAME_MULTIPLIERS = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
}


class CandleBuilder:
    """
    Builds OHLCV candles from raw tick data.

    Architecture:
    - Every tick updates the current 1m candle for that index
    - On minute boundary, the 1m candle is closed and stored
    - Higher timeframes are aggregated from closed 1m candles
    - Callbacks fire on each 1m close (triggers the analysis pipeline)
    """

    def __init__(self, state: MarketState):
        self._state = state

        # Current (building) candle per index
        self._current_candle: dict[str, Optional[dict]] = {
            "NIFTY": None,
            "SENSEX": None,
        }

        # Buffer of closed 1m candles for higher-TF aggregation (per index)
        self._1m_buffer: dict[str, list[Candle]] = {
            "NIFTY": [],
            "SENSEX": [],
        }

        # Count of 1m candles closed in current session (for higher-TF timing)
        self._1m_count: dict[str, int] = {
            "NIFTY": 0,
            "SENSEX": 0,
        }

        # Callbacks to fire on 1m candle close: async fn(index, candle)
        self._on_close_callbacks: list[Callable[[str, Candle], Awaitable[None]]] = []

        # Track the current minute to detect boundary crossings
        self._current_minute: dict[str, Optional[int]] = {
            "NIFTY": None,
            "SENSEX": None,
        }

        logger.info("CandleBuilder initialized")

    def on_candle_close(self, callback: Callable[[str, Candle], Awaitable[None]]) -> None:
        """Register a callback for 1m candle close events."""
        self._on_close_callbacks.append(callback)
        logger.debug(f"Registered candle close callback: {callback.__name__}")

    async def process_tick(
        self,
        index: str,
        ltp: float,
        volume: int,
        tick_time: datetime,
    ) -> None:
        """
        Process a single tick for an index.

        Called on every WebSocket tick. Updates the building candle
        and closes it on minute boundary.

        Args:
            index: "NIFTY" or "SENSEX"
            ltp: Last traded price
            volume: Tick volume (cumulative or per-tick depending on feed)
            tick_time: Timestamp from the exchange
        """
        tick_minute = tick_time.minute + tick_time.hour * 60  # Unique minute ID

        current = self._current_candle[index]
        prev_minute = self._current_minute[index]

        # ── Minute Boundary Detection ──────────────────────────
        if prev_minute is not None and tick_minute != prev_minute and current is not None:
            # Close the current candle
            closed_candle = Candle(
                timestamp=current["timestamp"],
                open=current["open"],
                high=current["high"],
                low=current["low"],
                close=current["close"],
                volume=current["volume"],
            )

            # Store in state
            await self._state.add_candle(index, "1m", closed_candle)

            # Buffer for higher-TF aggregation
            self._1m_buffer[index].append(closed_candle)
            self._1m_count[index] += 1

            # Build higher timeframes
            await self._aggregate_higher_timeframes(index)

            # Fire callbacks (this triggers the analysis pipeline)
            for callback in self._on_close_callbacks:
                try:
                    await callback(index, closed_candle)
                except Exception as e:
                    logger.error(f"Candle close callback error: {e}", exc_info=True)

            logger.debug(
                f"{index} 1m candle closed: "
                f"O={closed_candle.open:.2f} H={closed_candle.high:.2f} "
                f"L={closed_candle.low:.2f} C={closed_candle.close:.2f} "
                f"V={closed_candle.volume}"
            )

            # Reset for new candle
            self._current_candle[index] = None

        # ── Update Building Candle ─────────────────────────────
        if self._current_candle[index] is None:
            # Start a new candle
            # Align timestamp to the start of the minute
            candle_ts = tick_time.replace(second=0, microsecond=0)
            self._current_candle[index] = {
                "timestamp": candle_ts,
                "open": ltp,
                "high": ltp,
                "low": ltp,
                "close": ltp,
                "volume": volume,
            }
        else:
            current = self._current_candle[index]
            current["high"] = max(current["high"], ltp)
            current["low"] = min(current["low"], ltp)
            current["close"] = ltp
            current["volume"] += volume

        self._current_minute[index] = tick_minute

    async def _aggregate_higher_timeframes(self, index: str) -> None:
        """
        Build higher-timeframe candles from the 1m buffer.

        A 3m candle closes every 3rd 1m candle, 5m every 5th, etc.
        """
        count = self._1m_count[index]
        buffer = self._1m_buffer[index]

        for tf, multiplier in TIMEFRAME_MULTIPLIERS.items():
            if tf == "1m":
                continue  # Already handled

            if count % multiplier == 0 and len(buffer) >= multiplier:
                # Take the last N 1m candles
                component_candles = buffer[-multiplier:]
                aggregated = self._merge_candles(component_candles)

                if aggregated:
                    await self._state.add_candle(index, tf, aggregated)
                    logger.debug(
                        f"{index} {tf} candle closed: "
                        f"O={aggregated.open:.2f} C={aggregated.close:.2f}"
                    )

        # Keep buffer bounded (last 120 1m candles = 2 hours)
        if len(buffer) > 120:
            self._1m_buffer[index] = buffer[-120:]

    @staticmethod
    def _merge_candles(candles: list[Candle]) -> Optional[Candle]:
        """Merge multiple candles into one higher-timeframe candle."""
        if not candles:
            return None

        return Candle(
            timestamp=candles[0].timestamp,
            open=candles[0].open,
            high=max(c.high for c in candles),
            low=min(c.low for c in candles),
            close=candles[-1].close,
            volume=sum(c.volume for c in candles),
        )

    def get_building_candle(self, index: str) -> Optional[dict]:
        """Get the current (not yet closed) candle for live display."""
        return self._current_candle.get(index)

    async def force_close(self, index: str) -> None:
        """
        Force-close the current building candle.
        Used at session end (3:00 PM) to flush the last partial candle.
        """
        current = self._current_candle[index]
        if current is not None:
            closed = Candle(
                timestamp=current["timestamp"],
                open=current["open"],
                high=current["high"],
                low=current["low"],
                close=current["close"],
                volume=current["volume"],
            )
            await self._state.add_candle(index, "1m", closed)
            self._current_candle[index] = None
            logger.info(f"{index} final candle force-closed at session end")

    def reset(self) -> None:
        """Reset all state for a new session."""
        for index in ("NIFTY", "SENSEX"):
            self._current_candle[index] = None
            self._1m_buffer[index] = []
            self._1m_count[index] = 0
            self._current_minute[index] = None
        logger.info("CandleBuilder reset for new session")
