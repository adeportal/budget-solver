"""
Response curve models and fitting logic.
"""
import warnings
from contextlib import contextmanager

import numpy as np
from scipy.optimize import curve_fit

from budget_solver.constants import POWER_B_MIN, POWER_B_MAX


@contextmanager
def suppress_curve_fit_warnings():
    """Context manager to suppress warnings during curve fitting only."""
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore')
        yield


def power_curve(x, a, b):
    """revenue = a * spend^b   (b < 1 = diminishing returns)"""
    return a * np.power(np.maximum(x, 1e-9), b)


def log_curve(x, a, b):
    """revenue = a * ln(spend) + b"""
    return a * np.log(np.maximum(x, 1e-9)) + b


def make_safe_predictor(raw_predict_fn, min_observed_spend, anchor_revenue):
    """
    Prevent pathological low-spend extrapolation from producing negative revenue.

    Below the observed spend range, interpolate linearly from (0, 0) to the
    fitted value at the minimum observed spend. Within and above the observed
    range, floor predictions at zero.
    """
    min_observed_spend = float(max(min_observed_spend or 0.0, 0.0))
    anchor_revenue = float(max(anchor_revenue or 0.0, 0.0))

    def safe_predict(x):
        scalar_input = np.isscalar(x)
        x_arr = np.atleast_1d(np.asarray(x, dtype=float))
        result = np.zeros_like(x_arr, dtype=float)

        positive_mask = x_arr > 0
        if min_observed_spend > 0:
            low_mask = positive_mask & (x_arr < min_observed_spend)
            if np.any(low_mask):
                result[low_mask] = (x_arr[low_mask] / min_observed_spend) * anchor_revenue
            model_mask = positive_mask & ~low_mask
        else:
            model_mask = positive_mask

        if np.any(model_mask):
            result[model_mask] = np.maximum(raw_predict_fn(x_arr[model_mask]), 0.0)

        return float(result[0]) if scalar_input else result

    return safe_predict


def fit_portfolio_curves(account_data: dict, preferred_model: str = 'log') -> dict:
    """
    Fit response curves for all accounts with consistent curve family enforcement.

    CRITICAL: Ensures all accounts use the same curve type (log or power) to avoid
    inconsistent discrete mROAS comparisons across accounts. mROAS formulas differ:
    - Log: a/x
    - Power: a·b·x^(b-1)

    Strategy:
    1. Attempt to fit preferred model (log) to all accounts
    2. If ALL succeed → use log for all
    3. If ANY fail → refit ALL with power curve
    4. Linear fallback only for individual accounts that fail both (flagged separately)

    Args:
        account_data: Dict[account_name] → {'spend': array, 'revenue': array}
        preferred_model: 'log' or 'power' (default: 'log')

    Returns:
        Dict[account_name] → (predict_fn, params, r2, model_name)
    """
    results = {}
    failed_accounts = []

    # Phase 1: Try preferred model for all accounts
    for account, data in account_data.items():
        fn, params, r2, model_name = fit_response_curve(
            data['spend'],
            data['revenue'],
            force_model=preferred_model
        )
        results[account] = (fn, params, r2, model_name)

        if model_name == 'linear_fallback':
            failed_accounts.append(account)

    # Phase 2: If any accounts failed preferred model, refit ALL with fallback
    if failed_accounts and preferred_model == 'log':
        print(f"⚠️  Log fit failed for {', '.join(failed_accounts)}. "
              f"Refitting all accounts with power curve for consistency.")

        # Refit all accounts with power curve
        power_failed = []
        for account, data in account_data.items():
            fn, params, r2, model_name = fit_response_curve(
                data['spend'],
                data['revenue'],
                force_model='power'
            )
            results[account] = (fn, params, r2, model_name)

            if model_name == 'linear_fallback':
                power_failed.append(account)

        # Flag accounts that failed both log and power
        if power_failed:
            print(f"⚠️  {', '.join(power_failed)} could not be fitted with log or power curves — "
                  f"using linear proxy. Cross-account mROAS comparisons involving these accounts "
                  f"are approximate.")

    return results


