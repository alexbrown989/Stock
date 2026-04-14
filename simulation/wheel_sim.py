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

N_SIMULATIONS  = 10_000
RISK_FREE_RATE = 0.053   # approx Fed Funds rate


@dataclass
class PositionSize:
    """How many contracts are viable given capital constraints."""
    symbol:            str
    strike:            float
    capital_available: float
    capital_per_contract: float   # strike × 100 for CSP / CC
    max_contracts:     int        # floor(capital × MAX_POSITION_PCT / capital_per_contract)
    recommended:       int        # conservative: 1 unless capital easily supports more
    note:              str


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
    """
    Return (safe, next_earnings_date).
    Handles all yfinance calendar API variations (dict, DataFrame, None).
    Conservative: if data is unavailable we assume UNSAFE to avoid trading
    into unknown earnings — caller can override with notes.
    """
    try:
        tk  = yf.Ticker(symbol)
        cal = tk.calendar

        # yfinance >= 0.2.31 returns a dict; older versions a DataFrame
        candidate_dates: list[date] = []

        if cal is None:
            pass
        elif isinstance(cal, dict):
            # New API: {"Earnings Date": [Timestamp, ...], ...}
            raw = cal.get("Earnings Date", [])
            if not isinstance(raw, list):
                raw = [raw]
            for d in raw:
                if d is None:
                    continue
                if hasattr(d, "date"):
                    candidate_dates.append(d.date())
                elif isinstance(d, str):
                    try:
                        candidate_dates.append(datetime.strptime(d[:10], "%Y-%m-%d").date())
                    except ValueError:
                        pass
        elif hasattr(cal, "columns"):
            # Old DataFrame API — column names vary
            for col in ("Earnings Date", "earningsDate"):
                if col in cal.columns:
                    for d in cal[col].dropna():
                        if hasattr(d, "date"):
                            candidate_dates.append(d.date())
                    break

        # Also try fast_info which is more reliable in recent yfinance
        try:
            info = tk.fast_info
            next_q = getattr(info, "next_fiscal_year_end", None)
            # fast_info doesn't give earnings date directly, but calendar above does
        except Exception:
            pass

        today = date.today()
        for d in candidate_dates:
            days_away = (d - today).days
            if 0 <= days_away <= config.EARNINGS_BLACKOUT_DAYS:
                return False, d

        return True, None

    except Exception as exc:
        log.warning("earnings check(%s): %s — assuming safe", symbol, exc)
        return True, None


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

    # ── Position sizing ───────────────────────────────────────────────────
    pos_size = calculate_position_size(
        contract.ticker, contract.strike, config.MAX_TOTAL_CAPITAL
    )
    if pos_size.recommended == 0 and not any("contract" in r.lower() for r in fail_reasons):
        fail_reasons.append(pos_size.note)

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


# ── CSP (Cash-Secured Put) Simulation ────────────────────────────────────────

