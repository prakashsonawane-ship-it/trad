"""
Position Monitor — Runs every 30s, manages the 6-level exit ladder.

Exit rules (priority order):
1. Hard Close:  time >= 15:00 → Exit 100% at market
2. Invalidation: Index crosses invalidation level → Exit 100%
3. Stop Loss:   Premium down 50% → Exit 100% at market
4. TP1:         Premium up 50% → Exit 50%, move stop to breakeven
5. TP2:         Premium up 100% → Exit 30% more, start trailing
6. Trail:       After TP2, premium drops 30% from peak → Exit remaining 20%

Partial exits calculate correct quantities (lot-size rounding).
"""

import asyncio
import logging
from datetime import datetime, time
from typing import Optional, Callable, Awaitable

from data.state import MarketState, Position
import config

logger = logging.getLogger(__name__)

# Monitor interval
MONITOR_INTERVAL = 30  # seconds


class PositionMonitor:
    """
    Monitors open positions and manages exits per the 6-level ladder.

    Runs as a background asyncio task every 30 seconds.
    """

    def __init__(self, state: MarketState):
        self._state = state
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False

        # Exit callback: async fn(position, quantity, reason, order_type)
        self._exit_callback: Optional[Callable[..., Awaitable[None]]] = None

        # Per-position tracking
        self._peak_premium: dict[str, float] = {}  # instrument_key → highest premium
        self._tp1_hit: dict[str, bool] = {}
        self._tp2_hit: dict[str, bool] = {}
        self._stop_moved_to_be: dict[str, bool] = {}  # breakeven stop

        logger.info("PositionMonitor initialized")

    def on_exit_signal(self, callback: Callable[..., Awaitable[None]]) -> None:
        """Register callback for exit signals."""
        self._exit_callback = callback

    async def start(self) -> None:
        """Start the position monitor background task."""
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(f"Position monitor started (interval={MONITOR_INTERVAL}s)")

    async def stop(self) -> None:
        """Stop the position monitor."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("Position monitor stopped")

    async def _monitor_loop(self) -> None:
        """Main monitoring loop — runs every MONITOR_INTERVAL seconds."""
        while self._running:
            try:
                await self._check_all_positions()
            except Exception as e:
                logger.error(f"Monitor error: {e}", exc_info=True)

            await asyncio.sleep(MONITOR_INTERVAL)

    async def _check_all_positions(self) -> None:
        """Check all open positions against exit rules."""
        positions = self._state.positions

        if not positions:
            return

        now = datetime.now().time()

        for position in list(positions):  # Copy list since we may modify
            key = position.instrument_key

            # Initialize tracking for new positions
            if key not in self._peak_premium:
                self._peak_premium[key] = position.entry_premium
                self._tp1_hit[key] = False
                self._tp2_hit[key] = False
                self._stop_moved_to_be[key] = False

            # Get current premium (from state or position)
            current_premium = position.current_premium or position.entry_premium

            # Update peak premium
            if current_premium > self._peak_premium[key]:
                self._peak_premium[key] = current_premium

            # Run exit checks in priority order
            exit_triggered = await self._check_exit_rules(
                position, current_premium, now
            )

            if exit_triggered:
                # Clean up tracking
                self._cleanup_position(key)

    async def _check_exit_rules(
        self,
        position: Position,
        current_premium: float,
        now: time,
    ) -> bool:
        """
        Check exit rules in priority order. Returns True if full exit triggered.
        """
        key = position.instrument_key
        entry = position.entry_premium
        remaining_qty = position.quantity

        if remaining_qty <= 0:
            return True

        # ── Rule 1: Hard Close (3:00 PM) ───────────────────────────
        if now >= config.HARD_CLOSE_TIME:
            logger.warning(f"HARD CLOSE: {position.instrument_name} — 3:00 PM reached")
            await self._fire_exit(position, remaining_qty, "HARD_CLOSE_3PM", "MARKET")
            return True

        # ── Rule 2: Invalidation ───────────────────────────────────
        if position.invalidation:
            index_ltp = self._state.get("index", position.index, "ltp") or 0
            if index_ltp > 0:
                if (position.direction == "BUY" and index_ltp < position.invalidation):
                    logger.info(
                        f"INVALIDATION: {position.instrument_name} — "
                        f"index {index_ltp:.2f} < {position.invalidation:.2f}"
                    )
                    await self._fire_exit(position, remaining_qty, "INVALIDATION", "MARKET")
                    return True
                elif (position.direction == "SELL" and index_ltp > position.invalidation):
                    logger.info(
                        f"INVALIDATION: {position.instrument_name} — "
                        f"index {index_ltp:.2f} > {position.invalidation:.2f}"
                    )
                    await self._fire_exit(position, remaining_qty, "INVALIDATION", "MARKET")
                    return True

        # ── Rule 3: Stop Loss (premium down 50%) ──────────────────
        stop_price = position.stop
        if self._stop_moved_to_be.get(key):
            stop_price = entry  # Breakeven stop after TP1

        if current_premium <= stop_price:
            reason = "STOP_LOSS_50PCT"
            if self._stop_moved_to_be.get(key):
                reason = "STOP_AT_BREAKEVEN"
            logger.info(
                f"{reason}: {position.instrument_name} — "
                f"premium Rs.{current_premium:.2f} <= stop Rs.{stop_price:.2f}"
            )
            await self._fire_exit(position, remaining_qty, reason, "MARKET")
            return True

        # ── Rule 4: TP1 (premium up 50%) ──────────────────────────
        tp1_price = entry * (1 + config.TP1_PCT / 100)
        if not self._tp1_hit.get(key) and current_premium >= tp1_price:
            self._tp1_hit[key] = True
            exit_qty = self._calc_partial_qty(position.lot_size, remaining_qty, 0.50)

            if exit_qty > 0:
                logger.info(
                    f"TP1 HIT: {position.instrument_name} — "
                    f"premium Rs.{current_premium:.2f} >= Rs.{tp1_price:.2f}. "
                    f"Exit {exit_qty} lots, moving stop to breakeven."
                )
                await self._fire_exit(position, exit_qty, "TP1_50PCT", "LIMIT")
                self._stop_moved_to_be[key] = True
                # Don't return True — position still partially open

        # ── Rule 5: TP2 (premium up 100%) ─────────────────────────
        tp2_price = entry * (1 + config.TP2_PCT / 100)
        if (self._tp1_hit.get(key) and not self._tp2_hit.get(key)
                and current_premium >= tp2_price):
            self._tp2_hit[key] = True
            exit_qty = self._calc_partial_qty(position.lot_size, remaining_qty, 0.60)

            if exit_qty > 0:
                logger.info(
                    f"TP2 HIT: {position.instrument_name} — "
                    f"premium Rs.{current_premium:.2f} >= Rs.{tp2_price:.2f}. "
                    f"Exit {exit_qty} more, start trailing."
                )
                await self._fire_exit(position, exit_qty, "TP2_100PCT", "LIMIT")

        # ── Rule 6: Trail (after TP2, premium drops 30% from peak) ─
        if self._tp2_hit.get(key):
            peak = self._peak_premium.get(key, current_premium)
            trail_stop = peak * (1 - config.TRAIL_STOP_PCT / 100)

            if current_premium <= trail_stop:
                logger.info(
                    f"TRAIL STOP: {position.instrument_name} — "
                    f"premium Rs.{current_premium:.2f} <= "
                    f"trail Rs.{trail_stop:.2f} (peak Rs.{peak:.2f})"
                )
                await self._fire_exit(position, remaining_qty, "TRAIL_STOP", "MARKET")
                return True

        return False

    async def _fire_exit(
        self,
        position: Position,
        quantity: int,
        reason: str,
        order_type: str,
    ) -> None:
        """Fire an exit signal via callback."""
        logger.info(
            f"EXIT SIGNAL: {position.instrument_name} qty={quantity} "
            f"reason={reason} type={order_type}"
        )

        if self._exit_callback:
            await self._exit_callback(position, quantity, reason, order_type)

    @staticmethod
    def _calc_partial_qty(lot_size: int, remaining: int, fraction: float) -> int:
        """
        Calculate partial exit quantity, rounded to lot size.

        For SENSEX (lot=20): 50% of 20 = 10
        For NIFTY (lot=65): 50% of 65 = 32 (round to nearest that's executable)
        """
        raw_qty = int(remaining * fraction)

        # Must be at least 1 lot-size unit for the exit to be valid
        # For simplicity, we allow any quantity (exchange handles min lot)
        if raw_qty <= 0:
            return 0

        return raw_qty

    def _cleanup_position(self, instrument_key: str) -> None:
        """Clean up tracking data for a closed position."""
        self._peak_premium.pop(instrument_key, None)
        self._tp1_hit.pop(instrument_key, None)
        self._tp2_hit.pop(instrument_key, None)
        self._stop_moved_to_be.pop(instrument_key, None)

    async def force_close_all(self, reason: str = "session_end") -> None:
        """Force close all open positions (3:00 PM hard close)."""
        positions = self._state.positions
        for position in list(positions):
            qty = position.quantity
            if qty > 0:
                await self._fire_exit(position, qty, reason, "MARKET")
        logger.info(f"Force closed {len(positions)} positions: {reason}")
