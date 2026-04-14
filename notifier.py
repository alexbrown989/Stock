"""
Discord Notifier
================
Sends one clean, information-dense report per scan cycle.

Report structure (single POST, up to 10 embeds):
  [1] Header          — scan stats, timestamp
  [2..N] Ready setups — one full embed per READY candidate (max 6)
  [N+1]  Watching     — grouped, concise list of WATCH candidates
  [N+2]  Positions    — open trades + exit signals
  [N+3]  Monthly      — progress bar toward 5 % target

Each READY embed contains:
  • Strike, expiry, DTE, underlying price
  • Premium, Max Profit, Margin of Safety
  • Delta, IV Rank, Theta/day, PoP, Break-even
  • Annual yield, OI, volume, bid/ask spread
  • S/R levels chart (ASCII bar chart in a code block)
  • Resistance alignment flag
  • Max Pain zone flag

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

# Discord embed colours
GOLD  = 0xFFD700
GREEN = 0x57F287
BLUE  = 0x5865F2
RED   = 0xED4245
GREY  = 0x99AAB5


# ── Helpers ────────────────────────────────────────────────────────────────────

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
        ok = r.status_code in (200, 204)
        if not ok:
            log.error("Discord error %d: %s", r.status_code, r.text[:200])
        return ok
    except Exception as e:
        log.error("Discord post failed: %s", e)
        return False


def _field(name: str, value: str, inline: bool = True) -> dict:
    return {"name": name, "value": value, "inline": inline}


# ── Level chart ────────────────────────────────────────────────────────────────

def _levels_chart(s) -> str:
    """
    Build a compact ASCII chart for the Discord code block showing S1, S2, R1, R2.

    Example:
      SOFI @ $7.09
      ─────────────────────────────────
      R2  $ 8.50  ████████  ×5 ★★★
      R1  $ 8.00  ██████░░  ×4 ★★★  ← strike
          ──── $7.09 current ────
      S1  $ 6.50  █████░░░  ×3 ★★★
      S2  $ 5.80  ████████  ×6 ★★★
    """
    lvls     = s.lvls
    current  = s.underlying
    all_lvls = lvls.all_levels()
    if not all_lvls:
        return f"{s.ticker} @ ${current:.2f}\nNo levels found."

    max_visits = max(l.visits for _, l in all_lvls) if all_lvls else 1

    lines = [f"{s.ticker} @ ${current:.2f}", "─" * 38]
    for name, lvl in all_lvls:
        bar      = lvl.bar(max_visits)
        label    = lvl.label()
        is_curr  = (name == "S1" and lvl.price >= current) or (name == "R1" and lvl.price <= current)
        strike_marker = "  ← strike" if abs(lvl.price - s.strike) / s.strike < 0.01 else ""

        if name == "R1":
            # Insert the current price divider above R1 if R1 > current
            if lvl.price > current:
                lines.append(f"    ──── ${current:.2f} current ────")

        lines.append(
            f"{name}  ${lvl.price:>6.2f}  {bar}  ×{lvl.visits} {label}{strike_marker}"
        )

        if name == "R1" and lvl.price <= current:
            lines.append(f"    ──── ${current:.2f} current ────")

    return "\n".join(lines)


# ── Per-setup embed ─────────────────────────────────────────────────────────────

def _setup_embed(s) -> dict:
    """Build one rich embed for a single SetupCandidate."""
    # Title line
    resist_badge = "🎯 Resistance-aligned" if s.resistance_aligned else ""
    mp_badge     = "📍 Max-pain zone"      if s.in_max_pain_zone   else ""
    badges       = "  ".join(b for b in [resist_badge, mp_badge] if b)

    title = (
        f"✅ {s.ticker}  CALL ${s.strike}  "
        f"{s.expiry[5:]}  ({s.dte}DTE)"
    )

    # Levels chart in a code block
    chart = _levels_chart(s)
    desc  = f"```\n{chart}\n```"
    if badges:
        desc = f"{badges}\n{desc}"

    # Inline fields — 3 per row
    fields = [
        _field("💰 Premium",          f"${s.premium:.2f}/sh  •  ${s.premium_contract:.0f}/contract"),
        _field("📈 Max Profit",        f"${s.max_profit:.0f}  (if expires worthless)"),
        _field("🛡 Margin of Safety",  f"+{s.otm_pct:.1f}% OTM"),

        _field("Δ Delta",              f"{s.delta:.3f}"),
        _field("📊 IV Rank",           f"{s.iv_rank:.0f}%  (IV {s.iv*100:.0f}%)"),
        _field("⏱ Theta / day",        f"${s.theta_day:.2f}"),

        _field("🎯 PoP",               f"{s.pop:.0f}%"),
        _field("🔑 Break-even",        f"${s.break_even:.2f}  (−${s.underlying - s.break_even:.2f} buffer)"),
        _field("📅 Annual Yield",      f"{s.annual_yield:.1f}%"),

        _field("📋 Open Interest",     f"{s.open_interest:,}"),
        _field("📦 Volume",            f"{s.volume:,}"),
        _field("↔ Bid / Ask Spread",  f"${s.bid:.2f} / ${s.ask:.2f}  ({s.spread_pct:.0f}%)"),
    ]

    # Nearest resistance note (always useful even if not aligned)
    if s.nearest_resistance:
        dist = round((s.nearest_resistance - s.underlying) / s.underlying * 100, 1)
        fields.append(_field(
            "🚧 Nearest Resistance",
            f"${s.nearest_resistance:.2f}  (+{dist:.1f}% from price)",
            inline=False,
        ))

    return {
        "title":       title,
        "description": desc,
        "color":       GREEN,
        "fields":      fields,
    }


# ── Grouped WATCH embed ────────────────────────────────────────────────────────

def _watch_embed(watches: list) -> dict:
    """Compact list of WATCH candidates — no full breakdown."""
    lines = []
    for s in watches[:8]:
        r_note = "🎯" if s.resistance_aligned else ""
        lines.append(
            f"**{s.ticker}** `${s.strike}` {s.expiry[5:]} ({s.dte}DTE)  "
            f"Δ`{s.delta:.2f}` IVR`{s.iv_rank:.0f}%` "
            f"`${s.premium:.2f}`/sh `{s.pop:.0f}%`PoP {r_note}"
        )
    extra = f"\n*…and {len(watches) - 8} more*" if len(watches) > 8 else ""
    return {
        "title":       f"👀 Watching ({len(watches)}) — not resistance-aligned yet",
        "description": "\n".join(lines) + extra,
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
                icon, note = "💰", " **→ CLOSE** (50% hit)"
            elif mid >= entry * config.STOP_LOSS_MULT:
                icon, note = "🛑", " **→ STOP-LOSS**"
            else:
                icon, note = "🟢" if pnl_pct > 0 else "🔴", ""
            lines.append(
                f"{icon} **{t['ticker']}** `${t['strike']}` CALL  exp `{t['expiry']}`\n"
                f"> Entry `${entry:.2f}`  Now `${mid:.2f}`  "
                f"P&L `{pnl_pct:+.0f}%` (`${pnl_dol:+.0f}`){note}"
            )
        else:
            lines.append(
                f"⚪ **{t['ticker']}** `${t['strike']}` CALL  exp `{t['expiry']}`\n"
                f"> Entry `${entry:.2f}`  —  price unavailable"
            )
    return {
        "title":       f"📋 Open Positions ({len(trades)})",
        "description": "\n\n".join(lines) or "No open positions.",
        "color":       BLUE,
    }


# ── Monthly progress embed ─────────────────────────────────────────────────────

def _monthly_embed(m: dict) -> dict:
    pct   = max(0.0, m.get("pct_complete", 0))
    filled = min(10, int(pct / 10))
    bar    = "█" * filled + "░" * (10 - filled)
    status = "✅ On track" if m.get("on_track") else "⚠️ Behind target"
    desc = (
        f"`{bar}` **{pct:.0f}%**\n\n"
        f"Balance  `${m.get('current_balance', 0):,.0f}`\n"
        f"Target   `${m.get('target_balance', 0):,.0f}`\n"
        f"Earned   `${m.get('earned_this_month', 0):,.0f}` this month\n"
        f"Still need  `${max(0, m.get('still_needed', 0)):,.0f}`\n\n"
        f"{status}"
    )
    return {
        "title":       f"📊 {m.get('month', '—')} — Monthly Target ({config.MONTHLY_TARGET_PCT:.0f}%)",
        "description": desc,
        "color":       GREEN if m.get("on_track") else RED,
        "footer":      {"text": "Alpha-Harvest CC Scanner"},
    }


# ── Main send function ─────────────────────────────────────────────────────────

def send_scan_report(
    setups:      list,
    open_trades: list[dict],
    monthly:     dict,
    mids:        dict[str, float] | None = None,
) -> bool:
    """
    Compose and POST the full scan report.
    All sections fit into a single Discord API call (≤10 embeds).
    """
    now   = datetime.utcnow().strftime("%a %d %b %Y  %H:%M UTC")
    ready = [s for s in setups if s.tag == "READY"]
    watch = [s for s in setups if s.tag == "WATCH"]
    mids  = mids or {}

    embeds: list[dict] = []

    # ── [1] Header ─────────────────────────────────────────────────────────
    tickers_scanned = len(set(s.ticker for s in setups)) if setups else len(config.WATCHLIST)
    embeds.append({
        "title":       f"📡 CC Scanner — {now}",
        "description": (
            f"Scanned **{tickers_scanned}** tickers  •  "
            f"**{len(ready)}** ready  •  "
            f"**{len(watch)}** watching  •  "
            f"**{len(open_trades)}** position(s) open"
        ),
        "color": BLUE,
    })

    # ── [2..7] Ready setups (max 6 to stay within 10-embed limit) ─────────
    if ready:
        for s in ready[:6]:
            embeds.append(_setup_embed(s))
        if len(ready) > 6:
            embeds.append({
                "description": f"*…{len(ready) - 6} more READY setups not shown. Run `--once` for full list.*",
                "color": GREY,
            })
    else:
        embeds.append({
            "title":       "✅ Ready to Trade",
            "description": "No setups passed all filters this cycle.",
            "color":       GREY,
        })

    # ── [8] Watching ───────────────────────────────────────────────────────
    if watch:
        embeds.append(_watch_embed(watch))

    # ── [9] Open positions ─────────────────────────────────────────────────
    if open_trades:
        embeds.append(_positions_embed(open_trades, mids))

    # ── [10] Monthly progress ──────────────────────────────────────────────
    embeds.append(_monthly_embed(monthly))

    return _post({"username": "CC Scanner", "embeds": embeds[:10]})


def send_exit_alert(trade: dict, reason: str, pnl_pct: float, pnl_dol: float) -> bool:
    """Standalone urgent alert for a 50%-target hit or stop-loss."""
    icon  = "💰" if pnl_pct >= 0 else "🛑"
    color = GREEN if pnl_pct >= 0 else RED
    return _post({
        "username": "CC Scanner",
        "embeds": [{
            "title":       f"{icon} Exit Signal — {trade['ticker']} ${trade['strike']} CALL",
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
        "username": "CC Scanner",
        "embeds": [{
            "title":       "✅ Webhook test",
            "description": "CC Scanner is connected and ready.",
            "color":       GREEN,
        }],
    })
    print("✅ Sent!" if ok else "❌ Failed — check DISCORD_WEBHOOK_URL in .env")
