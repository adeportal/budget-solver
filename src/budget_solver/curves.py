"""
Response curve models and fitting logic.
"""
import warnings
from contextlib import contextmanager

import numpy as np
from scipy.optimize import curve_fit
from scipy.optimize import minimize as _scipy_minimize

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


def _fit_log_robust(spend: np.ndarray, revenue: np.ndarray,
                    weights: np.ndarray | None = None):
    """
    Fit log curve revenue = a*ln(spend) + b using weighted Huber loss minimisation.

    weights: per-observation weights (e.g. exponential decay so recent weeks
             matter more). If None, uniform weighting is used.
    Delta is set adaptively from the MAD of unweighted OLS residuals (scale
    parameter only — does not need to be weighted).

    Returns (params, r_squared) where params = [a, b].
    R² is unweighted for comparability with previous runs.
    """
    w = np.ones(len(spend)) if weights is None else np.asarray(weights, dtype=float)
    w = w / w.mean()  # normalise so total weight = n, keeping objective scale stable

    # OLS initial guess via curve_fit (fast, used only to seed Huber; unweighted is fine)
    try:
        with suppress_curve_fit_warnings():
            p0, _ = curve_fit(log_curve, spend, revenue,
                              p0=[revenue.mean(), 0.0], maxfev=5000)
        if p0[0] <= 0:
            raise ValueError("negative slope in OLS seed")
    except Exception:
        log_mean = float(np.log(np.maximum(spend.mean(), 1e-9)))
        p0 = np.array([revenue.mean() / max(log_mean, 1e-9), 0.0])

    ols_resid = revenue - log_curve(spend, *p0)
    mad = float(np.median(np.abs(ols_resid - np.median(ols_resid))))
    delta = max(1.35 * mad, 0.01 * float(revenue.ptp() or revenue.mean()))

    def huber_obj(params):
        r = revenue - log_curve(spend, *params)
        return float(np.sum(w * np.where(
            np.abs(r) <= delta,
            0.5 * r ** 2,
            delta * (np.abs(r) - 0.5 * delta)
        )))

    res = _scipy_minimize(
        huber_obj, p0,
        method='Nelder-Mead',
        options={'maxiter': 20000, 'xatol': 1e-7, 'fatol': 1e-7, 'adaptive': True}
    )
    params = res.x if (res.success and res.x[0] > 0) else p0

    # R² unweighted — keeps diagnostic comparable across runs
    ss_res = float(np.sum((revenue - log_curve(spend, *params)) ** 2))
    ss_tot = float(np.sum((revenue - revenue.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return params, r2


def fit_two_stage_curves(account_data: dict, preferred_model: str = 'log',
                         half_life_weeks: float = 16.0) -> dict:
    """
    Fit two-stage spend → clicks → revenue curves.

    Stage 1: clicks = g(spend)  — log or power curve on weekly spend/clicks data
    Stage 2: revenue = clicks × revenue_per_click  — rpc is a scalar from training data

    Portfolio-wide curve family consistency is enforced (same rule as fit_portfolio_curves).
    The returned params are EQUIVALENT REVENUE CURVE params so that all downstream
    mROAS and breakeven calculations work without modification:
        log:   a_r = a_clicks × rpc,  b_r = b_clicks × rpc   →  mROAS = a_r / spend
        power: a_r = a_clicks × rpc,  b_r = b_clicks          →  exponent unchanged

    R² is computed on revenue (not clicks) for fair comparison with single-stage fits.
    model_name is suffixed with '+2stage' so callers can identify the fit type.

    Why separate the two stages?
      CPCs rise as spend increases (Smart Bidding pushes into pricier auctions), so the
      spend→clicks relationship captures diminishing click efficiency independently from
      click quality (CVR × AOV). This makes the model more robust when CPC is changing
      or when spend is pushed outside the historical range.
    """
    # ── Stage 1: fit clicks curves per account ───────────────
    clicks_results = {}
    failed_accounts = []

    for account, data in account_data.items():
        clicks = data.get('clicks', np.array([]))
        spend  = data['spend']

        # Filter valid rows: positive spend and non-negative clicks
        mask   = (spend > 0) & (clicks >= 0)
        sp_c   = spend[mask]
        cl_c   = clicks[mask]

        if len(sp_c) < 3 or cl_c.sum() == 0:
            clicks_results[account] = None  # insufficient data
            failed_accounts.append(account)
            continue

        fn, params, _, name = fit_response_curve(sp_c, cl_c, force_model=preferred_model,
                                                  half_life_weeks=half_life_weeks)
        if name == 'linear_fallback':
            failed_accounts.append(account)
        clicks_results[account] = (fn, params, name, sp_c, cl_c)

    # ── Enforce portfolio-wide curve family consistency ──────
    if failed_accounts and preferred_model == 'log':
        print(f"  [2-stage] Log fit failed for {', '.join(failed_accounts)}. "
              f"Refitting all accounts with power curve.")
        for account, data in account_data.items():
            clicks = data.get('clicks', np.array([]))
            spend  = data['spend']
            mask   = (spend > 0) & (clicks >= 0)
            sp_c, cl_c = spend[mask], clicks[mask]
            if len(sp_c) < 3 or cl_c.sum() == 0:
                clicks_results[account] = None
                continue
            fn, params, _, name = fit_response_curve(sp_c, cl_c, force_model='power',
                                                      half_life_weeks=half_life_weeks)
            clicks_results[account] = (fn, params, name, sp_c, cl_c)

    # ── Stage 2: compute revenue_per_click + build combined predict_fn ──
    results = {}
    for account, data in account_data.items():
        clicks  = data.get('clicks', np.array([]))
        spend   = data['spend']
        revenue = data['revenue']

        cr = clicks_results.get(account)
        if cr is None:
            # Fall back to single-stage for this account
            fn, params, r2, name = fit_response_curve(spend, revenue,
                                                       half_life_weeks=half_life_weeks)
            results[account] = (fn, params, r2, name)
            continue

        clicks_fn, clicks_params, clicks_model, sp_c, cl_c = cr

        # Revenue per click from training data (total revenue / total clicks)
        total_clicks  = float(np.sum(clicks[clicks > 0]))
        total_revenue = float(np.sum(revenue[clicks > 0]))
        rpc = total_revenue / total_clicks if total_clicks > 0 else 1.0

        # Equivalent revenue curve params (so mROAS formulas work unchanged)
        if clicks_model == 'log':
            a_r = clicks_params[0] * rpc
            b_r = clicks_params[1] * rpc
            equiv_params = [a_r, b_r]
        elif clicks_model == 'power':
            a_r = clicks_params[0] * rpc
            b_r = clicks_params[1]           # exponent is dimensionless
            equiv_params = [a_r, b_r]
        else:
            # linear_fallback: clicks = avg_ctr × spend → revenue = avg_ctr × rpc × spend
            equiv_params = [clicks_params[0] * rpc, 1.0]

        # Combined predict function
        raw_predict = lambda x, fn=clicks_fn, r=rpc: fn(x) * r
        min_sp = float(sp_c.min()) if len(sp_c) else 0.0
        anchor = float(max(raw_predict(min_sp), 0.0)) if len(sp_c) else 0.0
        predict_fn = make_safe_predictor(raw_predict, min_sp, anchor)

        # R² on revenue for fair comparison with single-stage
        rev_mask  = (spend > 0) & (revenue >= 0)
        rev_pred  = np.array([predict_fn(s) for s in spend[rev_mask]])
        rev_true  = revenue[rev_mask]
        ss_res = float(np.sum((rev_true - rev_pred) ** 2))
        ss_tot = float(np.sum((rev_true - rev_true.mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

        results[account] = (predict_fn, equiv_params, r2, f'{clicks_model}+2stage')

    return results


def fit_portfolio_curves(account_data: dict, preferred_model: str = 'log',
                         half_life_weeks: float = 16.0) -> dict:
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
        half_life_weeks: Exponential decay half-life for recency weighting (passed to fit_response_curve)

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
            force_model=preferred_model,
            half_life_weeks=half_life_weeks,
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
                force_model='power',
                half_life_weeks=half_life_weeks,
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


def fit_response_curve(spend_arr, revenue_arr, force_model: str | None = None,
                       half_life_weeks: float = 16.0):
    """
    Fit a log curve (primary) to weekly spend/revenue data.
    Falls back to power curve if the log fit fails or yields a negative slope,
    and to a linear proxy if both fail or n < 3.

    Args:
        spend_arr: Weekly spend observations (chronologically sorted, oldest first)
        revenue_arr: Weekly revenue observations
        force_model: If specified, only attempt this model ('log' or 'power')
                    Used by fit_portfolio_curves for consistency enforcement
        half_life_weeks: Exponential decay half-life for recency weighting.
                        Recent weeks receive higher weight; set to np.inf to disable.

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

    # Exponential decay weights: index 0 = oldest, index n-1 = most recent → age 0
    n = len(spend)
    if np.isfinite(half_life_weeks) and half_life_weeks > 0:
        decay = np.log(2) / half_life_weeks
        ages  = np.arange(n - 1, -1, -1, dtype=float)  # 0 = most recent
        weights = np.exp(-decay * ages)
        weights = weights / weights.mean()  # normalise so sum = n
    else:
        weights = np.ones(n, dtype=float)

    chosen = None

    # ── Primary: log curve revenue = a * ln(spend) + b ───────
    if force_model is None or force_model == 'log':
        try:
            params, r2 = _fit_log_robust(spend, revenue, weights=weights)
            if params[0] > 0:  # positive slope required
                chosen = {'r2': r2, 'model': log_curve, 'params': params, 'name': 'log'}
        except Exception:
            pass

    # ── Fallback: power curve revenue = a * spend^b ──────────
    if chosen is None and (force_model is None or force_model == 'power'):
        try:
            p0 = [revenue.mean() / max(spend.mean() ** 0.7, 1e-9), 0.7]
            # sigma = 1/sqrt(w) → high-weight (recent) observations have smaller sigma
            sigma = 1.0 / np.sqrt(weights)
            with suppress_curve_fit_warnings():
                params, _ = curve_fit(power_curve, spend, revenue, p0=p0,
                                      bounds=([0, POWER_B_MIN], [np.inf, POWER_B_MAX]),
                                      sigma=sigma, absolute_sigma=True, maxfev=10000)
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
