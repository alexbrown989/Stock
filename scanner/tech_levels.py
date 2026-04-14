"""
Technical Levels Filter
========================
Selling calls ABOVE a clear resistance level is far better than just
selling above Max Pain. If the stock has been rejecting at $10 for weeks,
selling the $10 call is picking up pennies in front of a steamroller.
Sell the $11 or $12 call instead.

This module:
  1. Identifies swing highs (resistance) and swing lows (support)
     using a simple pivot-point algorithm on daily OHLC data.
  2. Flags whether a candidate call strike sits above the nearest
     resistance (good) or AT/BELOW it (risky).
  3. Computes the distance from the current price to the nearest
     resistance — useful for selecting the right strike.

Algorithm:
  A swing high is a bar where High[i] > High[i-n] for all n in [1..window].
  We use a 5-bar lookback as default (catches meaningful pivots without
  being too noisy).

This is intentionally simple. Production enhancement: add VWAP anchored
levels, 52-week high/low, round-number magnets, and Fibonacci extensions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

PIVOT_WINDOW = 5      # bars each side to confirm a swing high/low
LOOKBACK_DAYS = 60    # rolling window to identify relevant levels


@dataclass
class TechLevels:
    ticker:           str
    current_price:    float
    resistances:      list[float]    # sorted ascending; first = nearest above price
    supports:         list[float]    # sorted descending; first = nearest below price
    nearest_resistance: float | None
    nearest_support:    float | None
    distance_to_resistance_pct: float | None  # (resistance - price) / price * 100

    def call_strike_assessment(self, strike: float) -> tuple[str, str]:
        """
        Given a candidate call strike, assess quality.
        Returns (grade, reason).
        """
        if self.nearest_resistance is None:
            return "UNKNOWN", "No resistance data available"

        r = self.nearest_resistance
        pct_above_resistance = (strike - r) / r * 100

        if strike > r * 1.03:
            return "IDEAL", f"Strike ${strike} is {pct_above_resistance:.1f}% above resistance ${r:.2f} — well protected"
        elif strike > r:
            return "ACCEPTABLE", f"Strike ${strike} is {pct_above_resistance:.1f}% above resistance ${r:.2f}"
        elif abs(strike - r) / r < 0.01:
            return "RISKY", f"Strike ${strike} is AT resistance ${r:.2f} — high breach risk"
        else:
            return "AVOID", f"Strike ${strike} is BELOW resistance ${r:.2f} — likely breached on any move up"


def _find_pivots(df: pd.DataFrame, window: int = PIVOT_WINDOW) -> tuple[list[float], list[float]]:
    """
    Identify swing highs and swing lows from OHLC data.
    Returns (swing_highs, swing_lows) as sorted price lists.
    """
    highs: list[float] = []
    lows:  list[float] = []
    n = len(df)

    for i in range(window, n - window):
        # Swing high: local max in the window
        if df["High"].iloc[i] == df["High"].iloc[i - window: i + window + 1].max():
            highs.append(float(df["High"].iloc[i]))
        # Swing low: local min in the window
        if df["Low"].iloc[i] == df["Low"].iloc[i - window: i + window + 1].min():
            lows.append(float(df["Low"].iloc[i]))

    return sorted(set(highs)), sorted(set(lows), reverse=True)


def _cluster(levels: list[float], tolerance_pct: float = 0.015) -> list[float]:
    """
    Merge nearby levels that are within tolerance of each other.
    Strong levels = multiple pivots clustered close together.
    Returns representative prices (mean of each cluster).
    """
    if not levels:
        return []
    clustered: list[float] = []
    current_cluster = [levels[0]]
    for lvl in levels[1:]:
        if abs(lvl - current_cluster[-1]) / current_cluster[-1] < tolerance_pct:
            current_cluster.append(lvl)
        else:
            clustered.append(sum(current_cluster) / len(current_cluster))
            current_cluster = [lvl]
    clustered.append(sum(current_cluster) / len(current_cluster))
    return [round(l, 2) for l in clustered]


def analyze(symbol: str) -> TechLevels:
    """
    Compute support and resistance levels for a symbol.
    """
    try:
        tk   = yf.Ticker(symbol)
        hist = tk.history(period="3mo")
        if len(hist) < PIVOT_WINDOW * 3:
            raise ValueError(f"insufficient history ({len(hist)} bars)")

        # Use only the last LOOKBACK_DAYS bars for recency
        hist = hist.tail(LOOKBACK_DAYS)
        current_price = float(hist["Close"].iloc[-1])

        raw_highs, raw_lows = _find_pivots(hist)

        # Cluster nearby levels
        all_res = _cluster(sorted(raw_highs))
        all_sup = _cluster(sorted(raw_lows, reverse=True))

        # Filter: resistance above current price, support below
        resistances = sorted([r for r in all_res if r > current_price * 1.001])
        supports    = sorted([s for s in all_sup if s < current_price * 0.999], reverse=True)

        # Also add 52-week high as a hard resistance if not already captured
        yearly = tk.history(period="1y")
        if not yearly.empty:
            y_high = float(yearly["High"].max())
            if y_high > current_price * 1.01 and (not resistances or y_high > resistances[-1] * 1.02):
                resistances.append(round(y_high, 2))
                resistances.sort()

        nearest_res = resistances[0] if resistances else None
        nearest_sup = supports[0]    if supports    else None
        dist_pct = ((nearest_res - current_price) / current_price * 100) if nearest_res else None

        return TechLevels(
            ticker=symbol,
            current_price=round(current_price, 2),
            resistances=resistances[:5],   # top 5 are enough
            supports=supports[:5],
            nearest_resistance=nearest_res,
            nearest_support=nearest_sup,
            distance_to_resistance_pct=round(dist_pct, 2) if dist_pct else None,
        )

    except Exception as exc:
        log.warning("tech_levels.analyze(%s): %s", symbol, exc)
        return TechLevels(
            ticker=symbol, current_price=0.0,
            resistances=[], supports=[],
            nearest_resistance=None, nearest_support=None,
            distance_to_resistance_pct=None,
        )
