"""
Core Theta Harvest cycle engine.
Orchestrates scanning, filtering, and candidate ranking for the 14-day cycle.
"""

import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from config import (
    CYCLE_DAYS, MAX_CONCURRENT_POSITIONS,
    TH_PROFIT_TARGET_PCT, STOP_LOSS_PCT,
)
from scanner.universe import scan_universe
from scanner.liquidity import score_chain_liquidity
from scanner.sentiment import sentiment_profile
from scanner.macro import macro_environment
from analysis.iv_analysis import full_vol_profile
from strategy.trade_builder import build_short_put, build_put_spread, build_iron_condor
from tracking.positions import load_positions

logger = logging.getLogger(__name__)


def cycle_expiry_target() -> str:
    """
    Return the target expiration for the current cycle:
    next date that is CYCLE_DAYS + ~2 weeks out, rounded to a Friday.
    """
    target = datetime.today() + timedelta(days=CYCLE_DAYS + 14)
    # Roll to nearest Friday
    days_ahead = 4 - target.weekday()
    if days_ahead < 0:
        days_ahead += 7
    target += timedelta(days=days_ahead)
    return target.strftime("%Y-%m-%d")


def score_candidate(trade: Dict) -> float:
    """
    Score a trade candidate 0-100 based on quantitative quality.
    Higher = more favorable.
    """
    score = 0.0
    if not trade:
        return 0.0

    # Filter pass rate
    f = trade.get("filters", {})
    if f.get("passed"):
        score += 40
    elif f.get("score") and f.get("total"):
        score += 40 * (f["score"] / f["total"])

    # POP quality
    pop = trade.get("pop", 0)
    if pop:
        score += pop * 30  # 0-30 pts

    # Theta efficiency bonus
    theta = abs(trade.get("theta", 0))
    premium = trade.get("premium") or trade.get("net_credit") or 0
    if premium > 0:
        eff = theta / premium
        score += min(eff / 0.01, 15)  # up to 15 pts

    # Penalize high gamma risk
    gamma = abs(trade.get("gamma", 0))
    if gamma > 0.05:
        score -= 10
    elif gamma > 0.02:
        score -= 5

    return round(score, 2)


