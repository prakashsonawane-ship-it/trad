"""Phase 5 Validation — Execution Engine"""
import asyncio
from datetime import datetime, time
from data.state import MarketState, Position, Candle
from data.upstox_rest import UpstoxREST

# ─── 5A: Entry Watcher ────────────────────────────────────────────
from execution.entry_watcher import EntryWatcher

async def test_entry_watcher():
    state = MarketState()
    await state.update_tick("SENSEX", 81450, datetime.now())
    # Set flow data for delta confirmation
    await state.update("analysis", "flow", value={
        "delta_trend": "BUYING", "delta_2m": 500,
    })

    ew = EntryWatcher(state)

    entries_fired = []
    async def on_entry(watch_data):
        entries_fired.append(watch_data)

    ew.on_entry_confirmed(on_entry)

    # Start watching
    await ew.start_watching(
        index="SENSEX", direction="BUY",
        trigger_price=81400, price_direction="above",
        invalidation=81200,
        option_selection={"instrument_key": "BSE_FO|81500CE", "instrument_name": "SENSEX 81500 CE",
                          "option_type": "CE", "strike": 81500, "premium": 80,
                          "lot_size": 20, "total_cost": 1600},
        confidence=7, scenario_type="BREAKOUT",
    )

    assert ew.is_watching, "Should be watching"
    print(f"Entry watch active: {ew.active_watch['trigger_price']} {ew.active_watch['price_direction']}")

    # Simulate tick at invalidation — should cancel
    await ew.on_tick("SENSEX", 81190, datetime.now())
    assert not ew.is_watching, "Should be cancelled (invalidation)"
    print("Invalidation cancellation: OK")

    # Start new watch
    await ew.start_watching(
        index="SENSEX", direction="BUY",
        trigger_price=81400, price_direction="above",
        invalidation=81200,
        option_selection={"instrument_key": "BSE_FO|81500CE", "instrument_name": "SENSEX 81500 CE",
                          "option_type": "CE", "strike": 81500, "premium": 80,
                          "lot_size": 20, "total_cost": 1600},
        confidence=7, scenario_type="BREAKOUT",
    )

    # Simulate candle close in zone with delta confirmation
    candle = Candle(timestamp=datetime.now(), open=81390, high=81480, low=81380, close=81460, volume=1000)
    await ew.on_candle_close("SENSEX", candle)

    assert len(entries_fired) == 1, f"Expected 1 entry, got {len(entries_fired)}"
    assert not ew.is_watching, "Watch should be cleared after entry"
    print(f"Entry confirmed: {entries_fired[0]['direction']} at candle close")

    # Test delta flip abort
    await state.update("analysis", "flow", value={
        "delta_trend": "STRONG_SELLING", "delta_2m": -800,
    })
    await ew.start_watching(
        index="SENSEX", direction="BUY",
        trigger_price=81400, price_direction="above",
        invalidation=81200,
        option_selection={"instrument_key": "BSE_FO|81500CE", "instrument_name": "SENSEX 81500 CE",
                          "option_type": "CE", "strike": 81500, "premium": 80,
                          "lot_size": 20, "total_cost": 1600},
        confidence=7, scenario_type="BREAKOUT",
    )
    candle2 = Candle(timestamp=datetime.now(), open=81390, high=81480, low=81380, close=81460, volume=1000)
    await ew.on_candle_close("SENSEX", candle2)
    assert len(entries_fired) == 1, "Should NOT fire — delta flipped"
    assert not ew.is_watching, "Should be cancelled (delta flip)"
    print("Delta flip abort: OK")

    print("[PASS] execution/entry_watcher.py\n")

asyncio.run(test_entry_watcher())

# ─── 5B: Order Manager ───────────────────────────────────────────
from execution.order_manager import OrderManager

