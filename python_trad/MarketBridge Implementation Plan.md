MarketBridge AI v2.1 — Updated

# Phased Implementation Plan

Build an AI-powered expiry-day options trading system for Indian markets (SENSEX Tue / NIFTY Thu) · ₹3,000 capital · Claude Opus 4.6 decision engine · Full risk management

Phase 1

## Project Scaffolding & Data Foundation

\~3 days

### 1A — Project Setup & Configuration

| # | Deliverable | File | Details |
| --- | --- | --- | --- |
| 1 | Directory scaffold | `marketbridge/` | Create all directories: `data/`, `news/`, `analysis/`, `scenario/`, `agent/`, `execution/`, `dashboard/`, `logs/`, `data_store/` |
| 2 | Requirements | `requirements.txt` | `upstox-python-sdk`, `anthropic`, `pandas-ta`, `pandas`, `rich`, `newsapi-python`, `yfinance`, `apscheduler`, `aiohttp`, `python-dotenv`, `fastapi`, `uvicorn` |
| 3 | Config module | `config.py` | All constants in one place — see below |
| 4 | Env template | `.env.example` | `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`, `UPSTOX_REDIRECT_URI`, `ANTHROPIC_API_KEY`, `NEWSAPI_KEY` |

```
NIFTY_LOT = 65          # corrected
SENSEX_LOT = 20
MAX_TRADE_COST = 2000
RESERVE_CAPITAL = 1000
TOTAL_CAPITAL = 3000
STOP_LOSS_PCT = 50
MAX_OPEN_POSITIONS = 2
DAILY_LOSS_LIMIT = 1500
NIFTY_MAX_PREMIUM = 26
NIFTY_STRIKE_INTERVAL = 50
SENSEX_STRIKE_INTERVAL = 100
NO_ENTRY_AFTER = time(14, 15)
HARD_CLOSE_TIME = time(15, 0)
EXPIRY_DAYS = {"Tuesday": "SENSEX", "Thursday": "NIFTY"}
```

ImportantEvery risk parameter lives in `config.py`. No magic numbers elsewhere in the codebase.

✅ Acceptance Criteria

- `pip install -r requirements.txt` succeeds
- `config.py` importable, all constants accessible
- `.env` loaded via `python-dotenv`

### 1B — Canonical Market State

Thread-safe (asyncio-safe) shared state dict in `data/state.py`. State is pure in-memory — no disk I/O on the hot path.

```
state = {
    "index": {"NIFTY": {"ltp": 0, "open": 0, ...}, "SENSEX": {...}},
    "candles": {"NIFTY": {"1m": [], "3m": [], ...}, "SENSEX": {...}},
    "option_chain": {"NIFTY": {}, "SENSEX": {}},
    "indicators": {},
    "macro_context": {},
    "positions": [],
    "daily_pnl": 0,
    "session_active": False,
    "opening_range": {"high": None, "low": None},
    "trading_blocked": False,
}
```

✅ Acceptance Criteria

- `get_state()` / `update_state()` work correctly under concurrent asyncio tasks
- State is pure in-memory with no disk I/O on the hot path

### 1C — Upstox REST Client

| # | Deliverable | File | Details |
| --- | --- | --- | --- |
| 1 | Auth flow | `data/upstox_rest.py` | OAuth2 authorization code → access token, token persistence |
| 2 | Instrument fetch | `data/upstox_rest.py` | Get NIFTY + SENSEX instrument keys, option chain (ATM ± 10 strikes) |
| 3 | Historical data | `data/upstox_rest.py` | Fetch 20-day historical candles for IV percentile seeding |
| 4 | Order API | `data/upstox_rest.py` | `place_limit_order()`, `cancel_order()`, `get_order_status()` |

✅ Acceptance Criteria

- Auth token obtained and persisted across sessions
- Option chain for nearest 2 expiries fetchable
- Order placement returns valid order ID (test in sandbox)

### 1D — WebSocket Client & Candle Builder

| # | Deliverable | File | Details |
| --- | --- | --- | --- |
| 1 | WS client | `data/upstox_ws.py` | Async WebSocket connection, message parsing, heartbeat, auto-reconnect with exponential backoff |
| 2 | Subscribe | `data/upstox_ws.py` | Subscribe to NIFTY index, SENSEX index, and relevant option strikes |
| 3 | Candle builder | `data/candle_builder.py` | Aggregates ticks into 1m OHLCV candles; generates 3m, 5m, 15m, 30m, 1h from 1m |
| 4 | Stale detection | `data/upstox_ws.py` | If no tick for 10 seconds during market hours → log warning, trigger reconnect |

