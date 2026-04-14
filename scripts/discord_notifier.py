"""
Discord Notifier
================
Sends a formatted Markdown embed to a Discord webhook when a Golden Setup
is identified by the scanner.

Usage (called by the orchestrator, or directly for testing):
    python scripts/discord_notifier.py --test
    python scripts/discord_notifier.py --payload '{"ticker":"SOFI",...}'

Environment variable required:
    DISCORD_WEBHOOK_URL  — the full webhook URL from Discord server settings.

Embed structure:
  ┌────────────────────────────────────────────────────┐
  │ 🌀 GOLDEN SETUP — SOFI CALL $8.00 exp 2026-04-25  │
  │ PoP 72% | IV Rank 68% | θ/day $14.20              │
  │ ── Analysis ──────────────────────────────────────  │
  │  Break-even: $7.42  Max Profit: $58  Net Debit: …  │
  │  Monte Carlo P50: $51 | P10: -$12                   │
  │  Dark Pool: ⚑ Hidden Accumulation (MEDIUM)          │
  │  Gamma Squeeze Lead: NVDA→SOFI score 0.71 ⚡         │
  │  Sentiment: Peak Fear + Falling P/C ✅               │
  └────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

import requests

# Allow running directly from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402 — must come after sys.path insert

log = logging.getLogger(__name__)

# Discord embed colour (gold)
EMBED_COLOUR = 0xFFD700
DEFENSIVE_ROLL_COLOUR = 0xFF4500   # orange-red for roll alerts
REJECT_COLOUR = 0x808080


def _webhook_url() -> str:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        raise EnvironmentError(
            "DISCORD_WEBHOOK_URL is not set. "
            "Add it to your .env file or export it in the shell."
        )
    return url


def _truncate(s: str, n: int = 1024) -> str:
    """Discord embed field values are capped at 1024 chars."""
    return s if len(s) <= n else s[: n - 3] + "..."


def send_golden_setup(
    sim_result,   # SimResult from wheel_sim
    gamma_signals: list | None    = None,
    dark_pool_sig               = None,
    sentiment_sig               = None,
    max_pain_result             = None,
) -> bool:
    """
    Build and POST a rich Discord embed for a confirmed Golden Setup.
    Returns True on success.
    """
    c   = sim_result.contract
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    title = (
        f"{'✅' if sim_result.is_golden_setup else '⚠'} "
        f"{'GOLDEN SETUP' if sim_result.is_golden_setup else 'WATCH'} — "
        f"{c.ticker} {c.right.upper()} ${c.strike} exp {c.expiry} ({c.dte}DTE)"
    )

    colour = EMBED_COLOUR if sim_result.is_golden_setup else DEFENSIVE_ROLL_COLOUR

    # ── Core Fields ───────────────────────────────────────────────────────
    fields = [
        {
            "name": "📊 Greeks",
            "value": (
                f"**Delta:** `{c.delta:.3f}`\n"
                f"**Gamma:** `{c.gamma:.4f}` {'⚠ Roll Required' if sim_result.defensive_roll_needed else '✓'}\n"
                f"**Theta/day:** `${c.daily_theta_dollar:.2f}`\n"
                f"**θ/Premium:** `{c.theta_premium_ratio*100:.2f}%/day`\n"
                f"**IV Rank:** `{c.iv_rank:.1f}%`\n"
                f"**IV:** `{c.iv*100:.1f}%`"
            ),
            "inline": True,
        },
        {
            "name": "💰 P&L",
            "value": (
                f"**Premium:** `${sim_result.premium_collected:.2f}`\n"
                f"**Max Profit:** `${sim_result.max_profit:.2f}`\n"
                f"**Break-even:** `${sim_result.break_even_price:.2f}`\n"
                f"**MC P50:** `${sim_result.mc_pnl_p50:.0f}`\n"
                f"**MC P10:** `${sim_result.mc_pnl_p10:.0f}`\n"
                f"**ROI:** `{sim_result.roi_percent:.2f}%`"
            ),
            "inline": True,
        },
        {
            "name": "🎯 Probability",
            "value": (
                f"**PoP (delta):** `{sim_result.pop_delta:.1f}%`\n"
                f"**PoP (Monte Carlo):** `{sim_result.pop_monte_carlo:.1f}%`\n"
                f"**Capital Required:** `${sim_result.capital_required:,.0f}`\n"
                f"**Net Debit:** `${sim_result.net_debit:,.0f}`\n"
                f"**Capital OK:** `{'✅' if sim_result.within_capital_limit else '❌'}`\n"
                f"**Earnings Safe:** `{'✅' if sim_result.earnings_safe else '❌ ' + str(sim_result.next_earnings_date)}`"
            ),
            "inline": True,
        },
    ]

    # ── Max Pain ──────────────────────────────────────────────────────────
    if max_pain_result:
        mp = max_pain_result
        fields.append({
            "name": "😬 Max Pain",
            "value": (
                f"**Max Pain Strike:** `${mp.max_pain_strike:.2f}`\n"
                f"**Target CC Zone:** `${mp.target_call_low:.2f} – ${mp.target_call_high:.2f}`\n"
                f"**Current Strike in Zone:** "
                f"`{'✅' if mp.target_call_low <= c.strike <= mp.target_call_high else '❌'}`"
            ),
            "inline": True,
        })

    # ── Dark Pool ─────────────────────────────────────────────────────────
    if dark_pool_sig:
        dp = dark_pool_sig
        emoji = "⚑" if dp.hidden_accumulation else "–"
        fields.append({
            "name": f"{emoji} Dark Pool",
            "value": (
                f"**Hidden Accumulation:** `{'YES' if dp.hidden_accumulation else 'No'}`\n"
                f"**Confidence:** `{dp.confidence}`\n"
                f"**Source:** `{dp.source}`\n"
                f"**Vol Ratio:** `{dp.volume_ratio:.2f}×`\n"
                f"**Close Bias:** `{dp.close_bias:.3f}`"
            ),
            "inline": True,
        })

    # ── Gamma Squeeze Lead ────────────────────────────────────────────────
    if gamma_signals:
        top = [s for s in gamma_signals if s.target_ticker == c.ticker and s.alert][:2]
        if top:
            gtext = "\n".join(
                f"**{s.lead_ticker}→{s.target_ticker}:** score `{s.signal_score:.3f}` "
                f"corr `{s.correlation:.3f}` skew `{s.iv_skew_lead:.3f}`"
                for s in top
            )
            fields.append({
                "name": "⚡ Gamma Squeeze Lead",
                "value": _truncate(gtext),
                "inline": False,
            })

    # ── Sentiment ─────────────────────────────────────────────────────────
    if sentiment_sig:
        sen = sentiment_sig
        fields.append({
            "name": "🧠 Sentiment Entropy",
            "value": (
                f"**Reversal Signal:** `{'🔥 YES' if sen.reversal_signal else 'No'}`\n"
                f"**Peak Fear:** `{'Yes' if sen.peak_fear else 'No'}`\n"
                f"**P/C Ratio:** `{sen.put_call_ratio:.3f}` (avg `{sen.pc_ratio_avg:.3f}`)\n"
                f"**P/C Change:** `{sen.pc_ratio_change:+.3f}`\n"
                f"**Social:** `{sen.social_sentiment}`"
            ),
            "inline": True,
        })

    # ── Fail Reasons (if not golden) ──────────────────────────────────────
    if sim_result.fail_reasons:
        fields.append({
            "name": "❌ Filter Reasons",
            "value": _truncate("\n".join(f"• {r}" for r in sim_result.fail_reasons)),
            "inline": False,
        })

    # ── Defensive Roll Plan ───────────────────────────────────────────────
    if sim_result.defensive_roll_needed:
        fields.append({
            "name": "🛡 Defensive Roll Plan",
            "value": (
                f"Gamma `{c.gamma:.4f}` exceeds limit `{0.15}`. "
                "**Action:** Buy-to-close current call immediately. "
                f"Sell-to-open same ticker, next expiry cycle ({c.dte + 14}DTE), "
                f"strike `${c.strike * 1.03:.2f}` (+3%) to reset delta exposure. "
                "Monitor until delta < 0.30."
            ),
            "inline": False,
        })

    payload = {
        "username": "Alpha-Harvest Bot",
        "avatar_url": "",
        "embeds": [
            {
                "title": title,
                "color": colour,
                "fields": fields,
                "footer": {"text": f"Alpha-Harvest v4.6 | {now}"},
                "timestamp": datetime.utcnow().isoformat(),
            }
        ],
    }

    try:
        resp = requests.post(
            _webhook_url(),
            json=payload,
            timeout=10,
        )
        if resp.status_code in (200, 204):
            log.info("Discord notification sent for %s", c.ticker)
            return True
        else:
            log.error(
                "Discord webhook error %d: %s", resp.status_code, resp.text
            )
            return False
    except Exception as exc:
        log.error("Discord send failed: %s", exc)
        return False


def send_scan_summary(
    golden_count: int,
    total_scanned: int,
    scan_duration_s: float,
    top_tickers: list[str],
) -> bool:
    """
    Send a lightweight scan-cycle summary (not a full setup embed).
    """
    now  = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    desc = (
        f"Scanned **{total_scanned}** tickers in `{scan_duration_s:.1f}s`.\n"
        f"Found **{golden_count}** Golden Setup(s).\n"
        f"Top: {', '.join(f'`{t}`' for t in top_tickers[:5]) or 'None'}"
    )
    payload = {
        "username": "Alpha-Harvest Bot",
        "embeds": [
            {
                "title": f"🔍 Scan Complete — {now}",
                "description": desc,
                "color": 0x00BFFF,
                "footer": {"text": "Alpha-Harvest v4.6"},
            }
        ],
    }
    try:
        resp = requests.post(_webhook_url(), json=payload, timeout=10)
        return resp.status_code in (200, 204)
    except Exception as exc:
        log.error("Discord summary send failed: %s", exc)
        return False


def send_defensive_roll_alert(ticker: str, gamma: float, current_strike: float) -> bool:
    """Standalone alert: gamma has breached threshold, roll immediately."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    payload = {
        "username": "Alpha-Harvest Bot",
        "embeds": [
            {
                "title": f"🚨 DEFENSIVE ROLL REQUIRED — {ticker}",
                "description": (
                    f"**Gamma:** `{gamma:.4f}` has exceeded the `{config.GAMMA_RISK_MAX}` limit.\n\n"
                    f"**Recommended Action:**\n"
                    f"1. Buy-to-close your `${current_strike}` call immediately.\n"
                    f"2. Sell-to-open next expiry, strike `${current_strike * 1.03:.2f}` (+3%).\n"
                    f"3. Confirm new delta < 0.30 before re-entering."
                ),
                "color": DEFENSIVE_ROLL_COLOUR,
                "footer": {"text": f"Alpha-Harvest v4.6 | {now}"},
            }
        ],
    }
    try:
        resp = requests.post(_webhook_url(), json=payload, timeout=10)
        return resp.status_code in (200, 204)
    except Exception as exc:
        log.error("Discord roll alert failed: %s", exc)
        return False


# ── CLI entry-point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import os
    from dotenv import load_dotenv

    # Load .env from repo root
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Alpha-Harvest Discord Notifier")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Send a test scan-summary message to confirm the webhook works.",
    )
    args = parser.parse_args()

    if args.test:
        ok = send_scan_summary(
            golden_count   = 0,
            total_scanned  = 20,
            scan_duration_s= 0.1,
            top_tickers    = ["TEST"],
        )
        sys.exit(0 if ok else 1)

    # If called with JSON via stdin or --payload, parse and dispatch
    parser.print_help()
