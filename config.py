"""
Covered Call Scanner — Configuration
All thresholds live here. Change one number, it flows everywhere.
"""

import os

# ── Watchlist ──────────────────────────────────────────────────────────────────
WATCHLIST = [
    "IBIT", "AFRM", "SOFI", "HOOD", "MARA",
    "PLTR", "RIOT", "CLSK", "HIMS", "UPST",
    "SOUN", "GRAB", "NU",   "RKLB", "BBAI",
    "JOBY", "ACHR", "OPEN", "CIFR", "CLOV",
]

# ── Entry filters ──────────────────────────────────────────────────────────────
DELTA_MIN          = 0.25    # lower = safer but less premium
DELTA_MAX          = 0.40    # upper = more premium but higher assignment risk
IV_RANK_MIN        = 50      # % — only sell when volatility is elevated
DTE_MIN            = 10      # days to expiry — minimum
DTE_MAX            = 21      # days to expiry — maximum (theta sweet spot)
MIN_PREMIUM        = 0.15    # $ per share — skip junk
MIN_OPEN_INTEREST  = 100     # contracts — minimum liquidity
SPREAD_MAX_PCT     = 25      # % — skip wide bid/ask spreads (illiquid)
EARNINGS_BLACKOUT  = 14      # days — hard skip if earnings within this window
MAX_CAPITAL        = 7_000   # $ — skip if strike × 100 exceeds this

# ── Technical levels ───────────────────────────────────────────────────────────
LEVEL_HISTORY_DAYS = 90      # trading days to analyse for S/R
LEVEL_TOUCH_TOL    = 0.02    # 2 % band — a "touch" if candle overlaps this band
STRONG_LEVEL_MIN   = 3       # visits needed to be called "strong"
RESIST_ALIGN_TOL   = 0.03    # 3 % — strike is "resistance-aligned" if this close to R1/R2

# ── Exit rules ─────────────────────────────────────────────────────────────────
PROFIT_TARGET_PCT  = 0.50    # close when 50 % of max premium is captured
STOP_LOSS_MULT     = 2.00    # close if option costs 2× what you collected

# ── Capital goal ───────────────────────────────────────────────────────────────
MONTHLY_TARGET_PCT = 5.0     # % monthly compounding target

# ── Schedule ───────────────────────────────────────────────────────────────────
SCAN_INTERVAL_HOURS = 4
SCAN_WORKERS        = 5      # parallel ticker threads

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
LEDGER_FILE = os.path.join(BASE_DIR, "ledger.json")
LOG_DIR     = os.path.join(BASE_DIR, "logs")
