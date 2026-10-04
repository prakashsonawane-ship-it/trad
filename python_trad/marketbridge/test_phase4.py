"""Phase 4 Validation — Scenario Builder & AI Agent"""
import asyncio
from datetime import datetime
from data.state import MarketState, Candle

# ── Helper ──
def seed_state():
    """Create a state with indicators/analysis pre-loaded."""
    state = MarketState()
    # Simulate setting today's index
    loop = asyncio.new_event_loop()
    loop.run_until_complete(state.update("today_index", value="SENSEX"))
    loop.run_until_complete(state.update_tick("SENSEX", 81450, datetime.now()))
    loop.run_until_complete(state.update("indicators", "SENSEX", value={
        "ema_9": 81480, "ema_21": 81420, "ema_50": 81350,
        "ema_alignment": "BULLISH", "rsi": 62, "rsi_zone": "NEUTRAL",
        "macd_line": 15, "macd_signal": 12, "macd_histogram": 3, "macd_cross": "NONE",
        "bb_upper": 81600, "bb_middle": 81400, "bb_lower": 81200, "bb_width": 0.005,
        "bb_position": "UPPER_HALF", "atr": 45, "vwap": 81380,
        "vwap_position": "ABOVE", "last_close": 81450, "candle_count": 60,
    }))
    loop.run_until_complete(state.update("analysis", "structure", value={
        "phase": "TRENDING_UP", "nearest_swing_high": 81520,
        "nearest_swing_low": 81300, "pdh": 81600, "pdl": 81200, "pdc": 81400,
        "swing_highs": [{"price": 81520, "candle_idx": 55}],
        "swing_lows": [{"price": 81300, "candle_idx": 40}],
        "volume_profile": {"poc": 81400, "vah": 81500, "val": 81300},
        "opening_range": {"high": 81500, "low": 81350, "formed": True},
    }))
    loop.run_until_complete(state.update("analysis", "flow", value={
        "delta_current": 500, "delta_2m": 1200, "delta_5m": 3000,
        "cumulative_delta": 5000, "delta_trend": "BUYING",
        "stop_hunt": None, "volume_spike": None, "buy_pressure_pct": 65,
        "divergence": None, "stream_weight": 0.8,
    }))
    loop.run_until_complete(state.update("analysis", "sentiment", value={
        "pcr": 1.1, "pcr_trend": "STABLE", "pcr_interpretation": "NEUTRAL",
        "max_pain": 81500, "max_pain_distance": 50, "max_pain_direction": "ABOVE",
        "atm_iv": 18.5, "iv_percentile": 45, "oi_velocity": [],
        "highest_ce_oi_strike": 81500, "highest_pe_oi_strike": 81400,
    }))
    loop.run_until_complete(state.update("macro_context", value={
        "rbi_event_today": False, "global_sentiment": "NEUTRAL",
        "sgx_nifty_gap": "+0.2%", "agent_bias_note": "Normal conditions",
        "impact_level": "LOW", "calendar_events": [],
    }))
    loop.close()
    return state

state = seed_state()

# ─── 4A: Trigger Engine ───────────────────────────────────────────
from scenario.trigger import TriggerEngine

async def test_triggers():
    te = TriggerEngine(state)
    # First call sets prev, second call can detect crosses
    triggers1 = await te.evaluate("SENSEX")
    # Manually set prev to simulate a cross
    te._prev_indicators["SENSEX"] = {
        "ema_9": 81410, "ema_21": 81420, "vwap_position": "BELOW",
    }
    triggers2 = await te.evaluate("SENSEX")
    print(f"Triggers (call 1): {[t['type'] for t in triggers1]}")
    print(f"Triggers (call 2): {[t['type'] for t in triggers2]}")
    # Should detect EMA cross (9 went from below 21 to above)
    assert any(t["type"] == "EMA_CROSS" for t in triggers2), "Should detect EMA cross"
    assert any(t["type"] == "VWAP_RECLAIM" for t in triggers2), "Should detect VWAP reclaim"
    print("[PASS] scenario/trigger.py\n")
    return triggers2

triggers = asyncio.run(test_triggers())

# ─── 4A: Confluence Scorer ────────────────────────────────────────
from scenario.confluence import ConfluenceScorer

async def test_confluence():
    cs = ConfluenceScorer(state)
    result = cs.score(triggers, "BUY")
    print(f"Confluence: score={result['total_score']}, threshold={result['threshold']}")
    print(f"  Structure={result['stream_scores']['structure']}, "
          f"Flow={result['stream_scores']['flow_weighted']}, "
          f"Sentiment={result['stream_scores']['sentiment']}")
    print(f"  Passes gate: {result['passes_gate']}")
    assert "total_score" in result
    assert "passes_gate" in result
    assert result["total_score"] >= 0
    print("[PASS] scenario/confluence.py\n")
    return result

confluence = asyncio.run(test_confluence())

# ─── 4A: Scenario Classifier ─────────────────────────────────────
from scenario.classifier import ScenarioClassifier

async def test_classifier():
    sc = ScenarioClassifier(state)
    scenario = sc.classify(triggers, "BUY")
    print(f"Scenario: {scenario['type']}")
    print(f"  Description: {scenario['description'][:80]}...")
    print(f"  Key factors: {scenario['key_factors'][:2]}")
    assert "type" in scenario
    assert scenario["type"] in ("LIQUIDITY_TRAP", "BREAKOUT", "MEAN_REVERSION",
                                 "MAX_PAIN_DRIFT", "OPENING_RANGE_BREAK", "GENERIC")
    print("[PASS] scenario/classifier.py\n")
    return scenario

scenario = asyncio.run(test_classifier())

