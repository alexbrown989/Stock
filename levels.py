"""
Technical Level Analyzer
========================
Finds the two most significant support and resistance levels for a stock
using two complementary methods, then scores each level by how many times
price has revisited it.

Method 1 — Volume Profile
  Divides the 90-day price range into 120 bins and accumulates daily volume
  into each bin proportionally.  Price levels where the most volume traded
  act as natural magnets (market participants are anchored there).

Method 2 — Pivot Clustering
  Identifies swing highs and swing lows (5-bar local extremes), then groups
  pivots that fall within 1.5 % of each other into clusters.  A cluster of
  3+ pivots at the same price is a proven rejection zone.

Confluence: a level that shows up in BOTH methods gets a strength bonus.

Visit counting:
  For each candidate level, we scan every daily candle and count how many
  times the candle's high-low range overlapped with a 2 % band around the
  level.  3+ visits = STRONG.  2 = MODERATE.  1 = WEAK.

Output: StockLevels with S1, S2 (below price) and R1, R2 (above price),
        each carrying a visit count, strength tag, and source label.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
from scipy.ndimage import gaussian_filter1d
from scipy.signal import find_peaks


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Level:
    price:    float
    visits:   int
    strength: str    # "STRONG" | "MODERATE" | "WEAK"
    source:   str    # "volume" | "pivot" | "both"

    def bar(self, max_visits: int = 8) -> str:
        """Return a Unicode bar scaled to max_visits for the Discord chart."""
        filled = min(8, round(self.visits / max(max_visits, 1) * 8))
        return "█" * filled + "░" * (8 - filled)

    def label(self) -> str:
        stars = {"STRONG": "★★★", "MODERATE": "★★☆", "WEAK": "★☆☆"}
        return stars.get(self.strength, "")


@dataclass
class StockLevels:
    ticker:        str
    current_price: float
    S1: Optional[Level]   # nearest support
    S2: Optional[Level]   # second support
    R1: Optional[Level]   # nearest resistance
    R2: Optional[Level]   # second resistance

    def all_levels(self) -> list[tuple[str, Level]]:
        """Return all non-None levels as (label, Level) pairs, sorted price desc."""
        pairs = []
        for name, lvl in [("R2", self.R2), ("R1", self.R1),
                          ("S1", self.S1), ("S2", self.S2)]:
            if lvl is not None:
                pairs.append((name, lvl))
        return sorted(pairs, key=lambda x: x[1].price, reverse=True)

    def nearest_resistance(self) -> Optional[float]:
        return self.R1.price if self.R1 else None

    def strike_is_resistance_aligned(self, strike: float, tol: float = 0.03) -> bool:
        """True if the strike is within tol% of R1 or R2."""
        for lvl in [self.R1, self.R2]:
            if lvl and abs(strike - lvl.price) / lvl.price <= tol:
                return True
        return False


# ── Core algorithm ─────────────────────────────────────────────────────────────

def _count_visits(hist: pd.DataFrame, price: float, tol: float = 0.02) -> int:
    """
    Count how many daily candles 'touched' this price level.
    A touch = the candle's high-low range overlaps the [price*(1-tol), price*(1+tol)] band.
    """
    lo_band = price * (1 - tol)
    hi_band = price * (1 + tol)
    touches = ((hist["High"] >= lo_band) & (hist["Low"] <= hi_band)).sum()
    return max(1, int(touches))


def _strength(visits: int, strong_min: int = 3) -> str:
    if visits >= strong_min:
        return "STRONG"
    if visits >= 2:
        return "MODERATE"
    return "WEAK"


def _volume_profile_levels(hist: pd.DataFrame, n_bins: int = 120) -> list[float]:
    """
    Return price levels corresponding to peaks in the volume profile.
    We smooth the profile with a Gaussian filter before peak detection
    to avoid noise from single-day spikes.
    """
    lo = hist["Low"].min()
    hi = hist["High"].max()
    if hi <= lo:
        return []

    bins = np.linspace(lo, hi, n_bins)
    vol_profile = np.zeros(n_bins)

    for _, row in hist.iterrows():
        c_lo, c_hi, vol = row["Low"], row["High"], row["Volume"]
        mask = (bins >= c_lo) & (bins <= c_hi)
        n = mask.sum()
        if n > 0:
            vol_profile[mask] += vol / n   # distribute volume evenly across range

    # Smooth and find peaks
    smoothed  = gaussian_filter1d(vol_profile, sigma=2)
    threshold = np.percentile(smoothed, 65)   # only high-volume nodes
    peaks, _  = find_peaks(smoothed, distance=4, height=threshold)
    return [float(round(bins[i], 2)) for i in peaks]


def _pivot_levels(hist: pd.DataFrame, window: int = 5) -> list[float]:
    """
    Find swing highs and swing lows using a rolling window,
    then cluster nearby pivots (within 1.5 %) into single levels.
    """
    n = len(hist)
    raw: list[float] = []

    for i in range(window, n - window):
        slice_high = hist["High"].iloc[i - window: i + window + 1]
        slice_low  = hist["Low"].iloc[i - window: i + window + 1]
        if float(hist["High"].iloc[i]) == float(slice_high.max()):
            raw.append(float(hist["High"].iloc[i]))
        if float(hist["Low"].iloc[i]) == float(slice_low.min()):
            raw.append(float(hist["Low"].iloc[i]))

    if not raw:
        return []

    # Cluster: merge levels within 1.5 % of each other
    raw.sort()
    clusters: list[list[float]] = [[raw[0]]]
    for price in raw[1:]:
        if abs(price - clusters[-1][-1]) / clusters[-1][-1] < 0.015:
            clusters[-1].append(price)
        else:
            clusters.append([price])

    # Only keep clusters with 2+ members (touched from multiple pivots)
    return [round(float(np.mean(c)), 2) for c in clusters if len(c) >= 2]


def _merge_and_deduplicate(
    vp_levels: list[float],
    pivot_levels: list[float],
    tol: float = 0.015,
) -> list[tuple[float, str]]:
    """
    Combine volume-profile and pivot levels, tag each with its source.
    Levels within tol% of each other are merged (confluence → "both").
    Returns list of (price, source).
    """
    tagged: list[tuple[float, str]] = (
        [(p, "volume") for p in vp_levels] +
        [(p, "pivot")  for p in pivot_levels]
    )
    tagged.sort(key=lambda x: x[0])

    merged: list[tuple[float, str]] = []
    for price, source in tagged:
        if merged and abs(price - merged[-1][0]) / merged[-1][0] < tol:
            # Merge: upgrade source to "both" if different
            prev_p, prev_s = merged[-1]
            new_source = "both" if prev_s != source else prev_s
            merged[-1] = (round((prev_p + price) / 2, 2), new_source)
        else:
            merged.append((price, source))

    return merged


def analyze(symbol: str, hist: pd.DataFrame) -> StockLevels:
    """
    Run the full S/R analysis using pre-downloaded price history.
    `hist` should be 90+ days of daily OHLC + Volume from yfinance.

    Returns StockLevels with S1, S2, R1, R2.
    """
    import config

    if hist.empty or len(hist) < 20:
        return StockLevels(symbol, 0.0, None, None, None, None)

    current = float(hist["Close"].iloc[-1])

    # Use most recent LEVEL_HISTORY_DAYS trading days
    hist = hist.tail(config.LEVEL_HISTORY_DAYS).copy()

    # 1. Find candidate levels from both methods
    vp_levels    = _volume_profile_levels(hist)
    pivot_levels = _pivot_levels(hist)
    candidates   = _merge_and_deduplicate(vp_levels, pivot_levels)

    # 2. Score each level by visit count
    scored: list[tuple[float, str, int]] = []
    for price, source in candidates:
        visits = _count_visits(hist, price, tol=config.LEVEL_TOUCH_TOL)
        scored.append((price, source, visits))

    # 3. Split into supports (below price) and resistances (above price)
    # Keep a 0.5 % dead zone around current price
    buffer = current * 0.005
    supports    = [(p, s, v) for p, s, v in scored if p < current - buffer]
    resistances = [(p, s, v) for p, s, v in scored if p > current + buffer]

    # 4. Sort: supports → highest first (nearest), resistances → lowest first (nearest)
    supports    = sorted(supports,    key=lambda x: x[0], reverse=True)
    resistances = sorted(resistances, key=lambda x: x[0])

    def make_level(item: tuple | None) -> Optional[Level]:
        if item is None:
            return None
        price, source, visits = item
        return Level(
            price    = round(price, 2),
            visits   = visits,
            strength = _strength(visits, config.STRONG_LEVEL_MIN),
            source   = source,
        )

    return StockLevels(
        ticker        = symbol,
        current_price = round(current, 2),
        S1 = make_level(supports[0]    if len(supports)    > 0 else None),
        S2 = make_level(supports[1]    if len(supports)    > 1 else None),
        R1 = make_level(resistances[0] if len(resistances) > 0 else None),
        R2 = make_level(resistances[1] if len(resistances) > 1 else None),
    )
