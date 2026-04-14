"""
Discord Notifier
================
Sends one clean report per scan cycle.

Report layout:
  ✅ READY TO TRADE   — setups that pass every filter
  👀 WATCHING         — setups that miss the max-pain zone but are otherwise good
  📋 OPEN POSITIONS   — current trades from ledger + exit signals
  📊 MONTHLY PROGRESS — running total vs 5% target

Set DISCORD_WEBHOOK_URL in your .env file.
Test with:  python notifier.py --test
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

log = logging.getLogger(__name__)

GOLD  = 0xFFD700
BLUE  = 0x5865F2
GREEN = 0x57F287
RED   = 0xED4245
GREY  = 0x99AAB5


def _webhook() -> str:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        raise EnvironmentError(
            "DISCORD_WEBHOOK_URL not set — add it to your .env file."
        )
    return url


def _post(payload: dict) -> bool:
    try:
        r = requests.post(_webhook(), json=payload, timeout=10)
        return r.status_code in (200, 204)
    except Exception as e:
        log.error("Discord post failed: %s", e)
        return False


# ── Report builders ───────────────────────────────────────────────────────────

def _fmt_setup(s) -> str:
    """One-liner per setup candidate."""
    zone = "✅" if s.in_safe_zone else ("⚠️" if s.in_safe_zone is False else "")
    mp   = f"  Max pain ${s.max_pain:.2f} {zone}" if s.max_pain else ""
    return (
        f"**{s.ticker}** — `${s.strike}` call  exp `{s.expiry}` ({s.dte}DTE)\n"
        f"> Δ`{s.delta:.2f}`  IV Rank `{s.iv_rank:.0f}%`  Premium `${s.premium:.2f}` (${s.premium_contract:.0f}/contract)\n"
        f"> Break-even `${s.break_even:.2f}`  PoP `{s.pop:.0f}%`  Ann yield `{s.annual_yield:.1f}%/yr`{mp}"
    )


def _fmt_position(p: dict, current_mid: float | None = None) -> str:
    """One-liner for an open ledger position."""
    entry   = p["premium_collected"] / p["contracts"]   # per-share entry credit
    lines   = [
        f"**{p['ticker']}** — `${p['strike']}` {p['right'].upper()}  exp `{p['expiry']}`"
    ]
    if current_mid is not None and current_mid > 0:
        pnl_pct = (entry - current_mid) / entry * 100
        pnl_dol = (entry - current_mid) * 100 * p["contracts"]
        icon    = "💰" if pnl_pct >= 50 else ("🟡" if pnl_pct >= 25 else "🔴")
        action  = "  **→ CLOSE (50% target hit)**" if pnl_pct >= config.PROFIT_TARGET_PCT * 100 else ""
        lines.append(
            f"> Entry `${entry:.2f}`  Current `${current_mid:.2f}`  "
            f"P&L `{pnl_pct:+.0f}%` (${pnl_dol:+.0f}) {icon}{action}"
        )
    else:
        lines.append(f"> Entry `${entry:.2f}`  —  price unavailable")
    return "\n".join(lines)


def send_scan_report(
    setups:       list,          # list[SetupCandidate]
    open_trades:  list[dict],
    monthly:      dict,
    current_mids: dict[str, float] | None = None,  # trade_id → current mid
) -> bool:
    """
    Send the full scan report to Discord.
    Splits into multiple embeds if there are many setups.
    """
    now    = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    ready  = [s for s in setups if s.tag == "READY"]
    watch  = [s for s in setups if s.tag == "WATCH"]

    embeds = []

    # ── 1. Header embed ───────────────────────────────────────────────────
    total    = len(config.WATCHLIST)
    header   = (
        f"Scanned **{total}** tickers  •  "
        f"**{len(ready)}** ready  •  **{len(watch)}** watching"
    )
    embeds.append({
        "title":       f"📡 CC Scanner — {now}",
        "description": header,
        "color":       BLUE,
    })

    # ── 2. Ready to trade ─────────────────────────────────────────────────
    if ready:
        # Cap at 5 per message to keep it readable
        chunks = ready[:5]
        body   = "\n\n".join(_fmt_setup(s) for s in chunks)
        if len(ready) > 5:
            body += f"\n\n*…and {len(ready) - 5} more. Run `--once` to see all.*"
        embeds.append({
            "title":       f"✅ Ready to Trade ({len(ready)})",
            "description": body,
            "color":       GREEN,
        })
    else:
        embeds.append({
            "title":       "✅ Ready to Trade",
            "description": "No setups passed all filters this cycle.",
            "color":       GREY,
        })

    # ── 3. Watching ───────────────────────────────────────────────────────
    if watch:
        chunks = watch[:3]
        body   = "\n\n".join(_fmt_setup(s) for s in chunks)
        if len(watch) > 3:
            body += f"\n\n*…and {len(watch) - 3} more.*"
        embeds.append({
            "title":       f"👀 Watching ({len(watch)}) — out of max-pain zone",
            "description": body,
            "color":       GOLD,
        })

    # ── 4. Open positions ─────────────────────────────────────────────────
    if open_trades:
        mids = current_mids or {}
        body = "\n\n".join(
            _fmt_position(t, mids.get(t["id"])) for t in open_trades
        )
        embeds.append({
            "title":       f"📋 Open Positions ({len(open_trades)})",
            "description": body,
            "color":       BLUE,
        })

    # ── 5. Monthly progress ───────────────────────────────────────────────
    pct_done = monthly.get("pct_complete", 0)
    bar_filled = int(pct_done / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    status = "✅ On track" if monthly.get("on_track") else "⚠️ Behind target"
    body = (
        f"**{monthly.get('month', '—')}**\n"
        f"`{bar}` {pct_done:.0f}%\n"
        f"Start `${monthly.get('start_balance', 0):,.0f}` → "
        f"Now `${monthly.get('current_balance', 0):,.0f}` / "
        f"Target `${monthly.get('target_balance', 0):,.0f}`\n"
        f"Earned `${monthly.get('earned_this_month', 0):,.0f}` of "
        f"`${monthly.get('still_needed', 0) + monthly.get('earned_this_month', 0):,.0f}` needed  {status}"
    )
    embeds.append({
        "title": "📊 Monthly Progress",
        "description": body,
        "color": GREEN if monthly.get("on_track") else RED,
        "footer": {"text": "Alpha-Harvest CC Bot"},
    })

    # Discord allows max 10 embeds per message
    payload = {"username": "CC Scanner", "embeds": embeds[:10]}
    return _post(payload)


def send_exit_alert(trade: dict, reason: str, pnl_pct: float, pnl_dol: float) -> bool:
    """Fire a standalone alert when a position hits 50% target or stop-loss."""
    icon  = "💰" if pnl_pct >= 0 else "🛑"
    color = GREEN if pnl_pct >= 0 else RED
    payload = {
        "username": "CC Scanner",
        "embeds": [{
            "title": f"{icon} Exit Signal — {trade['ticker']} ${trade['strike']} {trade['right'].upper()}",
            "description": (
                f"{reason}\n\n"
                f"**P&L:** `{pnl_pct:+.0f}%` (${pnl_dol:+.0f})\n"
                f"**Expiry:** `{trade['expiry']}`"
            ),
            "color": color,
            "footer": {"text": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")},
        }],
    }
    return _post(payload)


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    logging.basicConfig(level=logging.INFO)

    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true", help="Send a test message")
    args = p.parse_args()

    if args.test:
        ok = _post({
            "username": "CC Scanner",
            "embeds": [{
                "title": "✅ Webhook test",
                "description": "CC Scanner is connected and working.",
                "color": GREEN,
            }],
        })
        print("Sent!" if ok else "Failed — check DISCORD_WEBHOOK_URL in .env")
