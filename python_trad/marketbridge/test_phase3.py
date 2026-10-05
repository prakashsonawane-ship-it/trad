"""Phase 3 Validation — Analysis Streams"""
import asyncio
import numpy as np
from datetime import datetime, timedelta
from data.state import MarketState, Candle

# ── Helper: generate test candles ──────────────────────────────────
def make_candles(n, base_price=24500, trend="up"):
    """Generate n test candles with a trend."""
    candles = []
    price = base_price
    t = datetime(2026, 10, 5, 9, 15, 0)
    for i in range(n):
        if trend == "up":
            delta = np.random.uniform(-10, 20)
        elif trend == "down":
            delta = np.random.uniform(-20, 10)
        else:
            delta = np.random.uniform(-15, 15)
        price += delta
        o = price
        h = price + np.random.uniform(5, 25)
        l = price - np.random.uniform(5, 25)
        c = price + np.random.uniform(-10, 10)
        vol = int(np.random.uniform(500, 5000))
        candles.append(Candle(timestamp=t, open=round(o,2), high=round(h,2),
                              low=round(l,2), close=round(c,2), volume=vol))
        t += timedelta(minutes=1)
    return candles

# ─── 3A: State Engine ─────────────────────────────────────────────
from analysis.state_engine import StateEngine

async def test_state_engine():
    state = MarketState()
    engine = StateEngine(state)

    # Seed 60 candles (past warmup)
    candles = make_candles(60, base_price=24500, trend="up")
    for c in candles:
        await state.add_candle("NIFTY", "1m", c)

    # Run engine
    result = await engine.on_candle_close("NIFTY", candles[-1])
    assert result is not None, "Should return indicators after warmup"
    assert "ema_9" in result
    assert "ema_21" in result
    assert "ema_50" in result
    assert "rsi" in result
    assert "macd_line" in result
    assert "bb_upper" in result
    assert "atr" in result
    assert "vwap" in result
    assert result["ema_9"] > 0
    assert 0 <= result["rsi"] <= 100
    print(f"EMA: 9={result['ema_9']}, 21={result['ema_21']}, 50={result['ema_50']}")
    print(f"RSI={result['rsi']} ({result['rsi_zone']})")
    print(f"MACD: {result['macd_line']}, signal={result['macd_signal']}, cross={result['macd_cross']}")
    print(f"BB: upper={result['bb_upper']}, mid={result['bb_middle']}, lower={result['bb_lower']}")
    print(f"ATR={result['atr']}, VWAP={result['vwap']} ({result['vwap_position']})")
    print(f"EMA alignment: {result['ema_alignment']}")
    print("[PASS] analysis/state_engine.py\n")

asyncio.run(test_state_engine())

# ─── 3B: Market Structure ─────────────────────────────────────────
from analysis.market_structure import MarketStructure

async def test_market_structure():
    state = MarketState()
    ms = MarketStructure(state)
    ms.set_previous_day_levels("NIFTY", high=24600, low=24300, close=24450)

    # Simulate opening range
    await state.update_tick("NIFTY", 24500, datetime(2026,10,5,9,20))
    await state.update_tick("NIFTY", 24520, datetime(2026,10,5,9,25))
    await state.update_tick("NIFTY", 24480, datetime(2026,10,5,9,28))

    # Seed candles with clear swing pattern
    candles = make_candles(30, base_price=24500, trend="up")
    for c in candles:
        await state.add_candle("NIFTY", "1m", c)

    result = await ms.on_candle_close("NIFTY", candles[-1])
    assert result is not None
    assert "phase" in result
    assert "pdh" in result
    assert result["pdh"] == 24600
    assert result["pdl"] == 24300

    # Volume profile
    vp = result["volume_profile"]
    assert "poc" in vp
    assert "vah" in vp
    assert "val" in vp
    print(f"Phase: {result['phase']}")
    print(f"PDH={result['pdh']}, PDL={result['pdl']}, PDC={result['pdc']}")
    print(f"Volume Profile: POC={vp['poc']}, VAH={vp['vah']}, VAL={vp['val']}")
    print(f"Price vs VP: {result['current_price_vs_vp']}")
    print(f"Swings: {len(result['swing_highs'])} highs, {len(result['swing_lows'])} lows")

    levels = ms.get_key_levels("NIFTY")
    print(f"Key levels: {len(levels)} total")
    print("[PASS] analysis/market_structure.py\n")