async def test_order_manager():
    state = MarketState()
    rest = UpstoxREST()
    om = OrderManager(state, rest)

    watch_data = {
        "index": "SENSEX", "direction": "BUY",
        "invalidation": 81200, "scenario_type": "BREAKOUT",
        "confidence": 7,
        "option_selection": {
            "instrument_key": "BSE_FO|81500CE",
            "instrument_name": "SENSEX 81500 CE",
            "option_type": "CE", "strike": 81500,
            "premium": 80, "lot_size": 20, "total_cost": 1600,
        },
    }

    # Should succeed (paper mode)
    order_id = await om.place_entry_order(watch_data)
    assert order_id is not None, "Order should be placed in paper mode"
    print(f"Entry order placed: {order_id}")

    # Simulate fill
    await om.on_order_filled(order_id, fill_price=80.0)
    assert len(state.positions) == 1
    pos = state.positions[0]
    assert pos.entry_premium == 80.0
    assert pos.entry_cost == 1600.0
    print(f"Position: {pos.instrument_name} cost=Rs.{pos.entry_cost}")

    # Try placing 3rd position (should be blocked)
    await state.add_position(Position(
        instrument_key="BSE_FO|81600PE", instrument_name="SENSEX 81600 PE",
        index="SENSEX", direction="SELL", option_type="PE",
        strike=81600, lot_size=20, quantity=20, entry_premium=50.0,
    ))
    assert len(state.positions) == 2

    order_id3 = await om.place_entry_order(watch_data)
    assert order_id3 is None, "3rd position should be blocked"
    print("Max positions enforced: OK")

    # Test daily loss limit
    await state.remove_position("BSE_FO|81500CE", pnl=-800)
    await state.remove_position("BSE_FO|81600PE", pnl=-800)
    # Daily PnL = -1600 > -1500 limit
    assert state.daily_pnl <= -config.DAILY_LOSS_LIMIT
    order_id4 = await om.place_entry_order(watch_data)
    assert order_id4 is None, "Should be blocked by daily loss limit"
    print(f"Daily loss limit enforced: PnL=Rs.{state.daily_pnl}")

    print(f"Stats: {om.stats}")
    print("[PASS] execution/order_manager.py\n")

import config
asyncio.run(test_order_manager())

# ─── 5C: Position Monitor ────────────────────────────────────────
from execution.position_monitor import PositionMonitor

async def test_position_monitor():
    state = MarketState()
    await state.update_tick("SENSEX", 81450, datetime.now())
    pm = PositionMonitor(state)

    exits = []
    async def on_exit(position, quantity, reason, order_type):
        exits.append({"name": position.instrument_name, "qty": quantity,
                       "reason": reason, "type": order_type})

    pm.on_exit_signal(on_exit)

    # Add a position
    pos = Position(
        instrument_key="BSE_FO|81500CE", instrument_name="SENSEX 81500 CE",
        index="SENSEX", direction="BUY", option_type="CE",
        strike=81500, lot_size=20, quantity=20,
        entry_premium=80.0, invalidation=81200,
    )
    await state.add_position(pos)

    # Test stop loss (premium drops to 50% = Rs.40)
    pos.current_premium = 38.0  # Below 50% stop (40)
    await pm._check_all_positions()
    assert len(exits) == 1
    assert exits[0]["reason"] == "STOP_LOSS_50PCT"
    print(f"Stop loss: {exits[0]['reason']} qty={exits[0]['qty']}")

    # Reset
    exits.clear()
    pm._cleanup_position("BSE_FO|81500CE")

    # Test TP1 (premium up 50% to Rs.120)
    pos2 = Position(
        instrument_key="BSE_FO|81600CE", instrument_name="SENSEX 81600 CE",
        index="SENSEX", direction="BUY", option_type="CE",
        strike=81600, lot_size=20, quantity=20,
        entry_premium=80.0,
    )
    await state.add_position(pos2)
    pos2.current_premium = 125.0  # > 120 (50% of 80)
    await pm._check_all_positions()
    tp1_exits = [e for e in exits if e["reason"] == "TP1_50PCT"]
    assert len(tp1_exits) == 1
    print(f"TP1: exit qty={tp1_exits[0]['qty']}, breakeven stop set")

    # Test TP2 (premium up 100% to Rs.160)
    pos2.current_premium = 165.0
    await pm._check_all_positions()
    tp2_exits = [e for e in exits if e["reason"] == "TP2_100PCT"]
    assert len(tp2_exits) == 1
    print(f"TP2: exit qty={tp2_exits[0]['qty']}, trailing starts")

    # Test trail stop
    pm._peak_premium["BSE_FO|81600CE"] = 180.0  # Peak was 180
    pos2.current_premium = 120.0  # Dropped 33% from peak (> 30% threshold)
    await pm._check_all_positions()
    trail_exits = [e for e in exits if e["reason"] == "TRAIL_STOP"]
    assert len(trail_exits) == 1
    print(f"Trail stop: triggered at premium Rs.120 (peak Rs.180)")

    # Test force close all
    exits.clear()
    pos3 = Position(
        instrument_key="BSE_FO|81700CE", instrument_name="SENSEX 81700 CE",
        index="SENSEX", direction="BUY", option_type="CE",
        strike=81700, lot_size=20, quantity=20, entry_premium=50.0,
    )
    await state.add_position(pos3)
    await pm.force_close_all("session_end")
    assert len(exits) > 0
    print(f"Force close: {len(exits)} positions closed")

    print("[PASS] execution/position_monitor.py\n")

asyncio.run(test_position_monitor())

print("=" * 50)
print("  ALL PHASE 5 MODULES VALIDATED SUCCESSFULLY")
print("=" * 50)
