"""
Covered Call Scanner
====================
Scans all watchlist tickers IN PARALLEL (ThreadPoolExecutor) and returns
SetupCandidate objects for every contract that passes the filters.

Per ticker — one download, everything reused:
  yf.history(6mo) → IV Rank + S/R levels + earnings
  yf.option_chain → greeks, premium, liquidity

Filters (all must pass):
  ✓ DTE 10–21 days
  ✓ IV Rank ≥ 50 %
  ✓ Delta 0.25–0.40
  ✓ Premium ≥ $0.15 / share
  ✓ Open interest ≥ 100 contracts
  ✓ Bid/ask spread ≤ 25 % of mid (liquidity quality check)
  ✓ No earnings within 14 days (hard rule)
  ✓ Strike × 100 ≤ $7,000 capital limit

Each result is tagged:
  "READY" — passes all filters AND strike is above or near R1 resistance
  "WATCH" — passes all filters but outside the resistance-aligned zone
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import numpy as np
import yfinance as yf
from scipy.stats import norm

import config
import levels as levels_mod
from levels import StockLevels

log = logging.getLogger(__name__)


# ── SetupCandidate ─────────────────────────────────────────────────────────────

@dataclass
class SetupCandidate:
    # Identity
    ticker:    str
    expiry:    str      # "YYYY-MM-DD"
    dte:       int
    strike:    float
    underlying: float

    # Pricing
    bid:              float
    ask:              float
    premium:          float   # mid per share
    premium_contract: float   # premium × 100 (one contract)
    spread_pct:       float   # (ask - bid) / mid × 100
    max_profit:       float   # = premium_contract (if expires worthless)

    # Greeks
    delta: float
    gamma: float
    theta: float   # per calendar day, always negative — sign kept for clarity
    iv:    float   # implied vol as decimal (0.65 = 65 %)
    iv_rank: float # 0–100

    # Risk/reward
    otm_pct:     float   # (strike − underlying) / underlying × 100
    break_even:  float   # underlying − premium (downside protection)
    pop:         float   # probability of profit % ≈ (1 − delta) × 100
    theta_day:   float   # abs(theta) × 100 — $ earned per day from decay
    annual_yield: float  # (premium / strike) × (365 / dte) × 100

    # Liquidity
    open_interest: int
    volume:        int

    # Context
    max_pain:           Optional[float]
    in_max_pain_zone:   Optional[bool]   # strike is 5–10 % above max pain
    resistance_aligned: bool             # strike within 3 % of R1 or R2
    nearest_resistance: Optional[float]
    lvls:               StockLevels

    earnings_safe:  bool
    next_earnings:  Optional[date]

    tag: str = "READY"   # "READY" | "WATCH"


# ── Black-Scholes fallback ─────────────────────────────────────────────────────

def _bs_greeks(S: float, K: float, T: float, sigma: float, r: float = 0.053) -> dict:
    """
    European call greeks via Black-Scholes.
    Only used when yfinance doesn't provide them (common on some tickers).
    T = DTE / 365, sigma = annualised IV as decimal.
    """
    if T <= 0 or sigma <= 0:
        return {"delta": 0.5, "gamma": 0.0, "theta": 0.0}
    sq = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    delta = float(norm.cdf(d1))
    gamma = float(norm.pdf(d1) / (S * sigma * sq))
    theta = float((
        -(S * norm.pdf(d1) * sigma) / (2 * sq)
        - r * K * np.exp(-r * T) * norm.cdf(d2)
    ) / 365)
    return {"delta": delta, "gamma": gamma, "theta": theta}


# ── IV Rank ────────────────────────────────────────────────────────────────────

def _iv_rank(hist: "pd.DataFrame", current_iv: float) -> float:
    """
    Rank current IV against the trailing 52-week range of realised vol.
    Uses 21-day rolling realised vol as a free proxy for historical IV.
    Computed once per ticker and shared across all contracts.
    """
    if len(hist) < 30:
        return 0.0
    rets = np.log(hist["Close"] / hist["Close"].shift(1)).dropna()
    rv   = rets.rolling(21).std() * np.sqrt(252)
    lo, hi = float(rv.min()), float(rv.max())
    if hi <= lo:
        return 50.0
    return round(max(0.0, min(100.0, (current_iv - lo) / (hi - lo) * 100)), 1)


# ── Earnings check ─────────────────────────────────────────────────────────────

def _check_earnings(tk: yf.Ticker) -> tuple[bool, Optional[date]]:
    """
    Returns (safe, next_earnings_date).
    Handles all yfinance calendar API formats (dict, DataFrame, None).
    If data is unavailable we assume safe — yfinance is inconsistent here.
    """
    try:
        cal = tk.calendar
        candidates: list[date] = []

        if not isinstance(cal, (dict, type(None))) and hasattr(cal, "status_code"):
            # yfinance 1.3+ returns a Response object — treat as unavailable
            return True, None
        if isinstance(cal, dict):
            raw = cal.get("Earnings Date", [])
            if not isinstance(raw, list):
                raw = [raw]
            for d in raw:
                if d is None:
                    continue
                candidates.append(
                    d.date() if hasattr(d, "date")
                    else datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
                )
        elif hasattr(cal, "columns"):
            for col in ("Earnings Date", "earningsDate"):
                if col in cal.columns:
                    for d in cal[col].dropna():
                        candidates.append(d.date() if hasattr(d, "date") else d)
                    break

        today = date.today()
        for d in candidates:
            if 0 <= (d - today).days <= config.EARNINGS_BLACKOUT:
                return False, d
        return True, None
    except Exception:
        return True, None


# ── Max Pain ───────────────────────────────────────────────────────────────────

def _max_pain(tk: yf.Ticker, expiry: str) -> Optional[float]:
    """
    Strike price where total option dollar pain is minimised.
    Selling 5–10 % above this is the target zone.
    """
    try:
        chain  = tk.option_chain(expiry)
        calls  = chain.calls[["strike", "openInterest"]].rename(columns={"openInterest": "c"})
        puts   = chain.puts[["strike",  "openInterest"]].rename(columns={"openInterest": "p"})
        merged = calls.merge(puts, on="strike", how="outer").fillna(0).sort_values("strike")
        s = merged["strike"].values
        c = merged["c"].values.astype(int)
        p = merged["p"].values.astype(int)
        pain = [
            np.sum(np.maximum(0, s - k) * c) + np.sum(np.maximum(0, k - s) * p)
            for k in s
        ]
        return float(s[int(np.argmin(pain))])
    except Exception:
        return None


# ── Per-ticker scan ────────────────────────────────────────────────────────────

def scan_ticker(symbol: str) -> list[SetupCandidate]:
    """
    Full scan for one ticker.  Returns a (possibly empty) list of candidates.
    All expensive downloads happen here — only one history call per ticker.
    """
    results: list[SetupCandidate] = []
    try:
        tk = yf.Ticker(symbol)
        try:
            hist = tk.history(period="6mo")
        except Exception:
            return results
        if hist.empty:
            return results

        current          = float(hist["Close"].iloc[-1])
        earnings_safe, next_earn = _check_earnings(tk)
        stock_levels     = levels_mod.analyze(symbol, hist)

        today = date.today()
        try:
            options_list = tk.options
            if not isinstance(options_list, (list, tuple)):
                return results
        except Exception:
            return results
        for exp_str in options_list:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte      = (exp_date - today).days
            if not (config.DTE_MIN <= dte <= config.DTE_MAX):
                continue

            mp = _max_pain(tk, exp_str)
            try:
                chain = tk.option_chain(exp_str)
                calls = chain.calls.copy()
            except Exception:
                continue

            # Basic pre-filter before the per-row loop
            calls = calls[
                (calls["bid"]          > 0) &
                (calls["openInterest"].fillna(0) >= config.MIN_OPEN_INTEREST)
            ]

            for _, row in calls.iterrows():
                # ── Pricing ────────────────────────────────────────────────
                strike = float(row["strike"])
                bid    = float(row.get("bid", 0))
                ask    = float(row.get("ask", 0))
                mid    = round((bid + ask) / 2, 2)
                iv     = float(row.get("impliedVolatility", 0))
                oi     = int(row.get("openInterest", 0) or 0)
                _vol   = row.get("volume", 0)
                vol    = int(_vol) if _vol is not None and not (isinstance(_vol, float) and np.isnan(_vol)) else 0

                if mid < config.MIN_PREMIUM or iv <= 0:
                    continue
                if strike * 100 > config.MAX_CAPITAL:
                    continue

                # Spread quality check
                spread_pct = round((ask - bid) / mid * 100, 1) if mid > 0 else 999
                if spread_pct > config.SPREAD_MAX_PCT:
                    continue

                # IV Rank (uses the pre-downloaded history — fast)
                ivr = _iv_rank(hist, iv)
                if ivr < config.IV_RANK_MIN:
                    continue

                # ── Greeks ─────────────────────────────────────────────────
                delta = float(row.get("delta", np.nan))
                gamma = float(row.get("gamma", np.nan))
                theta = float(row.get("theta", np.nan))
                if any(np.isnan(v) for v in [delta, gamma, theta]):
                    bs    = _bs_greeks(current, strike, dte / 365, iv)
                    delta, gamma, theta = bs["delta"], bs["gamma"], bs["theta"]

                if not (config.DELTA_MIN <= delta <= config.DELTA_MAX):
                    continue

                # Hard earnings skip
                if not earnings_safe:
                    continue

                # ── Derived metrics ─────────────────────────────────────────
                otm_pct     = round((strike - current) / current * 100, 1)
                break_even  = round(current - mid, 2)
                pop         = round((1 - delta) * 100, 1)
                theta_day   = round(abs(theta) * 100, 2)
                annual_yield = round((mid / strike) * (365 / dte) * 100, 1)
                max_profit  = round(mid * 100, 2)

                # ── Max Pain zone ───────────────────────────────────────────
                in_zone: Optional[bool] = None
                if mp is not None:
                    in_zone = (mp * 1.05) <= strike <= (mp * 1.10)

                # ── Resistance alignment ────────────────────────────────────
                r_aligned   = stock_levels.strike_is_resistance_aligned(
                    strike, config.RESIST_ALIGN_TOL
                )
                nearest_res = stock_levels.nearest_resistance()

                # ── Tag ────────────────────────────────────────────────────
                # READY: passes all filters AND is resistance-aligned or in max pain zone
                # WATCH: passes all filters but not perfectly positioned
                tag = "READY" if (r_aligned or in_zone) else "WATCH"

                results.append(SetupCandidate(
                    ticker=symbol, expiry=exp_str, dte=dte,
                    strike=strike, underlying=current,
                    bid=bid, ask=ask, premium=mid,
                    premium_contract=max_profit,
                    spread_pct=spread_pct,
                    max_profit=max_profit,
                    delta=round(delta, 3),
                    gamma=round(gamma, 4),
                    theta=round(theta, 4),
                    iv=round(iv, 3),
                    iv_rank=ivr,
                    otm_pct=otm_pct,
                    break_even=break_even,
                    pop=pop,
                    theta_day=theta_day,
                    annual_yield=annual_yield,
                    open_interest=oi,
                    volume=vol,
                    max_pain=mp,
                    in_max_pain_zone=in_zone,
                    resistance_aligned=r_aligned,
                    nearest_resistance=nearest_res,
                    lvls=stock_levels,
                    earnings_safe=earnings_safe,
                    next_earnings=next_earn,
                    tag=tag,
                ))

    except Exception as e:
        log.error("scan_ticker(%s): %s", symbol, e)

    return results


# ── Parallel watchlist scan ────────────────────────────────────────────────────

def scan_all(tickers: list[str] | None = None) -> list[SetupCandidate]:
    """
    Scan every ticker in parallel using a thread pool.
    Returns all candidates sorted: READY first, then by IV Rank descending.
    """
    tickers = tickers or config.WATCHLIST
    all_results: list[SetupCandidate] = []

    with ThreadPoolExecutor(max_workers=config.SCAN_WORKERS) as pool:
        futures = {pool.submit(scan_ticker, t): t for t in tickers}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                found = future.result()
                log.info("  %-6s  %d setup(s)", ticker, len(found))
                all_results.extend(found)
            except Exception as e:
                log.error("  %-6s  failed: %s", ticker, e)

    all_results.sort(key=lambda s: (s.tag != "READY", -s.iv_rank))
    return all_results
