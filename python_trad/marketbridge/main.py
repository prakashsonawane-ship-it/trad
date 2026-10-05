"""
MarketBridge AI v2.1 — Main Orchestrator

Wires all 6 phases together and manages the full trading session lifecycle.

Session timeline:
  8:30 AM  — Pre-market news scan, set macro_context
  9:00 AM  — Connect Upstox WS + REST, start MCP bridge
  9:15 AM  — Market opens, observe mode (build opening range)
  9:30 AM  — Trading loop: analysis → triggers → confluence → agent → execution
  3:00 PM  — Hard close all positions
  3:05 PM  — Write logs, session summary

Usage:
  python main.py                    # Normal run (checks expiry day)
  python main.py --auth             # Run OAuth2 auth flow
  python main.py --test-connection  # Test Upstox connection
  python main.py --force-run        # Run on non-expiry days (testing)
  python main.py --scan-only        # Run news scan only (no trading)
"""

import sys
import signal
import asyncio
import logging
import argparse
from datetime import datetime, date, time, timedelta

import config

# Phase 1: Data Foundation
from data.state import MarketState
from data.upstox_rest import UpstoxREST
from data.upstox_ws import UpstoxWebSocket
from data.candle_builder import CandleBuilder

# Phase 2: News Intelligence
from news.scanner import NewsScanner
from news.classifier import ImpactClassifier
from news.news_bias import get_bias_instruction

# Phase 3: Analysis Streams
from analysis.state_engine import StateEngine
from analysis.market_structure import MarketStructure
from analysis.liquidity_flow import LiquidityFlow
from analysis.sentiment import SentimentEngine

# Phase 4: Scenario Builder & AI Agent
from scenario.trigger import TriggerEngine
from scenario.confluence import ConfluenceScorer
from scenario.classifier import ScenarioClassifier
from scenario.builder import ContextBuilder
from agent.mcp_bridge import get_bridge
from agent.option_selector import OptionSelector

# Phase 5: Execution Engine
from execution.entry_watcher import EntryWatcher
from execution.order_manager import OrderManager
from execution.position_monitor import PositionMonitor

# Phase 6: Dashboard
from dashboard.terminal import TerminalDisplay

# ─── Logging Setup ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-28s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            config.LOG_DIR / f"system_{date.today().isoformat()}.log",
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("main")


def determine_today_index() -> str | None:
    """Check if today is an expiry day. Returns index name or None."""
    day_name = date.today().strftime("%A")
    index = config.EXPIRY_DAYS.get(day_name)
    if index:
        lot = config.NIFTY_LOT if index == "NIFTY" else config.SENSEX_LOT
        logger.info(f"Today is {day_name} -> {index} expiry (lot: {lot})")
    return index


# ═══════════════════════════════════════════════════════════════════
#  FULL SESSION ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════