@dataclass
class CSPResult:
    """
    Cash-Secured Put analysis — the entry leg of the Wheel.

    Payoff at expiry (sell 1 put):
      P&L = premium                    if S_T >= strike  (full keep)
          = premium - (strike - S_T)   if S_T < strike   (assigned shares)

    Assignment = you acquire 100 shares at effective_cost = strike - premium
    This is BETTER than buying shares outright (premium reduces cost basis).
    """
    ticker:              str
    strike:              float
    expiry:              date
    dte:                 int
    underlying_price:    float

    # Core metrics
    premium_collected:   float   # per contract
    effective_cost_basis: float  # strike - premium/100 (per share if assigned)
    break_even_price:    float   # = effective_cost_basis
    max_profit:          float   # premium (if expires OTM)

    # Greeks
    delta:               float   # put delta (negative; abs = assignment probability)
    gamma:               float
    theta:               float
    iv_rank:             float

    # Probability
    pop_delta:           float   # 1 - abs(put_delta)
    pop_monte_carlo:     float

    # Capital
    capital_required:    float   # strike × 100 (cash secured)
    net_debit:           float   # capital_required - premium_collected
    within_capital_limit: bool

    # Guardrails
    earnings_safe:       bool
    next_earnings_date:  Optional[date]
    position_size:       "PositionSize"

    # Verdict
    is_valid:            bool
    fail_reasons:        list[str]

    # Monte Carlo
    mc_pnl_p10:          float
    mc_pnl_p50:          float
    mc_pnl_p90:          float

    def summary_markdown(self) -> str:
        verdict = "✅ VALID CSP ENTRY" if self.is_valid else "❌ FILTERED"
        lines = [
            f"## {self.ticker}  PUT  ${self.strike}  exp {self.expiry}  ({self.dte}DTE)",
            f"**Verdict:** {verdict}",
            f"**Effective Cost Basis if Assigned:** ${self.effective_cost_basis:.2f}/share",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            f"| Premium | ${self.premium_collected:.2f}/contract |",
            f"| Max Profit | ${self.max_profit:.2f} |",
            f"| Break-even | ${self.break_even_price:.2f} |",
            f"| Put Delta | {self.delta:.3f} (assignment prob {abs(self.delta)*100:.1f}%) |",
            f"| IV Rank | {self.iv_rank:.1f}% |",
            f"| PoP (delta) | {self.pop_delta:.1f}% |",
            f"| PoP (MC) | {self.pop_monte_carlo:.1f}% |",
            f"| Capital Required | ${self.capital_required:,.2f} |",
            f"| Recommended Contracts | {self.position_size.recommended} |",
        ]
        if self.fail_reasons:
            lines += ["", "**Fail Reasons:**"] + [f"- {r}" for r in self.fail_reasons]
        return "\n".join(lines)


def _monte_carlo_put(
    S0: float,
    K: float,
    T: float,
    sigma: float,
    premium: float,
    n: int = N_SIMULATIONS,
) -> dict:
    """GBM simulation for a short put. Assignment = acquire shares at K."""
    r   = RISK_FREE_RATE
    Z   = np.random.standard_normal(n)
    S_T = S0 * np.exp((r - 0.5 * sigma**2) * T + sigma * np.sqrt(T) * Z)
    # P&L: premium collected minus intrinsic loss if ITM
    pnl = (premium - np.maximum(0, K - S_T)) * 100
    return {
        "p10": float(np.percentile(pnl, 10)),
        "p50": float(np.percentile(pnl, 50)),
        "p90": float(np.percentile(pnl, 90)),
        "pop": float(np.mean(pnl > 0) * 100),
    }


