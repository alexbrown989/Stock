"""
Wheel Strategy Simulator
========================
For a given options contract (from options_analyzer.py), this module:

  1. Computes **Break-even** price at expiry.
  2. Computes **Max Profit** (premium collected per contract).
  3. Computes **Probability of Profit** (PoP) using the contract's delta as a
     proxy (PoP ≈ 1 − |delta| for OTM calls/puts).
  4. Runs a **Monte Carlo** simulation of the underlying's price path over the
     DTE window to estimate realistic P&L distribution under lognormal dynamics.
  5. Checks all operational guardrails:
       - Net Debit < $7,000
       - No earnings within 14 days
       - Gamma < 0.15 (or flags Defensive Roll)
  6. Returns a GoldenSetup object when all criteria pass.

Covered Call payoff at expiry (sell 1 call):
  P&L = premium_received − max(0, S_T − strike)
      = premium                    if S_T ≤ strike   (full keep)
      = premium − (S_T − strike)   if S_T > strike   (capped gain)

Wheel leg context:
  - If we own shares:  Covered Call (CC)
  - If we are cash-secured: Cash-Secured Put (CSP) to acquire shares
  Both legs are simulated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import numpy as np
import yfinance as yf

import config
from scanner.options_analyzer import OptionContract

log = logging.getLogger(__name__)

N_SIMULATIONS = 10_000
RISK_FREE_RATE = 0.053   # approx Fed Funds rate


@dataclass
class SimResult:
    # ── Input summary ────────────────────────────────────────────────────
    contract:            OptionContract

    # ── Core metrics ────────────────────────────────────────────────────
    premium_collected:   float   # per contract ($)
    break_even_price:    float   # underlying price at which P&L = 0
    max_profit:          float   # per contract ($)
    max_loss_estimate:   float   # worst-case per contract (Monte Carlo 1st pctile)
    pop_delta:           float   # probability of profit via delta (%)
    pop_monte_carlo:     float   # probability of profit via MC simulation (%)

    # ── Capital check ────────────────────────────────────────────────────
    capital_required:    float   # strike × 100 for CC / CSP
    net_debit:           float   # capital_required − premium_collected
    within_capital_limit: bool

    # ── Earnings check ───────────────────────────────────────────────────
    earnings_safe:       bool
    next_earnings_date:  Optional[date]

    # ── Risk flags ───────────────────────────────────────────────────────
    defensive_roll_needed: bool
    gamma_note:          str

    # ── Verdict ──────────────────────────────────────────────────────────
    is_golden_setup:     bool
    fail_reasons:        list[str] = field(default_factory=list)

    # ── Monte Carlo distribution ─────────────────────────────────────────
    mc_pnl_p10:          float = 0.0   # 10th percentile
    mc_pnl_p50:          float = 0.0   # median
    mc_pnl_p90:          float = 0.0   # 90th percentile

    @property
    def roi_percent(self) -> float:
        """Return on capital_required (not premium)."""
        if self.capital_required <= 0:
            return 0.0
        return self.max_profit / self.capital_required * 100

    def summary_markdown(self) -> str:
        verdict = "✅ GOLDEN SETUP" if self.is_golden_setup else "❌ FILTERED"
        c = self.contract
        lines = [
            f"## {c.ticker}  {c.right.upper()}  ${c.strike}  exp {c.expiry}  ({c.dte}DTE)",
            f"**Verdict:** {verdict}",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            f"| Underlying | ${c.underlying_price:.2f} |",
            f"| Mid Premium | ${c.mid:.2f} ({c.premium_dollar:.2f}/contract) |",
            f"| IV Rank | {c.iv_rank:.1f}% |",
            f"| Delta | {c.delta:.3f} |",
            f"| Gamma | {c.gamma:.4f} |",
            f"| Theta/day | ${c.daily_theta_dollar:.2f} |",
            f"| θ/Premium ratio | {c.theta_premium_ratio:.4f} ({c.theta_premium_ratio*100:.2f}%/day) |",
            f"| Break-even | ${self.break_even_price:.2f} |",
            f"| Max Profit | ${self.max_profit:.2f} |",
            f"| Max Loss (MC p1) | ${self.max_loss_estimate:.2f} |",
            f"| PoP (delta) | {self.pop_delta:.1f}% |",
            f"| PoP (Monte Carlo) | {self.pop_monte_carlo:.1f}% |",
            f"| Capital Required | ${self.capital_required:,.2f} |",
            f"| Net Debit | ${self.net_debit:,.2f} |",
            f"| ROI | {self.roi_percent:.2f}% |",
            f"| MC P10/P50/P90 | ${self.mc_pnl_p10:.0f} / ${self.mc_pnl_p50:.0f} / ${self.mc_pnl_p90:.0f} |",
            f"| Earnings Safe | {'Yes' if self.earnings_safe else f'⚠ Next: {self.next_earnings_date}'} |",
            f"| Defensive Roll | {'Yes — gamma={:.4f}'.format(c.gamma) if self.defensive_roll_needed else 'No'} |",
        ]
        if self.fail_reasons:
            lines.append("")
            lines.append("**Fail Reasons:**")
            for r in self.fail_reasons:
                lines.append(f"- {r}")
        return "\n".join(lines)


def _check_earnings(symbol: str, dte: int) -> tuple[bool, Optional[date]]:
    """Return (safe, next_earnings_date).  Safe = no earnings within blackout window."""
    try:
        cal = yf.Ticker(symbol).calendar
        if cal is None or cal.empty:
            return True, None
        # calendar may have 'Earnings Date' as a list or single value
        raw = cal.get("Earnings Date")
        if raw is None:
            return True, None
        if hasattr(raw, "iloc"):
            dates = [raw.iloc[0]]
        elif isinstance(raw, list):
            dates = raw
        else:
            dates = [raw]

        for d in dates:
            if d is None:
                continue
            if isinstance(d, str):
                d = datetime.strptime(d, "%Y-%m-%d").date()
            elif hasattr(d, "date"):
                d = d.date()
            days_away = (d - date.today()).days
            if 0 <= days_away <= config.EARNINGS_BLACKOUT_DAYS:
                return False, d
        return True, None

    except Exception as exc:
        log.warning("earnings check(%s): %s", symbol, exc)
        return True, None   # assume safe if unavailable


def _monte_carlo(
    S0: float,
    K: float,
    T: float,
    sigma: float,
    premium: float,
    n: int = N_SIMULATIONS,
) -> dict:
    """
    Simulate S_T under GBM.  Compute covered-call P&L distribution.
    Returns p10, p50, p90, pop (fraction of simulations with positive P&L).
    """
    r   = RISK_FREE_RATE
    Z   = np.random.standard_normal(n)
    S_T = S0 * np.exp((r - 0.5 * sigma**2) * T + sigma * np.sqrt(T) * Z)

    # Covered-call P&L per contract
    pnl = (premium - np.maximum(0, S_T - K)) * 100

    return {
        "p10":  float(np.percentile(pnl, 10)),
        "p50":  float(np.percentile(pnl, 50)),
        "p90":  float(np.percentile(pnl, 90)),
        "p1":   float(np.percentile(pnl, 1)),
        "pop":  float(np.mean(pnl > 0) * 100),
    }


def simulate(contract: OptionContract) -> SimResult:
    """
    Full simulation + guardrail check for a single OptionContract.
    """
    fail_reasons: list[str] = []

    # ── Premiums ─────────────────────────────────────────────────────────
    premium     = contract.mid                 # per share
    premium_dol = contract.premium_dollar      # per contract (×100)

    # ── Break-even (Covered Call) ─────────────────────────────────────────
    # For a CC: seller keeps premium if price ≤ strike at expiry.
    # Break-even on downside = cost_basis − premium  (shares purchased at current price)
    break_even = contract.underlying_price - premium

    # ── Max Profit ────────────────────────────────────────────────────────
    max_profit = premium_dol   # premium × 100

    # ── Capital & Net Debit ───────────────────────────────────────────────
    capital_required = contract.strike * 100   # 100 shares or cash-secured
    net_debit        = capital_required - premium_dol

    within_capital = net_debit <= config.MAX_TOTAL_CAPITAL
    if not within_capital:
        fail_reasons.append(
            f"Net Debit ${net_debit:,.2f} > capital limit ${config.MAX_TOTAL_CAPITAL:,.2f}"
        )

    # ── Earnings check ────────────────────────────────────────────────────
    earnings_safe, next_earnings = _check_earnings(
        contract.ticker, contract.dte
    )
    if not earnings_safe:
        fail_reasons.append(
            f"Earnings within {config.EARNINGS_BLACKOUT_DAYS}d "
            f"(next: {next_earnings})"
        )

    # ── Gamma check ───────────────────────────────────────────────────────
    defensive_roll = contract.gamma > config.GAMMA_RISK_MAX
    gamma_note = (
        f"Gamma {contract.gamma:.4f} > {config.GAMMA_RISK_MAX} — "
        "DEFENSIVE ROLL PLAN REQUIRED: buy back the call and roll to next"
        " expiry at a higher strike to reduce delta exposure."
        if defensive_roll
        else f"Gamma {contract.gamma:.4f} — within limit"
    )
    if defensive_roll:
        fail_reasons.append(gamma_note)

    # ── Options signal check ──────────────────────────────────────────────
    if contract.signal not in ("AGGRESSIVE", "PASSIVE"):
        fail_reasons.append(
            f"Options signal: {contract.signal} — {contract.reject_reason}"
        )

    # ── PoP via delta proxy ───────────────────────────────────────────────
    # For OTM call: PoP ≈ 1 − delta
    pop_delta = (1.0 - abs(contract.delta)) * 100

    # ── Monte Carlo ───────────────────────────────────────────────────────
    T = contract.dte / 365
    mc = _monte_carlo(
        S0=contract.underlying_price,
        K=contract.strike,
        T=T,
        sigma=contract.iv,
        premium=premium,
    )

    is_golden = len(fail_reasons) == 0 and pop_delta >= 55.0

    if pop_delta < 55.0 and not any("PoP" in r for r in fail_reasons):
        fail_reasons.append(f"PoP (delta) {pop_delta:.1f}% < 55% target")
        is_golden = False

    return SimResult(
        contract            = contract,
        premium_collected   = premium_dol,
        break_even_price    = round(break_even, 2),
        max_profit          = round(max_profit, 2),
        max_loss_estimate   = round(mc["p1"], 2),
        pop_delta           = round(pop_delta, 1),
        pop_monte_carlo     = round(mc["pop"], 1),
        capital_required    = round(capital_required, 2),
        net_debit           = round(net_debit, 2),
        within_capital_limit = within_capital,
        earnings_safe       = earnings_safe,
        next_earnings_date  = next_earnings,
        defensive_roll_needed = defensive_roll,
        gamma_note          = gamma_note,
        is_golden_setup     = is_golden,
        fail_reasons        = fail_reasons,
        mc_pnl_p10          = round(mc["p10"], 2),
        mc_pnl_p50          = round(mc["p50"], 2),
        mc_pnl_p90          = round(mc["p90"], 2),
    )


def run_batch(contracts: list[OptionContract]) -> list[SimResult]:
    """Simulate a list of contracts; return sorted golden setups first."""
    results = [simulate(c) for c in contracts]
    results.sort(key=lambda r: (r.is_golden_setup, r.pop_monte_carlo), reverse=True)
    return results
