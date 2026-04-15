"""
Data fetching layer with lightweight file-based caching.
All external data access routes through here.
"""

import os
import json
import time
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    logging.warning("yfinance not installed. Run: pip install yfinance")

from config import CACHE_DIR, CACHE_TTL_SECONDS

logger = logging.getLogger(__name__)
os.makedirs(CACHE_DIR, exist_ok=True)


def _cache_path(key: str) -> str:
    h = hashlib.md5(key.encode()).hexdigest()
    return os.path.join(CACHE_DIR, f"{h}.json")


def _cache_read(key: str):
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            entry = json.load(f)
        if time.time() - entry["ts"] < CACHE_TTL_SECONDS:
            return entry["data"]
    except Exception:
        pass
    return None


def _cache_write(key: str, data) -> None:
    path = _cache_path(key)
    try:
        with open(path, "w") as f:
            json.dump({"ts": time.time(), "data": data}, f)
    except Exception as e:
        logger.debug(f"Cache write failed: {e}")


def get_price_history(ticker: str, period: str = "1y") -> Optional[pd.DataFrame]:
    """Return OHLCV DataFrame for ticker. Returns None on failure."""
    if not YFINANCE_AVAILABLE:
        return None
    cache_key = f"history_{ticker}_{period}"
    cached = _cache_read(cache_key)
    if cached:
        df = pd.DataFrame(cached)
        df.index = pd.to_datetime(df.index)
        return df
    try:
        t = yf.Ticker(ticker)
        df = t.history(period=period, auto_adjust=True)
        if df.empty:
            return None
        _cache_write(cache_key, df.reset_index().to_dict(orient="list"))
        return df
    except Exception as e:
        logger.warning(f"Failed to fetch history for {ticker}: {e}")
        return None


def get_current_price(ticker: str) -> Optional[float]:
    """Return latest closing price for ticker."""
    df = get_price_history(ticker, period="5d")
    if df is None or df.empty:
        return None
    return float(df["Close"].iloc[-1])


def get_options_chain(ticker: str, expiry: Optional[str] = None) -> dict:
    """
    Return options chain dict with keys 'calls', 'puts', and 'expiration'.
    expiry: specific expiration string (YYYY-MM-DD) or None to auto-select
    Returns empty dict on failure.
    """
    if not YFINANCE_AVAILABLE:
        return {}
    try:
        t = yf.Ticker(ticker)
        expirations = t.options
        if not expirations:
            return {}
        if expiry and expiry in expirations:
            target_exp = expiry
        else:
            # Select expiry closest to 30 DTE
            today = datetime.today().date()
            best = None
            best_diff = float("inf")
            for exp in expirations:
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                dte = (exp_date - today).days
                if dte < 7:
                    continue
                diff = abs(dte - 30)
                if diff < best_diff:
                    best_diff = diff
                    best = exp
            target_exp = best or expirations[0]

        chain = t.option_chain(target_exp)
        return {
            "expiration": target_exp,
            "calls": chain.calls,
            "puts": chain.puts,
        }
    except Exception as e:
        logger.warning(f"Failed to fetch options chain for {ticker}: {e}")
        return {}


def get_all_expirations(ticker: str) -> list:
    """Return list of available expiration strings."""
    if not YFINANCE_AVAILABLE:
        return []
    try:
        t = yf.Ticker(ticker)
        return list(t.options) or []
    except Exception:
        return []


def get_ticker_info(ticker: str) -> dict:
    """Return basic fundamental info dict."""
    if not YFINANCE_AVAILABLE:
        return {}
    cache_key = f"info_{ticker}"
    cached = _cache_read(cache_key)
    if cached:
        return cached
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        _cache_write(cache_key, info)
        return info
    except Exception as e:
        logger.warning(f"Failed to fetch info for {ticker}: {e}")
        return {}
