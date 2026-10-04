"""
Canonical Market State — Single source of truth for the entire system.

Every module reads from and writes to this shared state dict.
All access is asyncio-safe (single event loop, no threading).
No disk I/O on the hot path — everything in memory.
"""

import asyncio
import logging
from datetime import datetime, time
from typing import Any, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Tracks a single open option position."""
    instrument_key: str          # Upstox instrument key
    instrument_name: str         # Human-readable: "SENSEX 81500 PE"
    index: str                   # "NIFTY" or "SENSEX"
    direction: str               # "BUY" or "SELL" (the index signal direction)
    option_type: str             # "CE" or "PE"
    strike: int                  # Strike price
    lot_size: int                # 65 (NIFTY) or 20 (SENSEX)
    quantity: int                # Total lots × lot_size
    entry_premium: float         # Price paid per unit
    entry_time: datetime = field(default_factory=datetime.now)
    entry_cost: float = 0.0     # entry_premium × quantity
    order_id: str = ""

    # Exit tracking
    invalidation: float = 0.0   # Index level that breaks the thesis
    tp1_hit: bool = False
    tp2_hit: bool = False
    stop: float = 0.0           # Current stop level (moves to breakeven after TP1)
    trail_high: float = 0.0     # Highest premium seen (for trailing stop)
    remaining_qty: int = 0      # Quantity still held after partial exits
    realized_pnl: float = 0.0   # P&L from partial exits

    # Agent context
    scenario_type: str = ""     # LIQUIDITY_TRAP, BREAKOUT, etc.
    agent_reasoning: str = ""   # Why the agent took this trade
    confidence: int = 0         # Agent confidence 1–10

    def __post_init__(self):
        if self.entry_cost == 0:
            self.entry_cost = self.entry_premium * self.quantity
        if self.remaining_qty == 0:
            self.remaining_qty = self.quantity
        if self.stop == 0:
            # Default stop: 50% of entry premium
            self.stop = self.entry_premium * 0.5


@dataclass
class Candle:
    """Single OHLCV candle."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


