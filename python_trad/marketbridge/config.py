"""
MarketBridge AI v2.1 — Central Configuration

Every trading constant, API endpoint, risk parameter, and threshold lives here.
No magic numbers anywhere else in the codebase.
"""

import os
from datetime import time, timedelta
from pathlib import Path
from dotenv import load_dotenv

# ─── Load Environment ─────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ─── API Credentials ──────────────────────────────────────────────────
UPSTOX_API_KEY = os.getenv("UPSTOX_API_KEY", "")
UPSTOX_API_SECRET = os.getenv("UPSTOX_API_SECRET", "")
UPSTOX_REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "http://localhost:5000/callback")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")

# ─── Trading Mode ─────────────────────────────────────────────────────
PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"

# ─── Lot Sizes (SEBI-mandated, updated) ───────────────────────────────
NIFTY_LOT = 65
SENSEX_LOT = 20

# ─── Budget & Risk ────────────────────────────────────────────────────
TOTAL_CAPITAL = 3000               # ₹3,000 total
MAX_TRADE_COST = 2000              # Max ₹2,000 per single trade
RESERVE_CAPITAL = 1000             # Always keep ₹1,000 in reserve
STOP_LOSS_PCT = 50                 # Exit if premium drops 50%
MAX_OPEN_POSITIONS = 2             # Never more than 2 positions open
DAILY_LOSS_LIMIT = 1500            # Shut off entries if day P&L hits -₹1,500

# NIFTY premium ceiling: ₹26 per spec
# With lot 65, ₹26 × 65 = ₹1,690 — fits comfortably within MAX_TRADE_COST
NIFTY_MAX_PREMIUM = 26

# Budget gate: reject if cost < 400 (too far OTM, delta too low)
MIN_TRADE_COST = 400

# SENSEX premium ceiling: floor(2000 / 20) = Rs.100
SENSEX_MAX_PREMIUM = 100

# ─── Strike Intervals ─────────────────────────────────────────────────
NIFTY_STRIKE_INTERVAL = 50        # NIFTY strikes every 50 points
SENSEX_STRIKE_INTERVAL = 100      # SENSEX strikes every 100 points

# ─── Time Windows ─────────────────────────────────────────────────────
MARKET_OPEN = time(9, 15)
OPENING_RANGE_END = time(9, 30)    # Observe only until 9:30
PRIMARY_WINDOW_END = time(11, 30)  # Best setups: 9:30–11:30
QUIET_PERIOD_END = time(13, 30)    # Elevated threshold: 11:30–13:30
SECONDARY_WINDOW_END = time(14, 15)  # Max Pain pull: 13:30–14:15
NO_ENTRY_AFTER = time(14, 15)     # No new entries after 2:15 PM
HARD_CLOSE_TIME = time(15, 0)     # Exit ALL at 3:00 PM — no exceptions
LOG_WRITE_TIME = time(15, 5)      # Write session logs at 3:05 PM

# Pre-market schedule
NEWS_SCAN_TIME = time(8, 30)      # Pre-market news scan
NEWS_REFRESH_TIME = time(12, 0)   # Midday news refresh
SYSTEM_CONNECT_TIME = time(9, 0)  # Connect broker, load instruments

# ─── Expiry Days ───────────────────────────────────────────────────────
# Maps weekday name → which index expires that day
EXPIRY_DAYS = {
    "Tuesday": "SENSEX",
    "Thursday": "NIFTY",
}

# ─── Confluence Thresholds (by time window) ────────────────────────────
# Higher threshold = stricter filter = fewer but higher-quality trades
CONFLUENCE_THRESHOLDS = {
    "primary":   5,    # 9:30–11:30 — full activity
    "quiet":     7,    # 11:30–13:30 — choppy, higher bar
    "secondary": 6,    # 13:30–14:15 — Max Pain window
}

# ─── Take Profit Levels ───────────────────────────────────────────────
TP1_PCT = 50          # Exit 50% of position at +50% premium
TP1_EXIT_FRACTION = 0.5
TP2_PCT = 100         # Exit 30% more at +100% premium (doubled)
TP2_EXIT_FRACTION = 0.3
TRAIL_STOP_PCT = 30   # Trail stop = 30% below highest premium seen

