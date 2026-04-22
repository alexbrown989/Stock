"""
Performance analytics and feedback loop.
Analyzes closed trade history to surface patterns and drive adaptive adjustments.
"""

import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional

from config import PERFORMANCE_FILE

logger = logging.getLogger(__name__)


def load_performance_log() -> List[Dict]:
    """Load historical performance records."""
    if not os.path.exists(PERFORMANCE_FILE):
        return []
    try:
        with open(PERFORMANCE_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def record_cycle_performance(cycle_data: Dict) -> None:
    """Append a cycle performance snapshot to the log."""
    log = load_performance_log()
    cycle_data["recorded_at"] = datetime.now().isoformat()
    log.append(cycle_data)
    try:
        with open(PERFORMANCE_FILE, "w") as f:
            json.dump(log, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to write performance log: {e}")


def analyze_closed_trades(positions: List[Dict]) -> Dict:
    """
    Comprehensive analysis of closed trade history.
    Returns performance metrics and adaptive recommendations.
    """
    closed = [p for p in positions if p.get("status") == "closed" and p.get("pnl") is not None]
    if not closed:
        return {"message": "No closed trades to analyze"}

    pnls        = [p["pnl"] for p in closed]
    wins        = [p for p in closed if p["pnl"] > 0]
    losses      = [p for p in closed if p["pnl"] <= 0]
    total_pnl   = sum(pnls)
    win_rate    = len(wins) / len(closed)
    avg_win     = sum(p["pnl"] for p in wins) / len(wins) if wins else 0
    avg_loss    = sum(p["pnl"] for p in losses) / len(losses) if losses else 0
    profit_factor = abs(avg_win * len(wins) / (avg_loss * len(losses))) if losses and avg_loss != 0 else float("inf")

    # Per-structure breakdown
    structure_stats: Dict[str, List] = defaultdict(list)
    for p in closed:
        structure_stats[p.get("structure", "unknown")].append(p["pnl"])

    structure_summary = {}
    for struct, pnl_list in structure_stats.items():
        w = sum(1 for x in pnl_list if x > 0)
        structure_summary[struct] = {
            "count":     len(pnl_list),
            "win_rate":  round(w / len(pnl_list), 3),
            "total_pnl": round(sum(pnl_list), 2),
            "avg_pnl":   round(sum(pnl_list) / len(pnl_list), 2),
        }

    # Per-ticker breakdown
    ticker_stats: Dict[str, List] = defaultdict(list)
    for p in closed:
        ticker_stats[p["ticker"]].append(p["pnl"])

    # Close reason analysis
    reason_stats: Dict[str, int] = defaultdict(int)
    for p in closed:
        reason_stats[p.get("close_reason", "unknown")] += 1

    # --- Adaptive recommendations ---
    recommendations = []

    if win_rate < 0.55:
        recommendations.append(
            "Win rate below 55% — consider raising DELTA_MIN to select further OTM strikes"
        )
    if profit_factor < 1.2:
        recommendations.append(
            "Profit factor < 1.2 — review stop loss placement or reduce spread width"
        )
    for struct, stats in structure_summary.items():
        if stats["count"] >= 5 and stats["win_rate"] < 0.50:
            recommendations.append(
                f"Structure '{struct}' underperforming (WR={stats['win_rate']:.0%}) — "
                f"reduce allocation or remove from rotation"
            )
    for ticker, pnl_list in ticker_stats.items():
        if len(pnl_list) >= 3 and sum(pnl_list) < 0:
            recommendations.append(
                f"{ticker} has cumulative loss — review IV regime before re-entry"
            )

    stop_losses = reason_stats.get("stop_loss", 0)
    if stop_losses > len(closed) * 0.25:
        recommendations.append(
            f"Stop losses triggered on {stop_losses}/{len(closed)} trades — "
            f"consider tightening gamma filter or reducing position sizing"
        )

    if not recommendations:
        recommendations.append("System performing within expected parameters — maintain current settings")

    return {
        "total_trades":     len(closed),
        "win_rate":         round(win_rate, 3),
        "total_pnl":        round(total_pnl, 2),
        "avg_win":          round(avg_win, 2),
        "avg_loss":         round(avg_loss, 2),
        "profit_factor":    round(profit_factor, 3),
        "by_structure":     structure_summary,
        "close_reasons":    dict(reason_stats),
        "recommendations":  recommendations,
    }


def print_performance_report(positions: List[Dict]) -> None:
    """Print a formatted performance report to stdout."""
    report = analyze_closed_trades(positions)
    if "message" in report:
        print(f"\n[PERFORMANCE] {report['message']}")
        return

    print(f"\n{'='*60}")
    print(f"  THETA HARVEST — PERFORMANCE REPORT")
    print(f"{'='*60}")
    print(f"  Trades analyzed : {report['total_trades']}")
    print(f"  Win rate        : {report['win_rate']:.1%}")
    print(f"  Total P&L       : ${report['total_pnl']:+.2f}")
    print(f"  Avg win         : ${report['avg_win']:+.2f}")
    print(f"  Avg loss        : ${report['avg_loss']:+.2f}")
    print(f"  Profit factor   : {report['profit_factor']:.2f}x")

    if report.get("by_structure"):
        print(f"\n  By Structure:")
        for struct, stats in report["by_structure"].items():
            print(f"    {struct:<22} WR={stats['win_rate']:.0%}  "
                  f"P&L=${stats['total_pnl']:+.2f}  n={stats['count']}")

    print(f"\n  Adaptive Recommendations:")
    for rec in report["recommendations"]:
        print(f"    • {rec}")
    print(f"{'='*60}\n")
