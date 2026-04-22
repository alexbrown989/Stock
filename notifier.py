"""
Discord Notifier
================
Sends a clean scan report each cycle.

Structure (single POST):
  [1]     Header       — timestamp + quick stats
  [2..7]  READY plays  — one focused embed per setup (max 6)
  [8]     On Watch     — compact one-liner per WATCH candidate
  [9]     Positions    — open trades + exit signals (omitted if none)

Run  python notifier.py --test  to verify the webhook works.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from typing import Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

log = logging.getLogger(__name__)

GREEN  = 0x57F287
BLUE   = 0x5865F2
GOLD   = 0xFFD700
RED    = 0xED4245
GREY   = 0x2B2D31


# ── Internal helpers ───────────────────────────────────────────────────────────

def _webhook() -> str:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        raise EnvironmentError("DISCORD_WEBHOOK_URL not set — add it to your .env file.")
    return url


def _post(payload: dict) -> bool:
    try:
        r = requests.post(_webhook(), json=payload, timeout=10)
        ok = r.status_code in (200, 204)
        if not ok:
            log.error("Discord %d: %s", r.status_code, r.text[:200])
        return ok
    except Exception as e:
        log.error("Discord post failed: %s", e)
        return False


def _f(name: str, value: str, inline: bool = True) -> dict:
    return {"name": name, "value": value, "inline": inline}


# ── READY setup embed ──────────────────────────────────────────────────────────

def _setup_embed(s) -> dict:
    badges = []
    if s.resistance_aligned:
        badges.append("🎯 Resistance-aligned")
    if s.in_max_pain_zone:
        badges.append("📍 Max-pain zone")

    title = f"✅  {s.ticker}  CALL ${s.strike}  —  {s.expiry[5:]}  ({s.dte}d)"

    desc_parts = []
    if badges:
        desc_parts.append("  ".join(badges))
    if s.nearest_resistance:
        dist = (s.nearest_resistance - s.underlying) / s.underlying * 100
        desc_parts.append(f"Nearest resistance **${s.nearest_resistance:.2f}** (+{dist:.1f}%)")
    desc = "\n".join(desc_parts) if desc_parts else None

    fields = [
        _f("Premium",        f"**${s.premium:.2f}**/sh  •  **${s.premium_contract:.0f}**/contract"),
        _f("Delta / PoP",    f"`{s.delta:.2f}`  •  `{s.pop:.0f}%` PoP"),
        _f("IV Rank",        f"`{s.iv_rank:.0f}%`  (IV {s.iv*100:.0f}%)"),
        _f("OTM / Break-even", f"+{s.otm_pct:.1f}%  •  BE **${s.break_even:.2f}**"),
        _f("Theta / day",    f"**${s.theta_day:.2f}**"),
        _f("Ann. Yield",     f"**{s.annual_yield:.1f}%**"),
        _f("Liquidity",      f"OI `{s.open_interest:,}`  •  Spread `{s.spread_pct:.0f}%`", inline=False),
    ]

    embed: dict = {"title": title, "color": GREEN, "fields": fields}
    if desc:
        embed["description"] = desc
    return embed


# ── On-Watch summary embed ─────────────────────────────────────────────────────

def _watch_embed(watches: list) -> dict:
    lines = []
    for s in watches[:12]:
        marker = "🎯" if s.resistance_aligned else "  "
        lines.append(
            f"{marker} `{s.ticker:<5}` `${s.strike:<6.2f}` {s.expiry[5:]} ({s.dte}d)  "
            f"Δ`{s.delta:.2f}` IVR`{s.iv_rank:.0f}%`  `${s.premium:.2f}/sh`  PoP`{s.pop:.0f}%`"
        )
    extra = f"\n*…and {len(watches) - 12} more*" if len(watches) > 12 else ""
    return {
        "title":       f"👀  On Watch — {len(watches)} setup{'s' if len(watches) != 1 else ''}",
        "description": "\n".join(lines) + extra or "Nothing on watch.",
        "color":       GOLD,
    }


# ── Open positions embed ───────────────────────────────────────────────────────

def _positions_embed(trades: list[dict], mids: dict[str, float]) -> dict:
    lines = []
    for t in trades:
        mid   = mids.get(t["id"])
        entry = t["premium_collected"] / t["contracts"]
        if mid is not None and mid > 0:
            pnl_pct = (entry - mid) / entry * 100
            pnl_dol = (entry - mid) * 100 * t["contracts"]
            if pnl_pct >= config.PROFIT_TARGET_PCT * 100:
                icon, note = "💰", "  **→ CLOSE (50% hit)**"
            elif mid >= entry * config.STOP_LOSS_MULT:
                icon, note = "🛑", "  **→ STOP-LOSS**"
            else:
                icon, note = ("🟢" if pnl_pct > 0 else "🔴"), ""
            lines.append(
                f"{icon} **{t['ticker']}** `${t['strike']}` CALL  exp `{t['expiry']}`\n"
                f"> Entry `${entry:.2f}`  Now `${mid:.2f}`  P&L `{pnl_pct:+.0f}%` (`${pnl_dol:+.0f}`){note}"
            )
        else:
            lines.append(
                f"⚪ **{t['ticker']}** `${t['strike']}` CALL  exp `{t['expiry']}`\n"
                f"> Entry `${entry:.2f}`  —  price unavailable"
            )
    return {
        "title":       f"📋  Open Positions ({len(trades)})",
        "description": "\n\n".join(lines) or "No open positions.",
        "color":       BLUE,
    }


# ── Main send function ─────────────────────────────────────────────────────────

def send_scan_report(
    setups:      list,
    open_trades: list[dict],
    mids:        dict[str, float] | None = None,
) -> bool:
    now   = datetime.utcnow().strftime("%a %d %b  %H:%M UTC")
    ready = [s for s in setups if s.tag == "READY"]
    watch = [s for s in setups if s.tag == "WATCH"]
    mids  = mids or {}

    embeds: list[dict] = []

    # Header
    scanned = len(set(s.ticker for s in setups)) if setups else len(config.WATCHLIST)
    ready_label = f"**{len(ready)} plays ready**" if ready else "no plays ready"
    embeds.append({
        "title":       f"📡  Alpha-Harvest  •  {now}",
        "description": (
            f"{scanned} tickers scanned  •  {ready_label}  •  "
            f"{len(watch)} on watch  •  {len(open_trades)} position(s) open"
        ),
        "color": BLUE,
    })

    # READY setups
    if ready:
        for s in ready[:6]:
            embeds.append(_setup_embed(s))
        if len(ready) > 6:
            names = ", ".join(s.ticker for s in ready[6:])
            embeds.append({
                "description": f"*Also READY (not shown): {names}*",
                "color": GREY,
            })
    else:
        embeds.append({
            "title":       "No plays this cycle",
            "description": "Nothing passed all filters. Watching for setups.",
            "color":       GREY,
        })

    # On Watch
    if watch:
        embeds.append(_watch_embed(watch))

    # Open positions (only if any)
    if open_trades:
        embeds.append(_positions_embed(open_trades, mids))

    return _post({"username": "Alpha-Harvest", "embeds": embeds[:10]})


def send_exit_alert(trade: dict, reason: str, pnl_pct: float, pnl_dol: float) -> bool:
    icon  = "💰" if pnl_pct >= 0 else "🛑"
    color = GREEN if pnl_pct >= 0 else RED
    return _post({
        "username": "Alpha-Harvest",
        "embeds": [{
            "title":       f"{icon}  Exit Signal — {trade['ticker']} ${trade['strike']} CALL",
            "description": (
                f"{reason}\n\n"
                f"**P&L:** `{pnl_pct:+.0f}%`  (`${pnl_dol:+.0f}`)\n"
                f"**Expiry:** `{trade['expiry']}`"
            ),
            "color":  color,
            "footer": {"text": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")},
        }],
    })


# ── CLI test ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    logging.basicConfig(level=logging.INFO)
    ok = _post({
        "username": "Alpha-Harvest",
        "embeds": [{
            "title":       "✅  Webhook test",
            "description": "Alpha-Harvest CC Scanner is connected.",
            "color":       GREEN,
        }],
    })
    print("✅ Sent!" if ok else "❌ Failed — check DISCORD_WEBHOOK_URL in .env")
