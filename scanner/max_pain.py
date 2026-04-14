"""
Max Pain Calculator
===================
Max Pain is the strike price at which the total dollar value of all open
options (calls + puts) expiring that cycle would be minimised — i.e., the
point where option sellers (market makers) lose the least.

Algorithm:
  For each candidate strike K:
      pain(K) = Σ [max(0, K_i - K) × OI_call_i] × 100
              + Σ [max(0, K - K_j) × OI_put_j]  × 100
  max_pain_strike = argmin pain(K)

Target zone for Covered Calls:
  Sell the call 5–10 % ABOVE max_pain_strike (per config).

Data source: yfinance options chain (free).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd
import yfinance as yf

import config

log = logging.getLogger(__name__)


@dataclass
class MaxPainResult:
    ticker:            str
    expiry:            date
    max_pain_strike:   float
    underlying_price:  float
    target_call_low:   float   # max_pain × (1 + 5 %)
    target_call_high:  float   # max_pain × (1 + 10 %)
    pain_table:        pd.DataFrame   # full pain-by-strike table
    dte:               int


def calculate(symbol: str) -> list[MaxPainResult]:
    """
    Return MaxPainResult for every expiry inside the configured DTE window.
    """
    results: list[MaxPainResult] = []
    try:
        tk = yf.Ticker(symbol)
        spot = tk.history(period="1d")
        if spot.empty:
            log.warning("%s: no spot data", symbol)
            return []
        underlying = float(spot["Close"].iloc[-1])
        today = date.today()

        for exp_str in tk.options:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            if not (config.TARGET_DTE_MIN <= dte <= config.TARGET_DTE_MAX):
                continue

            chain  = tk.option_chain(exp_str)
            calls  = chain.calls[["strike", "openInterest"]].rename(
                columns={"openInterest": "call_oi"}
            )
            puts   = chain.puts[["strike", "openInterest"]].rename(
                columns={"openInterest": "put_oi"}
            )

            merged = (
                calls.merge(puts, on="strike", how="outer")
                .fillna(0)
                .sort_values("strike")
                .reset_index(drop=True)
            )
            merged["call_oi"] = merged["call_oi"].astype(int)
            merged["put_oi"]  = merged["put_oi"].astype(int)

            strikes = merged["strike"].values

            # vectorised pain calculation
            call_oi = merged["call_oi"].values
            put_oi  = merged["put_oi"].values

            pain_vals = []
            for k in strikes:
                call_pain = np.sum(np.maximum(0, strikes - k) * call_oi) * 100
                put_pain  = np.sum(np.maximum(0, k - strikes) * put_oi)  * 100
                pain_vals.append(call_pain + put_pain)

            merged["total_pain"] = pain_vals
            max_pain_strike = float(strikes[np.argmin(pain_vals)])

            results.append(MaxPainResult(
                ticker           = symbol,
                expiry           = exp_date,
                max_pain_strike  = max_pain_strike,
                underlying_price = underlying,
                target_call_low  = round(
                    max_pain_strike * (1 + config.MAX_PAIN_ABOVE_PCT_MIN), 2
                ),
                target_call_high = round(
                    max_pain_strike * (1 + config.MAX_PAIN_ABOVE_PCT_MAX), 2
                ),
                pain_table       = merged,
                dte              = dte,
            ))

    except Exception as exc:
        log.error("max_pain.calculate(%s): %s", symbol, exc, exc_info=True)

    return results


def nearest(symbol: str) -> MaxPainResult | None:
    """Convenience: return the nearest-DTE Max Pain result."""
    all_results = calculate(symbol)
    if not all_results:
        return None
    return min(all_results, key=lambda r: r.dte)