async def run_session(force_index: str = None):
    """
    Run a complete trading session.

    Phases:
    1. Pre-market: news scan, macro context
    2. Connect: Upstox WS/REST, MCP bridge
    3. Opening range: observe 9:15-9:30
    4. Trading loop: analysis → triggers → agent → execution
    5. Hard close: 3:00 PM
    6. Summary: logs, stats
    """
    # ── Determine Today's Index ────────────────────────────────
    today_index = force_index or determine_today_index()
    if not today_index:
        day_name = date.today().strftime("%A")
        logger.info(f"{day_name} is not an expiry day.")
        print(f"\n  {day_name} is not an expiry day.")
        print("  SENSEX = Tuesday, NIFTY = Thursday.")
        print("  Use --force-run to test anyway.\n")
        return

    lot_size = config.NIFTY_LOT if today_index == "NIFTY" else config.SENSEX_LOT

    # ── Initialize ALL Components ──────────────────────────────
    state = MarketState()
    await state.update("today_index", value=today_index)
    await state.update("today_lot_size", value=lot_size)

    rest = UpstoxREST()
    candle_builder = CandleBuilder(state)
    news_scanner = NewsScanner()
    classifier = ImpactClassifier()
    state_engine = StateEngine(state)
    market_structure = MarketStructure(state)
    liquidity_flow = LiquidityFlow(state)
    sentiment_engine = SentimentEngine(state)
    trigger_engine = TriggerEngine(state)
    confluence_scorer = ConfluenceScorer(state)
    scenario_classifier = ScenarioClassifier(state)
    context_builder = ContextBuilder(state)
    bridge = get_bridge()
    option_selector = OptionSelector(state)
    order_manager = OrderManager(state, rest)
    entry_watcher = EntryWatcher(state)
    position_monitor = PositionMonitor(state)
    terminal = TerminalDisplay(state)

    # ── Auth Check ─────────────────────────────────────────────
    if not rest.is_authenticated:
        logger.error("Not authenticated. Run: python main.py --auth")
        print("\n  Not authenticated. Run: python main.py --auth\n")
        return

    # ── Session Banner ─────────────────────────────────────────
    terminal.print_session_banner(today_index)

    # ══════════════════════════════════════════════════════════
    #  STEP 1: PRE-MARKET NEWS SCAN (8:30 AM)
    # ══════════════════════════════════════════════════════════
    logger.info("=== STEP 1: Pre-market news scan ===")
    try:
        macro_context = await news_scanner.run_full_scan()
        macro_context = classifier.classify(macro_context)
        bias = get_bias_instruction(macro_context)

        # Store in state
        await state.update("macro_context", value=macro_context)

        logger.info(f"Impact: {macro_context['impact_level']} | "
                     f"Blocked: {macro_context.get('trading_blocked', False)}")
        logger.info(f"Bias: {bias[:100]}...")

    except Exception as e:
        logger.error(f"News scan failed: {e}")
        macro_context = {"impact_level": "LOW", "trading_blocked": False,
                         "agent_bias_note": "News scan failed — use caution"}
        await state.update("macro_context", value=macro_context)

    # ══════════════════════════════════════════════════════════
    #  STEP 2: CONNECT BROKER (9:00 AM)
    # ══════════════════════════════════════════════════════════
    logger.info("=== STEP 2: Connect broker ===")

    # Fetch historical data for PDH/PDL
    try:
        hist = await rest.get_historical_candles(
            config.NIFTY_INDEX_KEY if today_index == "NIFTY" else config.SENSEX_INDEX_KEY,
            interval="day", days_back=2,
        )
        if hist:
            prev = hist[-1] if len(hist) >= 1 else {}
            market_structure.set_previous_day_levels(
                today_index,
                high=prev.get("high", 0),
                low=prev.get("low", 0),
                close=prev.get("close", 0),
            )
    except Exception as e:
        logger.warning(f"Historical data fetch failed: {e}")

    # Connect WebSocket
    ws = UpstoxWebSocket(state, rest)

    # ── Register tick callbacks (all layers) ───────────────────
    async def on_tick(index: str, ltp: float, volume: int, tick_time: datetime):
        """Master tick handler — routes to all consumers."""
        await candle_builder.process_tick(index, ltp, volume, tick_time)
        await liquidity_flow.process_tick(index, ltp, volume, tick_time)
        await entry_watcher.on_tick(index, ltp, tick_time)

    ws.on_tick(on_tick)

    # ── Register candle close callbacks (analysis pipeline) ────
    async def on_candle_close(index: str, candle):
        """Master 1m candle close handler — runs full analysis pipeline."""
        logger.debug(f"1m close: {index} O={candle.open} H={candle.high} L={candle.low} C={candle.close}")

        # Phase 3: Run all analysis streams
        await state_engine.on_candle_close(index, candle)
        await market_structure.on_candle_close(index, candle)
        flow = await liquidity_flow.on_candle_close(index, candle)

        # Notify entry watcher
        await entry_watcher.on_candle_close(index, candle)

        # Phase 4: Trigger evaluation (only after warmup)
        indicators = state.get("indicators", index)
        if not indicators:
            return  # Still warming up

        # Check if trading is blocked
        if state.macro_context.get("trading_blocked", False):
            return

        # Check time window
        now = datetime.now().time()
        if now < config.OPENING_RANGE_END or now >= config.NO_ENTRY_AFTER:
            return

        # Evaluate triggers
        triggers = await trigger_engine.evaluate(index)
        if not triggers:
            return

        # Determine primary direction
        direction_counts = {"BUY": 0, "SELL": 0}
        for t in triggers:
            direction_counts[t["direction"]] = direction_counts.get(t["direction"], 0) + t["strength"]
        direction = max(direction_counts, key=direction_counts.get)

        # Score confluence
        confluence = confluence_scorer.score(triggers, direction)

        if not confluence["passes_gate"]:
            return  # Suppressed

        # Classify scenario
        scenario = scenario_classifier.classify(triggers, direction)

        # Build context for agent
        memory = bridge.memory.get_memory()
        context = context_builder.build(
            index, triggers, scenario, confluence, memory
        )

        # Call agent
        logger.info(f"Calling agent: {scenario['type']} score={confluence['total_score']}")
        decision = await bridge.route_decision(
            context=context,
            scenario_type=scenario["type"],
            confluence_score=confluence["total_score"],
        )

        if not decision or decision.decision == "WAIT":
            logger.info(f"Agent says WAIT: {decision.reasoning if decision else 'no response'}")
            return

        if decision.confidence < 5:
            logger.info(f"Agent confidence too low: {decision.confidence}")
            return

        # Select option
        chain = state.get("option_chain", index) or {}
        option = option_selector.select(
            index, decision.decision, decision.confidence, chain
        )

        if not option:
            logger.warning("No affordable option found")
            return

        # Start entry watcher
        entry_zone = decision.entry_zone or {}
        await entry_watcher.start_watching(
            index=index,
            direction=decision.decision,
            trigger_price=entry_zone.get("trigger_price", candle.close),
            price_direction=entry_zone.get("direction", "above" if decision.decision == "BUY" else "below"),
            invalidation=decision.invalidation or 0,
            option_selection=option,
            confidence=decision.confidence,
            scenario_type=scenario["type"],
        )

    candle_builder.on_candle_close(on_candle_close)

    # ── Register entry confirmed callback ──────────────────────
    async def on_entry_confirmed(watch_data: dict):
        """Fire when entry watcher confirms an entry."""
        order_id = await order_manager.place_entry_order(watch_data)
        if order_id:
            # In paper mode, simulate immediate fill
            if config.PAPER_MODE:
                fill_price = watch_data["option_selection"]["premium"]
                await order_manager.on_order_filled(order_id, fill_price)

    entry_watcher.on_entry_confirmed(on_entry_confirmed)

    # ── Register exit callback ─────────────────────────────────
    async def on_exit_signal(position, quantity, reason, order_type):
        """Fire when position monitor triggers an exit."""
        order_id = await order_manager.place_exit_order(
            position, quantity, reason, order_type
        )
        if order_id and config.PAPER_MODE:
            # Simulate exit fill at current premium
            current = position.current_premium or position.entry_premium
            pnl = (current - position.entry_premium) * quantity
            await state.remove_position(position.instrument_key, pnl=pnl)
            bridge.memory.update_outcome(
                outcome="win" if pnl > 0 else "loss",
                pnl=pnl,
                exit_reason=reason,
            )

    position_monitor.on_exit_signal(on_exit_signal)

    # ── Connect WebSocket ──────────────────────────────────────
    connected = await ws.connect()
    if not connected:
        logger.error("WebSocket connection failed")
        await rest.close()
        return

    index_key = (config.NIFTY_INDEX_KEY if today_index == "NIFTY"
                 else config.SENSEX_INDEX_KEY)
    await ws.subscribe([index_key])
    logger.info(f"Subscribed to {today_index} live feed")

    # ── Start Position Monitor ─────────────────────────────────
    await position_monitor.start()

    # ══════════════════════════════════════════════════════════
    #  STEP 3-4: TRADING LOOP (9:15 AM - 3:00 PM)
    # ══════════════════════════════════════════════════════════
    logger.info("=== Trading session active ===")

    # ── Option chain refresh task (every 30 seconds) ───────────
    # Since we only trade on expiry days, today IS the expiry date
    today_expiry = date.today().isoformat()  # YYYY-MM-DD format

    async def chain_refresh_loop():
        """Refresh option chain every 30 seconds for sentiment analysis."""
        while True:
            await asyncio.sleep(30)
            try:
                chain = await rest.get_option_chain_parsed(
                    index=today_index,
                    expiry_date=today_expiry,
                )
                if chain:
                    await state.update("option_chain", today_index, value=chain)
                    await sentiment_engine.on_chain_update(today_index, chain)
                    # Post-response delay (2-3s)
                    await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Chain refresh error: {e}")

    chain_task = asyncio.create_task(chain_refresh_loop())

    # ── Midday news refresh (12:00 PM) ─────────────────────────
    async def midday_refresh():
        """Re-fetch headlines at noon."""
        now = datetime.now()
        noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
        if now < noon:
            delay = (noon - now).total_seconds()
            await asyncio.sleep(delay)
            try:
                refresh = await news_scanner.run_midday_refresh()
                macro = state.macro_context
                macro.update(refresh)
                macro = classifier.classify(macro)
                get_bias_instruction(macro)
                await state.update("macro_context", value=macro)
                logger.info("Midday news refresh complete")
            except Exception as e:
                logger.error(f"Midday refresh error: {e}")

    midday_task = asyncio.create_task(midday_refresh())

    # ── Periodic status display (every 60 seconds) ─────────────
    async def status_display():
        while True:
            await asyncio.sleep(60)
            terminal.print_status(today_index)

    status_task = asyncio.create_task(status_display())

    # ── Mark opening range as formed at 9:30 AM (Fix #5) ───────
    async def mark_opening_range():
        """Set opening_range['formed'] = True at OPENING_RANGE_END."""
        now = datetime.now()
        target = now.replace(
            hour=config.OPENING_RANGE_END.hour,
            minute=config.OPENING_RANGE_END.minute,
            second=0, microsecond=0,
        )
        if now < target:
            delay = (target - now).total_seconds()
            await asyncio.sleep(delay)
        await state.set_opening_range_formed()

    opening_range_task = asyncio.create_task(mark_opening_range())

    # ── Wait for session end (Ctrl+C or 3:05 PM) ──────────────
    try:
        while True:
            await asyncio.sleep(10)

            # Check for hard close time
            now = datetime.now().time()
            if now >= config.HARD_CLOSE_TIME:
                logger.info("3:00 PM — Hard close triggered")
                await position_monitor.force_close_all("HARD_CLOSE_3PM")
                break

            if now >= config.LOG_WRITE_TIME:
                break

    except asyncio.CancelledError:
        pass
    finally:
        # ══════════════════════════════════════════════════════
        #  STEP 5-6: SHUTDOWN & SUMMARY
        # ══════════════════════════════════════════════════════
        logger.info("=== Session shutdown ===")

        # Cancel background tasks
        chain_task.cancel()
        midday_task.cancel()
        status_task.cancel()

        # Stop components
        await position_monitor.stop()
        await entry_watcher.cancel_watch("session end")
        await candle_builder.force_close(today_index)
        await ws.disconnect()
        await rest.close()

        # Save session IV
        sentiment_data = state.get("analysis", "sentiment") or {}
        if sentiment_data.get("atm_iv"):
            sentiment_engine.save_session_iv(today_index, sentiment_data["atm_iv"])

        # Update terminal stats
        terminal.set_agent_stats(bridge.agent_client.stats)
        terminal.set_order_stats(order_manager.stats)

        # Print summary
        terminal.print_session_summary(today_index)

        logger.info(f"Session complete. PnL: Rs.{state.daily_pnl:.0f}")