asyncio.run(test_market_structure())

# ─── 3C: Liquidity Flow ───────────────────────────────────────────
from analysis.liquidity_flow import LiquidityFlow, STREAM_B_WEIGHT

async def test_liquidity_flow():
    state = MarketState()
    lf = LiquidityFlow(state)

    assert STREAM_B_WEIGHT == 0.8, f"Expected 0.8, got {STREAM_B_WEIGHT}"

    # Simulate ticks
    t = datetime(2026, 10, 5, 9, 30, 0)
    prices = [24500, 24505, 24510, 24508, 24515, 24520, 24518, 24525, 24530, 24528]
    for p in prices:
        await lf.process_tick("NIFTY", float(p), int(np.random.uniform(100, 500)), t)
        t += timedelta(seconds=3)

    # Seed some candles for context
    candles = make_candles(10, 24500, "up")
    for c in candles:
        await state.add_candle("NIFTY", "1m", c)

    # Close a candle
    result = await lf.on_candle_close("NIFTY", candles[-1])
    assert result is not None
    assert "delta_current" in result
    assert "cumulative_delta" in result
    assert "delta_trend" in result
    assert "stream_weight" in result
    assert result["stream_weight"] == 0.8
    print(f"Delta current: {result['delta_current']}")
    print(f"Cumulative delta: {result['cumulative_delta']}")
    print(f"Delta trend: {result['delta_trend']}")
    print(f"Buy pressure: {result['buy_pressure_pct']}%")
    print(f"Stream weight: {result['stream_weight']}")
    print(f"Volume spike: {result['volume_spike']}")
    print(f"Stop hunt: {result['stop_hunt']}")
    print("[PASS] analysis/liquidity_flow.py\n")

asyncio.run(test_liquidity_flow())

# ─── 3D: Sentiment Engine ─────────────────────────────────────────
from analysis.sentiment import SentimentEngine

async def test_sentiment():
    state = MarketState()
    await state.update_tick("NIFTY", 24500, datetime.now())
    se = SentimentEngine(state)

    # Build test option chain
    chain = {}
    for strike in range(24200, 24800, 50):
        dist = abs(strike - 24500)
        ce_oi = max(10000 - dist * 20, 500)
        pe_oi = max(8000 - dist * 15, 500)
        chain[strike] = {
            "CE": {"oi": ce_oi, "iv": 18.5 + dist * 0.01, "ltp": max(100 - dist * 0.5, 5),
                   "instrument_key": f"NSE_FO|{strike}CE"},
            "PE": {"oi": pe_oi, "iv": 19.0 + dist * 0.01, "ltp": max(80 - dist * 0.4, 5),
                   "instrument_key": f"NSE_FO|{strike}PE"},
        }

    result = await se.on_chain_update("NIFTY", chain)
    assert result is not None
    assert "pcr" in result
    assert "max_pain" in result
    assert "atm_iv" in result
    assert "iv_percentile" in result
    assert "oi_velocity" in result
    assert result["pcr"] > 0
    assert result["max_pain"] > 0
    print(f"PCR: {result['pcr']} ({result['pcr_interpretation']})")
    print(f"PCR trend: {result['pcr_trend']}")
    print(f"Max Pain: {result['max_pain']} (distance: {result['max_pain_distance']})")
    print(f"ATM IV: {result['atm_iv']}, IV percentile: {result['iv_percentile']}")
    print(f"Highest CE OI strike: {result['highest_ce_oi_strike']}")
    print(f"Highest PE OI strike: {result['highest_pe_oi_strike']}")
    print(f"OI velocity entries: {len(result['oi_velocity'])}")

    # Test IV save
    se.save_session_iv("NIFTY", result["atm_iv"])
    print("Session IV saved to DB")
    print("[PASS] analysis/sentiment.py\n")

asyncio.run(test_sentiment())

print("=" * 50)
print("  ALL PHASE 3 MODULES VALIDATED SUCCESSFULLY")
print("=" * 50)
