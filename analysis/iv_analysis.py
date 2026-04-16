"""
Volatility surface analytics:
  - Historical Volatility (realized)
  - IV Rank (IVR): where current IV sits vs 52-week range
  - IV Percentile (IVP): % of days current IV exceeds historical
  - IV / HV premium ratio
"""

import logging
import math
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from config import HV_WINDOW, RISK_FREE_RATE
from utils.data import get_price_history, get_options_chain
from analysis.greeks import implied_volatility

logger = logging.getLogger(__name__)


def historical_volatility(ticker: str, window: int = HV_WINDOW) -> Optional[float]:
    """
    Annualized close-to-close historical volatility over `window` trading days.
    Returns None if insufficient data.
    """
    df = get_price_history(ticker, period="6mo")
    if df is None or len(df) < window + 1:
        return None
    log_returns = np.log(df["Close"] / df["Close"].shift(1)).dropna()
    hv = log_returns.tail(window).std() * math.sqrt(252)
    return float(hv)


def _extract_composite_iv(ticker: str) -> Optional[float]:
    """
    Pull front-month ATM options and return a composite implied volatility.
    Averages the IV of ATM call and put (±2 strikes around ATM).
    """
    from utils.data import get_current_price
    spot = get_current_price(ticker)
    if spot is None:
        return None
    chain_data = get_options_chain(ticker)
    if not chain_data:
        return None

    expiry = chain_data["expiration"]
    from datetime import datetime
    today = datetime.today().date()
    exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
    T = max((exp_date - today).days / 365, 1 / 365)

    ivs = []
    for df, opt_type in [(chain_data.get("puts", pd.DataFrame()), "put"),
                         (chain_data.get("calls", pd.DataFrame()), "call")]:
        if df is None or df.empty:
            continue
        df = df.copy()
        df["moneyness"] = abs(df["strike"] - spot)
        df = df.nsmallest(3, "moneyness")
        for _, row in df.iterrows():
            mid = (row.get("bid", 0) + row.get("ask", 0)) / 2
            if mid <= 0:
                mid = row.get("lastPrice", 0)
            if mid <= 0:
                continue
            iv = implied_volatility(mid, spot, row["strike"], T, RISK_FREE_RATE, opt_type)
            if not math.isnan(iv):
                ivs.append(iv)

    return float(np.median(ivs)) if ivs else None


def iv_rank(ticker: str) -> Optional[float]:
    """
    IV Rank = (current_iv - 52w_low_iv) / (52w_high_iv - 52w_low_iv) * 100
    Returns 0-100 score. Higher = IV elevated vs recent history.
    Uses HV as proxy for historical IV when full IV time series unavailable.
    """
    df = get_price_history(ticker, period="1y")
    if df is None or len(df) < 30:
        return None

    # Compute rolling 20-day HV as IV proxy across the year
    log_ret = np.log(df["Close"] / df["Close"].shift(1))
    rolling_hv = log_ret.rolling(20).std() * math.sqrt(252)
    rolling_hv = rolling_hv.dropna()
    if len(rolling_hv) < 10:
        return None

    current_iv = _extract_composite_iv(ticker)
    if current_iv is None:
        current_iv = rolling_hv.iloc[-1]  # fallback to current HV

    lo = rolling_hv.min()
    hi = rolling_hv.max()
    if hi - lo < 1e-6:
        return 50.0
    rank = (current_iv - lo) / (hi - lo) * 100
    return float(np.clip(rank, 0, 100))


def iv_percentile(ticker: str) -> Optional[float]:
    """
    IV Percentile = % of days in past year where rolling HV < current IV.
    """
    df = get_price_history(ticker, period="1y")
    if df is None or len(df) < 30:
        return None

    log_ret = np.log(df["Close"] / df["Close"].shift(1))
    rolling_hv = log_ret.rolling(20).std() * math.sqrt(252)
    rolling_hv = rolling_hv.dropna()

    current_iv = _extract_composite_iv(ticker)
    if current_iv is None:
        current_iv = rolling_hv.iloc[-1]

    pct = (rolling_hv < current_iv).mean() * 100
    return float(pct)


def iv_hv_ratio(ticker: str) -> Optional[float]:
    """
    Ratio of current composite IV to 20-day historical volatility.
    Values > 1.0 indicate options are pricing in more vol than realized.
    """
    hv = historical_volatility(ticker)
    if hv is None or hv < 1e-4:
        return None
    iv = _extract_composite_iv(ticker)
    if iv is None:
        return None
    return float(iv / hv)


def full_vol_profile(ticker: str) -> dict:
    """Return complete volatility profile for a ticker."""
    hv   = historical_volatility(ticker)
    iv   = _extract_composite_iv(ticker)
    ivr  = iv_rank(ticker)
    ivp  = iv_percentile(ticker)
    ratio = (iv / hv) if (iv and hv and hv > 0) else None
    return {
        "ticker": ticker,
        "hv_20": round(hv, 4) if hv else None,
        "iv_composite": round(iv, 4) if iv else None,
        "iv_rank": round(ivr, 1) if ivr is not None else None,
        "iv_percentile": round(ivp, 1) if ivp is not None else None,
        "iv_hv_ratio": round(ratio, 3) if ratio else None,
    }
