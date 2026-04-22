"""
Unified configuration for both strategy systems in this repo.

  1. Covered Call / Wheel Scanner  (original)  — variables unchanged
  2. Theta Harvest                 (new)        — prefixed TH_* where conflicts exist
"""

import os

# ══════════════════════════════════════════════════════════════════════════════
#  COVERED CALL / WHEEL SCANNER  (original)
# ══════════════════════════════════════════════════════════════════════════════

WATCHLIST = [
    "IBIT", "AFRM", "SOFI", "HOOD", "MARA",
    "PLTR", "RIOT", "CLSK", "HIMS", "UPST",
    "SOUN", "GRAB", "NU",   "RKLB", "BBAI",
    "JOBY", "ACHR", "OPEN", "CIFR", "CLOV",
]

DELTA_MIN          = 0.25
DELTA_MAX          = 0.40
IV_RANK_MIN        = 50
DTE_MIN            = 10
DTE_MAX            = 21
MIN_PREMIUM        = 0.15
MIN_OPEN_INTEREST  = 100
SPREAD_MAX_PCT     = 25
EARNINGS_BLACKOUT  = 14
MAX_CAPITAL        = 7_000

LEVEL_HISTORY_DAYS = 90
LEVEL_TOUCH_TOL    = 0.02
STRONG_LEVEL_MIN   = 3
RESIST_ALIGN_TOL   = 0.03

PROFIT_TARGET_PCT  = 0.50
STOP_LOSS_MULT     = 2.00
MONTHLY_TARGET_PCT = 5.0
SCAN_INTERVAL_HOURS = 4
SCAN_WORKERS        = 5

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
LEDGER_FILE = os.path.join(BASE_DIR, "ledger.json")
LOG_DIR     = os.path.join(BASE_DIR, "logs")


# ══════════════════════════════════════════════════════════════════════════════
#  THETA HARVEST  (systematic 14-day theta decay cycle)
# ══════════════════════════════════════════════════════════════════════════════

LIQUID_UNIVERSE = [
    # Broad market index ETFs
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "TQQQ",

    # Sector ETFs
    "XLF", "XLE", "XLK", "XLV", "XLU", "XLI", "XLB", "XLP", "XLY", "XLC", "XLRE",

    # International & emerging
    "EEM", "EFA", "FXI", "KWEB",

    # Fixed income
    "TLT", "HYG", "LQD", "SHY",

    # Commodity & real assets
    "GLD", "SLV", "USO", "GDX", "GDXJ",

    # Volatility / risk proxies
    "VXX", "UVXY",

    # Mega-cap equities
    "AAPL", "MSFT", "NVDA", "META", "GOOGL", "AMZN", "TSLA",
    "AMD", "NFLX", "JPM", "BAC", "XOM", "CVX", "UBER", "COIN",
]

MIN_OPTIONS_VOLUME         = 500
TH_MIN_OI                  = 200
MAX_SPREAD_PCT             = 0.10

TH_DELTA_MIN               = 0.15
TH_DELTA_MAX               = 0.35
THETA_EFFICIENCY_MIN       = 0.005
GAMMA_STRESS_MAX_DELTA_CHG = 0.10

TH_IV_RANK_MIN             = 30
HV_WINDOW                  = 20
IV_HV_RATIO_MIN            = 1.10

TH_DTE_MIN                 = 14
TH_DTE_MAX                 = 45
MAX_RISK_PER_TRADE_PCT     = 0.02
SPREAD_PREMIUM_TARGET_PCT  = 0.25
POP_MIN                    = 0.65

CYCLE_DAYS                 = 14
MAX_CONCURRENT_POSITIONS   = 6
TH_PROFIT_TARGET_PCT       = 0.50
STOP_LOSS_PCT              = 2.00

RISK_FREE_RATE             = 0.053

CACHE_DIR                  = ".cache"
CACHE_TTL_SECONDS          = 900
POSITIONS_FILE             = "positions.json"
PERFORMANCE_FILE           = "performance.json"
