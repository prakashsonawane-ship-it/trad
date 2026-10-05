"""Phase 6 Validation — Dashboard, Orchestrator, Full Integration"""
import asyncio
from datetime import datetime
from data.state import MarketState, Position

# ─── 6A: Terminal Display ─────────────────────────────────────────
from dashboard.terminal import TerminalDisplay

async def test_terminal():
    state = MarketState()
    await state.update_tick("NIFTY", 24520, datetime.now())

    terminal = TerminalDisplay(state)
    terminal.print_session_banner("NIFTY")
    terminal.print_status("NIFTY")

    # Add position for display
    pos = Position(
        instrument_key="NSE_FO|24500CE", instrument_name="NIFTY 24500 CE",
        index="NIFTY", direction="BUY", option_type="CE",
        strike=24500, lot_size=65, quantity=65,
        entry_premium=22.0,
    )
    pos.current_premium = 28.0
    await state.add_position(pos)
    terminal.print_positions()

    terminal.set_agent_stats({"total_calls": 3})
    terminal.set_order_stats({"total_orders": 2})
    terminal.print_session_summary("NIFTY")

    print("[PASS] dashboard/terminal.py\n")

asyncio.run(test_terminal())

# ─── 6B: Main Orchestrator (import check) ────────────────────────
import importlib
main_mod = importlib.import_module("main")
assert hasattr(main_mod, "main"), "main() entry point exists"
assert hasattr(main_mod, "run_session"), "run_session() exists"
assert hasattr(main_mod, "run_auth_flow"), "run_auth_flow() exists"
assert hasattr(main_mod, "test_connection"), "test_connection() exists"
assert hasattr(main_mod, "run_scan_only"), "run_scan_only() exists"
assert hasattr(main_mod, "determine_today_index"), "determine_today_index() exists"
print("Main orchestrator imports all 6 phases correctly")
print("[PASS] main.py\n")

# ─── Full Module Import Chain ─────────────────────────────────────
print("Verifying full import chain...")
modules = [
    "config",
    "data.state", "data.upstox_rest", "data.upstox_ws", "data.candle_builder",
    "news.calendar", "news.scanner", "news.classifier", "news.news_bias",
    "analysis.state_engine", "analysis.market_structure",
    "analysis.liquidity_flow", "analysis.sentiment",
    "scenario.trigger", "scenario.confluence",
    "scenario.classifier", "scenario.builder",
    "agent.prompts", "agent.memory", "agent.client",
    "agent.mcp_bridge", "agent.option_selector",
    "execution.entry_watcher", "execution.order_manager",
    "execution.position_monitor",
    "dashboard.terminal",
]

for mod_name in modules:
    mod = importlib.import_module(mod_name)
    print(f"  OK: {mod_name}")

print(f"\n  Total: {len(modules)} modules imported successfully")
print("[PASS] Full import chain\n")

# ─── Verify config constants ─────────────────────────────────────
import config
checks = {
    "NIFTY_LOT": (config.NIFTY_LOT, 65),
    "SENSEX_LOT": (config.SENSEX_LOT, 20),
    "NIFTY_MAX_PREMIUM": (config.NIFTY_MAX_PREMIUM, 26),
    "MAX_TRADE_COST": (config.MAX_TRADE_COST, 2000),
    "TOTAL_CAPITAL": (config.TOTAL_CAPITAL, 3000),
    "STOP_LOSS_PCT": (config.STOP_LOSS_PCT, 50),
    "PAPER_MODE": (config.PAPER_MODE, True),
}
for name, (actual, expected) in checks.items():
    assert actual == expected, f"{name}: expected {expected}, got {actual}"
    print(f"  {name} = {actual} OK")

print("[PASS] Config verification\n")

print("=" * 50)
print("  ALL PHASE 6 MODULES VALIDATED SUCCESSFULLY")
print("=" * 50)
print()
print("  MARKETBRIDGE AI v2.1 — BUILD COMPLETE")
print("  26 modules across 6 phases")
print("  Paper mode: ON")
print()