class MarketState:
    """
    Thread-safe (asyncio-safe) shared state for the entire system.

    Single event loop architecture — no locks needed, but we use
    asyncio.Lock as a safety net for any future multi-task writes.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = self._initial_state()
        self._callbacks: list[callable] = []
        logger.info("MarketState initialized")

    def _initial_state(self) -> dict:
        """Build the canonical state structure."""
        return {
            # ── Index LTP & OHLC ──────────────────────────────────
            "index": {
                "NIFTY": {
                    "ltp": 0.0,
                    "open": 0.0,
                    "high": 0.0,
                    "low": 0.0,
                    "close": 0.0,      # Previous day close
                    "change_pct": 0.0,
                    "last_tick_time": None,
                },
                "SENSEX": {
                    "ltp": 0.0,
                    "open": 0.0,
                    "high": 0.0,
                    "low": 0.0,
                    "close": 0.0,
                    "change_pct": 0.0,
                    "last_tick_time": None,
                },
            },

            # ── Candle History (per index, per timeframe) ──────────
            "candles": {
                "NIFTY": {"1m": [], "3m": [], "5m": [], "15m": [], "30m": [], "1h": []},
                "SENSEX": {"1m": [], "3m": [], "5m": [], "15m": [], "30m": [], "1h": []},
            },

            # ── Option Chain Snapshots ─────────────────────────────
            "option_chain": {
                "NIFTY": {},   # {strike: {"CE": {...}, "PE": {...}}}
                "SENSEX": {},
            },

            # ── Computed Indicators (from state_engine.py) ─────────
            "indicators": {
                "NIFTY": {},
                "SENSEX": {},
            },

            # ── Analysis Stream Outputs ────────────────────────────
            "analysis": {
                "structure": {},   # Market structure stream A
                "flow": {},        # Liquidity flow stream B
                "sentiment": {},   # Sentiment stream C
            },

            # ── Current Affairs / Macro Context ────────────────────
            "macro_context": {
                "rbi_event_today": False,
                "global_sentiment": "NEUTRAL",
                "sgx_nifty_gap": "0.0%",
                "crude_change": "0.0%",
                "usd_inr": "0.0 stable",
                "earnings_today": [],
                "key_headline": "",
                "agent_bias_note": "No data yet.",
                "impact_level": "LOW",
            },

            # ── Open Positions ─────────────────────────────────────
            "positions": [],       # List[Position]

            # ── Session P&L ────────────────────────────────────────
            "daily_pnl": 0.0,
            "total_trades_today": 0,
            "winning_trades": 0,
            "losing_trades": 0,

            # ── Session Control ────────────────────────────────────
            "session_active": False,
            "trading_blocked": False,    # Set by news_bias on HIGH impact events
            "entries_blocked": False,    # Set after daily loss limit or 2:15 PM
            "today_index": None,         # "NIFTY" or "SENSEX" (set by main.py)
            "today_lot_size": 0,         # 65 or 20

            # ── Opening Range ──────────────────────────────────────
            "opening_range": {
                "high": None,
                "low": None,
                "formed": False,
            },

            # ── Agent Tracking ─────────────────────────────────────
            "agent_calls_today": 0,
            "last_agent_call_time": None,
            "agent_decisions": [],   # Rolling last-5 decisions

            # ── WebSocket Health ───────────────────────────────────
            "ws_connected": False,
            "ws_last_message_time": None,
            "ws_reconnect_count": 0,
        }

    # ── Read Access ────────────────────────────────────────────────

    def get(self, *keys: str, default: Any = None) -> Any:
        """
        Get nested state value by dot-path keys.
        Usage: state.get("index", "NIFTY", "ltp")
        """
        current = self._state
        for key in keys:
            if isinstance(current, dict):
                current = current.get(key, default)
            else:
                return default
            if current is default:
                return default
        return current

    @property
    def index_data(self) -> dict:
        return self._state["index"]

    @property
    def positions(self) -> list[Position]:
        return self._state["positions"]

    @property
    def daily_pnl(self) -> float:
        return self._state["daily_pnl"]

    @property
    def session_active(self) -> bool:
        return self._state["session_active"]

    @property
    def trading_blocked(self) -> bool:
        return self._state["trading_blocked"]

    @property
    def entries_blocked(self) -> bool:
        return self._state["entries_blocked"]

    @property
    def today_index(self) -> Optional[str]:
        return self._state["today_index"]

    @property
    def macro_context(self) -> dict:
        return self._state["macro_context"]

    @property
    def opening_range(self) -> dict:
        return self._state["opening_range"]

    @property
    def ws_connected(self) -> bool:
        return self._state["ws_connected"]

    def option_ltp(self, instrument_key: str) -> Optional[float]:
        """Get the LTP for a specific option instrument from the chain."""
        for index_name in ("NIFTY", "SENSEX"):
            chain = self._state["option_chain"].get(index_name, {})
            for strike, options in chain.items():
                for opt_type in ("CE", "PE"):
                    opt = options.get(opt_type, {})
                    if opt.get("instrument_key") == instrument_key:
                        return opt.get("ltp", 0.0)
        return None

    def get_candles(self, index: str, timeframe: str = "1m") -> list[Candle]:
        """Get candle list for an index and timeframe."""
        return self._state["candles"].get(index, {}).get(timeframe, [])

    # ── Write Access ───────────────────────────────────────────────

    async def update(self, *keys: str, value: Any) -> None:
        """
        Set nested state value by dot-path keys.
        Usage: await state.update("index", "NIFTY", "ltp", value=24500.50)
        """
        async with self._lock:
            current = self._state
            for key in keys[:-1]:
                if key not in current:
                    current[key] = {}
                current = current[key]
            current[keys[-1]] = value

    async def update_tick(self, index: str, ltp: float, tick_time: datetime) -> None:
        """Fast-path update for incoming ticks — the most frequent operation."""
        async with self._lock:
            idx = self._state["index"][index]
            idx["ltp"] = ltp
            idx["last_tick_time"] = tick_time

            # Update session high/low
            if idx["open"] == 0.0:
                idx["open"] = ltp
            if ltp > idx["high"] or idx["high"] == 0.0:
                idx["high"] = ltp
            if ltp < idx["low"] or idx["low"] == 0.0:
                idx["low"] = ltp

            # Update opening range if still forming
            opening = self._state["opening_range"]
            if not opening["formed"]:
                if opening["high"] is None or ltp > opening["high"]:
                    opening["high"] = ltp
                if opening["low"] is None or ltp < opening["low"]:
                    opening["low"] = ltp

            # Update WebSocket health
            self._state["ws_last_message_time"] = tick_time

    async def add_candle(self, index: str, timeframe: str, candle: Candle) -> None:
        """Append a closed candle to history."""
        async with self._lock:
            candles = self._state["candles"][index][timeframe]
            candles.append(candle)
            # Keep last 500 candles per timeframe to limit memory
            if len(candles) > 500:
                self._state["candles"][index][timeframe] = candles[-500:]

    async def add_position(self, position: Position) -> None:
        """Add a new open position."""
        async with self._lock:
            self._state["positions"].append(position)
            logger.info(f"Position opened: {position.instrument_name} @ ₹{position.entry_premium}")

    async def remove_position(self, instrument_key: str, pnl: float) -> None:
        """Remove a closed position and update daily P&L."""
        async with self._lock:
            self._state["positions"] = [
                p for p in self._state["positions"]
                if p.instrument_key != instrument_key
            ]
            self._state["daily_pnl"] += pnl
            self._state["total_trades_today"] += 1
            if pnl >= 0:
                self._state["winning_trades"] += 1
            else:
                self._state["losing_trades"] += 1

            # Check daily loss limit
            from config import DAILY_LOSS_LIMIT
            if self._state["daily_pnl"] <= -DAILY_LOSS_LIMIT:
                self._state["entries_blocked"] = True
                logger.warning(
                    f"Daily loss limit hit: ₹{self._state['daily_pnl']:.0f}. "
                    "All new entries blocked for the session."
                )

    async def update_option_chain(self, index: str, chain: dict) -> None:
        """Replace the option chain snapshot for an index."""
        async with self._lock:
            self._state["option_chain"][index] = chain

    async def update_macro_context(self, context: dict) -> None:
        """Update the macro context from news scanner."""
        async with self._lock:
            self._state["macro_context"].update(context)
            logger.info(f"Macro context updated: {context.get('agent_bias_note', 'N/A')}")

    async def set_session_active(self, active: bool) -> None:
        async with self._lock:
            self._state["session_active"] = active

    async def set_opening_range_formed(self) -> None:
        async with self._lock:
            self._state["opening_range"]["formed"] = True
            logger.info(
                f"Opening range formed: "
                f"H={self._state['opening_range']['high']} "
                f"L={self._state['opening_range']['low']}"
            )

    async def set_ws_connected(self, connected: bool) -> None:
        async with self._lock:
            self._state["ws_connected"] = connected
            if not connected:
                self._state["ws_reconnect_count"] += 1

    # ── Snapshot ───────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """
        Return a shallow copy of the full state for logging/dashboard.
        Positions are not deep-copied — read only.
        """
        return {**self._state}

    def time(self) -> Optional[datetime]:
        """Current market time based on last tick."""
        return self._state.get("ws_last_message_time")

    def __repr__(self) -> str:
        idx = self._state["today_index"] or "N/A"
        ltp = self._state["index"].get(idx, {}).get("ltp", 0)
        pos = len(self._state["positions"])
        pnl = self._state["daily_pnl"]
        return f"<MarketState {idx}={ltp:.2f} positions={pos} pnl=Rs.{pnl:.0f}>"
