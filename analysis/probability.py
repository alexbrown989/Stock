"""
Probability and expected-value calculations for short premium positions.
"""

import math
from typing import Optional

from analysis.greeks import probability_otm, bsm_price


def prob_of_profit(S: float, K_short: float, T: float, r: float, sigma: float,
                   option_type: str, credit: float,
                   K_long: Optional[float] = None) -> float:
    """
    Probability of profit for a short option (naked or spread).

    Naked short: POP ≈ probability the option expires OTM.
    Credit spread: breakeven = K_short ± credit, so POP uses breakeven strike.

    option_type: 'put' or 'call'
    credit: net credit received (per share, already accounting for spread if any)
    K_long: long strike for a spread (optional)
    """
    if option_type == "put":
        breakeven = K_short - credit  # short put breakeven
    else:
        breakeven = K_short + credit  # short call breakeven

    return probability_otm(S, breakeven, T, r, sigma, option_type)


def expected_value(S: float, K_short: float, T: float, r: float, sigma: float,
                   option_type: str, credit: float,
                   K_long: Optional[float] = None) -> float:
    """
    Simple EV = (POP * credit) - ((1 - POP) * max_loss)
    For spread: max_loss = width - credit
    For naked short: max_loss approximated as K_short (puts) or unbounded (calls)
    """
    pop = prob_of_profit(S, K_short, T, r, sigma, option_type, credit, K_long)

    if K_long is not None:
        width = abs(K_short - K_long)
        max_loss = width - credit
    else:
        if option_type == "put":
            max_loss = K_short - credit  # worst case: stock goes to 0
        else:
            max_loss = credit * 10  # placeholder for naked call

    ev = (pop * credit) - ((1 - pop) * max_loss)
    return float(ev)


def theta_efficiency(daily_theta: float, premium: float) -> float:
    """
    Theta efficiency = |daily theta| / premium collected.
    Higher is better — how much of the premium decays per day.
    """
    if premium <= 0:
        return 0.0
    return abs(daily_theta) / premium


def max_profit_days(T_years: float, target_pct: float = 0.50) -> int:
    """
    Days until theta decay accelerates enough to reach `target_pct` of premium.
    Based on the rule-of-thumb that theta accelerates in the final 1/3 of life.
    Returns the day count from today at which to consider closing.
    """
    total_days = T_years * 365
    return max(1, int(total_days * (1 - target_pct)))
