"""
Budget optimization engine using SLSQP constrained nonlinear optimization.
"""
import numpy as np
import pandas as pd
from scipy.optimize import minimize


def prepare_bounds(accounts, total_budget, min_spend=None, max_spend=None):
    """Validate per-account bounds and overall budget feasibility."""
    min_spend = min_spend or {}
    max_spend = max_spend or {}

    if total_budget < 0:
        raise ValueError('total budget must be non-negative.')

    bounds = []
    invalid = []
    for acc in accounts:
        lo = max(float(min_spend.get(acc, 0.0)), 0.0)
        hi = min(float(max_spend.get(acc, total_budget)), total_budget)
        if hi < lo:
            invalid.append(f'{acc} (min {lo:.2f} > max {hi:.2f})')
        bounds.append((lo, hi))

    if invalid:
        raise ValueError('invalid bounds: ' + '; '.join(invalid))

    sum_min = sum(lo for lo, _ in bounds)
    sum_max = sum(hi for _, hi in bounds)
    tol = 1e-6
    if sum_min > total_budget + tol:
        raise ValueError(
            f'infeasible minimum spends: total minimum {sum_min:.2f} exceeds budget {total_budget:.2f}.'
        )
    if sum_max < total_budget - tol:
        raise ValueError(
            f'infeasible maximum spends: total maximum {sum_max:.2f} is below budget {total_budget:.2f}.'
        )

    return bounds


def build_feasible_start(bounds, total_budget):
    """Construct a starting point that satisfies all bounds and sums to budget."""
    lows = np.array([lo for lo, _ in bounds], dtype=float)
    highs = np.array([hi for _, hi in bounds], dtype=float)
    x0 = lows.copy()
    remaining = float(total_budget - x0.sum())
    tol = 1e-9

    if remaining <= tol:
        return x0

    headroom = highs - lows
    total_headroom = float(headroom.sum())
    if total_headroom <= tol:
        raise ValueError('unable to build a feasible starting allocation within the provided bounds.')

    x0 += remaining * (headroom / total_headroom)
    return np.minimum(np.maximum(x0, lows), highs)


def optimize_budget(predict_fns, total_budget, min_spend=None, max_spend=None):
    """
    Maximize Σ predict_fn_i(spend_i) subject to Σspend_i = total_budget.

    predict_fns : dict {account_name: callable(spend) → revenue}
    total_budget: float
    min_spend   : dict {account_name: min_value}   (optional)
    max_spend   : dict {account_name: max_value}   (optional)

    Returns (optimal_alloc dict, success bool, total_predicted_revenue float)
    """
    accounts  = list(predict_fns.keys())
    bounds = prepare_bounds(accounts, total_budget, min_spend, max_spend)
    x0 = build_feasible_start(bounds, total_budget)

    def objective(x):
        return -sum(predict_fns[acc](x[i]) for i, acc in enumerate(accounts))

    constraints = [{'type': 'eq', 'fun': lambda x: np.sum(x) - total_budget}]

    result = minimize(
        objective, x0,
        method='SLSQP',
        bounds=bounds,
        constraints=constraints,
        options={'maxiter': 2000, 'ftol': 1e-10}
    )

    if not result.success:
        raise RuntimeError(f'optimizer failed to converge: {result.message}')

    tol = 1e-4
    if abs(np.sum(result.x) - total_budget) > tol:
        raise RuntimeError(
            f'optimizer returned an invalid allocation: total spend {np.sum(result.x):.4f} '
            f'does not match budget {total_budget:.4f}.'
        )
    for acc, spend, (lo, hi) in zip(accounts, result.x, bounds):
        if spend < lo - tol or spend > hi + tol:
            raise RuntimeError(
                f'optimizer returned an out-of-bounds allocation for {acc}: '
                f'{spend:.4f} not in [{lo:.4f}, {hi:.4f}].'
            )

    optimal_alloc = dict(zip(accounts, result.x))
    predicted_rev = -result.fun
    return optimal_alloc, True, predicted_rev


