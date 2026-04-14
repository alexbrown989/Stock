"""
Alpha-Harvest Wheel Strategy Bot — Main Orchestrator
=====================================================
Runs the full pipeline every SCAN_INTERVAL_HOURS (default 4 h):

  1.  Gamma Squeeze Lead scan (NVDA/TSLA → mid-caps)
  2.  Sentiment Entropy scan (P/C ratio + IV Rank)
  3.  Dark Pool heuristic scan
  4.  For each watchlist ticker:
        a. Options analyzer  (Greeks + IV Rank filter)
        b. Max Pain calculator
        c. Wheel simulation  (Monte Carlo + guardrails)
  5.  Golden Setups → Discord notification
  6.  Scan summary → Discord

Run:
    python main.py            # run once immediately, then schedule
    python main.py --once     # run once and exit (good for CI/cron)
    python main.py --ticker SOFI PLTR  # run only these tickers
    python main.py --ledger   # print ledger summary and exit

Environment variables (set in .env):
    DISCORD_WEBHOOK_URL
    UNUSUAL_WHALES_API_KEY   (optional)
    SENTIMENT_API_KEY        (optional)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

# ── Load .env before any module that reads env vars ───────────────────────────
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import config
import ledger as ledger_module
from scanner import dark_pool, gamma_squeeze, max_pain, sentiment
from scanner import exit_manager, tech_levels as tech_levels_mod
from scanner.options_analyzer import OptionContract, analyze_ticker
from simulation.wheel_sim import SimResult, run_batch, simulate

# Lazy import (only needed when DISCORD_WEBHOOK_URL is set)
try:
    from scripts import discord_notifier
    _discord_available = bool(os.environ.get("DISCORD_WEBHOOK_URL"))
except ImportError:
    _discord_available = False

# ── Logging setup ─────────────────────────────────────────────────────────────
os.makedirs(config.LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(config.LOG_DIR, "bot.log"),
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("orchestrator")


# ── Core Scan Cycle ───────────────────────────────────────────────────────────

def run_scan_cycle(tickers: list[str] | None = None) -> list[SimResult]:
    """
    Execute one full scan cycle.  Returns all SimResults (golden setups first).
    """
    tickers = tickers or config.WATCHLIST
    start_ts = time.time()
    log.info("=" * 60)
    log.info("Scan cycle started — %d tickers", len(tickers))
    log.info("=" * 60)

    # ── Step 1: Gamma Squeeze Leads ───────────────────────────────────────
    log.info("[1/4] Gamma Squeeze Lead scan …")
    try:
        gamma_signals = gamma_squeeze.scan(targets=tickers)
        alerted_leads = [s for s in gamma_signals if s.alert]
        log.info("  Gamma squeeze alerts: %d", len(alerted_leads))
        for sig in alerted_leads:
            log.info("  %s", sig.description)
    except Exception as exc:
        log.error("Gamma squeeze scan failed: %s", exc)
        gamma_signals = []

    # ── Step 2: Sentiment Entropy ─────────────────────────────────────────
    log.info("[2/4] Sentiment Entropy scan …")
    try:
        sentiment_signals = sentiment.scan(tickers)
        reversal_tickers  = [s.ticker for s in sentiment_signals if s.reversal_signal]
        log.info("  Reversal signals: %s", reversal_tickers or "None")
    except Exception as exc:
        log.error("Sentiment scan failed: %s", exc)
        sentiment_signals = []
    sentiment_map = {s.ticker: s for s in sentiment_signals}

    # ── Step 3: Dark Pool ─────────────────────────────────────────────────
    log.info("[3/4] Dark Pool scan …")
    try:
        dp_signals   = dark_pool.scan(tickers)
        dp_flagged   = [s.ticker for s in dp_signals if s.hidden_accumulation]
        log.info("  Hidden accumulation flags: %s", dp_flagged or "None")
    except Exception as exc:
        log.error("Dark pool scan failed: %s", exc)
        dp_signals = []
    dp_map = {s.ticker: s for s in dp_signals}

    # ── Step 4: Options + Simulation ─────────────────────────────────────
    log.info("[4/4] Options analysis + Monte Carlo simulation …")
    all_results: list[SimResult] = []

    for symbol in tickers:
        log.info("  Analyzing %s …", symbol)
        try:
            # Options chain
            contracts = analyze_ticker(symbol, mode="both")
            if not contracts:
                log.info("    %s: no contracts in DTE window", symbol)
                continue

            # Max Pain + Technical Levels (run in parallel context)
            mp     = max_pain.nearest(symbol)
            levels = tech_levels_mod.analyze(symbol)
            if levels.nearest_resistance:
                log.debug(
                    "  %s resistance: $%.2f (%.1f%% away)",
                    symbol, levels.nearest_resistance,
                    levels.distance_to_resistance_pct or 0,
                )

            # Run simulation on all candidate contracts
            sim_results = run_batch(contracts)

            # Attach supplemental context and notify for golden setups
            for result in sim_results:
                c = result.contract
                dp_sig  = dp_map.get(symbol)
                sen_sig = sentiment_map.get(symbol)
                gamma_sigs_for_ticker = [
                    s for s in gamma_signals if s.target_ticker == symbol
                ]

                if result.is_golden_setup:
                    log.info(
                        "  ✅ GOLDEN SETUP: %s %s $%.2f exp %s | "
                        "PoP=%.1f%% | IV Rank=%.1f%% | θ/Prem=%.2f%%/d",
                        symbol,
                        c.right.upper(),
                        c.strike,
                        c.expiry,
                        result.pop_monte_carlo,
                        c.iv_rank,
                        c.theta_premium_ratio * 100,
                    )
                    # Assess strike quality vs. technical resistance
                    tech_grade, tech_reason = levels.call_strike_assessment(c.strike)
                    if tech_grade in ("RISKY", "AVOID"):
                        log.warning("  ⚠ Tech level: %s — %s", tech_grade, tech_reason)

                    _notify_golden(
                        result,
                        gamma_sigs_for_ticker,
                        dp_sig,
                        sen_sig,
                        mp,
                    )

                elif result.defensive_roll_needed:
                    log.warning(
                        "  ⚠ DEFENSIVE ROLL: %s gamma=%.4f", symbol, c.gamma
                    )
                    if _discord_available:
                        discord_notifier.send_defensive_roll_alert(
                            ticker=symbol,
                            gamma=c.gamma,
                            current_strike=c.strike,
                        )

            all_results.extend(sim_results)

        except Exception as exc:
            log.error("  %s analysis failed: %s", symbol, exc, exc_info=True)

    # ── Scan summary ──────────────────────────────────────────────────────
    elapsed   = time.time() - start_ts
    golden    = [r for r in all_results if r.is_golden_setup]
    top_tick  = list(dict.fromkeys(r.contract.ticker for r in golden))

    log.info("=" * 60)
    log.info(
        "Scan complete in %.1fs | %d contracts | %d Golden Setups",
        elapsed, len(all_results), len(golden),
    )
    log.info("Golden tickers: %s", top_tick or "None")
    log.info("=" * 60)

    # Ledger monthly target progress
    try:
        progress = ledger_module.monthly_target()
        log.info(
            "Monthly target: $%.2f → $%.2f | Earned: $%.2f (%.1f%% complete) | On track: %s",
            progress["start_balance"],
            progress["target_balance"],
            progress["earned_this_month"],
            progress["pct_complete"],
            progress["on_track"],
        )
    except Exception as exc:
        log.warning("Ledger read failed: %s", exc)

    if _discord_available:
        discord_notifier.send_scan_summary(
            golden_count    = len(golden),
            total_scanned   = len(tickers),
            scan_duration_s = elapsed,
            top_tickers     = top_tick,
        )

    return all_results


# ── Position Monitor Cycle ────────────────────────────────────────────────────

def run_position_monitor() -> None:
    """
    Check all open ledger positions against the exit decision tree:
      - 50 % profit target → CLOSE
      - 2× credit loss     → CLOSE (stop-loss)
      - Delta breach >0.70 → URGENT ROLL
      - DTE < 7, untested  → ROLL
    Sends Discord alerts for any actionable signal.
    """
    signals = exit_manager.scan_open_positions()
    if not signals:
        log.info("Position monitor: no open trades.")
        return

    for sig in signals:
        icon = {"CLOSE": "💰", "ROLL": "🔄", "ROLL_UP": "🔄⬆",
                "URGENT_ROLL": "🚨", "HOLD": "✅"}.get(sig.action.value, "")
        log.info(
            "%s %s [%s] %s | P&L: $%.2f (%.1f%%)",
            icon, sig.ticker, sig.action.value, sig.reason,
            sig.pnl_if_closed, sig.pnl_pct,
        )
        if sig.action.value != "HOLD" and _discord_available:
            _notify_exit(sig)


def _notify_exit(sig) -> None:
    if not _discord_available:
        return
    try:
        now    = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        colour = {"CLOSE": 0x00FF00, "ROLL": 0x00BFFF,
                  "URGENT_ROLL": 0xFF4500, "ROLL_UP": 0xFF8C00}.get(sig.action.value, 0x808080)
        roll_text = ""
        if sig.roll_to_expiry:
            net_cr_str = f"Net credit: ${sig.roll_net_credit:.2f}" if sig.roll_net_credit is not None else ""
            roll_text = (
                f"\n**Roll to:** `{sig.roll_to_expiry}` strike `${sig.roll_to_strike}`  {net_cr_str}"
            )
        payload = {
            "username": "Alpha-Harvest Bot",
            "embeds": [{
                "title": f"{sig.action.value} SIGNAL — {sig.ticker} [{sig.trade_id}]",
                "description": (
                    f"{sig.reason}{roll_text}\n\n"
                    f"**P&L if closed now:** `${sig.pnl_if_closed:+.2f}` ({sig.pnl_pct:+.1f}%)\n"
                    f"**Current mid:** `${sig.current_mid:.2f}` vs entry `${sig.entry_credit:.2f}`\n"
                    f"**DTE remaining:** `{sig.current_dte}`"
                ),
                "color": colour,
                "footer": {"text": f"Alpha-Harvest v4.6 | {now}"},
            }],
        }
        import requests
        url = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if url:
            requests.post(url, json=payload, timeout=10)
    except Exception as exc:
        log.error("Exit notify failed: %s", exc)


def _notify_golden(
    result,
    gamma_sigs,
    dp_sig,
    sen_sig,
    mp,
) -> None:
    if not _discord_available:
        return
    try:
        discord_notifier.send_golden_setup(
            sim_result     = result,
            gamma_signals  = gamma_sigs,
            dark_pool_sig  = dp_sig,
            sentiment_sig  = sen_sig,
            max_pain_result= mp,
        )
    except Exception as exc:
        log.error("Discord notification failed: %s", exc)


# ── Scheduler ─────────────────────────────────────────────────────────────────

def run_scheduler(tickers: list[str] | None = None) -> None:
    """
    Run scan immediately, then repeat every SCAN_INTERVAL_HOURS.
    Ctrl-C to stop.
    """
    interval_s = config.SCAN_INTERVAL_HOURS * 3600
    log.info(
        "Scheduler started — interval: %dh  watchlist: %d tickers",
        config.SCAN_INTERVAL_HOURS,
        len(tickers or config.WATCHLIST),
    )
    while True:
        try:
            run_position_monitor()   # check exits first
            run_scan_cycle(tickers)  # then look for new entries
        except Exception as exc:
            log.error("Scan cycle error: %s", exc, exc_info=True)

        next_run = datetime.now().strftime("%Y-%m-%d %H:%M")
        log.info("Next scan in %dh (after %s + %dh)", config.SCAN_INTERVAL_HOURS, next_run, config.SCAN_INTERVAL_HOURS)
        time.sleep(interval_s)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Alpha-Harvest Wheel Strategy Bot"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one scan cycle and exit.",
    )
    parser.add_argument(
        "--ticker",
        nargs="+",
        metavar="SYMBOL",
        help="Limit scan to specific ticker(s).",
    )
    parser.add_argument(
        "--ledger",
        action="store_true",
        help="Print ledger summary and exit.",
    )
    parser.add_argument(
        "--add-trade",
        action="store_true",
        help="Interactively record a new trade in the ledger.",
    )
    parser.add_argument(
        "--close-trade",
        metavar="TRADE_ID",
        help="Close a trade by ID (prompts for close cost).",
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="Run one position-monitor cycle (check exits on open trades) and exit.",
    )
    args = parser.parse_args()

    if args.monitor:
        run_position_monitor()
        return

    if args.ledger:
        import json
        s = ledger_module.summary()
        print(json.dumps(s, indent=2, default=str))
        prog = ledger_module.monthly_target()
        print("\n── Monthly Progress ──────────────────────────────────")
        for k, v in prog.items():
            print(f"  {k:25s}: {v}")
        return

    if args.add_trade:
        print("── Add Trade ─────────────────────────────────────────")
        ticker  = input("Ticker: ").strip().upper()
        right   = input("right (call/put): ").strip().lower()
        strike  = float(input("Strike: "))
        expiry  = input("Expiry (YYYY-MM-DD): ").strip()
        dte     = int(input("DTE at entry: "))
        contr   = int(input("Contracts: "))
        prem    = float(input("Premium collected (per share, per contract): "))
        notes   = input("Notes (optional): ").strip()
        tid = ledger_module.add_trade(
            ticker=ticker, right=right, strike=strike, expiry=expiry,
            dte_at_entry=dte, contracts=contr, premium_collected=prem, notes=notes,
        )
        print(f"Trade added: {tid}")
        return

    if args.close_trade:
        cost = float(input(f"Close cost for {args.close_trade} (0 if expired worthless): "))
        notes = input("Notes (optional): ").strip()
        t = ledger_module.close_trade(args.close_trade, cost, notes=notes)
        print(f"Closed: {t['id']} | P&L: ${t['net_pnl']:.2f}")
        return

    tickers = [t.upper() for t in args.ticker] if args.ticker else None

    if args.once:
        run_scan_cycle(tickers)
    else:
        run_scheduler(tickers)


if __name__ == "__main__":
    main()
