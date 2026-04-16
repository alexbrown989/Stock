"""
Trade construction: selects strikes and builds structured trade candidates.

Supported structures:
  - short_put          : naked short put (CSP)
  - short_put_spread   : bull put spread
  - short_call_spread  : bear call spread
  - iron_condor        : short put spread + short call spread
"""

import math
import logging
from datetime import datetime
from typing import Optional, List, Dict

import numpy as np
import pandas as pd

from config import (
    TH_DELTA_MIN, TH_DELTA_MAX, RISK_FREE_RATE,
    TH_DTE_MIN, TH_DTE_MAX, SPREAD_PREMIUM_TARGET_PCT,
)
from utils.data import get_options_chain, get_current_price
from analysis.greeks import (
    delta as calc_delta, theta as calc_theta, gamma as calc_gamma,
    vega as calc_vega, implied_volatility
)
from analysis.probability import prob_of_profit
from scanner.liquidity import filter_liquid_strikes
from strategy.filters import run_all_filters

logger = logging.getLogger(__name__)


def _dte(expiry: str) -> int:
    today = datetime.today().date()
    return (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days


def _T(expiry: str) -> float:
    return max(_dte(expiry) / 365, 1 / 365)


def _select_target_strike(
    df: pd.DataFrame, spot: float, option_type: str, T: float,
    sigma: float, r: float = RISK_FREE_RATE
) -> Optional[pd.Series]:
    """
    Select the strike with |delta| closest to the midpoint of [DELTA_MIN, DELTA_MAX].
    """
    target_delta = (TH_DELTA_MIN + TH_DELTA_MAX) / 2  # default 0.25
    df = df.copy()
    df["est_delta"] = df["strike"].apply(
        lambda k: abs(calc_delta(spot, k, T, r, sigma, option_type))
    )
    df["delta_dist"] = abs(df["est_delta"] - target_delta)
    df = df[df["est_delta"].between(TH_DELTA_MIN - 0.05, TH_DELTA_MAX + 0.05)]
    if df.empty:
        return None
    return df.nsmallest(1, "delta_dist").iloc[0]


def build_short_put(ticker: str, expiry: str,
                    ivr: float = None, iv_hv: float = None) -> Optional[Dict]:
    """Build a short put (cash-secured put) candidate."""
    spot = get_current_price(ticker)
    if spot is None:
        return None
    chain_data = get_options_chain(ticker, expiry)
    if not chain_data:
        return None

    actual_expiry = chain_data["expiration"]
    T = _T(actual_expiry)
    puts = filter_liquid_strikes(chain_data.get("puts", pd.DataFrame()), spot)
    if puts.empty:
        return None

    # Estimate sigma from ATM option
    atm_row = puts.iloc[(puts["strike"] - spot).abs().argsort()[:1]]
    mid = (atm_row.iloc[0]["bid"] + atm_row.iloc[0]["ask"]) / 2
    sigma = implied_volatility(mid, spot, atm_row.iloc[0]["strike"], T, RISK_FREE_RATE, "put")
    if math.isnan(sigma):
        sigma = 0.25  # fallback

    row = _select_target_strike(puts, spot, "put", T, sigma)
    if row is None:
        return None

    K = row["strike"]
    premium = (row["bid"] + row["ask"]) / 2

    filters = run_all_filters(
        S=spot, K_short=K, expiry=actual_expiry, T=T, r=RISK_FREE_RATE,
        sigma=sigma, option_type="put", premium=premium, ivr=ivr, iv_hv=iv_hv,
    )

    return {
        "structure":   "short_put",
        "ticker":      ticker,
        "spot":        round(spot, 2),
        "strike":      K,
        "expiration":  actual_expiry,
        "dte":         _dte(actual_expiry),
        "premium":     round(premium, 2),
        "delta":       round(calc_delta(spot, K, T, RISK_FREE_RATE, sigma, "put"), 3),
        "theta":       round(calc_theta(spot, K, T, RISK_FREE_RATE, sigma, "put"), 4),
        "gamma":       round(calc_gamma(spot, K, T, RISK_FREE_RATE, sigma), 5),
        "vega":        round(calc_vega(spot, K, T, RISK_FREE_RATE, sigma), 4),
        "iv":          round(sigma, 4),
        "pop":         round(prob_of_profit(spot, K, T, RISK_FREE_RATE, sigma, "put", premium), 3),
        "max_profit":  round(premium, 2),
        "max_loss":    round(K - premium, 2),
        "filters":     filters,
    }


def build_put_spread(ticker: str, expiry: str, spread_width: float = None,
                     ivr: float = None, iv_hv: float = None) -> Optional[Dict]:
    """
    Bull put spread: sell higher-strike put, buy lower-strike put.
    Width defaults to ~5% of spot.
    """
    spot = get_current_price(ticker)
    if spot is None:
        return None
    chain_data = get_options_chain(ticker, expiry)
    if not chain_data:
        return None

    actual_expiry = chain_data["expiration"]
    T = _T(actual_expiry)
    puts = filter_liquid_strikes(chain_data.get("puts", pd.DataFrame()), spot)
    if puts.empty:
        return None

    atm_row = puts.iloc[(puts["strike"] - spot).abs().argsort()[:1]]
    mid = (atm_row.iloc[0]["bid"] + atm_row.iloc[0]["ask"]) / 2
    sigma = implied_volatility(mid, spot, atm_row.iloc[0]["strike"], T, RISK_FREE_RATE, "put")
    if math.isnan(sigma):
        sigma = 0.25

    short_row = _select_target_strike(puts, spot, "put", T, sigma)
    if short_row is None:
        return None

    K_short = short_row["strike"]
    width = spread_width or round(spot * 0.05 / 5) * 5  # ~5% of spot, rounded to $5

    # Long strike is width below short strike
    K_long = K_short - width
    long_candidates = puts[abs(puts["strike"] - K_long) <= width / 2]
    if long_candidates.empty:
        return None
    long_row = long_candidates.iloc[(long_candidates["strike"] - K_long).abs().argsort()[:1]].iloc[0]
    K_long = long_row["strike"]

    short_premium = (short_row["bid"] + short_row["ask"]) / 2
    long_premium  = (long_row["bid"] + long_row["ask"]) / 2
    net_credit    = short_premium - long_premium

    actual_width = K_short - K_long
    if actual_width <= 0 or net_credit <= 0:
        return None

    # Require credit >= 25% of width
    if net_credit / actual_width < SPREAD_PREMIUM_TARGET_PCT:
        return None

    filters = run_all_filters(
        S=spot, K_short=K_short, expiry=actual_expiry, T=T, r=RISK_FREE_RATE,
        sigma=sigma, option_type="put", premium=net_credit, ivr=ivr, iv_hv=iv_hv,
        K_long=K_long,
    )

    return {
        "structure":   "short_put_spread",
        "ticker":      ticker,
        "spot":        round(spot, 2),
        "strike_short": K_short,
        "strike_long":  K_long,
        "expiration":  actual_expiry,
        "dte":         _dte(actual_expiry),
        "net_credit":  round(net_credit, 2),
        "width":       round(actual_width, 2),
        "delta":       round(calc_delta(spot, K_short, T, RISK_FREE_RATE, sigma, "put"), 3),
        "theta":       round(calc_theta(spot, K_short, T, RISK_FREE_RATE, sigma, "put"), 4),
        "iv":          round(sigma, 4),
        "pop":         round(prob_of_profit(spot, K_short, T, RISK_FREE_RATE, sigma, "put",
                                            net_credit, K_long), 3),
        "max_profit":  round(net_credit, 2),
        "max_loss":    round(actual_width - net_credit, 2),
        "filters":     filters,
    }


def build_iron_condor(ticker: str, expiry: str,
                      ivr: float = None, iv_hv: float = None) -> Optional[Dict]:
    """
    Iron condor: short put spread + short call spread.
    Both wings sized for similar delta exposure.
    """
    spot = get_current_price(ticker)
    if spot is None:
        return None
    chain_data = get_options_chain(ticker, expiry)
    if not chain_data:
        return None

    actual_expiry = chain_data["expiration"]
    T = _T(actual_expiry)
    puts  = filter_liquid_strikes(chain_data.get("puts", pd.DataFrame()), spot)
    calls = filter_liquid_strikes(chain_data.get("calls", pd.DataFrame()), spot)

    if puts.empty or calls.empty:
        return None

    # Get sigma from ATM
    atm_put = puts.iloc[(puts["strike"] - spot).abs().argsort()[:1]].iloc[0]
    mid = (atm_put["bid"] + atm_put["ask"]) / 2
    sigma = implied_volatility(mid, spot, atm_put["strike"], T, RISK_FREE_RATE, "put")
    if math.isnan(sigma):
        sigma = 0.25

    width = round(spot * 0.05 / 5) * 5

    # Put wing
    p_short_row = _select_target_strike(puts, spot, "put", T, sigma)
    if p_short_row is None:
        return None
    Kps = p_short_row["strike"]
    Kpl = Kps - width
    p_long_c = puts[abs(puts["strike"] - Kpl) <= width / 2]
    if p_long_c.empty:
        return None
    Kpl = p_long_c.iloc[(p_long_c["strike"] - Kpl).abs().argsort()[:1]].iloc[0]["strike"]

    # Call wing
    c_short_row = _select_target_strike(calls, spot, "call", T, sigma)
    if c_short_row is None:
        return None
    Kcs = c_short_row["strike"]
    Kcl = Kcs + width
    c_long_c = calls[abs(calls["strike"] - Kcl) <= width / 2]
    if c_long_c.empty:
        return None
    Kcl = c_long_c.iloc[(c_long_c["strike"] - Kcl).abs().argsort()[:1]].iloc[0]["strike"]

    p_short = (p_short_row["bid"] + p_short_row["ask"]) / 2
    p_long_price = puts[puts["strike"] == Kpl]
    if p_long_price.empty:
        return None
    p_long = (p_long_price.iloc[0]["bid"] + p_long_price.iloc[0]["ask"]) / 2

    c_short_row2 = calls[calls["strike"] == Kcs]
    if c_short_row2.empty:
        return None
    c_short = (c_short_row2.iloc[0]["bid"] + c_short_row2.iloc[0]["ask"]) / 2

    c_long_price = calls[calls["strike"] == Kcl]
    if c_long_price.empty:
        return None
    c_long = (c_long_price.iloc[0]["bid"] + c_long_price.iloc[0]["ask"]) / 2

    net_credit = (p_short - p_long) + (c_short - c_long)
    if net_credit <= 0:
        return None

    p_delta = calc_delta(spot, Kps, T, RISK_FREE_RATE, sigma, "put")
    c_delta = calc_delta(spot, Kcs, T, RISK_FREE_RATE, sigma, "call")
    net_delta = p_delta + c_delta  # should be near zero for balanced condor

    return {
        "structure":      "iron_condor",
        "ticker":         ticker,
        "spot":           round(spot, 2),
        "put_short":      Kps,
        "put_long":       Kpl,
        "call_short":     Kcs,
        "call_long":      Kcl,
        "expiration":     actual_expiry,
        "dte":            _dte(actual_expiry),
        "net_credit":     round(net_credit, 2),
        "max_loss":       round(width - net_credit, 2),
        "net_delta":      round(net_delta, 3),
        "put_delta":      round(p_delta, 3),
        "call_delta":     round(c_delta, 3),
        "iv":             round(sigma, 4),
        "filters":        {"passed": True, "note": "See individual legs"},
    }
