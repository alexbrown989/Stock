"""
Gamma Squeeze Lead — Correlative Skew Monitor
==============================================
Logic:
  1. Compute the 20-day rolling *correlation* between the returns of each
     gamma-lead ticker (NVDA, TSLA, SPY, QQQ) and each mid-cap watchlist
     ticker.
  2. Compute the *IV skew* for the lead ticker: the ratio of 25-delta put IV
     to 25-delta call IV pulled from the ATM options chain.
  3. If correlation is high AND the lead ticker is experiencing an IV skew
     compression (calls becoming relatively more expensive than puts), flag
     the mid-cap as a potential "Gamma Squeeze Lead" — i.e., liquidity is
     likely to rotate into it.

Output: CorrelativeSkewSignal per (lead, mid-cap) pair that exceeds
        config.CORR_SKEW_THRESHOLD.

Note on dark-pool enhancement: In production, pairing this with Unusual Whales
or Tradier dark-pool prints significantly improves conviction. The dark_pool.py
module provides that layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

import config

log = logging.getLogger(__name__)


@dataclass
class CorrelativeSkewSignal:
    lead_ticker:   str
    target_ticker: str
    correlation:   float     # 20-day rolling, last value
    iv_skew_lead:  float     # put25_iv / call25_iv for the lead ticker
    signal_score:  float     # 0–1 composite
    alert:         bool
    description:   str


def _returns(symbol: str, period: str = "3mo") -> pd.Series:
    """Log returns for a symbol."""
    try:
        hist = yf.Ticker(symbol).history(period=period)
        return np.log(hist["Close"] / hist["Close"].shift(1)).dropna()
    except Exception as exc:
        log.warning("_returns(%s): %s", symbol, exc)
        return pd.Series(dtype=float)


def _iv_skew(symbol: str) -> float:
    """
    Approximate put/call skew from the nearest ATM options chain.
    Returns put25_iv / call25_iv.  Values > 1 indicate fear/put demand.
    Values approaching 1 (skew compression) can precede upside gamma squeeze.

    We approximate 25-delta options as strikes ~10 % OTM each side.
    """
    try:
        tk   = yf.Ticker(symbol)
        spot_hist = tk.history(period="1d")
        if spot_hist.empty:
            return 1.0
        spot = float(spot_hist["Close"].iloc[-1])

        exps = tk.options
        if not exps:
            return 1.0

        exp_str = exps[0]   # nearest expiry as a proxy
        chain   = tk.option_chain(exp_str)

        calls = chain.calls.copy()
        puts  = chain.puts.copy()

        # 10 % OTM strikes
        call_target = spot * 1.10
        put_target  = spot * 0.90

        call_row = calls.iloc[(calls["strike"] - call_target).abs().argsort()[:1]]
        put_row  = puts.iloc[(puts["strike"]   - put_target).abs().argsort()[:1]]

        call_iv = float(call_row["impliedVolatility"].values[0])
        put_iv  = float(put_row["impliedVolatility"].values[0])

        if call_iv <= 0:
            return 1.0

        return round(put_iv / call_iv, 4)

    except Exception as exc:
        log.warning("_iv_skew(%s): %s", symbol, exc)
        return 1.0


def scan(
    leads: list[str] | None   = None,
    targets: list[str] | None = None,
) -> list[CorrelativeSkewSignal]:
    """
    Cross-correlate each lead ticker against each target (mid-cap watchlist).
    Returns all signals; alerts are flagged when signal_score >= threshold.
    """
    leads   = leads   or config.GAMMA_LEAD_TICKERS
    targets = targets or config.WATCHLIST

    # Cache returns to avoid repeated downloads
    all_symbols = list(set(leads + targets))
    rets: dict[str, pd.Series] = {}
    for sym in all_symbols:
        r = _returns(sym)
        if not r.empty:
            rets[sym] = r

    # Compute IV skew for lead tickers
    lead_skews: dict[str, float] = {}
    for lead in leads:
        lead_skews[lead] = _iv_skew(lead)
        log.debug("Lead %s IV skew: %.4f", lead, lead_skews[lead])

    signals: list[CorrelativeSkewSignal] = []

    for lead in leads:
        if lead not in rets:
            continue
        lead_r  = rets[lead]
        iv_skew = lead_skews[lead]

        for target in targets:
            if target not in rets or target == lead:
                continue

            target_r = rets[target]
            # Align on shared dates
            aligned  = pd.concat([lead_r, target_r], axis=1).dropna()
            if len(aligned) < config.CORR_WINDOW_DAYS:
                continue

            window  = aligned.tail(config.CORR_WINDOW_DAYS)
            corr    = float(window.iloc[:, 0].corr(window.iloc[:, 1]))

            # Composite score: high correlation + low skew (squeeze risk)
            # skew_score approaches 1 as skew compresses toward 1.0
            skew_score   = max(0.0, 1.0 - abs(iv_skew - 1.0))
            corr_abs     = max(0.0, corr)   # only positive correlation matters
            signal_score = (corr_abs * 0.6) + (skew_score * 0.4)

            alert = signal_score >= config.CORR_SKEW_THRESHOLD

            description = (
                f"{lead}→{target}: corr={corr:.3f}, "
                f"lead_IV_skew={iv_skew:.3f}, "
                f"score={signal_score:.3f}"
            )
            if alert:
                description += " ⚡ GAMMA SQUEEZE LEAD DETECTED"

            signals.append(CorrelativeSkewSignal(
                lead_ticker   = lead,
                target_ticker = target,
                correlation   = corr,
                iv_skew_lead  = iv_skew,
                signal_score  = signal_score,
                alert         = alert,
                description   = description,
            ))

    signals.sort(key=lambda s: s.signal_score, reverse=True)
    return signals
