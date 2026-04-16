"""
Quantitative entry filters for the Theta Harvest strategy.
Each filter returns (pass: bool, reason: str).
All thresholds sourced from config.py (TH_ prefixed vars).
"""

import math
import logging
from datetime import datetime
from typing import Tuple, Optional

import pandas as pd

from config import (
    TH_DELTA_MIN, TH_DELTA_MAX,
    THETA_EFFICIENCY_MIN,
    GAMMA_STRESS_MAX_DELTA_CHG,
    TH_IV_RANK_MIN,
    IV_HV_RATIO_MIN,
    POP_MIN,
    TH_DTE_MIN, TH_DTE_MAX,
)
from analysis.greeks import delta as calc_delta, theta as calc_theta, gamma_stress
from analysis.probability import prob_of_profit, theta_efficiency

logger = logging.getLogger(__name__)


def filter_dte(expiry: str) -> Tuple[bool, str]:
    today = datetime.today().date()
    exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
    dte = (exp_date - today).days
    if TH_DTE_MIN <= dte <= TH_DTE_MAX:
        return True, f"DTE={dte} within [{TH_DTE_MIN},{TH_DTE_MAX}]"
    return False, f"DTE={dte} outside [{TH_DTE_MIN},{TH_DTE_MAX}]"


def filter_delta(
    S: float, K: float, T: float, r: float, sigma: float, option_type: str
) -> Tuple[bool, str]:
    d = abs(calc_delta(S, K, T, r, sigma, option_type))
    if TH_DELTA_MIN <= d <= TH_DELTA_MAX:
        return True, f"|delta|={d:.3f} within [{TH_DELTA_MIN},{TH_DELTA_MAX}]"
    return False, f"|delta|={d:.3f} outside [{TH_DELTA_MIN},{TH_DELTA_MAX}]"


def filter_theta_efficiency(
    S: float, K: float, T: float, r: float, sigma: float,
    option_type: str, premium: float
) -> Tuple[bool, str]:
    th = abs(calc_theta(S, K, T, r, sigma, option_type))
    eff = theta_efficiency(th, premium)
    if eff >= THETA_EFFICIENCY_MIN:
        return True, f"theta_eff={eff:.4f} >= {THETA_EFFICIENCY_MIN}"
    return False, f"theta_eff={eff:.4f} < {THETA_EFFICIENCY_MIN}"


def filter_gamma_stress(
    S: float, K: float, T: float, r: float, sigma: float, option_type: str
) -> Tuple[bool, str]:
    chg = gamma_stress(S, K, T, r, sigma, option_type)
    if chg <= GAMMA_STRESS_MAX_DELTA_CHG:
        return True, f"gamma_stress={chg:.3f} <= {GAMMA_STRESS_MAX_DELTA_CHG}"
    return False, f"gamma_stress={chg:.3f} > {GAMMA_STRESS_MAX_DELTA_CHG}"


def filter_iv_rank(ivr: Optional[float]) -> Tuple[bool, str]:
    if ivr is None:
        return False, "IV Rank unavailable"
    if ivr >= TH_IV_RANK_MIN:
        return True, f"IVR={ivr:.1f} >= {TH_IV_RANK_MIN}"
    return False, f"IVR={ivr:.1f} < {TH_IV_RANK_MIN}"


def filter_iv_hv_ratio(ratio: Optional[float]) -> Tuple[bool, str]:
    if ratio is None:
        return False, "IV/HV ratio unavailable"
    if ratio >= IV_HV_RATIO_MIN:
        return True, f"IV/HV={ratio:.3f} >= {IV_HV_RATIO_MIN}"
    return False, f"IV/HV={ratio:.3f} < {IV_HV_RATIO_MIN}"


def filter_pop(
    S: float, K_short: float, T: float, r: float, sigma: float,
    option_type: str, credit: float, K_long: Optional[float] = None
) -> Tuple[bool, str]:
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
