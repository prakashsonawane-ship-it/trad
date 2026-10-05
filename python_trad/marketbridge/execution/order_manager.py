"""
Order Manager — Places, tracks, and manages orders via Upstox API.

Rules:
- Limit orders only (never market orders for entries)
- If not filled in 30s → cancel
- One retry at ask + 1 tick
- Max 2 open positions enforced
- Daily loss limit (Rs.1,500) blocks new entries
- Paper mode: logs what it would have done, no real orders

Exits use market orders (speed over price for risk management).
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from data.state import MarketState, Position
from data.upstox_rest import UpstoxREST
import config

logger = logging.getLogger(__name__)

# Order fill timeout
ORDER_FILL_TIMEOUT = 30  # seconds
ORDER_RETRY_COUNT = 1


class OrderManager:
    """
    Manages order placement, fill tracking, and position creation.
    """

    def __init__(self, state: MarketState, rest_client: UpstoxREST):
        self._state = state
        self._rest = rest_client
        self._pending_orders: dict[str, dict] = {}
        self._order_count = 0
        logger.info(
            f"OrderManager initialized "
            f"(paper={'ON' if config.PAPER_MODE else 'OFF'})"
        )

    async def place_entry_order(self, watch_data: dict) -> Optional[str]:
        """
        Place an entry order based on confirmed entry watch data.

        Args:
            watch_data: Dict from EntryWatcher containing option_selection, direction, etc.

        Returns:
            Order ID string, or None if blocked/failed.
        """
        # ── Pre-flight checks ──────────────────────────────────────
        # Check max positions
        if len(self._state.positions) >= config.MAX_OPEN_POSITIONS:
            logger.warning(
                f"Order blocked: {len(self._state.positions)} positions open "
                f"(max {config.MAX_OPEN_POSITIONS})"
            )
            return None

        # Check daily loss limit
        if self._state.daily_pnl <= -config.DAILY_LOSS_LIMIT:
            logger.warning(
                f"Order blocked: daily PnL Rs.{self._state.daily_pnl:.0f} "
                f"exceeds loss limit Rs.{config.DAILY_LOSS_LIMIT}"
            )
            return None

        # Check time
        if datetime.now().time() >= config.NO_ENTRY_AFTER:
            logger.warning("Order blocked: past NO_ENTRY_AFTER time")
            return None

        option = watch_data["option_selection"]
        direction = watch_data["direction"]

        # ── Budget verification ────────────────────────────────────
        if option["total_cost"] > config.MAX_TRADE_COST:
            logger.warning(
                f"Order blocked: cost Rs.{option['total_cost']:.2f} > "
                f"max Rs.{config.MAX_TRADE_COST}"
            )
            return None

        # ── Place order ────────────────────────────────────────────
        order_price = option["premium"]  # Limit at current ask
        order_id = await self._place_limit_order(
            instrument_key=option["instrument_key"],
            instrument_name=option["instrument_name"],
            quantity=option["lot_size"],
            price=order_price,
            transaction_type="BUY",  # Always BUY options (CE or PE)
        )

        if not order_id:
            return None

        # Track pending order
        self._pending_orders[order_id] = {
            "watch_data": watch_data,
            "order_price": order_price,
            "placed_at": datetime.now(),
            "retries": 0,
            "status": "PENDING",
        }

        # Start fill monitoring
        asyncio.create_task(self._monitor_fill(order_id))

        return order_id

    async def place_exit_order(
        self,
        position: Position,
        quantity: int,
        reason: str,
        order_type: str = "MARKET",
    ) -> Optional[str]:
        """
        Place an exit order for a position.

        Exits use MARKET orders for speed (risk management priority).

        Args:
            position: The Position to exit
            quantity: Number of units to exit
            reason: Why we're exiting (for logging)
            order_type: "MARKET" (default) or "LIMIT"
        """
        if quantity <= 0:
            return None

        logger.info(
            f"EXIT: {position.instrument_name} qty={quantity} "
            f"reason={reason} type={order_type}"
        )

        if order_type == "MARKET":
            order_id = await self._place_market_order(
                instrument_key=position.instrument_key,
                instrument_name=position.instrument_name,
                quantity=quantity,
                transaction_type="SELL",
            )
        else:
            order_id = await self._place_limit_order(
                instrument_key=position.instrument_key,
                instrument_name=position.instrument_name,
                quantity=quantity,
                price=position.current_premium or position.entry_premium,
                transaction_type="SELL",
            )

        return order_id

    # ─── Internal Order Placement ──────────────────────────────────

    async def _place_limit_order(
        self,
        instrument_key: str,
        instrument_name: str,
        quantity: int,
        price: float,
        transaction_type: str,
    ) -> Optional[str]:
        """Place a limit order."""
        self._order_count += 1
        order_id = f"MB_{self._order_count:04d}_{datetime.now().strftime('%H%M%S')}"

        if config.PAPER_MODE:
            logger.info(
                f"[PAPER] LIMIT {transaction_type} {instrument_name} "
                f"qty={quantity} price=Rs.{price:.2f} → {order_id}"
            )
            return order_id

        # Live order via Upstox API
        try:
            result = await self._rest.place_order(
                instrument_key=instrument_key,
                quantity=quantity,
                price=price,
                transaction_type=transaction_type,
                order_type="LIMIT",
            )
            real_order_id = result.get("order_id", order_id)
            logger.info(
                f"[LIVE] LIMIT {transaction_type} {instrument_name} "
                f"qty={quantity} price=Rs.{price:.2f} → {real_order_id}"
            )
            return real_order_id

        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return None

    async def _place_market_order(
        self,
        instrument_key: str,
        instrument_name: str,
        quantity: int,
        transaction_type: str,
    ) -> Optional[str]:
        """Place a market order (for exits only)."""
        self._order_count += 1
        order_id = f"MB_{self._order_count:04d}_{datetime.now().strftime('%H%M%S')}"

        if config.PAPER_MODE:
            logger.info(
                f"[PAPER] MARKET {transaction_type} {instrument_name} "
                f"qty={quantity} → {order_id}"
            )
            return order_id

        try:
            result = await self._rest.place_order(
                instrument_key=instrument_key,
                quantity=quantity,
                price=0,
                transaction_type=transaction_type,
                order_type="MARKET",
            )
            real_order_id = result.get("order_id", order_id)
            logger.info(
                f"[LIVE] MARKET {transaction_type} {instrument_name} "
                f"qty={quantity} → {real_order_id}"
            )
            return real_order_id

        except Exception as e:
            logger.error(f"Market order failed: {e}")
            return None

    # ─── Fill Monitoring ───────────────────────────────────────────

    async def _monitor_fill(self, order_id: str) -> None:
        """
        Monitor an order for fill. Cancel after 30s, retry once at ask+1.
        """
        pending = self._pending_orders.get(order_id)
        if not pending:
            return

        # Wait for fill timeout
        await asyncio.sleep(ORDER_FILL_TIMEOUT)

        # Check if still pending
        if order_id not in self._pending_orders:
            return  # Already filled or cancelled

        pending = self._pending_orders[order_id]
        if pending["status"] == "FILLED":
            return

        # Cancel the order
        logger.info(f"Order {order_id} not filled in {ORDER_FILL_TIMEOUT}s — cancelling")
        await self._cancel_order(order_id)

        # Retry once at ask + 1 tick
        if pending["retries"] < ORDER_RETRY_COUNT:
            pending["retries"] += 1
            watch_data = pending["watch_data"]
            option = watch_data["option_selection"]

            # Retry at slightly higher price
            retry_price = pending["order_price"] + config.ORDER_RETRY_TICKS
            logger.info(
                f"Retrying order at Rs.{retry_price:.2f} "
                f"(+{config.ORDER_RETRY_TICKS} tick)"
            )

            new_order_id = await self._place_limit_order(
                instrument_key=option["instrument_key"],
                instrument_name=option["instrument_name"],
                quantity=option["lot_size"],
                price=retry_price,
                transaction_type="BUY",
            )

            if new_order_id:
                self._pending_orders[new_order_id] = {
                    "watch_data": watch_data,
                    "order_price": retry_price,
                    "placed_at": datetime.now(),
                    "retries": pending["retries"],
                    "status": "PENDING",
                }
                # Monitor the retry
                asyncio.create_task(self._monitor_fill(new_order_id))
        else:
            logger.warning(f"Order {order_id} failed after {ORDER_RETRY_COUNT} retries")

        # Clean up
        del self._pending_orders[order_id]

    async def _cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        if config.PAPER_MODE:
            logger.info(f"[PAPER] Cancel order {order_id}")
            return True

        try:
            await self._rest.cancel_order(order_id)
            return True
        except Exception as e:
            logger.error(f"Cancel order failed: {e}")
            return False

    async def on_order_filled(self, order_id: str, fill_price: float) -> None:
        """
        Called when an order is filled (from WebSocket or polling).
        Creates a Position in state.
        """
        pending = self._pending_orders.get(order_id)
        if not pending:
            return

        pending["status"] = "FILLED"
        watch_data = pending["watch_data"]
        option = watch_data["option_selection"]

        # Create Position
        position = Position(
            instrument_key=option["instrument_key"],
            instrument_name=option["instrument_name"],
            index=watch_data["index"],
            direction=watch_data["direction"],
            option_type=option["option_type"],
            strike=option["strike"],
            lot_size=option["lot_size"],
            quantity=option["lot_size"],
            entry_premium=fill_price,
            invalidation=watch_data.get("invalidation"),
        )

        await self._state.add_position(position)

        logger.info(
            f"Position opened: {position.instrument_name} "
            f"entry=Rs.{fill_price:.2f} "
            f"cost=Rs.{position.entry_cost:.2f} "
            f"stop=Rs.{position.stop:.2f}"
        )

        # Clean up
        del self._pending_orders[order_id]

    @property
    def stats(self) -> dict:
        return {
            "total_orders": self._order_count,
            "pending_orders": len(self._pending_orders),
            "paper_mode": config.PAPER_MODE,
        }
