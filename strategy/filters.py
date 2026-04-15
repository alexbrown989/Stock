"""
Quantitative entry filters for the Theta Harvest strategy.
Each filter returns (pass: bool, reason: str).
All thresholds sourced from config.py.
"""

import math
import logging
from datetime import datetime
from typing import Tuple, Optional

import pandas as pd

from config import (
    DELTA_MIN, DELTA_MAX,
    THETA_EFFICIENCY_MIN,
    GAMMA_STRESS_MAX_DELTA_CHANGE,
    IV_RANK_MIN,
    IV_HV_RATIO_MIN,
    POP_MIN,
    DTE_MIN, DTE_MAX,
)
from analysis.greeks import delta as calc_delta, theta as calc_theta, gamma_stress
from analysis.probability import prob_of_profit, theta_efficiency

logger = logging.getLogger(__name__)


def filter_dte(expiry: str) -> Tuple[bool, str]:
    """Days-to-expiration must be within [DTE_MIN, DTE_MAX]."""
    today = datetime.today().date()
    exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
    dte = (exp_date - today).days
    if DTE_MIN <= dte <= DTE_MAX:
        return True, f"DTE={dte} within [{DTE_MIN},{DTE_MAX}]"
    return False, f"DTE={dte} outside [{DTE_MIN},{DTE_MAX}]"


def filter_delta(
    S: float, K: float, T: float, r: float, sigma: float, option_type: str
) -> Tuple[bool, str]:
    """Absolute delta must be in [DELTA_MIN, DELTA_MAX]."""
    d = abs(calc_delta(S, K, T, r, sigma, option_type))
    if DELTA_MIN <= d <= DELTA_MAX:
        return True, f"|delta|={d:.3f} within [{DELTA_MIN},{DELTA_MAX}]"
    return False, f"|delta|={d:.3f} outside [{DELTA_MIN},{DELTA_MAX}]"


def filter_theta_efficiency(
    S: float, K: float, T: float, r: float, sigma: float,
    option_type: str, premium: float
) -> Tuple[bool, str]:
    """Daily theta as fraction of premium must exceed THETA_EFFICIENCY_MIN."""
    th = abs(calc_theta(S, K, T, r, sigma, option_type))
    eff = theta_efficiency(th, premium)
    if eff >= THETA_EFFICIENCY_MIN:
        return True, f"theta_eff={eff:.4f} >= {THETA_EFFICIENCY_MIN}"
    return False, f"theta_eff={eff:.4f} < {THETA_EFFICIENCY_MIN}"


def filter_gamma_stress(
    S: float, K: float, T: float, r: float, sigma: float, option_type: str
) -> Tuple[bool, str]:
    """Delta change under 2% shock must be within GAMMA_STRESS_MAX_DELTA_CHANGE."""
    chg = gamma_stress(S, K, T, r, sigma, option_type)
    if chg <= GAMMA_STRESS_MAX_DELTA_CHANGE:
        return True, f"gamma_stress_delta_chg={chg:.3f} <= {GAMMA_STRESS_MAX_DELTA_CHANGE}"
    return False, f"gamma_stress_delta_chg={chg:.3f} > {GAMMA_STRESS_MAX_DELTA_CHANGE}"


def filter_iv_rank(ivr: Optional[float]) -> Tuple[bool, str]:
    """IV Rank must exceed IV_RANK_MIN."""
    if ivr is None:
        return False, "IV Rank unavailable"
    if ivr >= IV_RANK_MIN:
        return True, f"IVR={ivr:.1f} >= {IV_RANK_MIN}"
    return False, f"IVR={ivr:.1f} < {IV_RANK_MIN}"


def filter_iv_hv_ratio(ratio: Optional[float]) -> Tuple[bool, str]:
    """IV/HV ratio must show IV premium over realized vol."""
    if ratio is None:
        return False, "IV/HV ratio unavailable"
    if ratio >= IV_HV_RATIO_MIN:
        return True, f"IV/HV={ratio:.3f} >= {IV_HV_RATIO_MIN}"
    return False, f"IV/HV={ratio:.3f} < {IV_HV_RATIO_MIN}"


def filter_pop(
    S: float, K_short: float, T: float, r: float, sigma: float,
    option_type: str, credit: float, K_long: Optional[float] = None
) -> Tuple[bool, str]:
    """Probability of profit must exceed POP_MIN."""
    pop = prob_of_profit(S, K_short, T, r, sigma, option_type, credit, K_long)
    if pop >= POP_MIN:
        return True, f"POP={pop:.2%} >= {POP_MIN:.0%}"
    return False, f"POP={pop:.2%} < {POP_MIN:.0%}"


def run_all_filters(
    S: float, K_short: float, expiry: str, T: float, r: float,
    sigma: float, option_type: str, premium: float,
    ivr: Optional[float] = None,
    iv_hv: Optional[float] = None,
    K_long: Optional[float] = None,
) -> dict:
    """
    Run all filters and return a combined result dict.
    {passed: bool, score: int, details: list[str], failures: list[str]}
    """
    checks = [
        filter_dte(expiry),
        filter_delta(S, K_short, T, r, sigma, option_type),
        filter_theta_efficiency(S, K_short, T, r, sigma, option_type, premium),
        filter_gamma_stress(S, K_short, T, r, sigma, option_type),
        filter_iv_rank(ivr),
        filter_pop(S, K_short, T, r, sigma, option_type, premium, K_long),
    ]

    if iv_hv is not None:
        checks.append(filter_iv_hv_ratio(iv_hv))

    passed   = [msg for ok, msg in checks if ok]
    failures = [msg for ok, msg in checks if not ok]

    return {
        "passed":   len(failures) == 0,
        "score":    len(passed),
        "total":    len(checks),
        "details":  passed,
        "failures": failures,
    }
