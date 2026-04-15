"""
Theta Harvest — System Configuration
All tunable parameters live here. Adjust thresholds based on cycle performance.
"""

# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
LIQUID_UNIVERSE = [
    # Broad market ETFs — highest liquidity, tight spreads
    "SPY", "QQQ", "IWM", "DIA",
    # Sector ETFs
    "XLF", "XLE", "XLK", "XLV", "GDX",
    # Large-cap equities
    "AAPL", "MSFT", "NVDA", "META", "GOOGL", "AMZN", "TSLA",
    "AMD", "NFLX", "JPM", "BAC", "XOM", "CVX",
]

# Minimum options daily volume to qualify for scanning
MIN_OPTIONS_VOLUME = 500

# Minimum open interest per strike
MIN_OPEN_INTEREST = 200

# Maximum bid-ask spread as fraction of mid price
MAX_SPREAD_PCT = 0.10  # 10%

# ---------------------------------------------------------------------------
# Options Greeks Filters
# ---------------------------------------------------------------------------
# Delta: absolute value range for short options
DELTA_MIN = 0.15
DELTA_MAX = 0.35

# Theta efficiency: daily theta / premium collected (want > this)
THETA_EFFICIENCY_MIN = 0.005  # 0.5% of premium per day

# Gamma stress: max delta change for a 2% underlying move
GAMMA_STRESS_MAX_DELTA_CHANGE = 0.10

# ---------------------------------------------------------------------------
# Volatility Filters
# ---------------------------------------------------------------------------
# Minimum IV Rank to qualify (prefers elevated IV environment)
IV_RANK_MIN = 30  # out of 100

# Historical volatility lookback window (days)
HV_WINDOW = 20

# IV / HV ratio minimum — want IV premium over realized vol
IV_HV_RATIO_MIN = 1.10

# ---------------------------------------------------------------------------
# Trade Structure
# ---------------------------------------------------------------------------
# Target days-to-expiration range
DTE_MIN = 14
DTE_MAX = 45

# Maximum capital at risk per position (% of notional)
MAX_RISK_PER_TRADE_PCT = 0.02  # 2%

# Target premium collected as % of width (for spreads)
SPREAD_PREMIUM_TARGET_PCT = 0.25  # collect >= 25% of width

# Probability of profit minimum
POP_MIN = 0.65  # 65%

# ---------------------------------------------------------------------------
# Cycle Management
# ---------------------------------------------------------------------------
CYCLE_DAYS = 14
MAX_CONCURRENT_POSITIONS = 6
PROFIT_TARGET_PCT = 0.50   # close at 50% of max profit
STOP_LOSS_PCT = 2.00       # close at 2x credit received

# ---------------------------------------------------------------------------
# Risk-Free Rate (annualized, updated periodically)
# ---------------------------------------------------------------------------
RISK_FREE_RATE = 0.053  # approx 3-month T-bill yield

# ---------------------------------------------------------------------------
# Data / Caching
# ---------------------------------------------------------------------------
CACHE_DIR = ".cache"
CACHE_TTL_SECONDS = 900  # 15 minutes
POSITIONS_FILE = "positions.json"
PERFORMANCE_FILE = "performance.json"
