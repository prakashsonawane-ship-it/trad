"""
Entry Watcher — Monitors price for entry zone confirmation.

Flow:
1. Agent decision received → entry_watcher starts watching
2. Every tick: check LTP vs entry_zone (trigger_price + direction)
3. On 1m close in zone:
   - Check last 2 candles delta still agrees with direction
   - YES: fire order via order_manager
   - NO: abort ("delta flip")
4. 10 min timeout: discard ("setup expired")
5. Time check: block if after 2:15 PM

Only candle-close confirmation — no entry on wicks.
"""

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Optional, Callable, Awaitable

from data.state import MarketState
import config

logger = logging.getLogger(__name__)

# Entry timeout
ENTRY_TIMEOUT_MINUTES = 10


class EntryWatcher:
    """
    Watches for entry zone confirmation after an agent decision.

    Ensures:
    - Entry happens on candle close (not wick-through)
    - Delta confirms the direction at entry time
    - 10-minute timeout prevents stale setups
    - No entries after 2:15 PM
    """

    def __init__(self, state: MarketState):
        self._state = state
        self._active_watch: Optional[dict] = None
        self._on_entry_callback: Optional[Callable[..., Awaitable[None]]] = None
        self._watch_task: Optional[asyncio.Task] = None
        logger.info("EntryWatcher initialized")

    def on_entry_confirmed(self, callback: Callable[..., Awaitable[None]]) -> None:
        """Register callback for when entry conditions are confirmed."""
        self._on_entry_callback = callback

    async def start_watching(
        self,
        index: str,
        direction: str,
        trigger_price: float,
        price_direction: str,
        invalidation: float,
        option_selection: dict,
        confidence: int,
        scenario_type: str,
    ) -> None:
        """
        Start watching for entry zone confirmation.

        Args:
            index: "NIFTY" or "SENSEX"
            direction: "BUY" or "SELL"
            trigger_price: Price level to trigger entry
            price_direction: "above" or "below" — where LTP must close
            invalidation: Price that invalidates the setup
            option_selection: Output from OptionSelector
            confidence: Agent confidence (1-10)
            scenario_type: e.g., "BREAKOUT"
        """
        # Cancel any existing watch
        await self.cancel_watch("new setup received")

        # Time check
        now = datetime.now().time()
        if now >= config.NO_ENTRY_AFTER:
            logger.warning(
                f"Entry blocked — after {config.NO_ENTRY_AFTER}. "
                "No new entries allowed."
            )
            return

        self._active_watch = {
            "index": index,
            "direction": direction,
            "trigger_price": trigger_price,
            "price_direction": price_direction,
            "invalidation": invalidation,
            "option_selection": option_selection,
            "confidence": confidence,
            "scenario_type": scenario_type,
            "started_at": datetime.now(),
            "timeout_at": datetime.now() + timedelta(minutes=ENTRY_TIMEOUT_MINUTES),
            "candle_close_in_zone": False,
        }

        logger.info(
            f"Entry watch started: {index} {direction} "
            f"trigger={trigger_price} {price_direction} "
            f"invalidation={invalidation} "
            f"timeout={ENTRY_TIMEOUT_MINUTES}min"
        )

        # Start timeout timer
        self._watch_task = asyncio.create_task(self._timeout_handler())

    async def on_tick(self, index: str, ltp: float, tick_time: datetime) -> None:
        """
        Called on every tick — check if price has hit invalidation.

        Invalidation is checked on every tick (immediate exit).
        Entry is only confirmed on candle close.
        """
        if not self._active_watch:
            return

        if self._active_watch["index"] != index:
            return

        # Check invalidation (immediate)
        inv = self._active_watch["invalidation"]
        direction = self._active_watch["direction"]

        if direction == "BUY" and ltp < inv:
            await self.cancel_watch(f"invalidation hit ({ltp:.2f} < {inv:.2f})")
            return

        if direction == "SELL" and ltp > inv:
            await self.cancel_watch(f"invalidation hit ({ltp:.2f} > {inv:.2f})")
            return

    async def on_candle_close(self, index: str, candle) -> None:
        """
        Called on 1m candle close — check if entry conditions met.

        Entry requires:
        1. Candle close in the entry zone
        2. Last 2 candle deltas agree with direction
        """
        if not self._active_watch:
            return

        if self._active_watch["index"] != index:
            return

        # Time check again (might have crossed 2:15 PM)
        if datetime.now().time() >= config.NO_ENTRY_AFTER:
            await self.cancel_watch("past NO_ENTRY_AFTER time")
            return

        trigger = self._active_watch["trigger_price"]
        price_dir = self._active_watch["price_direction"]
        direction = self._active_watch["direction"]

        # Check if candle CLOSED in the zone (not just wicked through)
        in_zone = False
        if price_dir == "above" and candle.close > trigger:
            in_zone = True
        elif price_dir == "below" and candle.close < trigger:
            in_zone = True

        if not in_zone:
            return

        logger.info(f"Candle closed in entry zone: {candle.close:.2f} ({price_dir} {trigger:.2f})")

        # Delta confirmation — check last 2 candle deltas
        delta_confirms = self._check_delta_confirmation(direction)

        if not delta_confirms:
            await self.cancel_watch("delta flip — direction no longer confirmed")
            return

        # ── ENTRY CONFIRMED ────────────────────────────────────────
        logger.info(
            f"ENTRY CONFIRMED: {index} {direction} at {candle.close:.2f} | "
            f"Delta confirms | Scenario: {self._active_watch['scenario_type']}"
        )

        # Fire callback
        if self._on_entry_callback:
            await self._on_entry_callback(self._active_watch)

        # Clear watch
        self._active_watch = None
        if self._watch_task:
            self._watch_task.cancel()
            self._watch_task = None

    def _check_delta_confirmation(self, direction: str) -> bool:
        """
        Check if the last 2 candle deltas agree with the trade direction.

        BUY: last 2 deltas should be positive (net buying)
        SELL: last 2 deltas should be negative (net selling)
        """
        flow = self._state.get("analysis", "flow") or {}
        delta_trend = flow.get("delta_trend", "")
        delta_2m = flow.get("delta_2m", 0)

        if direction == "BUY":
            if delta_trend in ("SELLING", "STRONG_SELLING"):
                logger.info(f"Delta flip detected: trend={delta_trend} vs direction=BUY")
                return False
            if delta_2m < 0:
                logger.info(f"Delta flip: 2m delta={delta_2m} vs direction=BUY")
                return False
            return True

        if direction == "SELL":
            if delta_trend in ("BUYING", "STRONG_BUYING"):
                logger.info(f"Delta flip detected: trend={delta_trend} vs direction=SELL")
                return False
            if delta_2m > 0:
                logger.info(f"Delta flip: 2m delta={delta_2m} vs direction=SELL")
                return False
            return True

        return False

    async def cancel_watch(self, reason: str = "manual") -> None:
        """Cancel the active entry watch."""
        if self._active_watch:
            logger.info(f"Entry watch cancelled: {reason}")
            self._active_watch = None

        if self._watch_task:
            self._watch_task.cancel()
            self._watch_task = None

    async def _timeout_handler(self) -> None:
        """Cancel the watch after ENTRY_TIMEOUT_MINUTES."""
        try:
            await asyncio.sleep(ENTRY_TIMEOUT_MINUTES * 60)
            if self._active_watch:
                await self.cancel_watch("setup expired (10 min timeout)")
        except asyncio.CancelledError:
            pass

    @property
    def is_watching(self) -> bool:
        return self._active_watch is not None

    @property
    def active_watch(self) -> Optional[dict]:
        return self._active_watch