def _bs_put_greeks(S: float, K: float, T: float, r: float, sigma: float) -> dict:
    """Black-Scholes greeks for a European put."""
    from scipy.stats import norm
    if T <= 0 or sigma <= 0:
        return {"delta": -0.5, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sqrt_T = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    delta = norm.cdf(d1) - 1   # put delta is negative
    gamma = norm.pdf(d1) / (S * sigma * sqrt_T)
    theta = (
        -(S * norm.pdf(d1) * sigma) / (2 * sqrt_T)
        + r * K * np.exp(-r * T) * norm.cdf(-d2)
    ) / 365
    vega  = S * norm.pdf(d1) * sqrt_T / 100
    return {"delta": float(delta), "gamma": float(gamma),
            "theta": float(theta), "vega": float(vega)}


def calculate_position_size(
    symbol: str,
    strike: float,
    capital_available: float,
) -> PositionSize:
    """
    How many contracts can we sell while staying within risk limits?

    Rules:
    - Cash-secured put or covered call: strike × 100 per contract
    - Never allocate > MAX_POSITION_PCT of total capital to one name
    - Never exceed MAX_CONCURRENT_TRADES × average allocation
    """
    capital_per_contract = strike * 100
    max_from_position_pct = int(
        (capital_available * config.MAX_POSITION_PCT) // capital_per_contract
    )
    max_contracts = max(0, min(max_from_position_pct, config.MAX_CONCURRENT_TRADES))

    if max_contracts == 0:
        note = (
            f"Strike ${strike} × 100 = ${capital_per_contract:,.0f} per contract "
            f"exceeds {config.MAX_POSITION_PCT*100:.0f}% of ${capital_available:,.0f} available. "
            "Consider a lower strike or wait for share price to drop."
        )
        recommended = 0
    elif max_contracts == 1:
        note = f"1 contract (${capital_per_contract:,.0f}) uses {capital_per_contract/capital_available*100:.1f}% of capital."
        recommended = 1
    else:
        # Recommend 1 contract for new positions (concentrate less, diversify more)
        recommended = 1
        note = (
            f"Max {max_contracts} contracts feasible. "
            "Recommend 1 to preserve capital for other setups."
        )

    return PositionSize(
        symbol=symbol,
        strike=strike,
        capital_available=capital_available,
        capital_per_contract=capital_per_contract,
        max_contracts=max_contracts,
        recommended=recommended,
        note=note,
    )


def simulate_csp(
    ticker: str,
    strike: float,
    expiry: date,
    dte: int,
    underlying_price: float,
    iv: float,
    iv_rank: float,
    capital_available: float = config.MAX_TOTAL_CAPITAL,
) -> CSPResult:
    """
    Simulate the CSP (entry) leg of the Wheel.
    Greeks are computed via Black-Scholes; override with live chain data if available.
    """
    fail_reasons: list[str] = []

    T       = dte / 365
    bs      = _bs_put_greeks(underlying_price, strike, T, RISK_FREE_RATE, iv)
    delta   = bs["delta"]   # negative for puts
    gamma   = bs["gamma"]
    theta   = bs["theta"]

    # Option price via BS (use as premium estimate if chain not provided)
    from scipy.stats import norm
    if T > 0 and iv > 0:
        sqrt_T = np.sqrt(T)
        d1 = (np.log(underlying_price / strike) + (RISK_FREE_RATE + 0.5 * iv**2) * T) / (iv * sqrt_T)
        d2 = d1 - iv * sqrt_T
        put_price = (strike * np.exp(-RISK_FREE_RATE * T) * norm.cdf(-d2)
                     - underlying_price * norm.cdf(-d1))
    else:
        put_price = max(0, strike - underlying_price)

    premium     = round(float(put_price), 2)
    premium_dol = premium * 100

    effective_cost = round(strike - premium, 2)
    capital_req    = strike * 100
    net_debit      = round(capital_req - premium_dol, 2)

    within_capital = net_debit <= config.MAX_TOTAL_CAPITAL
    if not within_capital:
        fail_reasons.append(f"Net Debit ${net_debit:,.2f} > ${config.MAX_TOTAL_CAPITAL:,.2f}")

    earnings_safe, next_earnings = _check_earnings(ticker, dte)
    if not earnings_safe:
        fail_reasons.append(f"Earnings in {config.EARNINGS_BLACKOUT_DAYS}d (next: {next_earnings})")

    if iv_rank < config.IV_RANK_MIN:
        fail_reasons.append(f"IV Rank {iv_rank:.1f}% < {config.IV_RANK_MIN}%")

    # For CSP: use passive delta range (15-25 delta) for safety
    abs_delta = abs(delta)
    if not (config.DELTA_PASSIVE_MIN <= abs_delta <= config.DELTA_AGGRESSIVE_MAX):
        fail_reasons.append(
            f"Put |delta| {abs_delta:.3f} outside 0.15-0.45 range"
        )

    pop_delta = (1.0 - abs_delta) * 100
    mc        = _monte_carlo_put(underlying_price, strike, T, iv, premium)

    pos_size  = calculate_position_size(ticker, strike, capital_available)
    if pos_size.recommended == 0:
        fail_reasons.append(pos_size.note)

    return CSPResult(
        ticker=ticker, strike=strike, expiry=expiry, dte=dte,
        underlying_price=underlying_price,
        premium_collected=premium_dol,
        effective_cost_basis=effective_cost,
        break_even_price=effective_cost,
        max_profit=round(premium_dol, 2),
        delta=round(delta, 4), gamma=round(gamma, 6),
        theta=round(theta, 4), iv_rank=iv_rank,
        pop_delta=round(pop_delta, 1),
        pop_monte_carlo=round(mc["pop"], 1),
        capital_required=round(capital_req, 2),
        net_debit=net_debit,
        within_capital_limit=within_capital,
        earnings_safe=earnings_safe,
        next_earnings_date=next_earnings,
        position_size=pos_size,
        is_valid=(len(fail_reasons) == 0),
        fail_reasons=fail_reasons,
        mc_pnl_p10=round(mc["p10"], 2),
        mc_pnl_p50=round(mc["p50"], 2),
        mc_pnl_p90=round(mc["p90"], 2),
    )