# ═══════════════════════════════════════════════════════════════════
#  AUTH FLOW
# ═══════════════════════════════════════════════════════════════════

async def run_auth_flow():
    """Interactive OAuth2 authentication."""
    rest = UpstoxREST()

    if rest.is_authenticated:
        logger.info("Already authenticated.")
        return

    print("\n  UPSTOX OAUTH2 AUTHENTICATION")
    print("  " + "=" * 40)

    auth_url = rest.get_auth_url()
    print(f"\n  1. Open this URL in your browser:")
    print(f"     {auth_url}\n")
    rest.open_auth_in_browser()

    print("  2. After login, copy the redirect URL.")
    redirect_url = input("\n  Paste redirect URL: ").strip()

    code = rest.extract_code_from_url(redirect_url)
    if not code:
        print("\n  Could not extract auth code.\n")
        return

    print("  3. Exchanging code for token...")
    success = await rest.authenticate_with_code(code)
    print(f"  {'Authentication successful!' if success else 'Authentication failed.'}\n")
    await rest.close()


# ═══════════════════════════════════════════════════════════════════
#  TEST CONNECTION
# ═══════════════════════════════════════════════════════════════════

async def test_connection():
    """Test Upstox API connectivity."""
    rest = UpstoxREST()

    if not rest.is_authenticated:
        print("\n  Not authenticated. Run: python main.py --auth\n")
        return

    print("\n  CONNECTION TEST")
    print("  " + "=" * 40)

    quotes = await rest.get_market_quote([
        config.NIFTY_INDEX_KEY, config.SENSEX_INDEX_KEY,
    ])
    if quotes:
        print("  REST API: OK")
        for key, data in quotes.items():
            ltp = data.get("last_price", data.get("ltp", "N/A"))
            print(f"    {key}: Rs.{ltp}")
    else:
        print("  REST API: FAILED")

    ws_url = await rest.get_ws_auth_url()
    print(f"  WebSocket: {'OK' if ws_url else 'FAILED'}")

    await rest.close()
    print()


