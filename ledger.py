"""
Ledger — 5% Monthly Compound Tracker
=====================================
Maintains ledger.json with:
  - Starting capital
  - Monthly target (5 % compound)
  - Per-trade entries (open / closed)
  - Realised P&L and cumulative ROI

Schema (ledger.json):
{
  "starting_capital": 7000.00,
  "monthly_target_pct": 5.0,
  "current_month": "2026-04",
  "month_start_balance": 7000.00,
  "current_balance": 7000.00,
  "cumulative_roi_pct": 0.0,
  "trades": [
    {
      "id": "SOFI-20260425-C8-001",
      "ticker": "SOFI",
      "right": "call",
      "strike": 8.0,
      "expiry": "2026-04-25",
      "dte_at_entry": 14,
      "entry_date": "2026-04-11",
      "close_date": null,
      "status": "OPEN",               // OPEN | CLOSED | ROLLED
      "contracts": 1,
      "premium_collected": 58.00,
      "close_cost": null,
      "net_pnl": null,
      "delta_at_entry": 0.32,
      "gamma_at_entry": 0.08,
      "iv_rank_at_entry": 67.2,
      "notes": ""
    }
  ],
  "monthly_snapshots": [
    {"month": "2026-03", "start": 6667, "end": 7000, "pnl": 333, "roi_pct": 5.0}
  ]
}
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Optional
from uuid import uuid4

import config

log = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _load() -> dict:
    if not os.path.exists(config.LEDGER_PATH):
        return _bootstrap()
    with open(config.LEDGER_PATH, "r") as f:
        return json.load(f)


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(config.LEDGER_PATH), exist_ok=True)
    with open(config.LEDGER_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)
    log.debug("Ledger saved to %s", config.LEDGER_PATH)


def _bootstrap() -> dict:
    today    = date.today()
    month_str = today.strftime("%Y-%m")
    data = {
        "starting_capital":    config.MAX_TOTAL_CAPITAL,
        "monthly_target_pct":  config.MONTHLY_COMPOUND_TARGET * 100,
        "current_month":       month_str,
        "month_start_balance": config.MAX_TOTAL_CAPITAL,
        "current_balance":     config.MAX_TOTAL_CAPITAL,
        "cumulative_roi_pct":  0.0,
        "trades":              [],
        "monthly_snapshots":   [],
    }
    _save(data)
    log.info("Bootstrapped new ledger at %s", config.LEDGER_PATH)
    return data


# ── Public API ────────────────────────────────────────────────────────────────

def add_trade(
    ticker:            str,
    right:             str,
    strike:            float,
    expiry:            str,
    dte_at_entry:      int,
    contracts:         int,
    premium_collected: float,
    delta_at_entry:    float  = 0.0,
    gamma_at_entry:    float  = 0.0,
    iv_rank_at_entry:  float  = 0.0,
    notes:             str    = "",
) -> str:
    """
    Record a new trade (premium-selling leg).
    Returns the generated trade ID.
    """
    data = _load()
    _maybe_rollover_month(data)

    trade_id = f"{ticker}-{expiry.replace('-','')}-{right[0].upper()}{int(strike)}-{str(uuid4())[:4].upper()}"
    entry = {
        "id":                  trade_id,
        "ticker":              ticker,
        "right":               right,
        "strike":              strike,
        "expiry":              expiry,
        "dte_at_entry":        dte_at_entry,
        "entry_date":          date.today().isoformat(),
        "close_date":          None,
        "status":              "OPEN",
        "contracts":           contracts,
        "premium_collected":   round(premium_collected * contracts, 2),
        "close_cost":          None,
        "net_pnl":             None,
        "delta_at_entry":      delta_at_entry,
        "gamma_at_entry":      gamma_at_entry,
        "iv_rank_at_entry":    iv_rank_at_entry,
        "notes":               notes,
    }
    data["trades"].append(entry)
    _save(data)
    log.info("Trade added: %s", trade_id)
    return trade_id


def close_trade(
    trade_id:   str,
    close_cost: float,    # total cost to buy back (positive = debit)
    status:     str = "CLOSED",  # CLOSED | ROLLED
    notes:      str = "",
) -> dict:
    """
    Close or roll a trade.  Updates net_pnl and current_balance.
    close_cost: amount paid to buy back the option (0 if expired worthless).
    Returns the updated trade dict.
    """
    data = _load()
    trade = next((t for t in data["trades"] if t["id"] == trade_id), None)
    if trade is None:
        raise KeyError(f"Trade {trade_id} not found in ledger.")

    net_pnl = round(trade["premium_collected"] - close_cost, 2)
    trade.update({
        "close_date": date.today().isoformat(),
        "status":     status,
        "close_cost": round(close_cost, 2),
        "net_pnl":    net_pnl,
        "notes":      notes or trade["notes"],
    })

    data["current_balance"] = round(data["current_balance"] + net_pnl, 2)
    start = data["starting_capital"]
    data["cumulative_roi_pct"] = round(
        (data["current_balance"] - start) / start * 100, 4
    )

    _save(data)
    log.info(
        "Trade %s closed. P&L: $%.2f | Balance: $%.2f",
        trade_id, net_pnl, data["current_balance"],
    )
    return trade


def monthly_target() -> dict:
    """
    Return a summary dict showing progress vs. this month's 5% target.
    """
    data  = _load()
    _maybe_rollover_month(data)

    start = data["month_start_balance"]
    curr  = data["current_balance"]
    target_balance = round(start * (1 + config.MONTHLY_COMPOUND_TARGET), 2)
    earned         = round(curr - start, 2)
    needed         = round(target_balance - curr, 2)
    pct_complete   = round((earned / (target_balance - start)) * 100, 1) if target_balance != start else 100.0

    return {
        "month":             data["current_month"],
        "start_balance":     start,
        "current_balance":   curr,
        "target_balance":    target_balance,
        "target_pct":        data["monthly_target_pct"],
        "earned_this_month": earned,
        "still_needed":      needed,
        "pct_complete":      pct_complete,
        "on_track":          curr >= target_balance,
    }


def open_trades() -> list[dict]:
    data = _load()
    return [t for t in data["trades"] if t["status"] == "OPEN"]


def summary() -> dict:
    data = _load()
    trades = data["trades"]
    closed = [t for t in trades if t["status"] == "CLOSED"]
    total_pnl = sum(t["net_pnl"] for t in closed if t["net_pnl"] is not None)
    wins = [t for t in closed if (t["net_pnl"] or 0) > 0]
    losses = [t for t in closed if (t["net_pnl"] or 0) <= 0]

    return {
        "starting_capital":   data["starting_capital"],
        "current_balance":    data["current_balance"],
        "cumulative_roi_pct": data["cumulative_roi_pct"],
        "total_trades":       len(trades),
        "open_trades":        len([t for t in trades if t["status"] == "OPEN"]),
        "closed_trades":      len(closed),
        "total_realised_pnl": round(total_pnl, 2),
        "win_rate":           round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "avg_win":            round(sum(t["net_pnl"] for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss":           round(sum(t["net_pnl"] for t in losses) / len(losses), 2) if losses else 0.0,
        "monthly_snapshots":  data.get("monthly_snapshots", []),
    }


def _maybe_rollover_month(data: dict) -> None:
    """
    If the calendar month has changed, snapshot last month's P&L
    and reset month_start_balance for compounding.
    """
    today_month = date.today().strftime("%Y-%m")
    if data.get("current_month") == today_month:
        return

    # Snapshot previous month
    snap = {
        "month":   data["current_month"],
        "start":   data["month_start_balance"],
        "end":     data["current_balance"],
        "pnl":     round(data["current_balance"] - data["month_start_balance"], 2),
        "roi_pct": round(
            (data["current_balance"] - data["month_start_balance"])
            / data["month_start_balance"] * 100, 4
        ),
    }
    data.setdefault("monthly_snapshots", []).append(snap)

    # Rollover
    data["current_month"]       = today_month
    data["month_start_balance"] = data["current_balance"]
    log.info(
        "Month rolled over to %s. New start balance: $%.2f",
        today_month, data["current_balance"],
    )
    _save(data)
