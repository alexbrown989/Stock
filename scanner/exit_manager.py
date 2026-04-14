"""
Exit Manager
============
The #1 most empirically-validated rule in systematic premium selling
(TastyTrade research, 2014-2024):

    "Close at 50 % of max premium. Roll if DTE < 7."

Why it works:
  - The last 50 % of premium takes far longer to decay (gamma accelerates).
  - Holding to expiry means accepting full gamma risk for diminishing theta.
  - Re-deploying capital from closed 50%-winners generates more total premium
    than holding any single trade to zero.
  - Win rate increases significantly: ~65 % (hold to expiry) → ~85 % (50 % rule).

Decision tree for each open trade:
  ┌── Is current_value <= 50% of entry_credit? ──► CLOSE (take profit)
  ├── Is current_value >= 2× entry_credit?      ──► CLOSE (hard stop-loss)
  ├── Is DTE < ROLL_DTE_THRESHOLD AND untested?  ──► ROLL (next cycle)
  ├── Is strike breached (delta > 0.70)?         ──► ROLL (defend or close)
  └── Otherwise                                  ──► HOLD

Roll logic:
  - Buy back current position
  - Sell same strike, next expiry (~21 DTE)
  - If tested (delta > 0.60), roll UP the strike by one step as well
  - Net credit should be > $0 for the roll to be worth it

Data source: yfinance (real-time quote for current option price).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Optional

import yfinance as yf

import config
import ledger as ledger_module

log = logging.getLogger(__name__)


class ExitAction(str, Enum):
    HOLD        = "HOLD"
    CLOSE       = "CLOSE"       # take profit or stop-loss
    ROLL        = "ROLL"        # roll to next expiry
    ROLL_UP     = "ROLL_UP"     # roll up strike AND next expiry (tested)
    URGENT_ROLL = "URGENT_ROLL" # delta breach — urgent


@dataclass
class ExitSignal:
    trade_id:        str
    ticker:          str
    action:          ExitAction
    reason:          str
    current_mid:     float      # current option mid price per share
    entry_credit:    float      # original premium collected per share
    current_value:   float      # what it would cost to buy back per share
    pnl_if_closed:   float      # P&L per contract if closed now
    pnl_pct:         float      # pnl_if_closed / entry_credit * 100
    current_delta:   Optional[float]
    current_dte:     int
    # Roll suggestion
    roll_to_expiry:  Optional[str] = None
    roll_to_strike:  Optional[float] = None
    roll_net_credit: Optional[float] = None
    urgent:          bool = False


def _get_current_option_price(
    symbol: str,
    expiry: str,
    strike: float,
    right: str = "call",
) -> tuple[float, float]:
    """
    Fetch current bid/ask for an option, return (mid, delta).
    delta is from the chain if available, else 0.0.
    """
    try:
        tk    = yf.Ticker(symbol)
        chain = tk.option_chain(expiry)
        table = chain.calls if right.lower() == "call" else chain.puts
        row   = table.iloc[(table["strike"] - strike).abs().argsort()[:1]]
        if row.empty:
            return 0.0, 0.0
        bid   = float(row["bid"].values[0])
        ask   = float(row["ask"].values[0])
        delta = float(row.get("delta", row).get("delta", [0.0]).values[0]) if "delta" in row.columns else 0.0
        return round((bid + ask) / 2, 2), round(delta, 4)
    except Exception as exc:
        log.warning("_get_current_option_price(%s %s %s): %s", symbol, expiry, strike, exc)
        return 0.0, 0.0


def _next_expiry_near_21dte(symbol: str) -> Optional[str]:
    """Find the listed expiry closest to 21 DTE for a roll target."""
    try:
        today = date.today()
        tk    = yf.Ticker(symbol)
        best  = None
        best_diff = 999
        for exp_str in tk.options:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte_diff = abs((exp_date - today).days - 21)
            if dte_diff < best_diff:
                best_diff = dte_diff
                best = exp_str
        return best
    except Exception:
        return None


def _roll_net_credit(
    symbol: str,
    current_expiry: str,
    current_strike: float,
    roll_expiry: str,
    roll_strike: float,
    current_mid: float,
    right: str = "call",
) -> float:
    """
    Estimate net credit/debit for rolling:
      net = sell_new_mid - buy_back_current_mid
    Positive = net credit (good). Negative = net debit (avoid if possible).
    """
    try:
        new_mid, _ = _get_current_option_price(symbol, roll_expiry, roll_strike, right)
        return round(new_mid - current_mid, 2)
    except Exception:
        return 0.0


def evaluate_trade(trade: dict) -> ExitSignal:
    """
    Evaluate a single open trade from the ledger.
    `trade` is a ledger trade dict (see ledger.py schema).
    """
    symbol       = trade["ticker"]
    right        = trade.get("right", "call")
    strike       = float(trade["strike"])
    expiry_str   = trade["expiry"]
    contracts    = int(trade.get("contracts", 1))
    entry_prem   = float(trade["premium_collected"]) / contracts  # per-contract → per-share

    today = date.today()
    try:
        exp_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        current_dte = (exp_date - today).days
    except Exception:
        current_dte = 0

    current_mid, current_delta = _get_current_option_price(symbol, expiry_str, strike, right)

    # If we can't get a price, default to conservative HOLD
    if current_mid <= 0:
        return ExitSignal(
            trade_id=trade["id"], ticker=symbol,
            action=ExitAction.HOLD, reason="could not fetch current price",
            current_mid=0, entry_credit=entry_prem, current_value=0,
            pnl_if_closed=0, pnl_pct=0, current_delta=None,
            current_dte=current_dte,
        )

    # P&L: we collected entry_prem; buying back costs current_mid
    pnl_per_share = entry_prem - current_mid
    pnl_contract  = pnl_per_share * 100 * contracts
    pnl_pct       = (pnl_per_share / entry_prem * 100) if entry_prem > 0 else 0

    # ── Decision tree ─────────────────────────────────────────────────────

    # 1. 50 % profit target (the primary exit)
    if current_mid <= entry_prem * (1 - config.PROFIT_TARGET_PCT):
        return ExitSignal(
            trade_id=trade["id"], ticker=symbol,
            action=ExitAction.CLOSE,
            reason=f"50% profit target hit: collected {entry_prem:.2f}, now {current_mid:.2f} ({pnl_pct:.1f}% gain)",
            current_mid=current_mid, entry_credit=entry_prem, current_value=current_mid,
            pnl_if_closed=round(pnl_contract, 2), pnl_pct=round(pnl_pct, 1),
            current_delta=current_delta, current_dte=current_dte,
        )

    # 2. Hard stop-loss (2× credit)
    if current_mid >= entry_prem * config.STOP_LOSS_MULTIPLIER:
        return ExitSignal(
            trade_id=trade["id"], ticker=symbol,
            action=ExitAction.CLOSE,
            reason=f"STOP-LOSS: position at {current_mid:.2f} = {current_mid/entry_prem:.1f}× entry credit {entry_prem:.2f}",
            current_mid=current_mid, entry_credit=entry_prem, current_value=current_mid,
            pnl_if_closed=round(pnl_contract, 2), pnl_pct=round(pnl_pct, 1),
            current_delta=current_delta, current_dte=current_dte, urgent=True,
        )

    # 3. Delta breach — strike tested (call delta > 0.70 means we're ITM)
    if current_delta and current_delta > 0.70:
        roll_expiry = _next_expiry_near_21dte(symbol)
        roll_strike = strike * 1.02  # roll up 2% (buy some distance)
        net_cr      = _roll_net_credit(symbol, expiry_str, strike, roll_expiry or expiry_str, roll_strike, current_mid, right) if roll_expiry else None
        return ExitSignal(
            trade_id=trade["id"], ticker=symbol,
            action=ExitAction.URGENT_ROLL,
            reason=f"Delta {current_delta:.3f} > 0.70 — strike BREACHED. Roll or close immediately.",
            current_mid=current_mid, entry_credit=entry_prem, current_value=current_mid,
            pnl_if_closed=round(pnl_contract, 2), pnl_pct=round(pnl_pct, 1),
            current_delta=current_delta, current_dte=current_dte,
            roll_to_expiry=roll_expiry, roll_to_strike=round(roll_strike, 2),
            roll_net_credit=net_cr, urgent=True,
        )

    # 4. DTE roll trigger (< 7 DTE and untested)
    if current_dte < config.ROLL_DTE_THRESHOLD and (not current_delta or current_delta < 0.50):
        roll_expiry = _next_expiry_near_21dte(symbol)
        roll_strike = strike
        net_cr      = _roll_net_credit(symbol, expiry_str, strike, roll_expiry or expiry_str, roll_strike, current_mid, right) if roll_expiry else None
        return ExitSignal(
            trade_id=trade["id"], ticker=symbol,
            action=ExitAction.ROLL,
            reason=f"DTE={current_dte} < {config.ROLL_DTE_THRESHOLD} — roll to next cycle (~21 DTE) for more premium",
            current_mid=current_mid, entry_credit=entry_prem, current_value=current_mid,
            pnl_if_closed=round(pnl_contract, 2), pnl_pct=round(pnl_pct, 1),
            current_delta=current_delta, current_dte=current_dte,
            roll_to_expiry=roll_expiry, roll_to_strike=roll_strike,
            roll_net_credit=net_cr,
        )

    # 5. Hold
    return ExitSignal(
        trade_id=trade["id"], ticker=symbol,
        action=ExitAction.HOLD,
        reason=f"Holding: {pnl_pct:.1f}% unrealised gain, {current_dte} DTE, delta={current_delta:.3f}" if current_delta else f"Holding: {pnl_pct:.1f}% unrealised gain, {current_dte} DTE",
        current_mid=current_mid, entry_credit=entry_prem, current_value=current_mid,
        pnl_if_closed=round(pnl_contract, 2), pnl_pct=round(pnl_pct, 1),
        current_delta=current_delta, current_dte=current_dte,
    )


def scan_open_positions() -> list[ExitSignal]:
    """
    Evaluate all open trades in the ledger and return exit signals.
    Urgent signals (stop-loss, delta breach) are surfaced first.
    """
    open_trades = ledger_module.open_trades()
    if not open_trades:
        log.info("No open positions to evaluate.")
        return []

    signals = [evaluate_trade(t) for t in open_trades]
    signals.sort(key=lambda s: (s.urgent, s.action != ExitAction.HOLD), reverse=True)
    return signals
