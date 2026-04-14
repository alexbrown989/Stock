"""
Covered Call Scanner — Configuration
"""

# Tickers to scan
WATCHLIST = [
    "IBIT", "AFRM", "SOFI", "HOOD", "MARA",
    "PLTR", "RIOT", "CLSK", "HIMS", "UPST",
    "SOUN", "GRAB", "NU",   "RKLB", "BBAI",
    "JOBY", "ACHR", "OPEN", "CIFR", "CLOV",
]

# ── Entry filters ──────────────────────────────────────────────────────────────
IV_RANK_MIN        = 50      # % — only sell when vol is expensive
DELTA_MIN          = 0.20    # too low = not enough premium
DELTA_MAX          = 0.40    # too high = too much assignment risk
DTE_MIN            = 10      # days to expiry
DTE_MAX            = 21      # sweet spot for theta decay
MIN_PREMIUM        = 0.15    # $ per share — skip junk premiums
MIN_OPEN_INTEREST  = 100     # contracts — liquidity check
EARNINGS_BLACKOUT  = 14      # days — skip if earnings within this window

# ── Exit rules ─────────────────────────────────────────────────────────────────
PROFIT_TARGET_PCT  = 0.50    # close when 50% of premium is captured
STOP_LOSS_MULT     = 2.00    # close if option value = 2× what you collected

# ── Capital ────────────────────────────────────────────────────────────────────
MAX_CAPITAL        = 7_000   # $ — hard cap; skip if strike × 100 > this
MONTHLY_TARGET_PCT = 5.0     # % monthly compound target

# ── Schedule ───────────────────────────────────────────────────────────────────
SCAN_INTERVAL_HOURS = 4

# ── Paths ──────────────────────────────────────────────────────────────────────
import os
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
LEDGER_FILE = os.path.join(BASE_DIR, "ledger.json")
LOG_DIR     = os.path.join(BASE_DIR, "logs")
