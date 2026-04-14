"""
Trade Ledger
============
Tracks open and closed covered call positions in ledger.json.
Straightforward — add a trade, close a trade, check the monthly target.

ledger.json schema:
{
  "balance":    7000.00,
  "month":      "2026-04",
  "month_start": 7000.00,
  "trades": [
    {
      "id":         "SOFI-20260425-C8",
      "ticker":     "SOFI",
      "right":      "call",
      "strike":     8.0,
      "expiry":     "2026-04-25",
      "entry_date": "2026-04-11",
      "contracts":  1,
      "premium_collected": 58.00,   // total $ collected (mid × 100 × contracts)
      "status":     "OPEN",         // OPEN | CLOSED | ROLLED
      "close_date": null,
      "close_cost": null,           // $ paid to buy back
      "pnl":        null
    }
  ]
}
"""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Optional

import config


def _load() -> dict:
    if not os.path.exists(config.LEDGER_FILE):
        return _empty()
    with open(config.LEDGER_FILE) as f:
        return json.load(f)


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(config.LEDGER_FILE), exist_ok=True)
    with open(config.LEDGER_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _empty() -> dict:
    today = date.today()
    data  = {
        "balance":     config.MAX_CAPITAL,
        "month":       today.strftime("%Y-%m"),
        "month_start": config.MAX_CAPITAL,
        "trades":      [],
    }
    _save(data)
    return data


def _rollover(data: dict) -> None:
    """Snapshot and reset at month boundary."""
    this_month = date.today().strftime("%Y-%m")
    if data.get("month") == this_month:
        return
    data["month"]       = this_month
    data["month_start"] = data["balance"]
    _save(data)


# ── Public API ────────────────────────────────────────────────────────────────

def add_trade(
    ticker:    str,
    right:     str,
    strike:    float,
    expiry:    str,
    contracts: int,
    premium:   float,          # total $ collected (already × 100 × contracts)
) -> str:
    data = _load()
    _rollover(data)
    trade_id = f"{ticker}-{expiry.replace('-','')}-{right[0].upper()}{int(strike)}"
    # Deduplicate: if same id exists, append a suffix
    existing = [t["id"] for t in data["trades"]]
    base     = trade_id
    n        = 1
    while trade_id in existing:
        trade_id = f"{base}-{n}"
        n += 1

    data["trades"].append({
        "id":                trade_id,
        "ticker":            ticker,
        "right":             right,
        "strike":            strike,
        "expiry":            expiry,
        "entry_date":        date.today().isoformat(),
        "contracts":         contracts,
        "premium_collected": round(premium, 2),
        "status":            "OPEN",
        "close_date":        None,
        "close_cost":        None,
        "pnl":               None,
    })
    _save(data)
    return trade_id


def close_trade(trade_id: str, close_cost: float, status: str = "CLOSED") -> dict:
    """
    close_cost: total $ paid to buy back (0 if expired worthless).
    """
    data  = _load()
    trade = next((t for t in data["trades"] if t["id"] == trade_id), None)
    if not trade:
        raise KeyError(f"Trade {trade_id} not found.")

    pnl = round(trade["premium_collected"] - close_cost, 2)
    trade.update({
        "status":     status,
        "close_date": date.today().isoformat(),
        "close_cost": round(close_cost, 2),
        "pnl":        pnl,
    })
    data["balance"] = round(data["balance"] + pnl, 2)
    _save(data)
    return trade


def open_trades() -> list[dict]:
    return [t for t in _load()["trades"] if t["status"] == "OPEN"]


def monthly_summary() -> dict:
    data   = _load()
    _rollover(data)
    start  = data["month_start"]
    now    = data["balance"]
    target = round(start * (1 + config.MONTHLY_TARGET_PCT / 100), 2)
    earned = round(now - start, 2)
    needed = round(target - now, 2)
    done   = round(earned / (target - start) * 100, 1) if target != start else 100.0
    return {
        "month":           data["month"],
        "start_balance":   start,
        "current_balance": now,
        "target_balance":  target,
        "earned_this_month": earned,
        "still_needed":    needed,
        "pct_complete":    max(0.0, done),
        "on_track":        now >= target,
    }


def full_summary() -> dict:
    data   = _load()
    closed = [t for t in data["trades"] if t["status"] == "CLOSED" and t["pnl"] is not None]
    wins   = [t for t in closed if t["pnl"] > 0]
    return {
        "balance":     data["balance"],
        "total_trades": len(data["trades"]),
        "open":        len([t for t in data["trades"] if t["status"] == "OPEN"]),
        "closed":      len(closed),
        "total_pnl":   round(sum(t["pnl"] for t in closed), 2),
        "win_rate":    round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
    }