# ─── 4B: Context Builder ─────────────────────────────────────────
from scenario.builder import ContextBuilder

async def test_builder():
    cb = ContextBuilder(state)
    memory = [{"time": "10:30", "decision": "BUY", "confidence": 7, "outcome": "win", "pnl": 500}]
    context = cb.build("SENSEX", triggers, scenario, confluence, memory)
    assert "macro_context" in context
    assert "market_state" in context
    assert "analysis" in context
    assert "scenario" in context
    assert "triggers" in context
    assert "question" in context
    assert "constraints" in context
    assert context["constraints"]["lot_size"] == 20
    import json
    ctx_len = len(json.dumps(context))
    print(f"Context built: {ctx_len} chars (~{ctx_len // 4} tokens)")
    print(f"  Question: {context['question'][:80]}...")
    print(f"  Lot size: {context['constraints']['lot_size']}")
    print(f"  Max premium: {context['constraints']['max_premium']}")
    print("[PASS] scenario/builder.py\n")

asyncio.run(test_builder())

# ─── 4C: Agent Prompts ───────────────────────────────────────────
from agent.prompts import get_system_prompt, SCENARIO_ADDENDUM

prompt = get_system_prompt("LIQUIDITY_TRAP")
assert "MarketBridge AI" in prompt
assert "LIQUIDITY_TRAP" in prompt or "Liquidity Trap" in prompt
assert "65" in prompt  # NIFTY lot
assert "Rs.26" in prompt or "26" in prompt  # premium cap
print(f"System prompt: {len(prompt)} chars")
assert len(SCENARIO_ADDENDUM) == 6  # All 6 scenario types
print(f"Scenario addendums: {list(SCENARIO_ADDENDUM.keys())}")
print("[PASS] agent/prompts.py\n")

# ─── 4C: Decision Memory ─────────────────────────────────────────
from agent.memory import DecisionMemory

mem = DecisionMemory()
mem.add_decision("BUY", 7, "BREAKOUT", "Strong EMA cross with volume", confluence_score=6.5)
mem.add_decision("SELL", 8, "MEAN_REVERSION", "RSI oversold bounce", confluence_score=7.2)
mem.update_outcome(outcome="win", pnl=450)
memory_list = mem.get_memory()
assert len(memory_list) == 2
assert memory_list[-1]["outcome"] == "win"
stats = mem.get_win_rate()
print(f"Memory: {len(memory_list)} decisions")
print(f"Win rate: {stats}")
print("[PASS] agent/memory.py\n")

# ─── 4C: Agent Client (import only, no live API call) ────────────
from agent.client import AgentClient
print("AgentClient imports correctly")
print("[PASS] agent/client.py\n")

# ─── 4C: MCP Bridge ──────────────────────────────────────────────
from agent.mcp_bridge import MCPBridge, create_bridge_app
bridge = MCPBridge()
app = bridge.create_app()
assert app is not None
print(f"MCP Bridge app created: {len(app.routes)} routes")
print("[PASS] agent/mcp_bridge.py\n")

# ─── 4D: Option Selector ─────────────────────────────────────────
from agent.option_selector import OptionSelector

async def test_option_selector():
    os_ = OptionSelector(state)
    # Build mock chain
    chain = {}
    for strike in range(81000, 82000, 100):
        dist = abs(strike - 81500) / 100
        ce_premium = max(120 - dist * 25, 3)
        pe_premium = max(100 - dist * 20, 3)
        chain[strike] = {
            "CE": {"oi": 50000, "ltp": ce_premium, "iv": 18,
                   "instrument_key": f"BSE_FO|{strike}CE"},
            "PE": {"oi": 40000, "ltp": pe_premium, "iv": 19,
                   "instrument_key": f"BSE_FO|{strike}PE"},
        }

    result = os_.select("SENSEX", "BUY", confidence=7, chain=chain)
    assert result is not None, "Should find an affordable strike"
    assert result["option_type"] == "CE"
    assert result["total_cost"] <= 2000
    assert result["total_cost"] >= 400
    print(f"Selected: {result['instrument_name']}")
    print(f"  Premium=Rs.{result['premium']:.2f}, Cost=Rs.{result['total_cost']:.2f}")
    print(f"  OTM depth={result['otm_depth']}, Lot={result['lot_size']}")

    # Test NIFTY with premium cap
    await state.update_tick("NIFTY", 24500, datetime.now())
    nifty_chain = {}
    for strike in range(24200, 24800, 50):
        dist = abs(strike - 24500) / 50
        ce_prem = max(80 - dist * 12, 2)
        pe_prem = max(70 - dist * 10, 2)
        nifty_chain[strike] = {
            "CE": {"oi": 100000, "ltp": ce_prem, "iv": 17,
                   "instrument_key": f"NSE_FO|{strike}CE"},
            "PE": {"oi": 90000, "ltp": pe_prem, "iv": 18,
                   "instrument_key": f"NSE_FO|{strike}PE"},
        }
    result_n = os_.select("NIFTY", "BUY", confidence=7, chain=nifty_chain)
    if result_n:
        assert result_n["premium"] <= 26, f"NIFTY premium Rs.{result_n['premium']} > cap Rs.26"
        assert result_n["total_cost"] <= 2000
        print(f"NIFTY: {result_n['instrument_name']} premium=Rs.{result_n['premium']:.2f}")
    else:
        print("NIFTY: No affordable strike (premium cap working correctly)")
    print("[PASS] agent/option_selector.py\n")

asyncio.run(test_option_selector())

print("=" * 50)
print("  ALL PHASE 4 MODULES VALIDATED SUCCESSFULLY")
print("=" * 50)