✅ Acceptance Criteria

- During market hours, NIFTY + SENSEX LTP print to console continuously
- 1m candles close correctly on the minute boundary
- Multi-timeframe candles derived from 1m data
- Auto-reconnect works after simulated disconnect

Phase 2

## Current Affairs Intelligence Layer Updated

\~2 days

### 2A — News Scanner & Data Fetching

**News sources (priority order):**

| Priority | Source | Type | Details |
| --- | --- | --- | --- |
| 1 — Primary | MoneyControl RSS | Free, no key needed | Indian-market focused. Feeds: `marketreports.xml`, `economy.xml`, `business.xml`. Deduplicate by URL; classify for impact. |
| 2 — Fallback | NewsAPI | 100 calls/day free | Activated automatically when MoneyControl RSS fails or returns \< 5 items. Query: NIFTY, SENSEX, RBI, India economy. |
| 3 — Always | yfinance | Free | SGX Nifty proxy, crude oil, USD/INR. Fetched regardless of news source. |
| 4 — Always | Economic calendar | Hardcoded | RBI policy dates, GDP/CPI releases. Returns `is_rbi_day`, `is_budget_day`, etc. |

✅ Acceptance Criteria

- MoneyControl RSS feeds parsed and headlines returned on successful fetch
- NewsAPI invoked automatically as fallback when MoneyControl RSS fails or returns \< 5 items
- Scanner returns structured dict with all fields populated regardless of which news source was used
- SGX gap calculated as percentage
- Earnings list for today's Nifty50 stocks populated

### 2B — Impact Classifier & Bias Generator

| # | Deliverable | File | Details |
| --- | --- | --- | --- |
| 1 | Classifier | `news/classifier.py` | Tags each event as `HIGH` / `MEDIUM` / `LOW` impact. Sets `trading_blocked` flag on HIGH impact pre-event |
| 2 | Bias generator | `news/news_bias.py` | Translates `macro_context` → plain-English instruction string prepended to every agent call |

**Bias rules:** RBI day → block trading until announcement, then confluence ≥ 8 only · RISK_OFF → bearish bias, extra delta confirmation for bullish · SGX gap > 0.5% → skip first 15 min, expect gap fill · Earnings today → watch for index-level impact

✅ Acceptance Criteria

- `get_bias_instruction()` returns correct strings for all test scenarios
- HIGH impact events set `state["trading_blocked"] = True`
- Midday refresh at 12:00 PM updates `macro_context`

Phase 3

## Analysis Streams Updated

\~3 days

### 3A — Technical Indicators (Stream Core)

`analysis/state_engine.py` — On each 1m close: compute EMA (9,21,50), VWAP, RSI(14), MACD(12,26,9), Bollinger Bands(20,2), ATR(14) using `pandas-ta`. Results stored in `state["indicators"]`.

✅ Acceptance Criteria

- All indicators compute on real 1m candle data
- No NaN values after warmup period (first \~50 candles)
- Results stored in `state["indicators"]`

### 3B — Market Structure (Stream A)

`analysis/market_structure.py` — Swing high/low detector, volume profile (POC, VAH, VAL), market phase tagger (`TRENDING_UP` / `TRENDING_DOWN` / `RANGING`), PDH/PDL levels from previous session.

✅ Acceptance Criteria

- Swing points detected within 2 candles of visual swing
- Phase correctly identifies trending vs. ranging conditions
- PDH/PDL levels loaded from previous session data

### 3C — Liquidity & Flow (Stream B) Rearchitected

Since Upstox WebSocket provides LTP ticks (not L2 order book data), Stream B uses a **tick-classification approach** — the best achievable without exchange co-location.

