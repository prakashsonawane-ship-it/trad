"""
Upstox v2 REST Client — Auth, market data, option chain, orders.

Handles:
- OAuth2 authorization code flow (one-time setup)
- Access token persistence and refresh
- Instrument list and option chain fetching
- Historical candle data for indicator seeding
- Order placement, cancellation, and status polling

All runtime methods are async (aiohttp). Auth flow is sync (one-time).
"""

import json
import asyncio
import logging
import webbrowser
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs

import aiohttp

import config

logger = logging.getLogger(__name__)

# Token file persists access token across restarts
TOKEN_FILE = config.BASE_DIR / ".upstox_token.json"


class UpstoxREST:
    """Async REST client for Upstox v2 API."""

    def __init__(self):
        self._access_token: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._load_saved_token()

    # ─── Auth Flow ─────────────────────────────────────────────────

    def _load_saved_token(self) -> None:
        """Load previously saved access token."""
        if TOKEN_FILE.exists():
            try:
                data = json.loads(TOKEN_FILE.read_text())
                saved_date = data.get("date", "")
                # Upstox tokens are valid for 1 day
                if saved_date == date.today().isoformat():
                    self._access_token = data["access_token"]
                    logger.info("Loaded saved access token (valid for today)")
                else:
                    logger.info("Saved token expired, re-auth required")
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Could not load saved token: {e}")

    def _save_token(self, token: str) -> None:
        """Persist access token for the day."""
        TOKEN_FILE.write_text(json.dumps({
            "access_token": token,
            "date": date.today().isoformat(),
        }))
        logger.info("Access token saved")

    @property
    def is_authenticated(self) -> bool:
        return self._access_token is not None

    @property
    def access_token(self) -> Optional[str]:
        return self._access_token

    def get_auth_url(self) -> str:
        """
        Get the Upstox authorization URL.
        User must visit this URL, log in, and provide the redirect URL
        containing the auth code.
        """
        params = {
            "client_id": config.UPSTOX_API_KEY,
            "redirect_uri": config.UPSTOX_REDIRECT_URI,
            "response_type": "code",
        }
        url = f"{config.UPSTOX_AUTH_URL}?{urlencode(params)}"
        return url

    async def authenticate_with_code(self, auth_code: str) -> bool:
        """
        Exchange authorization code for access token.

        Args:
            auth_code: The code from Upstox redirect URL query param

        Returns:
            True if authentication succeeded
        """
        payload = {
            "code": auth_code,
            "client_id": config.UPSTOX_API_KEY,
            "client_secret": config.UPSTOX_API_SECRET,
            "redirect_uri": config.UPSTOX_REDIRECT_URI,
            "grant_type": "authorization_code",
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    config.UPSTOX_TOKEN_URL,
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self._access_token = data["access_token"]
                        self._save_token(self._access_token)
                        logger.info("Authentication successful")
                        return True
                    else:
                        body = await resp.text()
                        logger.error(f"Auth failed ({resp.status}): {body}")
                        return False
            except Exception as e:
                logger.error(f"Auth request failed: {e}", exc_info=True)
                return False

    def open_auth_in_browser(self) -> None:
        """Open the Upstox login page in the default browser."""
        url = self.get_auth_url()
        logger.info(f"Opening auth URL: {url}")
        webbrowser.open(url)

    def extract_code_from_url(self, redirect_url: str) -> Optional[str]:
        """Extract authorization code from the redirect URL."""
        try:
            parsed = urlparse(redirect_url)
            code = parse_qs(parsed.query).get("code", [None])[0]
            return code
        except Exception as e:
            logger.error(f"Could not extract auth code: {e}")
            return None

    # ─── Session Management ────────────────────────────────────────

    def _headers(self) -> dict:
        """Standard auth headers for API requests."""
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Get or create the aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("HTTP session closed")

    async def _get(self, url: str, params: dict = None) -> Optional[dict]:
        """Make an authenticated GET request with error handling."""
        session = await self._ensure_session()
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", data)
                elif resp.status == 429:
                    logger.warning("Rate limited — waiting 1 second")
                    await asyncio.sleep(1)
                    return await self._get(url, params)  # Retry once
                else:
                    body = await resp.text()
                    logger.error(f"GET {url} failed ({resp.status}): {body}")
                    return None
        except aiohttp.ClientError as e:
            logger.error(f"GET {url} error: {e}")
            return None

    async def _post(self, url: str, payload: dict) -> Optional[dict]:
        """Make an authenticated POST request with error handling."""
        session = await self._ensure_session()
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", data)
                elif resp.status == 429:
                    logger.warning("Rate limited — waiting 1 second")
                    await asyncio.sleep(1)
                    return await self._post(url, payload)
                else:
                    body = await resp.text()
                    logger.error(f"POST {url} failed ({resp.status}): {body}")
                    return None
        except aiohttp.ClientError as e:
            logger.error(f"POST {url} error: {e}")
            return None

    async def _delete(self, url: str, params: dict = None) -> Optional[dict]:
        """Make an authenticated DELETE request."""
        session = await self._ensure_session()
        try:
            async with session.delete(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", data)
                else:
                    body = await resp.text()
                    logger.error(f"DELETE {url} failed ({resp.status}): {body}")
                    return None
        except aiohttp.ClientError as e:
            logger.error(f"DELETE {url} error: {e}")
            return None

    # ─── Market Data ───────────────────────────────────────────────

    async def get_market_quote(self, instrument_keys: list[str]) -> Optional[dict]:
        """
        Fetch full market quotes for given instruments.

        Args:
            instrument_keys: e.g. ["NSE_INDEX|Nifty 50", "BSE_INDEX|SENSEX"]

        Returns:
            Dict of instrument_key → quote data
        """
        keys_param = ",".join(instrument_keys)
        return await self._get(config.UPSTOX_MARKET_QUOTE_URL, {"instrument_key": keys_param})

    async def get_ltp(self, instrument_keys: list[str]) -> Optional[dict]:
        """Fetch just the LTP for given instruments."""
        keys_param = ",".join(instrument_keys)
        return await self._get(config.UPSTOX_LTP_URL, {"instrument_key": keys_param})

    # ─── Option Chain ──────────────────────────────────────────────

    async def get_option_chain(
        self,
        instrument_key: str,
        expiry_date: str,
    ) -> Optional[dict]:
        """
        Fetch the option chain for an index.

        Args:
            instrument_key: e.g. "NSE_INDEX|Nifty 50"
            expiry_date: YYYY-MM-DD format

        Returns:
            Dict with option chain data including strikes, OI, greeks
        """
        params = {
            "instrument_key": instrument_key,
            "expiry_date": expiry_date,
        }
        return await self._get(config.UPSTOX_OPTION_CHAIN_URL, params)

    async def get_option_chain_parsed(
        self,
        index: str,
        expiry_date: str,
    ) -> dict:
        """
        Fetch and parse the option chain into a structured format.

        Returns:
            {strike: {"CE": {ltp, oi, volume, iv, instrument_key, ...},
                       "PE": {ltp, oi, volume, iv, instrument_key, ...}}}
        """
        instrument_key = (
            config.NIFTY_INDEX_KEY if index == "NIFTY" else config.SENSEX_INDEX_KEY
        )

        raw = await self.get_option_chain(instrument_key, expiry_date)
        if not raw:
            logger.warning(f"Empty option chain for {index} expiry {expiry_date}")
            return {}

        parsed = {}
        chain_data = raw if isinstance(raw, list) else raw.get("data", [])

        for entry in chain_data:
            strike = entry.get("strike_price", 0)
            if strike == 0:
                continue

            if strike not in parsed:
                parsed[strike] = {"CE": {}, "PE": {}}

            # Parse call data
            call = entry.get("call_options", {})
            if call:
                market_data = call.get("market_data", {})
                option_greeks = call.get("option_greeks", {})
                parsed[strike]["CE"] = {
                    "instrument_key": call.get("instrument_key", ""),
                    "ltp": market_data.get("ltp", 0.0),
                    "oi": market_data.get("oi", 0),
                    "volume": market_data.get("volume", 0),
                    "bid": market_data.get("bid_price", 0.0),
                    "ask": market_data.get("ask_price", 0.0),
                    "iv": option_greeks.get("iv", 0.0),
                    "delta": option_greeks.get("delta", 0.0),
                    "gamma": option_greeks.get("gamma", 0.0),
                    "theta": option_greeks.get("theta", 0.0),
                }

            # Parse put data
            put = entry.get("put_options", {})
            if put:
                market_data = put.get("market_data", {})
                option_greeks = put.get("option_greeks", {})
                parsed[strike]["PE"] = {
                    "instrument_key": put.get("instrument_key", ""),
                    "ltp": market_data.get("ltp", 0.0),
                    "oi": market_data.get("oi", 0),
                    "volume": market_data.get("volume", 0),
                    "bid": market_data.get("bid_price", 0.0),
                    "ask": market_data.get("ask_price", 0.0),
                    "iv": option_greeks.get("iv", 0.0),
                    "delta": option_greeks.get("delta", 0.0),
                    "gamma": option_greeks.get("gamma", 0.0),
                    "theta": option_greeks.get("theta", 0.0),
                }

        logger.info(f"Parsed option chain for {index}: {len(parsed)} strikes")
        return parsed

    # ─── Historical Data ───────────────────────────────────────────

    async def get_historical_candles(
        self,
        instrument_key: str,
        interval: str = "1minute",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> list[dict]:
        """
        Fetch historical OHLCV candle data.

        Args:
            instrument_key: Upstox instrument key
            interval: "1minute", "30minute", "day", "week", "month"
            from_date: YYYY-MM-DD (default: 20 days ago)
            to_date: YYYY-MM-DD (default: today)

        Returns:
            List of candle dicts [{timestamp, open, high, low, close, volume}, ...]
        """
        if to_date is None:
            to_date = date.today().isoformat()
        if from_date is None:
            from_date = (date.today() - timedelta(days=20)).isoformat()

        url = f"{config.UPSTOX_HISTORICAL_URL}/{instrument_key}/{interval}/{to_date}/{from_date}"
        data = await self._get(url)

        if not data:
            return []

        candles_raw = data if isinstance(data, list) else data.get("candles", [])
        candles = []
        for c in candles_raw:
            # Upstox returns: [timestamp, open, high, low, close, volume, oi]
            if isinstance(c, list) and len(c) >= 6:
                candles.append({
                    "timestamp": c[0],
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": int(c[5]),
                })

        logger.info(f"Fetched {len(candles)} historical candles for {instrument_key}")
        return candles

    # ─── WebSocket Authorization ───────────────────────────────────

    async def get_ws_auth_url(self) -> Optional[str]:
        """
        Get the authorized WebSocket URL for market data feed.

        Returns:
            The WebSocket URL to connect to, or None on failure
        """
        data = await self._get(config.UPSTOX_WS_AUTH_URL)
        if data:
            ws_url = data.get("authorized_redirect_uri", data.get("authorizedRedirectUri"))
            if ws_url:
                logger.info("Got WebSocket auth URL")
                return ws_url
        logger.error("Failed to get WebSocket auth URL")
        return None

    # ─── Order Management ──────────────────────────────────────────

    async def place_order(
        self,
        instrument_key: str,
        transaction_type: str,    # "BUY" or "SELL"
        quantity: int,
        price: float,
        order_type: str = "LIMIT",
        product: str = "D",      # "D" = Day / Intraday
        validity: str = "DAY",
    ) -> Optional[str]:
        """
        Place an order via Upstox API.

        Args:
            instrument_key: Full Upstox instrument key
            transaction_type: "BUY" or "SELL"
            quantity: Number of shares/contracts
            price: Limit price (ignored for MARKET orders)
            order_type: "LIMIT" or "MARKET"
            product: "D" (intraday) or "I" (delivery)
            validity: "DAY" or "IOC"

        Returns:
            Order ID string, or None on failure
        """
        if config.PAPER_MODE:
            paper_id = f"PAPER_{datetime.now().strftime('%H%M%S')}_{instrument_key[-6:]}"
            logger.info(
                f"[PAPER] Order placed: {transaction_type} {quantity} "
                f"{instrument_key} @ ₹{price:.2f} → {paper_id}"
            )
            return paper_id

        payload = {
            "instrument_token": instrument_key,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "price": price,
            "order_type": order_type,
            "product": product,
            "validity": validity,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }

        data = await self._post(config.UPSTOX_PLACE_ORDER_URL, payload)
        if data:
            order_id = data.get("order_id", "")
            logger.info(
                f"Order placed: {transaction_type} {quantity} "
                f"{instrument_key} @ ₹{price:.2f} → {order_id}"
            )
            return order_id

        return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        if config.PAPER_MODE:
            logger.info(f"[PAPER] Order cancelled: {order_id}")
            return True

        data = await self._delete(
            config.UPSTOX_CANCEL_ORDER_URL,
            params={"order_id": order_id},
        )
        if data is not None:
            logger.info(f"Order cancelled: {order_id}")
            return True
        return False

    async def get_order_status(self, order_id: str) -> Optional[dict]:
        """
        Get status of a specific order.

        Returns:
            Order dict with status, filled_quantity, average_price, etc.
        """
        if config.PAPER_MODE:
            return {
                "order_id": order_id,
                "status": "complete",
                "filled_quantity": 0,
                "average_price": 0.0,
            }

        data = await self._get(config.UPSTOX_ORDER_BOOK_URL)
        if data:
            orders = data if isinstance(data, list) else [data]
            for order in orders:
                if order.get("order_id") == order_id:
                    return order
        return None

    async def get_all_orders(self) -> list[dict]:
        """Get all orders for today."""
        data = await self._get(config.UPSTOX_ORDER_BOOK_URL)
        if data:
            return data if isinstance(data, list) else [data]
        return []

    async def place_market_exit(
        self,
        instrument_key: str,
        transaction_type: str,
        quantity: int,
    ) -> Optional[str]:
        """
        Place a market order for exiting a position.

        Used for stop losses, hard close, and invalidation exits where
        getting filled is more important than price.
        """
        return await self.place_order(
            instrument_key=instrument_key,
            transaction_type=transaction_type,
            quantity=quantity,
            price=0,
            order_type="MARKET",
        )