# ─── Entry Watcher ─────────────────────────────────────────────────────
ENTRY_TIMEOUT_SECONDS = 600       # 10 minutes to hit entry zone
ORDER_FILL_TIMEOUT_SECONDS = 30   # Cancel limit order after 30 seconds
ORDER_RETRY_TICKS = 1             # Re-place at ask + 1 tick (once)

# ─── Agent Configuration ──────────────────────────────────────────────
AGENT_MODEL_DEFAULT = "claude-sonnet-4-6-20250514"   # Speed + cost
AGENT_MODEL_HIGH_CONVICTION = "claude-opus-4-5-20250514"  # High confluence only (Opus 4.6)
AGENT_HIGH_CONVICTION_THRESHOLD = 8  # Use Opus when confluence ≥ 8
AGENT_MEMORY_SIZE = 5             # Rolling last-5-decisions store
AGENT_MAX_CONTEXT_TOKENS = 4000   # Keep context lean for cost

# ─── Upstox API Endpoints ─────────────────────────────────────────────
UPSTOX_BASE_URL = "https://api.upstox.com/v2"
UPSTOX_AUTH_URL = f"{UPSTOX_BASE_URL}/login/authorization/dialog"
UPSTOX_TOKEN_URL = f"{UPSTOX_BASE_URL}/login/authorization/token"
UPSTOX_WS_AUTH_URL = f"{UPSTOX_BASE_URL}/feed/market-data-feed/authorize"
UPSTOX_MARKET_QUOTE_URL = f"{UPSTOX_BASE_URL}/market-quote/quotes"
UPSTOX_LTP_URL = f"{UPSTOX_BASE_URL}/market-quote/ltp"
UPSTOX_OPTION_CHAIN_URL = f"{UPSTOX_BASE_URL}/option/chain"
UPSTOX_HISTORICAL_URL = f"{UPSTOX_BASE_URL}/historical-candle"
UPSTOX_PLACE_ORDER_URL = f"{UPSTOX_BASE_URL}/order/place"
UPSTOX_CANCEL_ORDER_URL = f"{UPSTOX_BASE_URL}/order/cancel"
UPSTOX_ORDER_BOOK_URL = f"{UPSTOX_BASE_URL}/order/retrieve-all"

# ─── Instrument Keys ──────────────────────────────────────────────────
# Upstox v2 instrument key format
NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"
SENSEX_INDEX_KEY = "BSE_INDEX|SENSEX"

# F&O exchange segments
NIFTY_FO_SEGMENT = "NSE_FO"
SENSEX_FO_SEGMENT = "BSE_FO"

# ─── Candle Timeframes ────────────────────────────────────────────────
CANDLE_TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h"]
PRIMARY_TIMEFRAME = "1m"          # All analysis runs on 1m close

# ─── Indicator Warmup ─────────────────────────────────────────────────
# Minimum candles needed before indicators are valid (no NaN)
INDICATOR_WARMUP_CANDLES = 50

# ─── WebSocket Health ─────────────────────────────────────────────────
WS_STALE_TIMEOUT_SECONDS = 10    # Reconnect if no tick for 10 seconds
WS_RECONNECT_BASE_DELAY = 1     # Exponential backoff: 1, 2, 4, 8... seconds
WS_RECONNECT_MAX_DELAY = 60     # Cap at 60 seconds
WS_RECONNECT_MAX_ATTEMPTS = 20  # Give up after 20 consecutive failures

# ─── Option Chain ──────────────────────────────────────────────────────
OPTION_CHAIN_STRIKES = 10        # Fetch ATM ± 10 strikes

# ─── Logging ───────────────────────────────────────────────────────────
LOG_DIR = BASE_DIR / "logs"
DATA_STORE_DIR = BASE_DIR / "data_store"
IV_HISTORY_DB = DATA_STORE_DIR / "iv_history.db"

# Ensure directories exist
LOG_DIR.mkdir(exist_ok=True)
DATA_STORE_DIR.mkdir(exist_ok=True)