def fit_response_curve(spend_arr, revenue_arr, force_model: str | None = None):
    """
    Fit a log curve (primary) to weekly spend/revenue data.
    Falls back to power curve if the log fit fails or yields a negative slope,
    and to a linear proxy if both fail or n < 3.

    Args:
        spend_arr: Weekly spend observations
        revenue_arr: Weekly revenue observations
        force_model: If specified, only attempt this model ('log' or 'power')
                    Used by fit_portfolio_curves for consistency enforcement

    Returns (predict_fn, params, r_squared, model_name)
    where predict_fn(x) → predicted revenue at spend x (weekly scale).

    Why log? For small, saturating markets (e.g. Landal BE/NL/DE) the log
    function's derivative a/x captures fast early diminishing returns and
    matches empirical R² better than power across 6-month windows.
    Calibration to actual ROAS corrects the absolute level separately.
    """
    spend   = np.array(spend_arr,   dtype=float)
    revenue = np.array(revenue_arr, dtype=float)

    # Remove zero-spend rows (no signal)
    mask    = (spend > 0) & (revenue >= 0)
    spend   = spend[mask]
    revenue = revenue[mask]

    def r_squared(y_true, y_pred):
        ss_res = np.sum((y_true - y_pred) ** 2)
        ss_tot = np.sum((y_true - y_true.mean()) ** 2)
        return 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    if len(spend) < 3:
        # Insufficient data — use simple average ROAS as linear proxy
        avg_roas = (revenue / spend).mean() if spend.mean() > 0 else 0.0
        raw_fn = lambda x, r=avg_roas: r * np.asarray(x, dtype=float)
        min_observed_spend = float(spend.min()) if len(spend) else 0.0
        anchor_revenue = float(raw_fn(min_observed_spend)) if len(spend) else 0.0
        return make_safe_predictor(raw_fn, min_observed_spend, anchor_revenue), [avg_roas, 1.0], 0.0, 'linear_fallback'

    chosen = None

    # ── Primary: log curve revenue = a * ln(spend) + b ───────
    if force_model is None or force_model == 'log':
        try:
            with suppress_curve_fit_warnings():
                params, _ = curve_fit(log_curve, spend, revenue, p0=[revenue.mean(), 0], maxfev=10000)
            if params[0] > 0:  # positive slope required
                r2 = r_squared(revenue, log_curve(spend, *params))
                chosen = {'r2': r2, 'model': log_curve, 'params': params, 'name': 'log'}
        except Exception:
            pass

    # ── Fallback: power curve revenue = a * spend^b ──────────
    if chosen is None and (force_model is None or force_model == 'power'):
        try:
            p0 = [revenue.mean() / max(spend.mean() ** 0.7, 1e-9), 0.7]
            with suppress_curve_fit_warnings():
                params, _ = curve_fit(power_curve, spend, revenue, p0=p0,
                                      bounds=([0, POWER_B_MIN], [np.inf, POWER_B_MAX]), maxfev=10000)
            r2 = r_squared(revenue, power_curve(spend, *params))
            chosen = {'r2': r2, 'model': power_curve, 'params': params, 'name': 'power'}
        except Exception:
            pass

    if chosen is None:
        avg_roas = (revenue / spend).mean() if spend.mean() > 0 else 0.0
        raw_fn = lambda x, r=avg_roas: r * np.asarray(x, dtype=float)
        min_observed_spend = float(spend.min()) if len(spend) else 0.0
        anchor_revenue = float(raw_fn(min_observed_spend)) if len(spend) else 0.0
        return make_safe_predictor(raw_fn, min_observed_spend, anchor_revenue), [avg_roas, 1.0], 0.0, 'linear_fallback'

    fn   = chosen['model']
    p    = chosen['params']
    name = chosen['name']
    raw_fn = lambda x, fn=fn, p=p: fn(x, *p)
    min_observed_spend = float(spend.min()) if len(spend) else 0.0
    anchor_revenue = float(np.maximum(raw_fn(min_observed_spend), 0.0)) if len(spend) else 0.0
    return make_safe_predictor(raw_fn, min_observed_spend, anchor_revenue), p, chosen['r2'], name