| Component | File | Logic |
| --- | --- | --- |
| Tick classifier | `analysis/liquidity_flow.py` | Classify each tick as aggressive buy (uptick + volume > 20-tick rolling avg) or aggressive sell (downtick + volume > avg) |
| Per-candle delta | `analysis/liquidity_flow.py` | Sum of (buy_volume − sell_volume) per 1m candle. Signed: positive = net buying pressure |
| Cumulative delta | `analysis/liquidity_flow.py` | Rolling sum of per-candle delta from 9:15 AM. Tracks session-long order flow direction. Resets each session. |
| Delta divergence | `analysis/liquidity_flow.py` | Flags when price makes new high/low but cumulative delta diverges (e.g., price up but delta falling = distribution) |
| Stop-hunt detector | `analysis/liquidity_flow.py` | Conditions: (1) candle pierces known swing level by >0.1% AND (2) closes back above/below AND (3) delta on candle is negative for bullish wick. Flags as `STOP_HUNT` |
| Volume spike detector | `analysis/liquidity_flow.py` | Flags candles where volume > 2.5× the 20-candle average — marks institutional entry or exhaustion |

Limitation Upstox WebSocket tick data does not include bid/ask quotes, so delta is approximated via tick direction and volume. Stream B is a directional signal rather than true order flow. **Weight at 0.8×** relative to Structure (Stream A) and Sentiment (Stream C) in the confluence scorer.

✅ Acceptance Criteria

- Delta per candle computed and signed (positive = net buying pressure)
- Cumulative delta resets at 9:15 AM each session
- Delta divergence flags correctly on test data (price new high, delta declining)
- Stop-hunt detector flags wick-through-level + close-back patterns
- Volume spike flags candles where volume > 2.5× 20-candle average
- Stream B weight logged as 0.8× in confluence output

### 3D — Sentiment & Options Flow (Stream C) Updated

| # | Deliverable | File | Details |
| --- | --- | --- | --- |
| 1 | Sentiment engine | `analysis/sentiment.py` | PCR time series (from OI data), OI velocity (rate of change per strike), Max Pain calculation, IV percentile (vs. 20-day history from SQLite) |
| 2 | Option chain refresh rate | `data/upstox_rest.py` | REST call every 30 seconds (Upstox rate limit: \~2 calls/min). After each response completes, wait 2–3 seconds before processing. Never poll during reconnect. |

✅ Acceptance Criteria

- PCR updates on every option chain refresh (every 30 seconds)
- Option chain REST calls spaced at 30-second intervals with 2–3 second post-response delay
- Max Pain calculated correctly (sum of losses for all writers)
- IV percentile computed against historical data from `data_store/iv_history.db`
- OI velocity detects significant buildup/unwinding

Phase 4

## Scenario Builder & AI Agent Updated

\~5 days

### 4A — Trigger & Confluence System

| # | Deliverable | File | Details |
| --- | --- | --- | --- |
| 1 | Trigger engine | `scenario/trigger.py` | After each 1m close, checks all trigger conditions: EMA cross, VWAP reclaim/reject, RSI divergence, delta flip, OI spike, Max Pain proximity |
| 2 | Confluence scorer | `scenario/confluence.py` | Scores 3 streams independently (0–3 each). Stream B weighted at 0.8×. Gate: score \< 5 → suppress. Threshold adjusts by time: 5 (morning), 7 (midday), 6 (afternoon) |
| 3 | Scenario classifier | `scenario/classifier.py` | Maps trigger + stream signals → `LIQUIDITY_TRAP` \| `BREAKOUT` \| `MEAN_REVERSION` \| `MAX_PAIN_DRIFT` \| `OPENING_RANGE_BREAK` |

✅ Acceptance Criteria

- Triggers fire correctly on historical data replays
- Confluence gating reduces agent calls to 3–8 per session (not 30+)
- Scenario types map logically to market conditions
- Stream B weighted at 0.8× in confluence score calculation

### 4B — Context Builder

`scenario/builder.py` — Assembles final JSON context (target: under 4K tokens) from all streams + macro context + memory.

```
{
  "macro_context": { ... },
  "market_state": { "index": "SENSEX", "ltp": 81450, "time": "10:42" },
  "analysis": {
    "structure": { "phase": "TRENDING_DOWN", "nearest_swing_low": 81300 },
    "flow": { "delta_2m": -1200, "cumulative_delta": -8500, "stop_hunt": false },
    "sentiment": { "pcr": 0.85, "max_pain": 81500, "iv_percentile": 65 }
  },
  "scenario": { "type": "MEAN_REVERSION", "confluence_score": 7 },
  "memory": [ /* last 5 decisions */ ],
  "question": "Given this MEAN_REVERSION setup on SENSEX expiry..."
}
```

