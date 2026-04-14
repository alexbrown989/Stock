"""
Covered Call Scanner
====================
For each ticker: pull the options chain, compute what we need, and return
a list of SetupCandidate objects — one per qualifying contract.

Filters applied (in order):
  1. DTE within window (10–21 days)
  2. IV Rank >= 50%  (only sell expensive volatility)
  3. Delta 0.20–0.40 (sweet spot — enough premium, manageable assignment risk)
  4. Premium >= $0.15/share (skip micro-premium junk)
  5. Open Interest >= 100 (basic liquidity)
  6. No earnings within 14 days (hard rule — earnings = undefined risk)
  7. Strike × 100 <= $7,000 capital limit

Max Pain is calculated per expiry and used to flag whether the chosen
strike is in the "safe zone" (5–10% above max pain).

Greeks come from the options chain when available. If yfinance doesn't
include them (common on some tickers), we fall back to Black-Scholes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import numpy as np
import yfinance as yf
from scipy.stats import norm

import config

log = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class SetupCandidate:
    ticker:          str
    expiry:          str         # "YYYY-MM-DD"
    dte:             int
    strike:          float
    underlying:      float

    bid:             float
    ask:             float
    premium:         float       # mid price per share
    premium_contract: float      # premium × 100

    delta:           float
    gamma:           float
    theta:           float       # per day, negative = good
    iv:              float       # implied vol as decimal (0.65 = 65%)
    iv_rank:         float       # 0–100

    open_interest:   int
    volume:          int

    break_even:      float       # underlying − premium
    pop:             float       # probability of profit % (1 − delta) × 100
    annual_yield:    float       # (premium / strike) × (365 / dte) × 100

    # Max Pain context
    max_pain:        Optional[float]
    in_safe_zone:    Optional[bool]  # strike is 5–10% above max pain

    earnings_safe:   bool
    next_earnings:   Optional[date]

    # Human-readable tag: "READY" or "WATCH"
    tag:             str = "READY"

    def __str__(self) -> str:
        ep = f" ⚠ earnings {self.next_earnings}" if not self.earnings_safe else ""
        return (
            f"{self.ticker} CALL ${self.strike} | {self.expiry} ({self.dte}DTE) | "
            f"Δ{self.delta:.2f} | IV Rank {self.iv_rank:.0f}% | "
            f"${self.premium:.2f}/sh (${self.premium_contract:.0f}/contract) | "
            f"PoP {self.pop:.0f}%{ep}"
        )


# ── Greeks (Black-Scholes fallback) ──────────────────────────────────────────

def _bs_call(S, K, T, r=0.053, sigma=0.01) -> dict:
    if T <= 0 or sigma <= 0:
        return {"delta": 0.5, "gamma": 0.0, "theta": 0.0}
    sq = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    delta = float(norm.cdf(d1))
    gamma = float(norm.pdf(d1) / (S * sigma * sq))
    theta = float((
        -(S * norm.pdf(d1) * sigma) / (2 * sq) - r * K * np.exp(-r * T) * norm.cdf(d2)
    ) / 365)
    return {"delta": delta, "gamma": gamma, "theta": theta}


# ── IV Rank ───────────────────────────────────────────────────────────────────

def _iv_rank(hist_1y: "pd.DataFrame", current_iv: float) -> float:
    """Computed from 21-day rolling realised vol as IV proxy. Called once per ticker."""
    import pandas as pd
    if len(hist_1y) < 30:
        return 0.0
    rets = np.log(hist_1y["Close"] / hist_1y["Close"].shift(1)).dropna()
    rv   = rets.rolling(21).std() * np.sqrt(252)
    lo, hi = float(rv.min()), float(rv.max())
    if hi <= lo:
        return 50.0
    return round(max(0.0, min(100.0, (current_iv - lo) / (hi - lo) * 100)), 1)


# ── Earnings check ────────────────────────────────────────────────────────────

def _check_earnings(tk: yf.Ticker) -> tuple[bool, Optional[date]]:
    """Returns (safe, next_earnings_date). Safe = no earnings within blackout."""
    try:
        cal = tk.calendar
        dates: list[date] = []
        if isinstance(cal, dict):
            raw = cal.get("Earnings Date", [])
            if not isinstance(raw, list):
                raw = [raw]
            for d in raw:
                if d is None:
                    continue
                dates.append(d.date() if hasattr(d, "date") else
                             datetime.strptime(str(d)[:10], "%Y-%m-%d").date())
        elif hasattr(cal, "columns"):
            for col in ("Earnings Date", "earningsDate"):
                if col in cal.columns:
                    for d in cal[col].dropna():
                        dates.append(d.date() if hasattr(d, "date") else d)
                    break

        today = date.today()
        for d in dates:
            if 0 <= (d - today).days <= config.EARNINGS_BLACKOUT:
                return False, d
        return True, None
    except Exception:
        return True, None   # assume safe if data unavailable


# ── Max Pain ──────────────────────────────────────────────────────────────────

def _max_pain(tk: yf.Ticker, expiry: str) -> Optional[float]:
    """
    The strike where total options dollar pain is minimised.
    We sell 5–10% above this as a safety buffer.
    """
    try:
        chain = tk.option_chain(expiry)
        calls = chain.calls[["strike", "openInterest"]].rename(columns={"openInterest": "c_oi"})
        puts  = chain.puts[["strike", "openInterest"]].rename(columns={"openInterest": "p_oi"})
        merged = calls.merge(puts, on="strike", how="outer").fillna(0).sort_values("strike")
        strikes = merged["strike"].values
        c_oi    = merged["c_oi"].values.astype(int)
        p_oi    = merged["p_oi"].values.astype(int)
        pain    = [
            np.sum(np.maximum(0, strikes - k) * c_oi) +
            np.sum(np.maximum(0, k - strikes) * p_oi)
            for k in strikes
        ]
        return float(strikes[int(np.argmin(pain))])
    except Exception:
        return None


# ── Main scanner ──────────────────────────────────────────────────────────────

def scan_ticker(symbol: str) -> list[SetupCandidate]:
    """Scan one ticker and return qualifying CC setups."""
    results: list[SetupCandidate] = []
    try:
        tk     = yf.Ticker(symbol)
        hist   = tk.history(period="1y")
        if hist.empty:
            return []

        underlying = float(hist["Close"].iloc[-1])
        earnings_safe, next_earnings = _check_earnings(tk)

        today = date.today()
        for exp_str in tk.options:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte      = (exp_date - today).days
            if not (config.DTE_MIN <= dte <= config.DTE_MAX):
                continue

            mp = _max_pain(tk, exp_str)

            chain = tk.option_chain(exp_str)
            calls = chain.calls.copy()
            calls = calls[
                (calls["bid"] > 0) &
                (calls["openInterest"].fillna(0) >= config.MIN_OPEN_INTEREST)
            ]

            for _, row in calls.iterrows():
                strike = float(row["strike"])
                bid    = float(row.get("bid", 0))
                ask    = float(row.get("ask", 0))
                mid    = round((bid + ask) / 2, 2)
                iv     = float(row.get("impliedVolatility", 0))
                oi     = int(row.get("openInterest", 0) or 0)
                vol    = int(row.get("volume", 0) or 0)

                if mid < config.MIN_PREMIUM or iv <= 0:
                    continue

                # Capital guardrail
                if strike * 100 > config.MAX_CAPITAL:
                    continue

                # IV Rank (uses the pre-downloaded 1y history)
                ivr = _iv_rank(hist, iv)
                if ivr < config.IV_RANK_MIN:
                    continue

                # Greeks — chain first, BS fallback
                delta = float(row.get("delta",  np.nan))
                gamma = float(row.get("gamma",  np.nan))
                theta = float(row.get("theta",  np.nan))
                if any(np.isnan(v) for v in [delta, gamma, theta]):
                    bs    = _bs_call(underlying, strike, dte / 365, sigma=iv)
                    delta = bs["delta"]
                    gamma = bs["gamma"]
                    theta = bs["theta"]

                if not (config.DELTA_MIN <= delta <= config.DELTA_MAX):
                    continue

                # Earnings check
                if not earnings_safe:
                    continue    # hard skip — never trade into earnings

                # Max Pain zone
                in_zone: Optional[bool] = None
                if mp is not None:
                    in_zone = (mp * 1.05) <= strike <= (mp * 1.10)

                pop         = round((1 - delta) * 100, 1)
                break_even  = round(underlying - mid, 2)
                ann_yield   = round((mid / strike) * (365 / dte) * 100, 1)

                # Tag: READY = passes everything; WATCH = passes filters but not in safe zone
                tag = "READY" if (in_zone is None or in_zone) else "WATCH"

                results.append(SetupCandidate(
                    ticker=symbol, expiry=exp_str, dte=dte,
                    strike=strike, underlying=underlying,
                    bid=bid, ask=ask, premium=mid,
                    premium_contract=round(mid * 100, 2),
                    delta=round(delta, 3), gamma=round(gamma, 4),
                    theta=round(theta, 4), iv=round(iv, 3),
                    iv_rank=ivr, open_interest=oi, volume=vol,
                    break_even=break_even, pop=pop, annual_yield=ann_yield,
                    max_pain=mp, in_safe_zone=in_zone,
                    earnings_safe=earnings_safe, next_earnings=next_earnings,
                    tag=tag,
                ))

    except Exception as e:
        log.error("scan_ticker(%s): %s", symbol, e)

    return results


def scan_all(tickers: list[str] | None = None) -> list[SetupCandidate]:
    """Scan every ticker in the watchlist. Returns all setups, READY first."""
    tickers = tickers or config.WATCHLIST
    all_setups: list[SetupCandidate] = []
    for sym in tickers:
        log.info("Scanning %s…", sym)
        setups = scan_ticker(sym)
        log.info("  %s: %d setup(s)", sym, len(setups))
        all_setups.extend(setups)

    # Sort: READY first, then by IV Rank descending
    all_setups.sort(key=lambda s: (s.tag != "READY", -s.iv_rank))
    return all_setups
