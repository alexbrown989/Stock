"""
Cross-asset macro signal scanner.
Evaluates relative moves in energy (XLE/OIL), rates (TLT/IEF), crypto (BTC proxy),
and VIX-like signals to identify macro-driven dislocations in equities.

These signals are used as context filters — not direct entry triggers.
Elevated macro stress = wider spreads, higher IVR, potentially better theta setups.
"""

import logging
import math
from typing import Dict

import numpy as np

from utils.data import get_price_history, get_current_price

logger = logging.getLogger(__name__)


# Cross-asset tickers used as macro proxies
MACRO_PROXIES = {
    "rates_long":   "TLT",   # 20+ year Treasury — rising = risk-off
    "rates_short":  "SHY",   # 1-3 year Treasury
    "energy":       "XLE",   # Energy sector
    "volatility":   "VXX",   # VIX ETP (short-term)
    "gold":         "GLD",   # Safe haven demand
    "dollar":       "UUP",   # Dollar strength
    "crypto_proxy": "BITO",  # BTC futures ETF
    "high_yield":   "HYG",   # Credit risk appetite
}

LOOKBACK = 10  # days for momentum measurement


def _pct_change(ticker: str, days: int = LOOKBACK) -> float:
    """Return N-day percentage price change. Returns 0 on failure."""
    df = get_price_history(ticker, period="1mo")
    if df is None or len(df) < days + 1:
        return 0.0
    prices = df["Close"].tail(days + 1)
    return float((prices.iloc[-1] / prices.iloc[0]) - 1)


def _zscore(ticker: str, window: int = 20) -> float:
    """Z-score of latest price vs rolling mean/std. 0 on failure."""
    df = get_price_history(ticker, period="3mo")
    if df is None or len(df) < window + 1:
        return 0.0
    closes = df["Close"]
    mu = closes.tail(window).mean()
    sd = closes.tail(window).std()
    if sd < 1e-8:
        return 0.0
    return float((closes.iloc[-1] - mu) / sd)


def macro_environment() -> Dict:
    """
    Assess current macro environment.

    Returns a dict with:
      - signals: per-asset reading
      - stress_score: 0-10 composite stress level (higher = more fear/dislocation)
      - regime: 'calm', 'elevated', 'stressed'
      - notes: human-readable observations
    """
    signals = {}
    notes = []
    stress = 0.0

    # --- Rates regime ---
    tlt_chg = _pct_change("TLT")
    signals["TLT_10d_chg"] = round(tlt_chg * 100, 2)
    if tlt_chg > 0.02:
        notes.append("TLT rallying — flight-to-safety signal, rates falling")
        stress += 1.5
    elif tlt_chg < -0.02:
        notes.append("TLT selling off — rates rising, equity pressure possible")
        stress += 1.0

    # --- VIX proxy ---
    vxx_chg = _pct_change("VXX")
    signals["VXX_10d_chg"] = round(vxx_chg * 100, 2)
    if vxx_chg > 0.10:
        notes.append("VXX spiking — elevated fear, IV likely elevated (favorable for selling)")
        stress += 2.5
    elif vxx_chg > 0.05:
        notes.append("VXX rising modestly — mild stress")
        stress += 1.0

    # --- Credit / risk appetite ---
    hyg_chg = _pct_change("HYG")
    signals["HYG_10d_chg"] = round(hyg_chg * 100, 2)
    if hyg_chg < -0.015:
        notes.append("HYG declining — credit spreads widening, risk-off")
        stress += 1.5
    elif hyg_chg > 0.01:
        notes.append("HYG firm — risk appetite intact")

    # --- Energy ---
    xle_chg = _pct_change("XLE")
    signals["XLE_10d_chg"] = round(xle_chg * 100, 2)
    if abs(xle_chg) > 0.05:
        notes.append(f"XLE moved {xle_chg*100:.1f}% — energy dislocation affecting cost inputs")
        stress += 0.5

    # --- Dollar ---
    uup_chg = _pct_change("UUP")
    signals["UUP_10d_chg"] = round(uup_chg * 100, 2)
    if uup_chg > 0.02:
        notes.append("USD strengthening — potential headwind for multinationals")
        stress += 0.5

    # --- Crypto proxy ---
    bito_chg = _pct_change("BITO")
    signals["BITO_10d_chg"] = round(bito_chg * 100, 2)
    if bito_chg < -0.10:
        notes.append("Crypto selling off sharply — risk-off sentiment spreading")
        stress += 1.0
    elif bito_chg > 0.15:
        notes.append("Crypto rallying — risk appetite present")

    stress = min(10.0, stress)

    if stress < 2:
        regime = "calm"
    elif stress < 5:
        regime = "elevated"
    else:
        regime = "stressed"

    if not notes:
        notes.append("No significant macro dislocations detected")

    return {
        "signals": signals,
        "stress_score": round(stress, 2),
        "regime": regime,
        "notes": notes,
    }