### 4C — Claude Agent Client Updated

| # | Deliverable | File | Details |
| --- | --- | --- | --- |
| 1 | API client | `agent/client.py` | Direct Anthropic API. Uses `claude-sonnet-4-6` (confluence 5–7), `claude-opus-4-5` (confluence 8–9 = Opus 4.6). JSON mode enforced. Retry with backoff on 429/500. Used as fallback when Antigravity is offline. |
| 2 | System prompt | `agent/prompts.py` | Master system prompt defining agent persona, rules, response schema. Per-scenario question templates. |
| 3 | Decision memory | `agent/memory.py` | Rolling list of last 5 decisions with outcomes. Fed back to agent for self-correction. |
| 4 | MCP bridge | `agent/mcp_bridge.py` | FastAPI server on `localhost:8765`. Exposes `POST /decide` — MarketBridge posts context JSON → bridge forwards to Antigravity harness via MCP tool call → returns structured JSON. Falls back to `client.py` if Antigravity is offline. `GET /health` reports Antigravity status. |

**Agent response schema:**

```
{
  "decision": "BUY" | "SELL" | "WAIT",
  "confidence": 1-10,
  "entry_zone": { "trigger_price": 81400, "direction": "below" },
  "invalidation": 81700,
  "targets": [81200, 81100],
  "reasoning": "...",
  "trap_flags": ["gap_fill_incomplete"]
}
```

✅ Acceptance Criteria

- Agent returns valid JSON 100% of the time (retry on parse failure)
- `claude-sonnet-4-6` used for score 5–7; `claude-opus-4-5` (Opus 4.6) for score 8–9
- Decision memory correctly tracks outcomes of past calls
- All agent calls logged to `logs/agent_calls_YYYYMMDD.jsonl`
- MCP bridge starts on `localhost:8765` at system startup
- `/health` returns `{"status": "ok", "antigravity": true/false}`
- Fallback to direct Anthropic API works automatically when Antigravity is offline
- All decisions routed through bridge regardless of backend

### 4D — Option Selector

`agent/option_selector.py` — Direction + confidence + time → exact strike + budget check.

| Step | Logic |
| --- | --- |
| 1 | Direction → CE (BUY) or PE (SELL) |
| 2 | ATM: NIFTY `round(LTP/50)*50`, SENSEX `round(LTP/100)*100` |
| 3 | OTM depth: SENSEX conf ≥ 8 before 11am → ATM; 6-7 or 11–13:30 → 1-OTM; after 13:30 → 2-OTM. NIFTY: always 2–3 OTM, only if premium ≤ ₹26 |
| 4 | OI wall check: if target strike has highest OI → move 1 strike further OTM |
| 5 | Budget gate: `400 ≤ premium × lot_size ≤ 2000` |

✅ Acceptance Criteria

- NIFTY trades rejected if premium > ₹26
- Cost never exceeds ₹2,000
- OI wall avoidance works correctly
- Returns `None` if no affordable strike found (system waits)

Phase 5

## Execution Engine

\~3 days

### 5A — Entry Watcher

Agent decision received → entry_watcher starts → Every tick: check LTP vs. entry_zone → On 1m close in zone: → Check: last 2 candles delta still agrees? → YES: fire order → NO: abort ("delta flip") → 10 min timeout: discard ("setup expired") → Time check: block if after 2:15 PM

✅ Acceptance Criteria

- No entry on wick-through — only candle close confirmation
- Delta reversal correctly aborts entry
- Timeout fires after exactly 10 minutes
- Entries blocked after 2:15 PM

### 5B — Order Manager

`execution/order_manager.py` — Limit order at ask price. If not filled in 30s → cancel. One retry at ask+1 tick. Max 2 open positions enforced.

✅ Acceptance Criteria

- Limit orders only (never market orders for entries)
- 30-second cancel timer works
- 3rd position blocked if 2 already open
- Daily loss limit (₹1,500) enforced — blocks new entries

### 5C — Position Monitor

Runs every 30 seconds. Full 6-level exit ladder (priority order):

| Rule | Trigger | Action |
| --- | --- | --- |
| Hard Close | `time >= 15:00` | Exit 100% at market |
| Invalidation | Index crosses invalidation level | Exit 100% |
| Stop Loss | Premium down 50% | Exit 100% at market |
| TP1 | Premium up 50% | Exit 50%, move stop to breakeven |
| TP2 | Premium up 100% | Exit 30% more, start trailing |
| Trail | After TP2, premium drops 30% from peak | Exit remaining 20% |