def run_daily_scan(structures: List[str] = None, verbose: bool = True) -> Dict:
    """
    Execute the full Theta Harvest daily scan.

    Returns:
      {
        'macro': macro environment dict,
        'candidates': list of ranked trade dicts,
        'top_trade': best candidate,
        'scan_time': datetime string,
      }
    """
    structures = structures or ["short_put", "put_spread"]
    results = {
        "scan_time": datetime.now().isoformat(),
        "macro": {},
        "candidates": [],
        "top_trade": None,
    }

    # --- Step 1: Macro context ---
    logger.info("=== Step 1: Macro Environment ===")
    macro = macro_environment()
    results["macro"] = macro
    if verbose:
        print(f"\n[MACRO] Regime: {macro['regime'].upper()} | Stress: {macro['stress_score']}/10")
        for note in macro["notes"]:
            print(f"  • {note}")

    # Abort if macro is catastrophically stressed — wait for normalization
    if macro["stress_score"] >= 9:
        logger.warning("Macro stress >= 9. Halting scan — unfavorable environment.")
        results["halt_reason"] = "Macro stress critical"
        return results

    # --- Step 2: Universe scan ---
    logger.info("=== Step 2: Universe Scan ===")
    universe = scan_universe()
    if not universe:
        results["halt_reason"] = "No liquid candidates found"
        return results
    if verbose:
        print(f"\n[UNIVERSE] {len(universe)} tickers passed liquidity gate")

    # --- Step 3: Per-ticker analysis & trade construction ---
    logger.info("=== Step 3: Trade Construction ===")
    active_positions = {p["ticker"] for p in load_positions()}
    remaining_slots  = MAX_CONCURRENT_POSITIONS - len(active_positions)

    if remaining_slots <= 0:
        if verbose:
            print(f"\n[POSITIONS] All {MAX_CONCURRENT_POSITIONS} slots filled — no new trades")
        results["halt_reason"] = "Max positions reached"
        return results

    all_trades = []
    for cand in universe:
        ticker = cand["ticker"]
        if ticker in active_positions:
            logger.debug(f"  {ticker}: already in portfolio, skipping")
            continue

        logger.debug(f"  Analyzing {ticker}...")
        try:
            # Vol profile
            vol = full_vol_profile(ticker)
            ivr  = vol.get("iv_rank")
            iv_hv = vol.get("iv_hv_ratio")

            # Basic liquidity score
            liq = score_chain_liquidity(ticker)
            liq_score = liq.get("liquidity_score", 0)
            if liq_score < 40:
                logger.debug(f"  {ticker}: liquidity score {liq_score} too low")
                continue

            # Sentiment
            sent = sentiment_profile(ticker)

            # Build trades based on requested structures
            for structure in structures:
                trade = None
                if structure == "short_put":
                    trade = build_short_put(ticker, expiry=None, ivr=ivr, iv_hv=iv_hv)
                elif structure == "put_spread":
                    trade = build_put_spread(ticker, expiry=None, ivr=ivr, iv_hv=iv_hv)
                elif structure == "iron_condor":
                    trade = build_iron_condor(ticker, expiry=None, ivr=ivr, iv_hv=iv_hv)

                if trade and trade.get("filters", {}).get("passed"):
                    trade["vol_profile"]  = vol
                    trade["liquidity"]    = liq_score
                    trade["sentiment"]    = sent.get("bias")
                    trade["score"]        = score_candidate(trade)
                    all_trades.append(trade)
                    logger.debug(f"  {ticker} {structure}: score={trade['score']}")

        except Exception as e:
            logger.warning(f"  {ticker}: error — {e}")
            continue

    # --- Step 4: Rank and select ---
    all_trades.sort(key=lambda t: t["score"], reverse=True)
    results["candidates"] = all_trades[:10]  # top 10
    results["top_trade"]  = all_trades[0] if all_trades else None

    if verbose:
        _print_summary(results)

    return results


def _print_summary(results: Dict) -> None:
    """Print formatted scan summary to stdout."""
    candidates = results.get("candidates", [])
    if not candidates:
        print("\n[RESULT] No qualifying trades found in this scan cycle.")
        return

    print(f"\n{'='*70}")
    print(f"  THETA HARVEST — DAILY SCAN RESULTS")
    print(f"  {results['scan_time'][:16]}")
    print(f"{'='*70}")
    print(f"  {'#':<3} {'Ticker':<7} {'Structure':<20} {'Strike':<9} {'Exp':<12} "
          f"{'Credit':<8} {'POP':<7} {'Score':<6}")
    print(f"  {'-'*68}")
    for i, t in enumerate(candidates[:5], 1):
        strike = t.get("strike") or t.get("strike_short") or t.get("put_short")
        credit = t.get("premium") or t.get("net_credit") or 0
        pop    = t.get("pop", 0)
        print(f"  {i:<3} {t['ticker']:<7} {t['structure']:<20} "
              f"{strike:<9} {t['expiration']:<12} "
              f"${credit:<7.2f} {pop:<7.1%} {t['score']:<6}")
    print(f"{'='*70}\n")

    top = results["top_trade"]
    if top:
        print(f"  TOP TRADE: {top['ticker']} | {top['structure'].upper()}")
        print(f"  Strike: {top.get('strike') or top.get('strike_short')} | "
              f"Exp: {top['expiration']} | "
              f"Credit: ${top.get('premium') or top.get('net_credit'):.2f}")
        print(f"  Delta: {top.get('delta','N/A')} | "
              f"Theta: {top.get('theta','N/A')} | "
              f"IV: {top.get('iv',0):.1%}")
        f = top.get("filters", {})
        print(f"  Filters: {f.get('score')}/{f.get('total')} passed")
        if f.get("failures"):
            for fail in f["failures"]:
                print(f"    ✗ {fail}")
        print()
