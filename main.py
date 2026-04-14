"""
Covered Call Scanner — Entry Point
====================================
Usage:
  python main.py              # run now, then every 4 hours
  python main.py --once       # run once and exit
  python main.py --ticker SOFI PLTR   # scan specific tickers only
  python main.py --ledger     # print trade summary
  python main.py --add        # log a new trade
  python main.py --close ID   # close / mark expired a trade

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
import yfinance as yf
from scanner import scan_all

# ── Logging ────────────────────────────────────────────────────────────────────
os.makedirs(config.LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(config.LOG_DIR, "scanner.log"),
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("main")


# ── Scan cycle ─────────────────────────────────────────────────────────────────

def run_once(tickers: list[str] | None = None) -> None:
    log.info("─── scan started ──────────────────────────────────────")

    setups   = scan_all(tickers)
    ready    = [s for s in setups if s.tag == "READY"]
    watching = [s for s in setups if s.tag == "WATCH"]

    log.info("Results: %d ready, %d watching", len(ready), len(watching))
    for s in ready:
        log.info(
            "  ✅ %-6s CALL $%-6.2f  exp %s  Δ%.2f  IVR%.0f%%  $%.2f/sh  PoP%.0f%%  OTM+%.1f%%",
            s.ticker, s.strike, s.expiry, s.delta, s.iv_rank,
            s.premium, s.pop, s.otm_pct,
        )

    open_trades  = ledger.open_trades()
    current_mids = _fetch_current_mids(open_trades)
    _check_exits(open_trades, current_mids)

    monthly = ledger.monthly_summary()
    log.info(
        "Monthly: %.0f%% of target  ($%.0f / $%.0f earned)",
        monthly["pct_complete"],
        monthly["earned_this_month"],
        monthly["target_balance"] - monthly["start_balance"],
    )

    notifier.send_scan_report(setups, open_trades, monthly, current_mids)
    log.info("─── scan complete ─────────────────────────────────────")


def _fetch_current_mids(trades: list[dict]) -> dict[str, float]:
    """Fetch the current option mid price for each open trade."""
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
    """Fire a Discord alert if any position hits the 50% target or stop-loss."""
    for t in trades:
        mid = mids.get(t["id"])
        if mid is None:
            continue
        entry   = t["premium_collected"] / t["contracts"]
        pnl_pct = (entry - mid) / entry * 100
        pnl_dol = (entry - mid) * 100 * t["contracts"]

        if pnl_pct >= config.PROFIT_TARGET_PCT * 100:
            reason = f"50% profit target reached — buy back at ${mid:.2f}"
            log.info("💰 EXIT  %s — %s", t["id"], reason)
            notifier.send_exit_alert(t, reason, pnl_pct, pnl_dol)

        elif mid >= entry * config.STOP_LOSS_MULT:
            reason = f"Stop-loss — option now ${mid:.2f} ({mid/entry:.1f}× entry credit)"
            log.warning("🛑 STOP  %s — %s", t["id"], reason)
            notifier.send_exit_alert(t, reason, pnl_pct, pnl_dol)


# ── Scheduler ──────────────────────────────────────────────────────────────────

def run_scheduler(tickers: list[str] | None = None) -> None:
    interval = config.SCAN_INTERVAL_HOURS * 3600
    log.info("Scheduler started — every %dh", config.SCAN_INTERVAL_HOURS)
    while True:
        try:
            run_once(tickers)
        except Exception as e:
            log.error("Cycle error: %s", e, exc_info=True)
        log.info("Next scan in %dh", config.SCAN_INTERVAL_HOURS)
        time.sleep(interval)


# ── CLI ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Covered Call Scanner")
    p.add_argument("--once",   action="store_true", help="Run one scan and exit")
    p.add_argument("--ticker", nargs="+",           help="Scan specific ticker(s) only")
    p.add_argument("--ledger", action="store_true", help="Print trade summary")
    p.add_argument("--add",    action="store_true", help="Log a trade you placed")
    p.add_argument("--close",  metavar="ID",        help="Close/expire a trade by ID")
    args = p.parse_args()

    if args.ledger:
        data = {
            "summary": ledger.full_summary(),
            "monthly": ledger.monthly_summary(),
        }
        print(json.dumps(data, indent=2))
        for t in ledger.open_trades():
            print(f"  {t['id']}  ${t['strike']} {t['right']}  "
                  f"exp {t['expiry']}  collected ${t['premium_collected']:.2f}")
        return

    if args.add:
        print("Log a covered call you placed:")
        ticker    = input("  Ticker:                       ").strip().upper()
        strike    = float(input("  Strike:                    $"))
        expiry    = input("  Expiry (YYYY-MM-DD):          ").strip()
        contracts = int(input("  Contracts:                    "))
        premium   = float(input("  Total premium collected ($):  "))
        tid       = ledger.add_trade(ticker, "call", strike, expiry, contracts, premium)
        print(f"  Saved as {tid}")
        return

    if args.close:
        cost = float(input(f"Cost to buy back {args.close} (0 = expired worthless): $"))
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
