"""
Sentiment and positioning analysis.
Compares put/call ratio, skew, and volume patterns to identify
setups where retail fear creates option premium inflation.
"""

import logging
from typing import Dict, Optional

import numpy as np
import pandas as pd

from utils.data import get_options_chain, get_current_price, get_price_history

logger = logging.getLogger(__name__)


def put_call_ratio(ticker: str) -> Optional[float]:
    """
    Volume-based put/call ratio from the front-month chain.
    PCR > 1.2 = elevated fear (potentially favorable for put selling)
    PCR < 0.7 = complacency (calls may be overpriced)
    """
    chain_data = get_options_chain(ticker)
    if not chain_data:
        return None
    puts = chain_data.get("puts", pd.DataFrame())
    calls = chain_data.get("calls", pd.DataFrame())
    if puts is None or calls is None or puts.empty or calls.empty:
        return None
    put_vol  = puts["volume"].sum() if "volume" in puts.columns else 0
    call_vol = calls["volume"].sum() if "volume" in calls.columns else 0
    if call_vol <= 0:
        return None
    return float(put_vol / call_vol)


def iv_skew(ticker: str) -> Optional[float]:
    """
    25-delta skew proxy: IV(OTM put) - IV(OTM call)
    Positive skew = market paying more for downside protection.
    Extreme positive skew suggests fear-driven put premium.
    """
    from datetime import datetime
    from analysis.greeks import implied_volatility
    from config import RISK_FREE_RATE

    spot = get_current_price(ticker)
    if spot is None:
        return None
    chain_data = get_options_chain(ticker)
    if not chain_data:
        return None

    expiry = chain_data["expiration"]
    today = datetime.today().date()
    exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
    T = max((exp_date - today).days / 365, 1 / 365)

    def get_otm_iv(df: pd.DataFrame, option_type: str, otm_pct: float = 0.05):
        """Get IV for strike approximately otm_pct OTM."""
        if df is None or df.empty:
            return None
        target = spot * (1 - otm_pct) if option_type == "put" else spot * (1 + otm_pct)
        df = df.copy()
        df["dist"] = abs(df["strike"] - target)
        row = df.nsmallest(1, "dist").iloc[0]
        mid = (row.get("bid", 0) + row.get("ask", 0)) / 2
        if mid <= 0:
            mid = row.get("lastPrice", 0)
        if mid <= 0:
            return None
        return implied_volatility(mid, spot, row["strike"], T, RISK_FREE_RATE, option_type)

    put_iv  = get_otm_iv(chain_data.get("puts"), "put")
    call_iv = get_otm_iv(chain_data.get("calls"), "call")

    if put_iv is None or call_iv is None:
        return None
    import math
    if math.isnan(put_iv) or math.isnan(call_iv):
        return None
    return float(put_iv - call_iv)


def price_trend_score(ticker: str) -> float:
    """
    Simple trend score based on price vs moving averages.
    Returns: >0 = uptrend, <0 = downtrend, 0 = neutral
    """
    df = get_price_history(ticker, period="6mo")
    if df is None or len(df) < 50:
        return 0.0
    close = df["Close"]
    ma20  = close.tail(20).mean()
    ma50  = close.tail(50).mean()
    last  = float(close.iloc[-1])
    score = 0.0
    if last > ma20:
        score += 1.0
    if last > ma50:
        score += 1.0
    if ma20 > ma50:
        score += 1.0
    return score - 1.5  # center around 0


def sentiment_profile(ticker: str) -> Dict:
    """
    Return full sentiment profile for a ticker.
    Identifies conditions where retail fear/greed is likely mispriced.
    """
    pcr   = put_call_ratio(ticker)
    skew  = iv_skew(ticker)
    trend = price_trend_score(ticker)

    notes = []
    bias  = "neutral"

    if pcr is not None:
        if pcr > 1.5:
            notes.append(f"PCR={pcr:.2f}: extreme fear — put premium elevated, selling puts may have edge")
            bias = "bullish_edge"
        elif pcr > 1.2:
            notes.append(f"PCR={pcr:.2f}: elevated fear — moderate put selling opportunity")
        elif pcr < 0.7:
            notes.append(f"PCR={pcr:.2f}: complacency — calls may be overpriced")
            bias = "bearish_edge"

    if skew is not None:
        if skew > 0.08:
            notes.append(f"Skew={skew:.3f}: steep — market overpaying for downside protection")
        elif skew < -0.02:
            notes.append(f"Skew={skew:.3f}: inverted — unusual, possible mean reversion setup")

    if trend > 1.0:
        notes.append("Price above both MAs — underlying uptrend, supports cash-secured puts")
    elif trend < -1.0:
        notes.append("Price below both MAs — downtrend, be cautious with naked short puts")

    return {
        "ticker":       ticker,
        "put_call_ratio": round(pcr, 3) if pcr else None,
        "iv_skew":      round(skew, 4) if skew else None,
        "trend_score":  round(trend, 2),
        "bias":         bias,
        "notes":        notes,
    }