# ═══════════════════════════════════════════════════════════════════
#  NEWS SCAN ONLY
# ═══════════════════════════════════════════════════════════════════

async def run_scan_only():
    """Run news scan and print results without trading."""
    scanner = NewsScanner()
    classifier = ImpactClassifier()

    print("\n  PRE-MARKET SCAN")
    print("  " + "=" * 40)

    context = await scanner.run_full_scan()
    context = classifier.classify(context)
    bias = get_bias_instruction(context)

    print(f"\n  Impact Level: {context['impact_level']}")
    print(f"  Trading Blocked: {context.get('trading_blocked', False)}")
    print(f"  Global Sentiment: {context.get('global_sentiment', 'N/A')}")
    print(f"  SGX Gap: {context.get('sgx_nifty_gap', 'N/A')}")
    print(f"  Calendar Events: {context.get('calendar_events', [])}")
    print(f"\n  Headlines ({len(context.get('headlines', []))}):")
    for h in context.get("headlines", [])[:5]:
        print(f"    - {h['title'][:80]}")
    print(f"\n  Bias: {bias[:120]}...")
    print()


# ═══════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

def main():
    """Parse arguments and run."""
    parser = argparse.ArgumentParser(
        description="MarketBridge AI v2.1 — Expiry Day Trading Bot"
    )
    parser.add_argument("--auth", action="store_true", help="OAuth2 auth flow")
    parser.add_argument("--test-connection", action="store_true", help="Test API")
    parser.add_argument("--force-run", action="store_true", help="Run on non-expiry day")
    parser.add_argument("--scan-only", action="store_true", help="News scan only")
    args = parser.parse_args()

    if args.auth:
        asyncio.run(run_auth_flow())
    elif args.test_connection:
        asyncio.run(test_connection())
    elif args.scan_only:
        asyncio.run(run_scan_only())
    else:
        force_index = None
        if args.force_run:
            # Default to SENSEX for testing on non-expiry days
            force_index = determine_today_index() or "SENSEX"

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        task = loop.create_task(run_session(force_index))

        try:
            if sys.platform != "win32":
                loop.add_signal_handler(signal.SIGINT, task.cancel)
                loop.add_signal_handler(signal.SIGTERM, task.cancel)
            loop.run_until_complete(task)
        except KeyboardInterrupt:
            task.cancel()
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                pass
        finally:
            loop.close()


if __name__ == "__main__":
    main()
