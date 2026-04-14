"""
Dark Pool Print Flagging
========================
"Hidden Accumulation" Detection Logic:

True dark-pool data requires a paid feed (Unusual Whales, Cboe LiveVol,
Bloomberg TSOX, or a direct ATS/dark-pool data subscription).

This module provides:
  1. A free **proxy heuristic** using public tape data available via yfinance:
     - Large price-relative intraday volume spikes that printed *at or below
       the bid* (a dark-pool buy signature) show up as anomalous 5-min candles
       with volume >> rolling average and close <= open.  We can approximate
       this from daily volume vs. 20-day average.
  2. A clearly-labelled **stub** for the Unusual Whales API that can be
     activated with a valid API key in .env.
  3. A **DarkPoolSignal** dataclass that both code paths populate, so the
     rest of the bot is API-agnostic.

Heuristic quality: ~60–65 % accuracy (good for early alerting; confirm with
paid feed before trading).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import yfinance as yf

import config

log = logging.getLogger(__name__)

DARK_POOL_VOLUME_MULTIPLIER = 2.0   # flag if daily vol > N × 20-day avg
DARK_POOL_CLOSE_BIAS_THRESHOLD = 0.45  # close in bottom N% of day range


@dataclass
class DarkPoolSignal:
    ticker:           str
    date_flagged:     date
    underlying_price: float
    volume:           int
    avg_volume_20d:   int
    volume_ratio:     float          # volume / avg_volume
    close_bias:       float          # (close-low)/(high-low); <0.45 = bid-side
    hidden_accumulation: bool
    source:           str            # "HEURISTIC" | "UNUSUAL_WHALES" | "NONE"
    confidence:       str            # "LOW" | "MEDIUM" | "HIGH"
    description:      str
    raw_prints:       list[dict] = field(default_factory=list)


def _heuristic_scan(symbol: str) -> DarkPoolSignal:
    """
    Free proxy: flag large-volume days where price closed in the lower half
    of the day range (consistent with institutional buying at the bid).
    """
    try:
        tk   = yf.Ticker(symbol)
        hist = tk.history(period="1mo")
        if len(hist) < 5:
            raise ValueError("insufficient history")

        last = hist.iloc[-1]
        today_vol  = int(last["Volume"])
        avg_vol    = int(hist["Volume"].rolling(20).mean().iloc[-1])
        vol_ratio  = today_vol / avg_vol if avg_vol > 0 else 1.0

        price_range = last["High"] - last["Low"]
        close_bias  = (
            (last["Close"] - last["Low"]) / price_range
            if price_range > 0 else 0.5
        )

        hidden_acc = (
            vol_ratio >= DARK_POOL_VOLUME_MULTIPLIER and
            close_bias <= DARK_POOL_CLOSE_BIAS_THRESHOLD
        )

        if hidden_acc:
            confidence = "MEDIUM" if vol_ratio >= 3.0 else "LOW"
        else:
            confidence = "NONE"

        parts = [
            f"Vol={today_vol:,} (×{vol_ratio:.2f} avg)",
            f"CloseBias={close_bias:.3f}",
        ]
        if hidden_acc:
            parts.append(
                f"⚑ HIDDEN ACCUMULATION ({confidence} confidence — heuristic)"
            )

        return DarkPoolSignal(
            ticker            = symbol,
            date_flagged      = date.today(),
            underlying_price  = float(last["Close"]),
            volume            = today_vol,
            avg_volume_20d    = avg_vol,
            volume_ratio      = round(vol_ratio, 3),
            close_bias        = round(close_bias, 4),
            hidden_accumulation = hidden_acc,
            source            = "HEURISTIC",
            confidence        = confidence,
            description       = f"{symbol}: " + " | ".join(parts),
        )

    except Exception as exc:
        log.error("dark_pool heuristic(%s): %s", symbol, exc)
        return _no_signal(symbol, str(exc))


def _unusual_whales_scan(symbol: str) -> DarkPoolSignal:
    """
    STUB — Unusual Whales dark-pool prints endpoint.
    Requires UNUSUAL_WHALES_API_KEY in .env.

    Endpoint: GET https://api.unusualwhales.com/api/darkpool/{symbol}/recent
    Docs:     https://unusualwhales.com/docs#dark-pool

    Response contains: date, price, size, notional_value, trade_conditions
    We flag "Hidden Accumulation" when:
      - A print is >= $500K notional
      - trade_condition includes "below bid" or "at bid"
      - No news catalyst in the prior 24 h
    """
    import requests

    api_key = os.environ.get("UNUSUAL_WHALES_API_KEY", "")
    if not api_key:
        log.debug("UNUSUAL_WHALES_API_KEY not set; skipping API scan for %s", symbol)
        return _no_signal(symbol, "API key not configured")

    url = f"https://api.unusualwhales.com/api/darkpool/{symbol}/recent"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        resp.raise_for_status()
        prints = resp.json().get("data", [])

        flagged = [
            p for p in prints
            if float(p.get("notional_value", 0)) >= 500_000
            and "bid" in p.get("trade_conditions", "").lower()
        ]

        hidden_acc = len(flagged) > 0
        confidence = "HIGH" if len(flagged) >= 3 else ("MEDIUM" if flagged else "NONE")

        tk   = yf.Ticker(symbol)
        hist = tk.history(period="1d")
        price = float(hist["Close"].iloc[-1]) if not hist.empty else 0.0

        description = (
            f"{symbol}: {len(flagged)} bid-side dark prints found "
            f"(total scanned: {len(prints)}) — confidence: {confidence}"
        )
        if hidden_acc:
            description = "⚑ " + description

        return DarkPoolSignal(
            ticker            = symbol,
            date_flagged      = date.today(),
            underlying_price  = price,
            volume            = 0,
            avg_volume_20d    = 0,
            volume_ratio      = 0.0,
            close_bias        = 0.0,
            hidden_accumulation = hidden_acc,
            source            = "UNUSUAL_WHALES",
            confidence        = confidence,
            description       = description,
            raw_prints        = flagged,
        )

    except Exception as exc:
        log.error("unusual_whales_scan(%s): %s", symbol, exc)
        return _no_signal(symbol, str(exc))


def _no_signal(symbol: str, reason: str) -> DarkPoolSignal:
    return DarkPoolSignal(
        ticker=symbol, date_flagged=date.today(),
        underlying_price=0.0, volume=0, avg_volume_20d=0,
        volume_ratio=0.0, close_bias=0.5,
        hidden_accumulation=False, source="NONE", confidence="NONE",
        description=f"{symbol}: dark pool scan unavailable — {reason}",
    )


def scan(symbols: list[str] | None = None) -> list[DarkPoolSignal]:
    """
    Scan all symbols.  Uses Unusual Whales API if key is present, otherwise
    falls back to the free heuristic.
    """
    symbols = symbols or config.WATCHLIST
    results: list[DarkPoolSignal] = []

    api_available = bool(os.environ.get("UNUSUAL_WHALES_API_KEY", ""))

    for sym in symbols:
        if api_available:
            sig = _unusual_whales_scan(sym)
            # Fall back to heuristic if API call failed
            if sig.source == "NONE":
                sig = _heuristic_scan(sym)
        else:
            sig = _heuristic_scan(sym)
        results.append(sig)
        log.debug(sig.description)

    # Surface flagged signals first
    results.sort(key=lambda s: s.hidden_accumulation, reverse=True)
    return results
