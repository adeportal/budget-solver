"""
Marginal ROAS (mROAS) calculations.

Two types of mROAS are used:
1. Instantaneous mROAS: analytical derivative of the fitted curve at a spend level.
   Used for cap logic only (determines spending ceiling).

2. Discrete mROAS: ΔRevenue ÷ ΔSpend between two scenarios.
   Used for scenario comparison and reallocation decisions (primary metric).
"""
import numpy as np


def discrete_mroas(
    rev_from: float,
    rev_to: float,
    spend_from: float,
    spend_to: float
) -> float | None:
    """
    Calculate discrete marginal ROAS: ΔRevenue ÷ ΔSpend.

    Returns None if ΔSpend is ~0 (undefined or budget-neutral reallocation).

    Sign convention:
    - For increases (ΔSpend > 0): positive discrete mROAS = revenue gained per € added
    - For cuts (ΔSpend < 0): positive discrete mROAS = revenue given up per € cut
      (the formula handles this naturally via sign of ΔRev)
    """
    delta_spend = spend_to - spend_from
    if abs(delta_spend) < 1e-6:
        return None

    delta_rev = rev_to - rev_from
    return delta_rev / delta_spend


def breakeven_weekly_spend(params: list, model_name: str, min_mroas: float, max_weekly: float | None = None) -> float:
    """
    Solve inst. mROAS = min_mroas analytically for the WEEKLY spend level.

    - Log  (y = a·ln(x) + b):  dy/dx = a/x        → x = a / min_mroas
    - Power (y = a·x^b):       dy/dx = a·b·x^(b-1) → x = (a·b / min_mroas)^(1/(1-b))
    - Linear (y = a·x):        dy/dx = a (constant) → no finite breakeven; capped at max_weekly

    For power curves with b close to 1, the result can blow up. max_weekly caps it at a
    practical upper bound (e.g. auto spend cap / WEEKS_PER_MONTH).
    """
    base_model = model_name.replace("+cal", "").replace("+2stage", "")
    if base_model == "log":
        result = params[0] / min_mroas
    elif base_model == "power":
        a, b = params[0], params[1]
        if b <= 0 or b >= 1:
            result = 0.0
        else:
            result = (a * b / min_mroas) ** (1.0 / (1.0 - b))
    elif base_model == "linear_fallback":
        result = float('inf')
    else:
        result = 0.0

    if max_weekly is not None and result > max_weekly:
        return max_weekly
    return result


def instantaneous_mroas(params: list, model_name: str, spend: float) -> float:
    """
    Calculate instantaneous mROAS: analytical derivative of the fitted curve at `spend`.

    Used for cap logic only (determines max justified spend = a / min_mroas).

    Args:
        params: Fitted curve parameters from fit_response_curve
        model_name: "log", "power", "linear_fallback" (optionally with "+cal" suffix)
        spend: Daily spend level (must be > 0 for log/power curves)

    Returns:
        Instantaneous mROAS (derivative dRevenue/dSpend at the given spend)

    Curve derivatives:
        - Log curve (y = a·ln(x) + b):     dy/dx = a/x
        - Power curve (y = a·x^b):         dy/dx = a·b·x^(b-1)
        - Linear fallback (y = a·x):       dy/dx = a
    """
    if spend <= 0:
        return 0.0

    # Strip "+cal" suffix if present (calibration doesn't change the derivative formula)
    base_model = model_name.replace("+cal", "").replace("+2stage", "")

    if base_model == "log":
        a = params[0]
        return a / spend

    elif base_model == "power":
        a, b = params[0], params[1]
        if spend == 0:
            return 0.0
        return a * b * (spend ** (b - 1))

    elif base_model == "linear_fallback":
        a = params[0]
        return a

    else:
        # Unknown model, return 0 as safe fallback
        return 0.0
