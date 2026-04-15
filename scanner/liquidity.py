"""
Liquidity scoring for options chains.
Evaluates bid-ask spreads, open interest, and volume across strikes.
"""

import logging
from typing import Dict, Optional

import numpy as np
import pandas as pd

from config import MAX_SPREAD_PCT, MIN_OPEN_INTEREST, MIN_OPTIONS_VOLUME
from utils.data import get_options_chain, get_current_price

logger = logging.getLogger(__name__)


def score_chain_liquidity(ticker: str) -> Dict:
    """
    Score the options chain liquidity for a ticker.
    Returns dict with liquidity metrics and a 0-100 composite score.
    """
    spot = get_current_price(ticker)
    if spot is None:
        return {"ticker": ticker, "liquidity_score": 0, "error": "no spot price"}

    chain_data = get_options_chain(ticker)
    if not chain_data:
        return {"ticker": ticker, "liquidity_score": 0, "error": "no options chain"}

    puts = chain_data.get("puts", pd.DataFrame())
    calls = chain_data.get("calls", pd.DataFrame())
    expiry = chain_data.get("expiration", "unknown")

    if puts is None or puts.empty:
        return {"ticker": ticker, "liquidity_score": 0, "error": "empty puts chain"}

    metrics = {}
    metrics["expiration"] = expiry
    metrics["ticker"] = ticker

    # --- Spread quality ---
    for df, label in [(puts, "put"), (calls, "call")]:
        if df is None or df.empty:
            continue
        df = df.copy()
        df = df[(df["bid"] > 0) & (df["ask"] > 0)]
        if df.empty:
            continue
        df["mid"] = (df["bid"] + df["ask"]) / 2
        df["spread_pct"] = (df["ask"] - df["bid"]) / df["mid"]
        df["moneyness"] = abs(df["strike"] / spot - 1)

        # Focus on strikes within 15% of spot (the tradeable zone)
        near = df[df["moneyness"] < 0.15]
        if not near.empty:
            metrics[f"{label}_avg_spread_pct"] = round(near["spread_pct"].mean(), 4)
            metrics[f"{label}_median_oi"] = int(near["openInterest"].median()) if "openInterest" in near else 0
            metrics[f"{label}_total_volume"] = int(near["volume"].sum()) if "volume" in near else 0

    # --- Composite score (0-100) ---
    score = 100.0

    # Penalize wide spreads
    for label in ["put", "call"]:
        spread_key = f"{label}_avg_spread_pct"
        if spread_key in metrics:
            sp = metrics[spread_key]
            if sp > MAX_SPREAD_PCT:
                score -= 20
            elif sp > MAX_SPREAD_PCT / 2:
                score -= 10

    # Penalize low OI
    for label in ["put", "call"]:
        oi_key = f"{label}_median_oi"
        if oi_key in metrics:
            oi = metrics[oi_key]
            if oi < MIN_OPEN_INTEREST:
                score -= 20
            elif oi < MIN_OPEN_INTEREST * 2:
                score -= 10

    # Penalize low volume
    for label in ["put", "call"]:
        vol_key = f"{label}_total_volume"
        if vol_key in metrics:
            v = metrics[vol_key]
            if v < MIN_OPTIONS_VOLUME:
                score -= 15
            elif v < MIN_OPTIONS_VOLUME * 3:
                score -= 7

    metrics["liquidity_score"] = max(0, round(score, 1))
    return metrics


def filter_liquid_strikes(df: pd.DataFrame, spot: float,
                           min_oi: int = MIN_OPEN_INTEREST,
                           max_spread_pct: float = MAX_SPREAD_PCT) -> pd.DataFrame:
    """
    Filter an options DataFrame to strikes that meet minimum liquidity requirements.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if "openInterest" in df.columns:
        df = df[df["openInterest"] >= min_oi]
    if "bid" in df.columns and "ask" in df.columns:
        df = df[(df["bid"] > 0) & (df["ask"] > 0)]
        df["mid"] = (df["bid"] + df["ask"]) / 2
        df["spread_pct"] = (df["ask"] - df["bid"]) / df["mid"]
        df = df[df["spread_pct"] <= max_spread_pct]
    return df