✅ Acceptance Criteria

- All 6 exit conditions fire correctly
- Partial exits calculate correct quantities (rounding for lot size)
- Trail stop tracks highest premium and triggers at 70% of peak
- 3:00 PM hard close is non-negotiable — all positions closed

Phase 6

## Dashboard, Orchestration & Go-Live

\~2 days

### 6B — Main Orchestrator & Scheduler

1\. Load .env, config 2. Check: is today Tuesday or Thursday? → No: print "Not an expiry day", exit → Yes: determine SENSEX or NIFTY 3. 8:30 AM: Run news scanner → set macro_context 4. 9:00 AM: Start mcp_bridge on :8765, connect Upstox WS + REST 5. 9:15 AM: Market opens → observe mode (build opening range) 6. 9:30 AM: Trading loop → analysis → triggers → agent → execution 7. 3:00 PM: Hard close all 8. 3:05 PM: Write logs, session summary

### 6D — Paper Trading & Go-Live

| # | Step | Details |
| --- | --- | --- |
| 1 | Paper trade mode | `PAPER_MODE = True` in config. Everything runs except `order_manager` places no real orders — logs what it would have done |
| 2 | Run 2 full sessions | 1 SENSEX Tuesday + 1 NIFTY Thursday in paper mode |
| 3 | Review | Compare agent decisions to actual market movement. Were triggers right? Did confluence gating prevent bad trades? |
| 4 | Go live | Set `PAPER_MODE = False`. First live session: SENSEX Tuesday with ₹3,000 |

Caution Do NOT skip paper trading. Running live without 2 verified paper sessions risks real capital loss on untested edge cases.

Risk Rules Enforcement Matrix

These are system-enforced, not advisory. Every rule maps to a specific code gate.

| Rule | Enforced In | Mechanism |
| --- | --- | --- |
| Max ₹2,000 per trade | `agent/option_selector.py` | `premium × lot_size > 2000` → reject |
| NIFTY premium ≤ ₹26 | `agent/option_selector.py` | Hard reject above ₹26 |
| Max 2 open positions | `execution/order_manager.py` | Block new entry if `len(positions) >= 2` |
| 50% stop loss | `execution/position_monitor.py` | Exit at market, no override |
| No entries after 2:15 PM | `execution/entry_watcher.py` | Time check before firing |
| 3:00 PM hard close | APScheduler + `position_monitor.py` | Non-negotiable market exit |
| Daily loss limit ₹1,500 | `execution/order_manager.py` | `daily_pnl <= -1500` → block entries |
| Expiry days only | `main.py` | Day-of-week check at startup |
| HIGH impact event block | `news/news_bias.py` → `entry_watcher.py` | `trading_blocked` flag |

Build Summary

| Phase | Key Files | Duration | Key Risk |
| --- | --- | --- | --- |
| 1 — Foundation | `config.py`, `state.py`, `upstox_rest.py`, `upstox_ws.py`, `candle_builder.py` | 3 days | Upstox OAuth complexity, WebSocket stability |
| 2 — News Intel | `scanner.py`, `classifier.py`, `news_bias.py`, `calendar.py` | 2 days | MoneyControl RSS stability; NewsAPI as fallback |
| 3 — Analysis | `state_engine.py`, `market_structure.py`, `liquidity_flow.py`, `sentiment.py` | 3 days | Stream B is approximate (no L2 data); weighted 0.8× |
| 4 — AI Decision | `trigger.py`, `confluence.py`, `builder.py`, `client.py`, `mcp_bridge.py` | 5 days | Prompt engineering for JSON; MCP bridge reliability with Antigravity |
| 5 — Execution | `entry_watcher.py`, `order_manager.py`, `position_monitor.py` | 3 days | Partial exit math with lot sizes; order fill edge cases |
| 6 — Operations | `terminal.py`, `main.py`, logging, paper trade | 2 days | End-to-end integration bugs; scheduler timing |

Total **\~22–25 working days.** Budget at the high end if building solo — Upstox OAuth alone typically takes a full day in production, and prompt engineering for consistent JSON output requires iteration across real market sessions.