def optimize_with_inequality_constraint(
    predict_fns,
    max_budget,
    fixed_accounts=None,
    min_spend=None,
    max_spend=None
):
    """
    Maximize Σ predict_fn_i(spend_i) subject to Σspend_i ≤ max_budget (inequality).

    This is the Phase 3 optimizer for Scenario C reallocation. Unlike the main
    optimize_budget() which enforces Σspend = budget (equality), this uses an
    inequality constraint to allow unallocated budget when all accounts hit
    their breakeven caps.

    Args:
        predict_fns: Dict {account_name: callable(spend) → revenue}
        max_budget: Maximum total budget (inequality constraint, not equality)
        fixed_accounts: Dict {account_name: fixed_spend} for capped accounts
        min_spend: Dict {account_name: min_value} for variable accounts
        max_spend: Dict {account_name: max_value} for variable accounts (breakevens)

    Returns:
        Tuple of (optimal_alloc dict, success bool, total_revenue float, diagnosis dict)

        diagnosis dict contains:
        - 'binding_constraint': 'budget' | 'breakeven' | 'mixed' | 'none'
        - 'budget_used': float
        - 'budget_available': float
        - 'budget_slack': float (positive = unused budget)
        - 'accounts_at_breakeven': list of account names
        - 'breakeven_headroom': float (sum of breakevens - budget_used)
    """
    fixed_accounts = fixed_accounts or {}
    min_spend = min_spend or {}
    max_spend = max_spend or {}

    # Separate fixed and variable accounts
    all_accounts = list(predict_fns.keys())
    fixed_accts = list(fixed_accounts.keys())
    variable_accts = [acc for acc in all_accounts if acc not in fixed_accts]

    # Calculate budget already consumed by fixed accounts
    fixed_total = sum(fixed_accounts.values())
    available_budget = max_budget - fixed_total

    if available_budget < -1e-6:
        raise ValueError(
            f'Fixed accounts consume €{fixed_total:,.0f}, exceeding max budget €{max_budget:,.0f}'
        )

    # If no variable accounts, return fixed allocations
    if not variable_accts:
        diagnosis = {
            'binding_constraint': 'none',
            'budget_used': fixed_total,
            'budget_available': max_budget,
            'budget_slack': max_budget - fixed_total,
            'accounts_at_breakeven': list(fixed_accts),
            'breakeven_headroom': 0.0
        }
        total_revenue = sum(
            predict_fns[acc](spend) for acc, spend in fixed_accounts.items()
        )
        return fixed_accounts.copy(), True, total_revenue, diagnosis

    # Prepare bounds for variable accounts
    bounds = []
    for acc in variable_accts:
        lo = max(float(min_spend.get(acc, 0.0)), 0.0)
        hi = min(float(max_spend.get(acc, available_budget)), available_budget)
        if hi < lo:
            raise ValueError(f'{acc}: min {lo:.2f} > max {hi:.2f}')
        bounds.append((lo, hi))

    # Build feasible starting point for variable accounts
    # Start with proportional allocation of available budget
    lows = np.array([lo for lo, _ in bounds], dtype=float)
    highs = np.array([hi for _, hi in bounds], dtype=float)

    # Start at minimum spend for all accounts
    x0 = lows.copy()
    remaining = available_budget - x0.sum()

    if remaining > 1e-9:
        # Distribute remaining budget proportionally to headroom
        headroom = highs - lows
        total_headroom = headroom.sum()
        if total_headroom > 1e-9:
            x0 += remaining * (headroom / total_headroom)
            x0 = np.minimum(np.maximum(x0, lows), highs)

    # Objective: maximize revenue
    def objective(x):
        return -sum(predict_fns[acc](x[i]) for i, acc in enumerate(variable_accts))

    # Constraint: Σspend ≤ available_budget (inequality, not equality)
    constraints = [
        {'type': 'ineq', 'fun': lambda x: available_budget - np.sum(x)}
    ]

    result = minimize(
        objective, x0,
        method='SLSQP',
        bounds=bounds,
        constraints=constraints,
        options={'maxiter': 2000, 'ftol': 1e-10}
    )

    if not result.success:
        raise RuntimeError(f'Optimizer failed to converge: {result.message}')

    # Build final allocation (fixed + optimized)
    optimal_alloc = fixed_accounts.copy()
    for i, acc in enumerate(variable_accts):
        optimal_alloc[acc] = result.x[i]

    # Calculate total revenue
    total_revenue = -result.fun + sum(
        predict_fns[acc](spend) for acc, spend in fixed_accounts.items()
    )

    # Constraint diagnosis
    budget_used = sum(optimal_alloc.values())
    budget_slack = max_budget - budget_used

    # Check which accounts are at their breakeven (max bound)
    tol_pct = 0.01  # 1% tolerance for "at breakeven"
    accounts_at_breakeven = list(fixed_accts)  # Fixed accounts are already at breakeven

    for i, acc in enumerate(variable_accts):
        max_allowed = bounds[i][1]
        if max_allowed > 0 and result.x[i] >= max_allowed * (1 - tol_pct):
            accounts_at_breakeven.append(acc)

    # Calculate breakeven headroom (sum of all breakevens - budget used)
    total_breakeven_ceiling = sum(max_spend.get(acc, max_budget) for acc in all_accounts)
    breakeven_headroom = total_breakeven_ceiling - budget_used

    # Determine binding constraint
    budget_binds = budget_slack / max_budget < 0.001  # Budget is tight (< 0.1%)
    breakeven_binds = len(accounts_at_breakeven) > 0

    if budget_binds and not breakeven_binds:
        binding_constraint = 'budget'
    elif breakeven_binds and not budget_binds:
        binding_constraint = 'breakeven'
    elif budget_binds and breakeven_binds:
        binding_constraint = 'mixed'
    else:
        binding_constraint = 'none'

    diagnosis = {
        'binding_constraint': binding_constraint,
        'budget_used': budget_used,
        'budget_available': max_budget,
        'budget_slack': budget_slack,
        'accounts_at_breakeven': accounts_at_breakeven,
        'breakeven_headroom': breakeven_headroom
    }

    return optimal_alloc, True, total_revenue, diagnosis


def run_sensitivity(predict_fns, base_budget, min_spend=None, max_spend=None):
    """Run optimization at budget ±30% in 10% steps."""
    rows = []
    for pct in range(-30, 41, 10):
        budget    = base_budget * (1 + pct / 100)
        row = {
            'budget_change_%': pct,
            'total_budget':    round(budget, 2),
            'predicted_revenue': None,
            'roas': None,
            'status': 'ok',
            'note': ''
        }
        try:
            alloc, ok, rev = optimize_budget(predict_fns, budget, min_spend, max_spend)
            row['predicted_revenue'] = round(rev, 2)
            row['roas'] = round(rev / budget, 3) if budget > 0 else 0
            for acc, spend in alloc.items():
                row[f'spend_{acc}'] = round(spend, 2)
        except (ValueError, RuntimeError) as exc:
            row['status'] = 'infeasible'
            row['note'] = str(exc)
        rows.append(row)
    return pd.DataFrame(rows)
