"""
Universe management: filter the liquid universe down to scannable candidates
based on price, volume, and basic data availability.
"""

import logging
from typing import List, Dict

import pandas as pd

from config import LIQUID_UNIVERSE, MIN_OPTIONS_VOLUME
from utils.data import get_current_price, get_price_history, get_all_expirations

logger = logging.getLogger(__name__)


def scan_universe(tickers: List[str] = None) -> List[Dict]:
    """
    Filter universe to candidates that pass basic liquidity and data gates.
    Returns list of dicts: {ticker, price, avg_volume, has_options}
    """
    tickers = tickers or LIQUID_UNIVERSE
    candidates = []

    for ticker in tickers:
        logger.debug(f"Screening {ticker}...")
        try:
            price = get_current_price(ticker)
            if price is None or price < 5:
                logger.debug(f"  {ticker}: price {price} too low or unavailable")
                continue

            df = get_price_history(ticker, period="1mo")
            if df is None or df.empty or len(df) < 5:
                logger.debug(f"  {ticker}: insufficient price history")
                continue

            avg_vol = df["Volume"].tail(20).mean()
            if avg_vol < 500_000:  # minimum 500k shares/day
                logger.debug(f"  {ticker}: avg volume {avg_vol:.0f} too low")
                continue

            expirations = get_all_expirations(ticker)
            if not expirations:
                logger.debug(f"  {ticker}: no options available")
                continue

            candidates.append({
                "ticker": ticker,
                "price": round(price, 2),
                "avg_daily_volume": int(avg_vol),
                "available_expirations": expirations[:6],  # first 6
            })
            logger.debug(f"  {ticker}: passed — price={price:.2f}, vol={avg_vol:.0f}")

        except Exception as e:
            logger.warning(f"  {ticker}: error during screen — {e}")
            continue

    logger.info(f"Universe scan: {len(candidates)}/{len(tickers)} passed")
    return candidates
