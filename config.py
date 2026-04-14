"""
Wheel Strategy Bot — Central Configuration
All tunable parameters live here.
"""

# ── Target Tickers ────────────────────────────────────────────────────────────
WATCHLIST: list[str] = [
    "IBIT", "AFRM", "SOFI", "HOOD", "MARA",
    "PLTR", "RIOT", "CLSK", "CIFR", "HIMS",
    "JOBY", "RKLB", "ACHR", "OPEN", "UPST",
    "SOUN", "GRAB", "NU",   "CLOV", "BBAI",
]

# Tickers used as gamma-squeeze lead indicators (large-cap momentum drivers)
GAMMA_LEAD_TICKERS: list[str] = ["NVDA", "TSLA", "SPY", "QQQ"]

# ── Capital Guardrails ────────────────────────────────────────────────────────
MAX_TOTAL_CAPITAL: float  = 7_000.00   # hard cap per trade / net debit
MONTHLY_COMPOUND_TARGET: float = 0.05  # 5 % per month

# ── Options Filter Thresholds ─────────────────────────────────────────────────
# Delta bands
DELTA_AGGRESSIVE_MIN: float = 0.30
DELTA_AGGRESSIVE_MAX: float = 0.45
DELTA_PASSIVE_MIN: float    = 0.15
DELTA_PASSIVE_MAX: float    = 0.25

IV_RANK_MIN: float          = 55.0     # percent — only sell expensive vol
THETA_PREMIUM_RATIO_MIN: float = 0.018 # 1.8 % per day minimum
LIQUIDITY_FLOOR_DAILY: float   = 1_000_000  # $1 M daily options volume on strike
MIN_OPEN_INTEREST: int         = 100

# ── Gamma / Risk Guardrails ───────────────────────────────────────────────────
GAMMA_RISK_MAX: float       = 0.15     # above this → suggest Defensive Roll
EARNINGS_BLACKOUT_DAYS: int = 14       # no trade within N days of earnings

# ── Max Pain Band ─────────────────────────────────────────────────────────────
# Sell calls this many percent ABOVE max-pain strike
MAX_PAIN_ABOVE_PCT_MIN: float = 0.05   # 5 %
MAX_PAIN_ABOVE_PCT_MAX: float = 0.10   # 10 %

# ── DTE (Days to Expiration) ──────────────────────────────────────────────────
TARGET_DTE_MIN: int = 10
TARGET_DTE_MAX: int = 21   # 2-week premium harvest window

# ── Exit Management (TastyTrade 50 % rule — core discipline) ─────────────────
# Close when the position has earned 50 % of max premium. This is the single
# most empirically-validated rule in systematic premium selling. It frees
# capital for the next trade and eliminates gamma risk near expiry.
PROFIT_TARGET_PCT: float    = 0.50   # close at 50 % of max premium collected
STOP_LOSS_MULTIPLIER: float = 2.0    # hard stop: loss = 2× credit received
ROLL_DTE_THRESHOLD: int     = 7      # roll if untested and DTE falls below this

# ── Position Sizing ───────────────────────────────────────────────────────────
# With $7K capital we can typically sell 1–3 contracts depending on the name.
MAX_POSITION_PCT: float     = 0.30   # max 30 % of total capital per name
MAX_CONCURRENT_TRADES: int  = 4      # avoids over-concentration

# ── Scan Schedule ─────────────────────────────────────────────────────────────
SCAN_INTERVAL_HOURS: int = 4

# ── Correlative Skew (Gamma Squeeze Lead) ────────────────────────────────────
# Rolling window (days) used to compute correlation between lead & mid-cap
CORR_WINDOW_DAYS: int = 20
# IV skew threshold to flag a potential squeeze setup
CORR_SKEW_THRESHOLD: float = 0.65

# ── Sentiment Entropy ─────────────────────────────────────────────────────────
# Put/Call ratio drop that signals reversal when sentiment is "Peak Fear"
PC_RATIO_DROP_THRESHOLD: float = 0.15  # 15 % drop in P/C ratio
PEAK_FEAR_IV_RANK_THRESHOLD: float = 75.0  # IV Rank > 75 % = peak fear

# ── Paths ─────────────────────────────────────────────────────────────────────
import os
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
LEDGER_PATH = os.path.join(BASE_DIR, "ledger.json")
DATA_DIR    = os.path.join(BASE_DIR, "data")
LOG_DIR     = os.path.join(BASE_DIR, "data", "logs")
CACHE_DIR   = os.path.join(BASE_DIR, "data", "cache")
