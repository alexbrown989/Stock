#!/usr/bin/env python3
"""
Theta Harvest — Main Pipeline
==============================
Runs the full daily scan cycle or individual sub-commands.

Usage:
  python pipeline.py scan                  # Run full daily scan
  python pipeline.py scan --structures short_put put_spread
  python pipeline.py positions             # Show open positions
  python pipeline.py performance           # Print performance report
  python pipeline.py exit-check            # Check open positions for exits
  python pipeline.py open <ticker>         # Manually record a new position
  python pipeline.py close <id> <price>    # Close a position
"""

import argparse
import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
# Quiet noisy third-party loggers
logging.getLogger("yfinance").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("peewee").setLevel(logging.ERROR)

logger = logging.getLogger("pipeline")


def cmd_scan(args) -> None:
    from strategy.theta_harvest import run_daily_scan
    structures = args.structures or ["short_put", "put_spread"]
    results = run_daily_scan(structures=structures, verbose=True)

    if args.json:
        # Sanitize for JSON output (remove DataFrames etc.)
        def _clean(obj):
            if hasattr(obj, "to_dict"):
                return obj.to_dict()
            if isinstance(obj, float) and (obj != obj):  # NaN
                return None
            return obj
        print(json.dumps(results, default=str, indent=2))


def cmd_positions(args) -> None:
    from tracking.positions import portfolio_summary
    summary = portfolio_summary()
    print(f"\n{'='*55}")
    print(f"  OPEN POSITIONS ({summary['open_positions']})")
    print(f"{'='*55}")
    for pos in summary["positions"]:
        print(f"  {pos['id']}")
        print(f"    {pos['structure']} | {pos['ticker']} | "
              f"Strike: {pos['strike']} | Exp: {pos['expiration']}")
        print(f"    Credit: ${pos['credit']:.2f} | "
              f"Delta: {pos.get('delta_entry','?')} | "
              f"IV: {pos.get('iv_entry',0):.1%}")
        print()
    print(f"  Total open credit : ${summary['total_open_credit']:.2f}")
    print(f"  Realized P&L      : ${summary['realized_pnl']:+.2f}")
    if summary["win_rate"] is not None:
        print(f"  Historical win rate: {summary['win_rate']:.1%}")
    print(f"{'='*55}\n")


def cmd_performance(args) -> None:
    from tracking.positions import load_positions
    from tracking.performance import print_performance_report
    positions = load_positions()
    print_performance_report(positions)


def cmd_exit_check(args) -> None:
    from tracking.positions import load_positions, check_exit_conditions
    positions = load_positions()
    to_close  = check_exit_conditions([p for p in positions if p["status"] == "open"])

    if not to_close:
        print("\n[EXIT CHECK] No positions currently meeting exit criteria.\n")
        return

    print(f"\n{'='*60}")
    print(f"  EXIT SIGNALS ({len(to_close)} positions)")
    print(f"{'='*60}")
    for pos in to_close:
        print(f"  {pos['id']}")
        print(f"    Signal    : {pos.get('exit_signal','unknown')}")
        print(f"    Credit    : ${pos.get('credit',0):.2f}")
        print(f"    Current   : ${pos.get('current_price',0):.2f}")
        print(f"    Est. P&L  : ${pos.get('unrealized_pnl',0):+.2f}")
        print()
    print(f"{'='*60}\n")


def cmd_open(args) -> None:
    """Manually record a new position from command line."""
    from tracking.positions import open_position
    trade = {
        "ticker":     args.ticker,
        "structure":  args.structure,
        "spot":       args.spot,
        "strike":     args.strike,
        "expiration": args.expiration,
        "dte":        args.dte,
        "premium":    args.credit,
        "delta":      args.delta,
        "iv":         args.iv,
    }
    pos = open_position(trade)
    print(f"\n[OPEN] Position recorded: {pos['id']}\n")


def cmd_close(args) -> None:
    from tracking.positions import close_position
    pos = close_position(args.id, args.price, args.reason or "manual")
    if pos:
        print(f"\n[CLOSE] {pos['id']} closed. P&L: ${pos['pnl']:+.2f}\n")
    else:
        print(f"\n[ERROR] Position '{args.id}' not found or already closed.\n")


def main():
    parser = argparse.ArgumentParser(description="Theta Harvest Options Pipeline")
    sub = parser.add_subparsers(dest="command")

    # scan
    p_scan = sub.add_parser("scan", help="Run daily scan")
    p_scan.add_argument("--structures", nargs="+",
                        choices=["short_put", "put_spread", "iron_condor"],
                        help="Trade structures to scan for")
    p_scan.add_argument("--json", action="store_true", help="Output raw JSON")
    p_scan.set_defaults(func=cmd_scan)

    # positions
    p_pos = sub.add_parser("positions", help="Show open portfolio")
    p_pos.set_defaults(func=cmd_positions)

    # performance
    p_perf = sub.add_parser("performance", help="Print performance report")
    p_perf.set_defaults(func=cmd_performance)

    # exit-check
    p_exit = sub.add_parser("exit-check", help="Check positions for exits")
    p_exit.set_defaults(func=cmd_exit_check)

    # open
    p_open = sub.add_parser("open", help="Record a new position")
    p_open.add_argument("ticker")
    p_open.add_argument("--structure", default="short_put")
    p_open.add_argument("--spot", type=float, required=True)
    p_open.add_argument("--strike", type=float, required=True)
    p_open.add_argument("--expiration", required=True)
    p_open.add_argument("--dte", type=int)
    p_open.add_argument("--credit", type=float, required=True)
    p_open.add_argument("--delta", type=float)
    p_open.add_argument("--iv", type=float)
    p_open.set_defaults(func=cmd_open)

    # close
    p_close = sub.add_parser("close", help="Close a position")
    p_close.add_argument("id", help="Position ID")
    p_close.add_argument("price", type=float, help="Closing price (debit paid)")
    p_close.add_argument("--reason", help="Close reason")
    p_close.set_defaults(func=cmd_close)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
