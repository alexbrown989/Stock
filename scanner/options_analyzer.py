"""
Options Analyzer — v2
=====================
Key fix over v1: IV Rank is computed ONCE per ticker (not per contract),
dramatically reducing network calls from O(contracts) to O(1) per symbol.

Performance: 20 tickers × 1 history call each = ~20 HTTP calls vs. ~600.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
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
    iv_rank:       float        # 0-100 — computed once per ticker
    open_interest: int
    volume:        int
    underlying_price: float
    dte:           int
    theta_premium_ratio: float  # |theta| / mid  (daily)
    signal:        str = "PENDING"   # AGGRESSIVE | PASSIVE | REJECT
    reject_reason: str = ""

    @property
    def premium_dollar(self) -> float:
        return self.mid * 100

    @property
    def daily_theta_dollar(self) -> float:
        return abs(self.theta) * 100

    @property
    def annualised_premium_yield(self) -> float:
        """Premium / strike, annualised. Useful for comparing across prices."""
        if self.strike <= 0:
            return 0.0
        return (self.mid / self.strike) * (365 / self.dte) if self.dte > 0 else 0.0


def _compute_iv_rank(hist: pd.DataFrame, current_iv: float) -> float:
    """
    IV Rank = (current_iv - 52wk_low) / (52wk_high - 52wk_low) * 100

    Uses 21-day rolling realised vol as IV proxy (no options history needed).
    Called ONCE per ticker; result is shared across all contracts.
    """
    if len(hist) < 30:
        return 0.0
    rets = np.log(hist["Close"] / hist["Close"].shift(1)).dropna()
    rolling_vol = rets.rolling(21).std() * np.sqrt(252)
    rolling_vol = rolling_vol.dropna()
    low_iv  = float(rolling_vol.min())
    high_iv = float(rolling_vol.max())
    if high_iv <= low_iv:
        return 50.0
    rank = (current_iv - low_iv) / (high_iv - low_iv) * 100
    return round(max(0.0, min(100.0, rank)), 1)


def _nearest_expiries(tk: yf.Ticker, dte_min: int, dte_max: int) -> list[str]:
    today = date.today()
    return [
        e for e in tk.options
        if dte_min <= (datetime.strptime(e, "%Y-%m-%d").date() - today).days <= dte_max
    ]


def _bs_greeks(S: float, K: float, T: float, r: float, sigma: float) -> dict[str, float]:
    """Black-Scholes greeks for a European call. T in years, sigma annualised."""
    from scipy.stats import norm
    if T <= 0 or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sqrt_T = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    delta = norm.cdf(d1)
    gamma = norm.pdf(d1) / (S * sigma * sqrt_T)
    theta = (
        -(S * norm.pdf(d1) * sigma) / (2 * sqrt_T)
        - r * K * np.exp(-r * T) * norm.cdf(d2)
    ) / 365
    vega = S * norm.pdf(d1) * sqrt_T / 100
    return {"delta": float(delta), "gamma": float(gamma),
            "theta": float(theta), "vega": float(vega)}


def analyze_ticker(
    symbol: str,
    mode: str = "both",
) -> list[OptionContract]:
    """
    Fetch options chain, compute IV Rank ONCE, classify every contract.
    `mode`: "aggressive" | "passive" | "both"
    """
    results: list[OptionContract] = []
    try:
        tk        = yf.Ticker(symbol)
        hist_1y   = tk.history(period="1y")   # single download, reused below
        if hist_1y.empty:
            log.warning("%s: no price history", symbol)
            return []

        underlying_price = float(hist_1y["Close"].iloc[-1])

        expiries = _nearest_expiries(tk, config.TARGET_DTE_MIN, config.TARGET_DTE_MAX)
        if not expiries:
            log.info("%s: no expiries in %d–%d DTE window",
                     symbol, config.TARGET_DTE_MIN, config.TARGET_DTE_MAX)
            return []

        for exp_str in expiries:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte      = (exp_date - date.today()).days

            chain = tk.option_chain(exp_str)
            calls = chain.calls.copy()
            calls = calls[(calls["bid"] > 0) &
                          (calls["openInterest"].fillna(0) >= config.MIN_OPEN_INTEREST)]

            for _, row in calls.iterrows():
                strike = float(row["strike"])
                bid    = float(row.get("bid", 0))
                ask    = float(row.get("ask", 0))
                mid    = round((bid + ask) / 2, 2)
                iv_raw = float(row.get("impliedVolatility", 0))
                if mid <= 0 or iv_raw <= 0:
                    continue

                # IV Rank computed from the single 1y download
                iv_rank_val = _compute_iv_rank(hist_1y, iv_raw)

                # Greeks: use chain values if present, otherwise BS fallback
                delta = float(row.get("delta",  np.nan))
                gamma = float(row.get("gamma",  np.nan))
                theta = float(row.get("theta",  np.nan))
                vega  = float(row.get("vega",   np.nan))
                if any(np.isnan(v) for v in [delta, gamma, theta, vega]):
                    bs    = _bs_greeks(underlying_price, strike, dte / 365, 0.053, iv_raw)
                    delta = bs["delta"]
                    gamma = bs["gamma"]
                    theta = bs["theta"]
                    vega  = bs["vega"]

                theta_ratio   = abs(theta) / mid if mid > 0 else 0.0
                vol_contracts = int(row.get("volume", 0) or 0)
                dollar_vol    = vol_contracts * mid * 100

                c = OptionContract(
                    ticker=symbol, expiry=exp_date, strike=strike, right="call",
                    bid=bid, ask=ask, mid=mid,
                    delta=delta, gamma=gamma, theta=theta, vega=vega,
                    iv=iv_raw, iv_rank=iv_rank_val,
                    open_interest=int(row.get("openInterest", 0) or 0),
                    volume=vol_contracts,
                    underlying_price=underlying_price,
                    dte=dte,
                    theta_premium_ratio=theta_ratio,
                )
                _classify(c, dollar_vol, mode)
                results.append(c)

    except Exception as exc:
        log.error("analyze_ticker(%s): %s", symbol, exc, exc_info=True)

    return results


def _classify(c: OptionContract, dollar_vol: float, mode: str) -> None:
    reasons: list[str] = []

    if c.iv_rank < config.IV_RANK_MIN:
        reasons.append(f"IV Rank {c.iv_rank:.1f}% < {config.IV_RANK_MIN}%")

    if c.theta_premium_ratio < config.THETA_PREMIUM_RATIO_MIN:
        reasons.append(
            f"Theta/Premium {c.theta_premium_ratio*100:.2f}%/day "
            f"< {config.THETA_PREMIUM_RATIO_MIN*100:.1f}%/day"
        )

    if dollar_vol < config.LIQUIDITY_FLOOR_DAILY:
        reasons.append(f"Dollar vol ${dollar_vol:,.0f} < ${config.LIQUIDITY_FLOOR_DAILY:,.0f}")

    in_aggressive = config.DELTA_AGGRESSIVE_MIN <= c.delta <= config.DELTA_AGGRESSIVE_MAX
    in_passive    = config.DELTA_PASSIVE_MIN    <= c.delta <= config.DELTA_PASSIVE_MAX
    delta_ok = (
        (mode in ("aggressive", "both") and in_aggressive) or
        (mode in ("passive",    "both") and in_passive)
    )
    if not delta_ok:
        reasons.append(f"Delta {c.delta:.3f} outside band(s) for mode={mode}")

    if reasons:
        c.signal       = "REJECT"
        c.reject_reason = " | ".join(reasons)
        return

    if c.gamma > config.GAMMA_RISK_MAX:
        c.signal       = "DEFENSIVE_ROLL_REQUIRED"
        c.reject_reason = f"Gamma {c.gamma:.4f} > {config.GAMMA_RISK_MAX}"
        return

    c.signal = "AGGRESSIVE" if in_aggressive else "PASSIVE"
