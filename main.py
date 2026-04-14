"""
Covered Call Scanner — Entry Point
===================================
Usage:
  python main.py              # run once now, then every 4 hours
  python main.py --once       # run once and exit
  python main.py --ledger     # print trade summary
  python main.py --add        # log a trade you placed
  python main.py --close ID   # close/mark expired a trade

Set DISCORD_WEBHOOK_URL in .env before running.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import config
import ledger
import notifier
from scanner import scan_all

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs(config.LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(config.LOG_DIR, "scanner.log"), encoding="utf-8"),
    ],
)
log = logging.getLogger("main")


# ── Core cycle ────────────────────────────────────────────────────────────────

def run_once(tickers: list[str] | None = None) -> None:
    log.info("── Scan started ────────────────────────────────")

    # 1. Scan for setups
    setups = scan_all(tickers)
    ready  = [s for s in setups if s.tag == "READY"]
    log.info("Setups found: %d ready, %d watching", len(ready), len(setups) - len(ready))
    for s in ready:
        log.info("  ✅ %s", s)

    # 2. Check open positions for exit signals
    open_trades  = ledger.open_trades()
    current_mids = _fetch_current_mids(open_trades)
    _check_exits(open_trades, current_mids)

    # 3. Monthly progress
    monthly = ledger.monthly_summary()
    log.info(
        "Monthly: $%.0f earned / $%.0f target (%.0f%%)",
        monthly["earned_this_month"],
        monthly["target_balance"] - monthly["start_balance"],
        monthly["pct_complete"],
    )

    # 4. Send Discord report
    notifier.send_scan_report(setups, open_trades, monthly, current_mids)
    log.info("── Scan complete ───────────────────────────────")


def _fetch_current_mids(trades: list[dict]) -> dict[str, float]:
    """Fetch current option mid price for each open position."""
    import yfinance as yf
    mids: dict[str, float] = {}
    for t in trades:
        try:
            tk    = yf.Ticker(t["ticker"])
            chain = tk.option_chain(t["expiry"])
            table = chain.calls if t["right"] == "call" else chain.puts
            row   = table.iloc[(table["strike"] - t["strike"]).abs().argsort()[:1]]
            if not row.empty:
                bid = float(row["bid"].values[0])
                ask = float(row["ask"].values[0])
                mids[t["id"]] = round((bid + ask) / 2, 2)
        except Exception:
            pass
    return mids


def _check_exits(trades: list[dict], mids: dict[str, float]) -> None:
    """Log and alert on 50% profit target or 2× stop-loss."""
    for t in trades:
        mid = mids.get(t["id"])
        if mid is None:
            continue
        entry   = t["premium_collected"] / t["contracts"]
        pnl_pct = (entry - mid) / entry * 100
        pnl_dol = (entry - mid) * 100 * t["contracts"]

        if pnl_pct >= config.PROFIT_TARGET_PCT * 100:
            msg = f"50% profit target hit — buy back at ${mid:.2f}"
            log.info("💰 EXIT SIGNAL %s: %s", t["id"], msg)
            notifier.send_exit_alert(t, msg, pnl_pct, pnl_dol)

        elif mid >= entry * config.STOP_LOSS_MULT:
            msg = f"STOP-LOSS — option at ${mid:.2f} = {mid/entry:.1f}× entry credit"
            log.warning("🛑 STOP-LOSS %s: %s", t["id"], msg)
            notifier.send_exit_alert(t, msg, pnl_pct, pnl_dol)


# ── Scheduler ─────────────────────────────────────────────────────────────────

def run_scheduler(tickers: list[str] | None = None) -> None:
    interval = config.SCAN_INTERVAL_HOURS * 3600
    log.info("Scheduler running — every %dh", config.SCAN_INTERVAL_HOURS)
    while True:
        try:
            run_once(tickers)
        except Exception as e:
            log.error("Cycle error: %s", e, exc_info=True)
        log.info("Next scan in %dh", config.SCAN_INTERVAL_HOURS)
        time.sleep(interval)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Covered Call Scanner")
    p.add_argument("--once",   action="store_true", help="Run one scan and exit")
    p.add_argument("--ticker", nargs="+",           help="Scan specific ticker(s) only")
    p.add_argument("--ledger", action="store_true", help="Print trade summary")
    p.add_argument("--add",    action="store_true", help="Log a trade you placed")
    p.add_argument("--close",  metavar="ID",        help="Close a trade by ID")
    args = p.parse_args()

    if args.ledger:
        summary = ledger.full_summary()
        monthly = ledger.monthly_summary()
        print(json.dumps({"summary": summary, "monthly": monthly}, indent=2))
        trades = ledger.open_trades()
        if trades:
            print(f"\nOpen trades ({len(trades)}):")
            for t in trades:
                print(f"  {t['id']}  ${t['strike']} {t['right']}  exp {t['expiry']}  "
                      f"premium ${t['premium_collected']:.2f}")
        return

    if args.add:
        print("Log a new trade:")
        ticker    = input("  Ticker:     ").strip().upper()
        strike    = float(input("  Strike:     $"))
        expiry    = input("  Expiry (YYYY-MM-DD): ").strip()
        contracts = int(input("  Contracts:  "))
        premium   = float(input("  Total premium collected ($, e.g. 58.00): "))
        tid = ledger.add_trade(ticker, "call", strike, expiry, contracts, premium)
        print(f"  Saved as {tid}")
        return

    if args.close:
        cost = float(input(f"Cost to buy back {args.close} (0 if expired worthless): $"))
        t    = ledger.close_trade(args.close, cost)
        print(f"Closed {t['id']} — P&L: ${t['pnl']:+.2f}")
        return

    tickers = [t.upper() for t in args.ticker] if args.ticker else None

    if args.once:
        run_once(tickers)
    else:
        run_scheduler(tickers)


if __name__ == "__main__":
    main()
