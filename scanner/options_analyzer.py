"""
Options Analyzer
================
Pulls live options chains via yfinance, computes IV Rank from 252-day
historical vol, validates Greek thresholds, and tags each contract as
AGGRESSIVE / PASSIVE / REJECT.

Data source: yfinance (free, no API key).
For production: swap _fetch_chain() to use CBOE LiveVol / Tradier / TDAmeritrade
for real-time Greeks and verified OI volume figures.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

import config

log = logging.getLogger(__name__)


@dataclass
class OptionContract:
    ticker:        str
    expiry:        date
    strike:        float
    right:         str          # "call" | "put"
    bid:           float
    ask:           float
    mid:           float
    delta:         float
    gamma:         float
    theta:         float
    vega:          float
    iv:            float        # implied vol (annualised, as fraction)
    iv_rank:       float        # 0-100
    open_interest: int
    volume:        int
    underlying_price: float
    dte:           int
    theta_premium_ratio: float  # |theta| / mid  (daily)
    signal:        str = "PENDING"   # AGGRESSIVE | PASSIVE | REJECT
    reject_reason: str = ""

    @property
    def premium_dollar(self) -> float:
        return self.mid * 100  # 1 contract = 100 shares

    @property
    def daily_theta_dollar(self) -> float:
        return abs(self.theta) * 100


def _iv_rank(ticker_obj: yf.Ticker, current_iv: float) -> float:
    """
    IV Rank = (current IV − 52-wk low IV) / (52-wk high IV − 52-wk low IV) × 100

    We approximate historical IV from the close-to-close historical vol
    computed on daily returns over the past 252 trading days.
    """
    try:
        hist = ticker_obj.history(period="1y")
        if len(hist) < 30:
            return 0.0
        rets = np.log(hist["Close"] / hist["Close"].shift(1)).dropna()
        # rolling 21-day realised vol as an IV proxy
        rolling_vol = rets.rolling(21).std() * np.sqrt(252)
        rolling_vol = rolling_vol.dropna()
        low_iv  = float(rolling_vol.min())
        high_iv = float(rolling_vol.max())
        if high_iv == low_iv:
            return 50.0
        rank = (current_iv - low_iv) / (high_iv - low_iv) * 100
        return max(0.0, min(100.0, rank))
    except Exception as exc:
        log.warning("IV Rank calculation failed for %s: %s", ticker_obj.ticker, exc)
        return 0.0


def _nearest_expiries(ticker_obj: yf.Ticker, dte_min: int, dte_max: int) -> list[str]:
    """Return expiry strings that fall within the DTE window."""
    today = date.today()
    expiries = []
    for exp_str in ticker_obj.options:
        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        dte = (exp_date - today).days
        if dte_min <= dte <= dte_max:
            expiries.append(exp_str)
    return expiries


def analyze_ticker(
    symbol: str,
    mode: str = "both",   # "aggressive" | "passive" | "both"
) -> list[OptionContract]:
    """
    Fetch and filter call options for `symbol` against all configured thresholds.
    Returns a list of OptionContract objects tagged with their signal.
    """
    results: list[OptionContract] = []
    try:
        tk = yf.Ticker(symbol)
        spot_data = tk.history(period="1d")
        if spot_data.empty:
            log.warning("%s: no spot data available", symbol)
            return []
        underlying_price = float(spot_data["Close"].iloc[-1])

        expiries = _nearest_expiries(
            tk,
            config.TARGET_DTE_MIN,
            config.TARGET_DTE_MAX,
        )
        if not expiries:
            log.info("%s: no expiries in DTE window %d–%d",
                     symbol, config.TARGET_DTE_MIN, config.TARGET_DTE_MAX)
            return []

        for exp_str in expiries:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - date.today()).days

            chain = tk.option_chain(exp_str)
            calls = chain.calls.copy()

            # ── Filter to liquid strikes near the money ───────────────────
            calls = calls[calls["bid"] > 0]
            calls = calls[calls["openInterest"] >= config.MIN_OPEN_INTEREST]

            for _, row in calls.iterrows():
                strike = float(row["strike"])
                bid    = float(row.get("bid", 0))
                ask    = float(row.get("ask", 0))
                mid    = round((bid + ask) / 2, 2)
                if mid <= 0:
                    continue

                iv_raw = float(row.get("impliedVolatility", 0))
                if iv_raw <= 0:
                    continue

                # yfinance may include Greeks or not depending on version
                delta = float(row.get("delta", np.nan))
                gamma = float(row.get("gamma", np.nan))
                theta = float(row.get("theta", np.nan))
                vega  = float(row.get("vega",  np.nan))

                # If Greeks are absent, compute via Black-Scholes
                if any(np.isnan(v) for v in [delta, gamma, theta, vega]):
                    bs = _black_scholes_greeks(
                        S=underlying_price,
                        K=strike,
                        T=dte / 365,
                        r=0.053,   # risk-free ≈ current Fed Funds
                        sigma=iv_raw,
                    )
                    delta = bs["delta"]
                    gamma = bs["gamma"]
                    theta = bs["theta"]
                    vega  = bs["vega"]

                iv_rank_val = _iv_rank(tk, iv_raw)

                # theta/premium daily ratio
                theta_ratio = abs(theta) / mid if mid > 0 else 0.0

                # dollar volume proxy for the specific strike
                vol_contracts = int(row.get("volume", 0) or 0)
                dollar_vol    = vol_contracts * mid * 100

                contract = OptionContract(
                    ticker=symbol,
                    expiry=exp_date,
                    strike=strike,
                    right="call",
                    bid=bid,
                    ask=ask,
                    mid=mid,
                    delta=delta,
                    gamma=gamma,
                    theta=theta,
                    vega=vega,
                    iv=iv_raw,
                    iv_rank=iv_rank_val,
                    open_interest=int(row.get("openInterest", 0) or 0),
                    volume=vol_contracts,
                    underlying_price=underlying_price,
                    dte=dte,
                    theta_premium_ratio=theta_ratio,
                )

                _classify(contract, dollar_vol, mode)
                results.append(contract)

    except Exception as exc:
        log.error("analyze_ticker(%s) failed: %s", symbol, exc, exc_info=True)

    return results


def _classify(c: OptionContract, dollar_vol: float, mode: str) -> None:
    """Tag the contract in-place: AGGRESSIVE / PASSIVE / REJECT."""
    reasons: list[str] = []

    # IV Rank gate (same for both modes)
    if c.iv_rank < config.IV_RANK_MIN:
        reasons.append(f"IV Rank {c.iv_rank:.1f}% < {config.IV_RANK_MIN}%")

    # Theta/Premium ratio
    if c.theta_premium_ratio < config.THETA_PREMIUM_RATIO_MIN:
        reasons.append(
            f"Theta/Premium {c.theta_premium_ratio:.4f} < {config.THETA_PREMIUM_RATIO_MIN}"
        )

    # Liquidity floor
    if dollar_vol < config.LIQUIDITY_FLOOR_DAILY:
        reasons.append(
            f"Dollar vol ${dollar_vol:,.0f} < ${config.LIQUIDITY_FLOOR_DAILY:,.0f}"
        )

    # Gamma safety
    gamma_fail = c.gamma > config.GAMMA_RISK_MAX

    # Delta classification
    in_aggressive = (
        config.DELTA_AGGRESSIVE_MIN <= c.delta <= config.DELTA_AGGRESSIVE_MAX
    )
    in_passive = (
        config.DELTA_PASSIVE_MIN <= c.delta <= config.DELTA_PASSIVE_MAX
    )

    delta_ok = (
        (mode in ("aggressive", "both") and in_aggressive) or
        (mode in ("passive",    "both") and in_passive)
    )
    if not delta_ok:
        reasons.append(
            f"Delta {c.delta:.3f} outside requested band(s)"
        )

    if reasons:
        c.signal       = "REJECT"
        c.reject_reason = " | ".join(reasons)
        return

    if gamma_fail:
        # Still valid premium but flag the gamma risk
        c.signal       = "DEFENSIVE_ROLL_REQUIRED"
        c.reject_reason = f"Gamma {c.gamma:.4f} > {config.GAMMA_RISK_MAX}"
        return

    if in_aggressive:
        c.signal = "AGGRESSIVE"
    elif in_passive:
        c.signal = "PASSIVE"
    else:
        c.signal = "REJECT"
        c.reject_reason = "Delta band mismatch"


# ── Black-Scholes Greeks ──────────────────────────────────────────────────────

def _black_scholes_greeks(
    S: float, K: float, T: float, r: float, sigma: float
) -> dict[str, float]:
    """
    Returns delta, gamma, theta (per calendar day), vega for a European call.
    T in years, sigma annualised.
    """
    from scipy.stats import norm

    if T <= 0 or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    sqrt_T = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    N  = norm.cdf
    n  = norm.pdf

    delta = N(d1)
    gamma = n(d1) / (S * sigma * sqrt_T)
    theta = (
        -(S * n(d1) * sigma) / (2 * sqrt_T)
        - r * K * np.exp(-r * T) * N(d2)
    ) / 365   # per calendar day
    vega = S * n(d1) * sqrt_T / 100  # per 1 % move in vol

    return {
        "delta": float(delta),
        "gamma": float(gamma),
        "theta": float(theta),
        "vega":  float(vega),
    }
