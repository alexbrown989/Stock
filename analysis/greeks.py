"""
Black-Scholes-Merton pricing and Greeks.
All functions are pure — no I/O, no state.

Conventions:
  S  — current underlying price
  K  — strike price
  T  — time to expiration in years
  r  — annualized risk-free rate (e.g. 0.053)
  sigma — annualized implied volatility (e.g. 0.25)
  q  — continuous dividend yield (default 0)
"""

import math
from scipy.stats import norm
from typing import Literal


OptionType = Literal["call", "put"]

_N = norm.cdf
_n = norm.pdf


def _d1d2(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0):
    if T <= 0 or sigma <= 0:
        return None, None
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def bsm_price(S: float, K: float, T: float, r: float, sigma: float,
              option_type: OptionType = "call", q: float = 0.0) -> float:
    """Black-Scholes-Merton option price."""
    if T <= 0:
        intrinsic = max(S - K, 0) if option_type == "call" else max(K - S, 0)
        return intrinsic
    d1, d2 = _d1d2(S, K, T, r, sigma, q)
    if option_type == "call":
        return (S * math.exp(-q * T) * _N(d1)) - (K * math.exp(-r * T) * _N(d2))
    else:
        return (K * math.exp(-r * T) * _N(-d2)) - (S * math.exp(-q * T) * _N(-d1))


def delta(S: float, K: float, T: float, r: float, sigma: float,
          option_type: OptionType = "call", q: float = 0.0) -> float:
    """Delta: dP/dS"""
    if T <= 0:
        if option_type == "call":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1, _ = _d1d2(S, K, T, r, sigma, q)
    if option_type == "call":
        return math.exp(-q * T) * _N(d1)
    return math.exp(-q * T) * (_N(d1) - 1)


def gamma(S: float, K: float, T: float, r: float, sigma: float,
          q: float = 0.0) -> float:
    """Gamma: d²P/dS² (same for calls and puts)"""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1, _ = _d1d2(S, K, T, r, sigma, q)
    return (math.exp(-q * T) * _n(d1)) / (S * sigma * math.sqrt(T))


def theta(S: float, K: float, T: float, r: float, sigma: float,
          option_type: OptionType = "call", q: float = 0.0) -> float:
    """
    Theta: dP/dt — daily theta (divided by 365).
    Negative for long options; positive when reported as decay on short positions.
    """
    if T <= 0 or sigma <= 0:
        return 0.0
    d1, d2 = _d1d2(S, K, T, r, sigma, q)
    common = -(S * math.exp(-q * T) * _n(d1) * sigma) / (2 * math.sqrt(T))
    if option_type == "call":
        th = common - r * K * math.exp(-r * T) * _N(d2) + q * S * math.exp(-q * T) * _N(d1)
    else:
        th = common + r * K * math.exp(-r * T) * _N(-d2) - q * S * math.exp(-q * T) * _N(-d1)
    return th / 365  # per-day theta


def vega(S: float, K: float, T: float, r: float, sigma: float,
         q: float = 0.0) -> float:
    """Vega: dP/d(sigma), expressed per 1% move in vol."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1, _ = _d1d2(S, K, T, r, sigma, q)
    return S * math.exp(-q * T) * _n(d1) * math.sqrt(T) * 0.01


def implied_volatility(market_price: float, S: float, K: float, T: float,
                       r: float, option_type: OptionType = "call",
                       q: float = 0.0, max_iter: int = 100,
                       tol: float = 1e-6) -> float:
    """
    Newton-Raphson IV solver.
    Returns float IV or NaN if it fails to converge.
    """
    if T <= 0 or market_price <= 0:
        return float("nan")

    # Bounds check: market price must exceed intrinsic
    intrinsic = max(S - K, 0) if option_type == "call" else max(K - S, 0)
    if market_price < intrinsic:
        return float("nan")

    sigma = 0.3  # starting guess
    for _ in range(max_iter):
        price = bsm_price(S, K, T, r, sigma, option_type, q)
        v = vega(S, K, T, r, sigma, q) * 100  # convert back from per-1%
        if abs(v) < 1e-10:
            break
        diff = price - market_price
        sigma = sigma - diff / v
        if sigma <= 0:
            sigma = 1e-6
        if abs(diff) < tol:
            break
    return sigma if 1e-4 < sigma < 20.0 else float("nan")


def gamma_stress(S: float, K: float, T: float, r: float, sigma: float,
                 option_type: OptionType, move_pct: float = 0.02,
                 q: float = 0.0) -> float:
    """
    Approximate delta change for a ±move_pct move in S.
    Returns the maximum absolute delta change across +/- shock.
    """
    d_base = delta(S, K, T, r, sigma, option_type, q)
    d_up   = delta(S * (1 + move_pct), K, T, r, sigma, option_type, q)
    d_down = delta(S * (1 - move_pct), K, T, r, sigma, option_type, q)
    return max(abs(d_up - d_base), abs(d_base - d_down))


def probability_otm(S: float, K: float, T: float, r: float, sigma: float,
                    option_type: OptionType = "put", q: float = 0.0) -> float:
    """
    Probability the option expires OTM (i.e., worthless) under log-normal.
    For short option sellers this is the approximate probability of profit.
    """
    if T <= 0 or sigma <= 0:
        if option_type == "put":
            return 1.0 if S > K else 0.0
        return 1.0 if S < K else 0.0
    _, d2 = _d1d2(S, K, T, r, sigma, q)
    if option_type == "put":
        return _N(d2)   # P(S_T > K)
    return _N(-d2)      # P(S_T < K)
