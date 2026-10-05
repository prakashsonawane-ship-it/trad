"""
Phase 1 Validation Script — verifies all modules import and work correctly.
"""
import sys
import asyncio
from datetime import datetime

# --- config.py ---
from config import (
    NIFTY_LOT, SENSEX_LOT, NIFTY_MAX_PREMIUM, MAX_TRADE_COST,
    AGENT_MODEL_DEFAULT, AGENT_MODEL_HIGH_CONVICTION,
    PAPER_MODE, EXPIRY_DAYS, LOG_DIR, DATA_STORE_DIR
)
print(f"NIFTY_LOT={NIFTY_LOT}, SENSEX_LOT={SENSEX_LOT}")
print(f"NIFTY_MAX_PREMIUM={NIFTY_MAX_PREMIUM}")
print(f"MAX_TRADE_COST={MAX_TRADE_COST}")
print(f"Agent default: {AGENT_MODEL_DEFAULT}")
print(f"Agent high-conviction: {AGENT_MODEL_HIGH_CONVICTION}")
print(f"PAPER_MODE={PAPER_MODE}")
print(f"EXPIRY_DAYS={EXPIRY_DAYS}")
print(f"LOG_DIR exists: {LOG_DIR.exists()}")
print(f"DATA_STORE_DIR exists: {DATA_STORE_DIR.exists()}")
assert NIFTY_LOT == 65, f"Expected 65, got {NIFTY_LOT}"
assert SENSEX_LOT == 20, f"Expected 20, got {SENSEX_LOT}"
assert NIFTY_MAX_PREMIUM == 26, f"Expected 26, got {NIFTY_MAX_PREMIUM}"
print("[PASS] config.py\n")

# --- data/state.py ---
from data.state import MarketState, Position, Candle
state = MarketState()
print(f"State initialized: {state}")

# Test Position dataclass
pos = Position(
    instrument_key="TEST|12345",
    instrument_name="SENSEX 81500 PE",
    index="SENSEX",
    direction="SELL",
    option_type="PE",
    strike=81500,
    lot_size=20,
    quantity=20,
    entry_premium=70.0,
)
assert pos.entry_cost == 1400.0, f"Expected 1400, got {pos.entry_cost}"
assert pos.stop == 35.0, f"Expected 35.0, got {pos.stop}"
print(f"Position: {pos.instrument_name} cost=Rs.{pos.entry_cost}")

# Test Candle dataclass
c = Candle(timestamp=datetime.now(), open=100, high=105, low=98, close=103, volume=500)
d = c.to_dict()
assert all(k in d for k in ("open", "high", "low", "close", "volume"))
print(f"Candle: O={c.open} H={c.high} L={c.low} C={c.close}")

# Test async state operations
async def test_state():
    await state.update_tick("NIFTY", 24500.50, datetime.now())
    ltp = state.get("index", "NIFTY", "ltp")
    assert ltp == 24500.50, f"Expected 24500.50, got {ltp}"

    await state.update("macro_context", "rbi_event_today", value=True)
    assert state.macro_context["rbi_event_today"] == True

    await state.add_candle("NIFTY", "1m", c)
    candles = state.get_candles("NIFTY", "1m")
    assert len(candles) == 1

    await state.add_position(pos)
    assert len(state.positions) == 1

    await state.remove_position("TEST|12345", pnl=700.0)
    assert len(state.positions) == 0
    assert state.daily_pnl == 700.0

    print("Async state operations: all passed")

asyncio.run(test_state())
print("[PASS] data/state.py\n")

# --- data/candle_builder.py ---
from data.candle_builder import CandleBuilder
state2 = MarketState()
cb = CandleBuilder(state2)

candles_closed = []
async def on_close(index, candle):
    candles_closed.append((index, candle))

cb.on_candle_close(on_close)

async def test_candle_builder():
    # Simulate ticks across a minute boundary
    t1 = datetime(2026, 10, 5, 9, 30, 10)
    t2 = datetime(2026, 10, 5, 9, 30, 30)
    t3 = datetime(2026, 10, 5, 9, 30, 50)
    t4 = datetime(2026, 10, 5, 9, 31, 5)  # New minute -> closes previous candle

    await cb.process_tick("NIFTY", 24500, 100, t1)
    await cb.process_tick("NIFTY", 24520, 150, t2)
    await cb.process_tick("NIFTY", 24490, 200, t3)
    assert len(candles_closed) == 0, "Should not close yet"

    await cb.process_tick("NIFTY", 24510, 120, t4)
    assert len(candles_closed) == 1, f"Expected 1 closed candle, got {len(candles_closed)}"

    idx, closed = candles_closed[0]
    assert idx == "NIFTY"
    assert closed.open == 24500
    assert closed.high == 24520
    assert closed.low == 24490
    assert closed.close == 24490  # Last tick of the minute
    print(f"Candle closed: O={closed.open} H={closed.high} L={closed.low} C={closed.close}")

asyncio.run(test_candle_builder())
print("[PASS] data/candle_builder.py\n")

# --- data/upstox_rest.py ---
from data.upstox_rest import UpstoxREST
rest = UpstoxREST()
print(f"REST client: authenticated={rest.is_authenticated}")
auth_url = rest.get_auth_url()
assert "api.upstox.com" in auth_url
print(f"Auth URL generated: ...{auth_url[-40:]}")
print("[PASS] data/upstox_rest.py\n")

# --- data/upstox_ws.py ---
from data.upstox_ws import UpstoxWebSocket
print("WebSocket module imports cleanly")
print("[PASS] data/upstox_ws.py\n")

# --- main.py ---
from main import determine_today_index
result = determine_today_index()
today = datetime.now().strftime("%A")
print(f"Today is {today} -> index={result}")
print("[PASS] main.py\n")

print("=" * 50)
print("  ALL PHASE 1 MODULES VALIDATED SUCCESSFULLY")
print("=" * 50)
