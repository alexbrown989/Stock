"""
Sentiment Entropy Analyzer
===========================
Strategy: "Peak Fear + Falling Put/Call = Reversal Entry"

Algorithm:
  1. Derive a *Put/Call Ratio* from the options chain (total put OI / call OI).
     This is a free proxy when no paid P/C feed is available.
  2. Compare today's P/C to the 10-day average.  A drop >= threshold while
     IV Rank is high (>= PEAK_FEAR_IV_RANK_THRESHOLD) signals that fear is
     being unwound — a high-IV entry opportunity for premium selling.
  3. Optionally enhance with Unusual Whales / Benzinga social sentiment API.
     The `social_sentiment_stub()` function shows where to plug that in.

Output: SentimentSignal per ticker.

Note: True "social volume" metrics (Reddit mentions, StockTwits, X/Twitter
velocity) require a paid API like Quiver Quant, Unusual Whales, or Benzinga
Pro.  The stub below is clearly labelled and can be swapped with a live call.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

import config

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
PC_HISTORY_DAYS = 10   # rolling window for P/C average


@dataclass
class SentimentSignal:
    ticker:           str
    iv_rank:          float
    put_call_ratio:   float        # current
    pc_ratio_avg:     float        # N-day average
    pc_ratio_change:  float        # current - avg  (negative = P/C dropping)
    peak_fear:        bool         # iv_rank >= PEAK_FEAR threshold
    pc_dropping:      bool         # ratio fell >= DROP_THRESHOLD
    reversal_signal:  bool         # peak_fear AND pc_dropping
    social_sentiment: str          # "BULLISH" | "BEARISH" | "NEUTRAL" | "UNAVAILABLE"
    description:      str


def _put_call_ratio_from_chain(symbol: str, exp_str: str) -> float:
    """Compute put OI / call OI for a single expiry."""
    try:
        chain    = yf.Ticker(symbol).option_chain(exp_str)
        total_put_oi  = int(chain.puts["openInterest"].fillna(0).sum())
        total_call_oi = int(chain.calls["openInterest"].fillna(0).sum())
        if total_call_oi == 0:
            return 1.0
        return round(total_put_oi / total_call_oi, 4)
    except Exception as exc:
        log.warning("_put_call_ratio(%s, %s): %s", symbol, exp_str, exc)
        return 1.0


def _iv_rank_fast(symbol: str) -> float:
    """Quick IV Rank using 90-day rolling realised vol."""
    try:
        hist = yf.Ticker(symbol).history(period="1y")
        if len(hist) < 30:
            return 0.0
        rets   = np.log(hist["Close"] / hist["Close"].shift(1)).dropna()
        rv     = rets.rolling(21).std() * np.sqrt(252)
        current_iv = float(rv.iloc[-1])
        low_iv  = float(rv.min())
        high_iv = float(rv.max())
        if high_iv == low_iv:
            return 50.0
        return max(0.0, min(100.0, (current_iv - low_iv) / (high_iv - low_iv) * 100))
    except Exception:
        return 0.0


def social_sentiment_stub(symbol: str) -> str:
    """
    STUB — Replace with a real API call to one of:
      - Unusual Whales:  GET /api/stock/{ticker}/sentiment
      - Quiver Quant:    GET /beta/live/wallstreetbets (filtered by ticker)
      - Benzinga Pro:    /v2/signal/option-activity
      - StockTwits:      https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json

    Requires: SENTIMENT_API_KEY in .env
    Returns "BULLISH" | "BEARISH" | "NEUTRAL" | "UNAVAILABLE"
    """
    api_key = os.environ.get("SENTIMENT_API_KEY", "")
    if not api_key:
        return "UNAVAILABLE"

    # ── Example: Quiver Quant WallStreetBets endpoint ─────────────────────
    # import requests
    # resp = requests.get(
    #     f"https://api.quiverquant.com/beta/live/wallstreetbets",
    #     headers={"Authorization": f"Token {api_key}"},
    #     timeout=10,
    # )
    # data = [r for r in resp.json() if r["Ticker"] == symbol]
    # if not data:
    #     return "NEUTRAL"
    # score = data[0].get("Score", 0)
    # if score > 2:   return "BULLISH"
    # if score < -2:  return "BEARISH"
    # return "NEUTRAL"

    return "UNAVAILABLE"


def analyze(symbol: str) -> SentimentSignal:
    """
    Compute sentiment entropy for a single ticker.
    """
    try:
        tk = yf.Ticker(symbol)
        exps = tk.options
        if not exps:
            raise ValueError("no options listed")

        # Use nearest two expiries for a more stable P/C signal
        nearest_exps = exps[:2]
        pc_ratios = [
            _put_call_ratio_from_chain(symbol, e) for e in nearest_exps
        ]
        current_pc = float(np.mean(pc_ratios))

        # Rolling N-day P/C approximation: we don't have historical P/C
        # so we use the spread between nearest-expiry and next-expiry P/C
        # as a proxy for the trend direction.
        if len(pc_ratios) >= 2:
            pc_avg = float(np.mean(pc_ratios[1:]))  # prior expiry as baseline
        else:
            pc_avg = current_pc

        pc_change  = current_pc - pc_avg
        pc_dropping = pc_change <= -config.PC_RATIO_DROP_THRESHOLD

        iv_rank   = _iv_rank_fast(symbol)
        peak_fear = iv_rank >= config.PEAK_FEAR_IV_RANK_THRESHOLD

        reversal_signal = peak_fear and pc_dropping

        social = social_sentiment_stub(symbol)

        parts = [
            f"P/C={current_pc:.3f} (avg={pc_avg:.3f}, Δ={pc_change:+.3f})",
            f"IV Rank={iv_rank:.1f}%",
            f"Social={social}",
        ]
        if reversal_signal:
            parts.append("🔥 PEAK FEAR + FALLING P/C → HIGH-IV REVERSAL ENTRY")
        elif peak_fear:
            parts.append("⚠ Peak Fear — waiting for P/C confirmation")
        elif pc_dropping:
            parts.append("P/C dropping — IV not yet elevated enough")

        return SentimentSignal(
            ticker          = symbol,
            iv_rank         = iv_rank,
            put_call_ratio  = current_pc,
            pc_ratio_avg    = pc_avg,
            pc_ratio_change = pc_change,
            peak_fear       = peak_fear,
            pc_dropping     = pc_dropping,
            reversal_signal = reversal_signal,
            social_sentiment= social,
            description     = f"{symbol}: " + " | ".join(parts),
        )

    except Exception as exc:
        log.error("sentiment.analyze(%s): %s", symbol, exc, exc_info=True)
        return SentimentSignal(
            ticker=symbol, iv_rank=0, put_call_ratio=1.0,
            pc_ratio_avg=1.0, pc_ratio_change=0.0,
            peak_fear=False, pc_dropping=False, reversal_signal=False,
            social_sentiment="UNAVAILABLE",
            description=f"{symbol}: error — {exc}",
        )


def scan(symbols: list[str] | None = None) -> list[SentimentSignal]:
    symbols = symbols or config.WATCHLIST
    results = [analyze(s) for s in symbols]
    # Sort so reversal signals surface first
    results.sort(key=lambda s: (s.reversal_signal, s.peak_fear), reverse=True)
    return results
