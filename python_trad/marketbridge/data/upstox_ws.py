"""
Upstox v2 WebSocket Client — Live market data feed.

Handles:
- Authorized WebSocket connection to Upstox data feed
- Instrument subscription (index + option strikes)
- Binary protobuf message decoding
- Auto-reconnect with exponential backoff
- Stale data detection (no tick for 10 seconds → reconnect)
- Tick callbacks to state + candle builder

Architecture:
- Runs as a long-lived asyncio task
- Fires callbacks on every tick (non-blocking)
- Self-heals on disconnect/errors
"""

import asyncio
import json
import logging
import time as time_mod
from datetime import datetime
from typing import Callable, Optional, Awaitable

import websockets
from google.protobuf.json_format import MessageToDict

import config
from data.state import MarketState

logger = logging.getLogger(__name__)


class UpstoxWebSocket:
    """
    Async WebSocket client for Upstox v2 live market data feed.

    Usage:
        ws = UpstoxWebSocket(state, rest_client)
        ws.on_tick(my_tick_handler)
        await ws.connect()
        await ws.subscribe(["NSE_INDEX|Nifty 50", "BSE_INDEX|SENSEX"])
    """

    def __init__(self, state: MarketState, rest_client):
        """
        Args:
            state: Shared MarketState instance
            rest_client: UpstoxREST instance for getting WebSocket auth URL
        """
        self._state = state
        self._rest = rest_client
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._subscribed_keys: list[str] = []

        # Reconnection state
        self._reconnect_count = 0
        self._reconnect_delay = config.WS_RECONNECT_BASE_DELAY

        # Stale data detection
        self._last_message_time: float = 0
        self._stale_check_task: Optional[asyncio.Task] = None

        # Tick callbacks: async fn(index: str, ltp: float, volume: int, tick_time: datetime)
        self._tick_callbacks: list[Callable[..., Awaitable[None]]] = []

        # Protobuf decoder — lazy import to handle missing proto gracefully
        self._proto_module = None

        logger.info("UpstoxWebSocket initialized")

    def on_tick(self, callback: Callable[..., Awaitable[None]]) -> None:
        """Register a callback for tick events."""
        self._tick_callbacks.append(callback)

    async def connect(self) -> bool:
        """
        Establish the WebSocket connection.

        1. Get authorized WebSocket URL from REST API
        2. Connect to the URL
        3. Start message loop and stale detection

        Returns:
            True if connection established
        """
        try:
            ws_url = await self._rest.get_ws_auth_url()
            if not ws_url:
                logger.error("Could not get WebSocket auth URL")
                return False

            logger.info(f"Connecting to WebSocket...")
            self._ws = await websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            )

            self._running = True
            self._reconnect_count = 0
            self._reconnect_delay = config.WS_RECONNECT_BASE_DELAY
            self._last_message_time = time_mod.time()

            await self._state.set_ws_connected(True)
            logger.info("WebSocket connected")

            # Start the message processing loop
            asyncio.create_task(self._message_loop())

            # Start stale data detection
            self._stale_check_task = asyncio.create_task(self._stale_detector())

            # Re-subscribe if we had previous subscriptions (reconnect case)
            if self._subscribed_keys:
                await self._send_subscription(self._subscribed_keys, "sub")

            return True

        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}", exc_info=True)
            await self._state.set_ws_connected(False)
            return False

    async def disconnect(self) -> None:
        """Gracefully close the WebSocket connection."""
        self._running = False

        if self._stale_check_task:
            self._stale_check_task.cancel()

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        await self._state.set_ws_connected(False)
        logger.info("WebSocket disconnected")

    async def subscribe(self, instrument_keys: list[str], mode: str = "full") -> None:
        """
        Subscribe to live data for instruments.

        Args:
            instrument_keys: List of Upstox instrument keys
            mode: "full" (OHLC+volume+OI) or "ltpc" (LTP only)
        """
        self._subscribed_keys = instrument_keys
        if self._ws and self._running:
            await self._send_subscription(instrument_keys, "sub", mode)

    async def unsubscribe(self, instrument_keys: list[str]) -> None:
        """Unsubscribe from instruments."""
        self._subscribed_keys = [
            k for k in self._subscribed_keys if k not in instrument_keys
        ]
        if self._ws and self._running:
            await self._send_subscription(instrument_keys, "unsub")

    async def _send_subscription(
        self,
        instrument_keys: list[str],
        method: str,
        mode: str = "full",
    ) -> None:
        """Send a subscription/unsubscription message."""
        message = json.dumps({
            "guid": f"mb_{method}_{int(time_mod.time())}",
            "method": method,
            "data": {
                "mode": mode,
                "instrumentKeys": instrument_keys,
            },
        })

        try:
            await self._ws.send(message)
            logger.info(
                f"WebSocket {method}: {len(instrument_keys)} instruments "
                f"(mode={mode})"
            )
        except Exception as e:
            logger.error(f"Subscription send failed: {e}")

    # ─── Message Processing ────────────────────────────────────────

    async def _message_loop(self) -> None:
        """Main loop — receives and processes WebSocket messages."""
        try:
            async for message in self._ws:
                if not self._running:
                    break

                self._last_message_time = time_mod.time()

                try:
                    await self._process_message(message)
                except Exception as e:
                    logger.error(f"Message processing error: {e}", exc_info=True)

        except websockets.ConnectionClosed as e:
            logger.warning(f"WebSocket connection closed: {e}")
        except Exception as e:
            logger.error(f"WebSocket message loop error: {e}", exc_info=True)
        finally:
            await self._state.set_ws_connected(False)
            if self._running:
                await self._reconnect()

    async def _process_message(self, message) -> None:
        """
        Decode and dispatch a WebSocket message.

        Upstox v2 sends binary protobuf messages.
        Falls back to JSON parsing if protobuf decode fails.
        """
        feeds = {}

        if isinstance(message, bytes):
            feeds = self._decode_protobuf(message)
        elif isinstance(message, str):
            # Some messages (subscription confirmations) come as text
            try:
                data = json.loads(message)
                if "feeds" in data:
                    feeds = data["feeds"]
                else:
                    logger.debug(f"WS text message: {data.get('type', 'unknown')}")
                    return
            except json.JSONDecodeError:
                logger.warning(f"Unparseable WS text message")
                return

        # Dispatch ticks from decoded feeds
        for instrument_key, feed_data in feeds.items():
            await self._dispatch_tick(instrument_key, feed_data)

    def _decode_protobuf(self, binary_data: bytes) -> dict:
        """
        Decode binary protobuf message from Upstox.

        Returns:
            Dict of {instrument_key: feed_data}
        """
        try:
            # Lazy-load protobuf module
            if self._proto_module is None:
                try:
                    from upstox_client.feeder import market_data_feed_pb2
                    self._proto_module = market_data_feed_pb2
                except ImportError:
                    logger.warning(
                        "upstox_client proto module not found. "
                        "Falling back to raw binary handling."
                    )
                    return self._decode_protobuf_fallback(binary_data)

            feed_response = self._proto_module.FeedResponse()
            feed_response.ParseFromString(binary_data)
            data_dict = MessageToDict(feed_response)
            return data_dict.get("feeds", {})

        except Exception as e:
            logger.error(f"Protobuf decode error: {e}")
            return {}

    def _decode_protobuf_fallback(self, binary_data: bytes) -> dict:
        """
        Fallback decoder when upstox_client proto is unavailable.
        Attempts basic JSON parsing of the binary payload.
        """
        try:
            text = binary_data.decode("utf-8", errors="ignore")
            data = json.loads(text)
            return data.get("feeds", {})
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    async def _dispatch_tick(self, instrument_key: str, feed_data: dict) -> None:
        """
        Extract LTP and volume from feed data and fire tick callbacks.

        Handles both LTPC mode and Full Feed mode responses.
        """
        # Determine which index this is for
        index = self._key_to_index(instrument_key)
        if not index:
            return  # Option tick — may still be useful for OI updates

        # Extract LTP from various possible response structures
        ltp = None
        volume = 0
        tick_time = datetime.now()

        # Full feed format
        ff = feed_data.get("ff", feed_data.get("fullFeed", {}))
        if ff:
            market_ff = ff.get("marketFF", ff.get("indexFF", {}))
            ltpc = market_ff.get("ltpc", {})
            ltp = ltpc.get("ltp", 0.0)
            volume = market_ff.get("marketLevel", {}).get("lastTradeQty", 0)

            ts_str = ltpc.get("ltt", "")
            if ts_str:
                try:
                    tick_time = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

        # LTPC format (simpler)
        ltpc_data = feed_data.get("ltpc", {})
        if not ltp and ltpc_data:
            ltp = ltpc_data.get("ltp", 0.0)

        if ltp is None or ltp == 0:
            return

        # Update state
        await self._state.update_tick(index, float(ltp), tick_time)

        # Fire tick callbacks
        for callback in self._tick_callbacks:
            try:
                await callback(index, float(ltp), volume, tick_time)
            except Exception as e:
                logger.error(f"Tick callback error: {e}", exc_info=True)

    @staticmethod
    def _key_to_index(instrument_key: str) -> Optional[str]:
        """Map an instrument key to NIFTY or SENSEX."""
        if "Nifty 50" in instrument_key or "NIFTY" in instrument_key.upper():
            return "NIFTY"
        elif "SENSEX" in instrument_key.upper():
            return "SENSEX"
        return None

    # ─── Stale Data Detection ──────────────────────────────────────

    async def _stale_detector(self) -> None:
        """
        Check for stale data — if no tick received for WS_STALE_TIMEOUT_SECONDS,
        trigger a reconnect.

        Runs as a background task, checks every 5 seconds.
        """
        while self._running:
            await asyncio.sleep(5)

            if not self._running:
                break

            elapsed = time_mod.time() - self._last_message_time
            if elapsed > config.WS_STALE_TIMEOUT_SECONDS:
                logger.warning(
                    f"Stale data detected — no tick for {elapsed:.0f}s. "
                    "Triggering reconnect."
                )
                # Close current connection to trigger reconnect
                if self._ws:
                    await self._ws.close()
                break

    # ─── Auto-Reconnect ────────────────────────────────────────────

    async def _reconnect(self) -> None:
        """
        Reconnect with exponential backoff.

        Backs off: 1s, 2s, 4s, 8s, ... up to WS_RECONNECT_MAX_DELAY.
        Gives up after WS_RECONNECT_MAX_ATTEMPTS consecutive failures.
        """
        if not self._running:
            return

        self._reconnect_count += 1

        if self._reconnect_count > config.WS_RECONNECT_MAX_ATTEMPTS:
            logger.critical(
                f"WebSocket reconnect failed after "
                f"{config.WS_RECONNECT_MAX_ATTEMPTS} attempts. "
                "MANUAL INTERVENTION REQUIRED."
            )
            return

        delay = min(
            self._reconnect_delay,
            config.WS_RECONNECT_MAX_DELAY,
        )

        logger.info(
            f"WebSocket reconnecting in {delay}s "
            f"(attempt {self._reconnect_count}/{config.WS_RECONNECT_MAX_ATTEMPTS})"
        )

        await asyncio.sleep(delay)

        # Exponential backoff
        self._reconnect_delay = min(
            self._reconnect_delay * 2,
            config.WS_RECONNECT_MAX_DELAY,
        )

        success = await self.connect()
        if not success:
            await self._reconnect()  # Retry with increased backoff
