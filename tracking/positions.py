"""
Position tracking: open, close, and monitor active trades.
Persists to a JSON file for cycle continuity.
"""

import json
import logging
import os
from datetime import datetime
from typing import List, Dict, Optional

from config import POSITIONS_FILE, PROFIT_TARGET_PCT, STOP_LOSS_PCT

logger = logging.getLogger(__name__)


def load_positions() -> List[Dict]:
    """Load all active positions from disk."""
    if not os.path.exists(POSITIONS_FILE):
        return []
    try:
        with open(POSITIONS_FILE) as f:
            data = json.load(f)
        return data.get("positions", [])
    except Exception as e:
        logger.warning(f"Failed to load positions: {e}")
        return []


def save_positions(positions: List[Dict]) -> None:
    """Write positions list to disk."""
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump({"positions": positions, "updated": datetime.now().isoformat()}, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save positions: {e}")


def open_position(trade: Dict) -> Dict:
    """
    Record a new position.
    Adds entry metadata and appends to the positions file.
    """
    positions = load_positions()
    pos = {
        "id":           f"{trade['ticker']}_{trade['expiration']}_{datetime.now().strftime('%Y%m%d%H%M')}",
        "ticker":       trade["ticker"],
        "structure":    trade["structure"],
        "spot_entry":   trade.get("spot"),
        "strike":       trade.get("strike") or trade.get("strike_short"),
        "strike_long":  trade.get("strike_long") or trade.get("put_long"),
        "expiration":   trade["expiration"],
        "dte_entry":    trade.get("dte"),
        "credit":       trade.get("premium") or trade.get("net_credit"),
        "delta_entry":  trade.get("delta"),
        "theta_entry":  trade.get("theta"),
        "iv_entry":     trade.get("iv"),
        "pop_entry":    trade.get("pop"),
        "status":       "open",
        "opened_at":    datetime.now().isoformat(),
        "closed_at":    None,
        "close_price":  None,
        "pnl":          None,
        "close_reason": None,
    }
    positions.append(pos)
    save_positions(positions)
    logger.info(f"Opened position: {pos['id']}")
    return pos


def close_position(position_id: str, close_price: float, reason: str) -> Optional[Dict]:
    """
    Mark a position as closed and record P&L.
    P&L = credit received - close price (positive = profit).
    """
    positions = load_positions()
    for pos in positions:
        if pos["id"] == position_id and pos["status"] == "open":
            credit = pos.get("credit", 0)
            pnl = credit - close_price
            pos.update({
                "status":       "closed",
                "closed_at":    datetime.now().isoformat(),
                "close_price":  close_price,
                "pnl":          round(pnl, 2),
                "close_reason": reason,
            })
            save_positions(positions)
            logger.info(f"Closed {position_id}: P&L={pnl:+.2f} ({reason})")
            return pos
    logger.warning(f"Position {position_id} not found or already closed")
    return None


def check_exit_conditions(positions: List[Dict]) -> List[Dict]:
    """
    Check each open position against profit target and stop loss.
    Returns list of positions that should be closed.

    Requires current market prices — this is a decision-support function.
    Actual closing must be confirmed by the operator.
    """
    from utils.data import get_options_chain, get_current_price
    import math
    from datetime import datetime
    from analysis.greeks import bsm_price, implied_volatility
    from config import RISK_FREE_RATE

    to_close = []
    for pos in positions:
        if pos["status"] != "open":
            continue

        ticker = pos["ticker"]
        expiry = pos["expiration"]
        strike = pos.get("strike")
        credit = pos.get("credit", 0)
        structure = pos.get("structure", "short_put")

        if not all([ticker, expiry, strike, credit]):
            continue

        try:
            # Check DTE
            today = datetime.today().date()
            exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
            dte_remaining = (exp_date - today).days

            if dte_remaining <= 0:
                pos["exit_signal"] = "expiration"
                to_close.append(pos)
                continue

            T = dte_remaining / 365
            spot = get_current_price(ticker)
            if spot is None:
                continue

            # Estimate current option value
            chain_data = get_options_chain(ticker, expiry)
            if not chain_data:
                continue

            opt_type = "put" if "put" in structure else "call"
            df = chain_data.get("puts") if opt_type == "put" else chain_data.get("calls")
            if df is None or df.empty:
                continue

            row = df[abs(df["strike"] - strike) < 0.01]
            if row.empty:
                row = df.iloc[(df["strike"] - strike).abs().argsort()[:1]]

            current_mid = (row.iloc[0]["bid"] + row.iloc[0]["ask"]) / 2
            if current_mid <= 0:
                continue

            # Profit target: close at PROFIT_TARGET_PCT of credit
            if current_mid <= credit * (1 - PROFIT_TARGET_PCT):
                pos["exit_signal"] = f"profit_target ({PROFIT_TARGET_PCT:.0%})"
                pos["current_price"] = current_mid
                pos["unrealized_pnl"] = round(credit - current_mid, 2)
                to_close.append(pos)
                continue

            # Stop loss: close at STOP_LOSS_PCT × credit
            if current_mid >= credit * STOP_LOSS_PCT:
                pos["exit_signal"] = f"stop_loss ({STOP_LOSS_PCT:.0%}x)"
                pos["current_price"] = current_mid
                pos["unrealized_pnl"] = round(credit - current_mid, 2)
                to_close.append(pos)

        except Exception as e:
            logger.debug(f"Exit check failed for {pos.get('id')}: {e}")

    return to_close


def portfolio_summary() -> Dict:
    """Return a summary of the current open portfolio."""
    positions = load_positions()
    open_pos   = [p for p in positions if p["status"] == "open"]
    closed_pos = [p for p in positions if p["status"] == "closed"]

    total_credit    = sum(p.get("credit", 0) for p in open_pos)
    realized_pnl    = sum(p.get("pnl", 0) for p in closed_pos if p.get("pnl"))
    win_trades      = [p for p in closed_pos if (p.get("pnl") or 0) > 0]
    win_rate        = len(win_trades) / len(closed_pos) if closed_pos else None

    return {
        "open_positions":  len(open_pos),
        "closed_positions": len(closed_pos),
        "total_open_credit": round(total_credit, 2),
        "realized_pnl":    round(realized_pnl, 2),
        "win_rate":        round(win_rate, 3) if win_rate else None,
        "positions":       open_pos,
    }
