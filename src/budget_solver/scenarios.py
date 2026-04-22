"""
Scenario generation for budget optimization.

This module defines the data model for the 4-scenario framework (A, B, C, D)
and provides functions to generate each scenario from fitted response curves.
"""
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from budget_solver.constants import (
    DAYS_PER_MONTH,
    WEEKS_PER_MONTH,
    BUDGET_NEUTRAL_THRESHOLD,
    MIN_CHANGE_FOR_LABEL_EUR,
    BUDGET_SLACK_EPSILON
)
from budget_solver.mroas import instantaneous_mroas, discrete_mroas
from budget_solver.solver import optimize_with_inequality_constraint
from budget_solver.stability import apply_change_limit, phasing_warnings, detect_recent_churn


@dataclass
class AccountAllocation:
    """Per-account allocation details within a scenario."""

    account: str
    daily_spend: float
    monthly_spend: float
    daily_revenue: float
    monthly_revenue: float
    roas: float
    inst_mroas: float

    # Discrete mROAS vs previous scenario in the sequence (A→B→C→D)
    # None means "no previous scenario" (e.g., Scenario A baseline)
    # 0.0 means "account didn't move" (mathematically valid but rare)
    discrete_mroas_vs_prev: float | None = None

    # Change vs previous scenario
    change_vs_prev: float = 0.0                    # monthly €, signed
    change_label: str = "— unchanged"              # "▲ +€X", "▼ −€X", "— unchanged"


@dataclass
class Scenario:
    """A single scenario (A, B, C, C1, or D) with portfolio and account-level metrics."""

    id: str                                        # "A", "B", "C", "C1", "D"
    name: str                                      # "Current Run Rate", "Target Budget", ...
    description: str                               # one-paragraph explainer

    # Portfolio-level metrics (realized values, not targets)
    # budget_monthly is the ACTUAL budget for this scenario, which may be less than
    # the user's --budget input if breakeven constraints bind (Phase 3)
    budget_monthly: float
    revenue_monthly: float
    blended_roas: float

    # Portfolio discrete mROAS vs previous scenario
    # None for baseline (A) or budget-neutral reallocations (C1) where ΔSpend ≈ 0
    portfolio_discrete_mroas: float | None

    # Per-account allocations
    allocations: list[AccountAllocation]

    # Warnings about structural issues (e.g., "LDL DACH below 2.5x floor")
    warnings: list[str] = field(default_factory=list)

    # True only for Scenario C or C1
    recommended: bool = False

    # True if Scenario C matches B exactly (no reallocation needed)
    no_reallocation: bool = False


@dataclass
class ScenarioSet:
    """Complete set of scenarios (A, B, C/C1, D) with shared context."""

    scenarios: list[Scenario]

    # Fitted response curves (account -> callable)
    # Retained for downstream use in:
    # - Phase 3: recomputing breakeven bounds with min-mroas constraint
    # - Phase 5: translating inst. mROAS to terminal ROAS in narrative
    # - Phase 6: curve diagnostics in Excel sheets
    predict_fns: dict

    # Model info (account -> (fn, params, r2, mname))
    # Used in Phase 6 for curve diagnostics
    model_info: dict

    # Minimum instantaneous mROAS floor (typically 2.5x)
    min_mroas: float

    generated_at: datetime


# ══════════════════════════════════════════════════════════════════════════════
# Scenario Generators
# ══════════════════════════════════════════════════════════════════════════════

def scenario_a(
    df: pd.DataFrame,
    predict_fns: dict,
    model_info: dict,
    min_mroas: float,
    baseline_window_days: int = 7
) -> Scenario:
    """
    Generate Scenario A: Current Run Rate (baseline).

    Takes actual spend from the last N days, extrapolates to monthly (daily × 30.4),
    and computes revenue from the calibrated curve (NOT raw actuals).

    Args:
        df: Input dataframe with columns: account_name, cost, date (or week_start/week)
        predict_fns: Dict of account -> callable(monthly_spend) -> monthly_revenue
        model_info: Dict of account -> (fn, params, r2, model_name)
        min_mroas: Minimum instantaneous mROAS floor (for warnings)
        baseline_window_days: Number of recent days to use for baseline (default 7)

    Returns:
        Scenario A with baseline allocations
    """
    # Find the date column
    date_col = next((c for c in ('date', 'week_start', 'week') if c in df.columns), None)
    if not date_col:
        raise ValueError("DataFrame must have a 'date', 'week_start', or 'week' column")

    # Parse dates and get the last N days
    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
    latest_date = df[date_col].max()
    cutoff_date = latest_date - pd.Timedelta(days=baseline_window_days - 1)

    recent_df = df[(df[date_col] >= cutoff_date) & (df[date_col] <= latest_date)]

    if len(recent_df) == 0:
        raise ValueError(f"No data found in the last {baseline_window_days} days")

    # Compute daily spend per account (sum over N days / N)
    spend_by_account = recent_df.groupby('account_name')['cost'].sum()
    daily_spend = spend_by_account / baseline_window_days

    # Build allocations
    allocations = []
    warnings = []

    for account in sorted(predict_fns.keys()):
        daily_sp = daily_spend.get(account, 0.0)
        monthly_sp = daily_sp * DAYS_PER_MONTH

        # Revenue from CURVE, not actuals
        monthly_rev = predict_fns[account](monthly_sp)
        daily_rev = monthly_rev / DAYS_PER_MONTH

        roas = monthly_rev / monthly_sp if monthly_sp > 0 else 0.0

        # Instantaneous mROAS (cap reference)
        _, params, _, model_name = model_info[account]
        # Curves are fitted on WEEKLY data, so convert daily spend to weekly for derivative
        weekly_sp = daily_sp * 7
        inst_mroas_val = instantaneous_mroas(params, model_name, weekly_sp)

        # Check for below-floor warnings
        if inst_mroas_val < min_mroas and monthly_sp > 0:
            pct_below = (1 - inst_mroas_val / min_mroas) * 100
            warnings.append(
                f"{account}: inst. mROAS = {inst_mroas_val:.2f}x "
                f"({pct_below:.0f}% below {min_mroas:.1f}x floor)"
            )

        allocations.append(AccountAllocation(
            account=account,
            daily_spend=daily_sp,
            monthly_spend=monthly_sp,
            daily_revenue=daily_rev,
            monthly_revenue=monthly_rev,
            roas=roas,
            inst_mroas=inst_mroas_val,
            discrete_mroas_vs_prev=None,  # No previous scenario
            change_vs_prev=0.0,
            change_label="— baseline"
        ))

    # Portfolio totals
    total_monthly_spend = sum(a.monthly_spend for a in allocations)
    total_monthly_revenue = sum(a.monthly_revenue for a in allocations)
    blended_roas = total_monthly_revenue / total_monthly_spend if total_monthly_spend > 0 else 0.0

    description = (
        f"Baseline performance from the last {baseline_window_days} days "
        f"({cutoff_date.date()} to {latest_date.date()}), extrapolated to monthly. "
        f"Revenue computed from calibrated response curves."
    )

    return Scenario(
        id="A",
        name="Current Run Rate",
        description=description,
        budget_monthly=total_monthly_spend,
        revenue_monthly=total_monthly_revenue,
        blended_roas=blended_roas,
        portfolio_discrete_mroas=None,  # Baseline has no previous scenario
        allocations=allocations,
        warnings=warnings,
        recommended=False
    )


def scenario_b(
    scenario_a: Scenario,
    target_budget: float,
    predict_fns: dict,
    model_info: dict,
    min_mroas: float
) -> Scenario:
    """
    Generate Scenario B: Target Budget (proportional scaling from A).

    Scales each account's spend by (target_budget / scenario_a.budget_monthly),
    recomputes revenue/ROAS/inst. mROAS at the new levels, and calculates discrete
    mROAS vs Scenario A.

    Args:
        scenario_a: Baseline scenario to scale from
        target_budget: Target monthly budget to allocate
        predict_fns: Dict of account -> callable(monthly_spend) -> monthly_revenue
        model_info: Dict of account -> (fn, params, r2, model_name)
        min_mroas: Minimum instantaneous mROAS floor (for warnings)

    Returns:
        Scenario B with proportionally scaled allocations
    """
    if scenario_a.budget_monthly <= 0:
        raise ValueError("Scenario A budget must be > 0 for proportional scaling")

    # Scaling factor
    scale = target_budget / scenario_a.budget_monthly

    # Build allocations
    allocations = []
    warnings = []

    # Look up Scenario A allocations by account for comparison
    a_allocs = {alloc.account: alloc for alloc in scenario_a.allocations}

    for account in sorted(predict_fns.keys()):
        a_alloc = a_allocs.get(account)
        if not a_alloc:
            # Account not in Scenario A (shouldn't happen, but handle gracefully)
            continue

        # Scale spend proportionally
        daily_sp = a_alloc.daily_spend * scale
        monthly_sp = daily_sp * DAYS_PER_MONTH

        # Revenue from curve at new spend level
        monthly_rev = predict_fns[account](monthly_sp)
        daily_rev = monthly_rev / DAYS_PER_MONTH

        roas = monthly_rev / monthly_sp if monthly_sp > 0 else 0.0

        # Instantaneous mROAS at new spend level
        _, params, _, model_name = model_info[account]
        weekly_sp = daily_sp * 7
        inst_mroas_val = instantaneous_mroas(params, model_name, weekly_sp)

        # Discrete mROAS vs Scenario A
        disc_mroas = discrete_mroas(
            rev_from=a_alloc.monthly_revenue,
            rev_to=monthly_rev,
            spend_from=a_alloc.monthly_spend,
            spend_to=monthly_sp
        )

        # Change vs A
        change_monthly = monthly_sp - a_alloc.monthly_spend
        if abs(change_monthly) >= 100:
            if change_monthly > 0:
                change_label = f"▲ +€{abs(change_monthly):,.0f}"
            else:
                change_label = f"▼ −€{abs(change_monthly):,.0f}"
        else:
            change_label = "— unchanged"

        # Check for below-floor warnings
        if inst_mroas_val < min_mroas and monthly_sp > 0:
            pct_below = (1 - inst_mroas_val / min_mroas) * 100
            warnings.append(
                f"{account}: inst. mROAS = {inst_mroas_val:.2f}x "
                f"({pct_below:.0f}% below {min_mroas:.1f}x floor) at target budget"
            )

        allocations.append(AccountAllocation(
            account=account,
            daily_spend=daily_sp,
            monthly_spend=monthly_sp,
            daily_revenue=daily_rev,
            monthly_revenue=monthly_rev,
            roas=roas,
            inst_mroas=inst_mroas_val,
            discrete_mroas_vs_prev=disc_mroas,
            change_vs_prev=change_monthly,
            change_label=change_label
        ))

    # Portfolio totals
    total_monthly_spend = sum(a.monthly_spend for a in allocations)
    total_monthly_revenue = sum(a.monthly_revenue for a in allocations)
    blended_roas = total_monthly_revenue / total_monthly_spend if total_monthly_spend > 0 else 0.0

    # Portfolio discrete mROAS vs A
    portfolio_disc_mroas = discrete_mroas(
        rev_from=scenario_a.revenue_monthly,
        rev_to=total_monthly_revenue,
        spend_from=scenario_a.budget_monthly,
        spend_to=total_monthly_spend
    )

    # Description
    change_pct = (scale - 1.0) * 100
    if scale > 1.0:
        direction = f"increased {change_pct:+.0f}%"
    elif scale < 1.0:
        direction = f"decreased {abs(change_pct):.0f}%"
    else:
        direction = "unchanged"

    description = (
        f"Target budget of €{target_budget:,.0f} allocated proportionally from Scenario A "
        f"(scaling factor: {scale:.2f}x, {direction}). "
        f"All accounts scaled equally — no reallocation optimization."
    )

    return Scenario(
        id="B",
        name="Target Budget",
        description=description,
        budget_monthly=total_monthly_spend,
        revenue_monthly=total_monthly_revenue,
        blended_roas=blended_roas,
        portfolio_discrete_mroas=portfolio_disc_mroas,
        allocations=allocations,
        warnings=warnings,
        recommended=False
    )


def scenario_d(
    predict_fns: dict,
    model_info: dict,
    min_mroas: float,
    scenario_b: Scenario = None  # Phase 2 placeholder: used for discrete mROAS until C is implemented
) -> Scenario:
    """
    Generate Scenario D: Maximum Justified Budget @ min_mroas Floor.

    Sets every account to its breakeven spend level where inst. mROAS = min_mroas.
    The sum of all breakevens defines the portfolio ceiling — the maximum budget
    that can be justified at the minimum efficiency threshold.

    Args:
        predict_fns: Dict of account -> callable(monthly_spend) -> monthly_revenue
        model_info: Dict of account -> (fn, params, r2, model_name)
        min_mroas: Minimum instantaneous mROAS floor (typically 2.5x)
        scenario_b: Scenario B for discrete mROAS comparison (Phase 2 placeholder)

    Returns:
        Scenario D with all accounts at breakeven spend
    """
    allocations = []

    # Build reference dict for Scenario B allocations (if provided)
    b_allocs = {}
    if scenario_b:
        b_allocs = {alloc.account: alloc for alloc in scenario_b.allocations}

    for account in sorted(predict_fns.keys()):
        _, params, _, model_name = model_info[account]

        # Breakeven calculation:
        # - Curves fitted on WEEKLY spend → weekly revenue
        # - Inst. mROAS at weekly spend w: a/w
        # - Setting a/w = min_mroas gives: w = a/min_mroas (weekly breakeven)
        # - Monthly breakeven = w × WEEKS_PER_MONTH
        a = params[0]
        weekly_breakeven = a / min_mroas
        monthly_sp = weekly_breakeven * WEEKS_PER_MONTH
        daily_sp = monthly_sp / DAYS_PER_MONTH

        # Revenue from curve at breakeven spend
        monthly_rev = predict_fns[account](monthly_sp)
        daily_rev = monthly_rev / DAYS_PER_MONTH

        roas = monthly_rev / monthly_sp if monthly_sp > 0 else 0.0

        # Instantaneous mROAS at breakeven (should be exactly min_mroas)
        inst_mroas_val = instantaneous_mroas(params, model_name, weekly_breakeven)

        # Discrete mROAS vs Scenario B (Phase 2 placeholder - will be C in Phase 3+)
        disc_mroas = None
        change_monthly = 0.0
        change_label = "— at breakeven"

        if scenario_b and account in b_allocs:
            b_alloc = b_allocs[account]
            disc_mroas = discrete_mroas(
                rev_from=b_alloc.monthly_revenue,
                rev_to=monthly_rev,
                spend_from=b_alloc.monthly_spend,
                spend_to=monthly_sp
            )
            change_monthly = monthly_sp - b_alloc.monthly_spend
            if abs(change_monthly) >= 100:
                if change_monthly > 0:
                    change_label = f"▲ +€{abs(change_monthly):,.0f}"
                else:
                    change_label = f"▼ −€{abs(change_monthly):,.0f}"
            else:
                change_label = "— unchanged"

        allocations.append(AccountAllocation(
            account=account,
            daily_spend=daily_sp,
            monthly_spend=monthly_sp,
            daily_revenue=daily_rev,
            monthly_revenue=monthly_rev,
            roas=roas,
            inst_mroas=inst_mroas_val,
            discrete_mroas_vs_prev=disc_mroas,
            change_vs_prev=change_monthly,
            change_label=change_label
        ))

    # Portfolio totals
    total_monthly_spend = sum(a.monthly_spend for a in allocations)
    total_monthly_revenue = sum(a.monthly_revenue for a in allocations)
    blended_roas = total_monthly_revenue / total_monthly_spend if total_monthly_spend > 0 else 0.0

    # Portfolio discrete mROAS vs B (Phase 2 placeholder)
    # TODO Phase 3: Update to use Scenario C instead of B
    portfolio_disc_mroas = None
    if scenario_b:
        portfolio_disc_mroas = discrete_mroas(
            rev_from=scenario_b.revenue_monthly,
            rev_to=total_monthly_revenue,
            spend_from=scenario_b.budget_monthly,
            spend_to=total_monthly_spend
        )

    description = (
        f"Maximum justified budget at {min_mroas:.1f}x instantaneous mROAS floor. "
        f"Every account pushed to its breakeven spend level (a/{min_mroas:.1f}). "
        f"This defines the portfolio ceiling — the upper limit of justifiable investment. "
        f"Not a recommendation for immediate action, but useful for sensitivity planning."
    )

    return Scenario(
        id="D",
        name=f"Max Justified @ {min_mroas:.1f}x Floor",
        description=description,
        budget_monthly=total_monthly_spend,
        revenue_monthly=total_monthly_revenue,
        blended_roas=blended_roas,
        portfolio_discrete_mroas=portfolio_disc_mroas,
        allocations=allocations,
        warnings=[],
        recommended=False
    )



# ══════════════════════════════════════════════════════════════════════════════
# Scenario C Helper Functions
# ══════════════════════════════════════════════════════════════════════════════

def _compute_breakevens(model_info: dict, min_mroas: float) -> dict:
    """
    Compute breakeven monthly spend for all accounts.

    Breakeven = spend level where inst. mROAS = min_mroas floor.
    Formula: weekly_breakeven = a / min_mroas, monthly = weekly × WEEKS_PER_MONTH

    Args:
        model_info: Dict[account] → (fn, params, r2, model_name)
        min_mroas: Minimum instantaneous mROAS floor

    Returns:
        Dict[account] → monthly_breakeven_spend
    """
    breakevens = {}
    for account in sorted(model_info.keys()):
        _, params, _, _ = model_info[account]
        a = params[0]
        weekly_breakeven = a / min_mroas
        monthly_breakeven = weekly_breakeven * WEEKS_PER_MONTH
        breakevens[account] = monthly_breakeven
    return breakevens


def _is_budget_neutral(budget_a: float, budget_b: float) -> bool:
    """Check if budget change is within neutral threshold (C1 detection)."""
    if budget_a <= 0:
        return False
    return abs(budget_b - budget_a) / budget_a < BUDGET_NEUTRAL_THRESHOLD


def _format_change_label(change_monthly: float) -> str:
    """Format change label with ▲/▼ arrows."""
    if abs(change_monthly) >= MIN_CHANGE_FOR_LABEL_EUR:
        if change_monthly > 0:
            return f"▲ +€{abs(change_monthly):,.0f}"
        else:
            return f"▼ −€{abs(change_monthly):,.0f}"
    return "— unchanged"


def _build_allocation_from_spend(
    account: str,
    monthly_spend: float,
    predict_fns: dict,
    model_info: dict,
    b_alloc: AccountAllocation = None
) -> AccountAllocation:
    """
    Build AccountAllocation from spend level.

    Args:
        account: Account name
        monthly_spend: Monthly spend level
        predict_fns: Response curve predictors
        model_info: Model parameters
        b_alloc: Previous scenario allocation for comparison (optional)

    Returns:
        AccountAllocation with computed metrics
    """
    daily_sp = monthly_spend / DAYS_PER_MONTH

    # Revenue from curve
    monthly_rev = predict_fns[account](monthly_spend)
    daily_rev = monthly_rev / DAYS_PER_MONTH

    roas = monthly_rev / monthly_spend if monthly_spend > 0 else 0.0

    # Inst. mROAS
    _, params, _, model_name = model_info[account]
    weekly_sp = daily_sp * 7
    inst_mroas_val = instantaneous_mroas(params, model_name, weekly_sp)

    # Change vs previous scenario
    if b_alloc:
        change_monthly = monthly_spend - b_alloc.monthly_spend
        disc_mroas = discrete_mroas(
            rev_from=b_alloc.monthly_revenue,
            rev_to=monthly_rev,
            spend_from=b_alloc.monthly_spend,
            spend_to=monthly_spend
        )
        change_label = _format_change_label(change_monthly)
    else:
        change_monthly = 0.0
        disc_mroas = None
        change_label = "— unchanged"

    return AccountAllocation(
        account=account,
        daily_spend=daily_sp,
        monthly_spend=monthly_spend,
        daily_revenue=daily_rev,
        monthly_revenue=monthly_rev,
        roas=roas,
        inst_mroas=inst_mroas_val,
        discrete_mroas_vs_prev=disc_mroas,
        change_vs_prev=change_monthly,
        change_label=change_label
    )


def _format_constraint_diagnosis(
    diagnosis: dict,
    breakevens: dict,
    min_mroas: float,
    budget_b: float
) -> str:
    """
    Format constraint binding diagnosis message.

    Args:
        diagnosis: Solver diagnosis dict (from optimize_with_inequality_constraint)
        breakevens: Per-account breakeven spend levels
        min_mroas: Minimum mROAS floor
        budget_b: Scenario B budget (target)

    Returns:
        Formatted constraint note string
    """
    binding = diagnosis['binding_constraint']
    budget_used = diagnosis['budget_used']
    budget_slack = diagnosis['budget_slack']
    accounts_at_breakeven = diagnosis['accounts_at_breakeven']

    # Override binding if ALL accounts at breakeven with significant slack
    if len(accounts_at_breakeven) == len(breakevens) and budget_slack > BUDGET_SLACK_EPSILON:
        binding = 'breakeven'

    if binding == 'budget':
        return (
            f"BUDGET CAP binds — allocated full €{budget_used:,.0f} "
            f"(slack: €{budget_slack:,.0f}). "
            f"Breakeven headroom: €{diagnosis['breakeven_headroom']:,.0f}."
        )
    elif binding == 'breakeven':
        total_breakeven = sum(breakevens.values())
        return (
            f"BREAKEVEN binds — all {len(accounts_at_breakeven)} account(s) at {min_mroas:.1f}x floor. "
            f"Portfolio ceiling: €{total_breakeven:,.0f}. "
            f"Unallocated budget: €{budget_slack:,.0f} "
            f"(cannot deploy without violating minimum efficiency)."
        )
    elif binding == 'mixed':
        return (
            f"MIXED binding — budget tight (slack: €{budget_slack:,.0f}) "
            f"with {len(accounts_at_breakeven)} account(s) at {min_mroas:.1f}x floor."
        )
    else:
        return (
            f"Budget allocated: €{budget_used:,.0f} of €{budget_b:,.0f} available "
            f"(slack: €{budget_slack:,.0f})."
        )


def _generate_breakeven_warnings(
    accounts_at_breakeven: list,
    optimal_alloc: dict,
    b_allocs: dict,
    to_cap: list,
    min_mroas: float
) -> list:
    """
    Generate warnings for accounts at breakeven.

    Args:
        accounts_at_breakeven: List of account names at breakeven
        optimal_alloc: Final optimized allocation
        b_allocs: Scenario B allocations (for comparison)
        to_cap: List of accounts that were capped from B
        min_mroas: Minimum mROAS floor

    Returns:
        List of warning messages
    """
    warnings = []
    for account in accounts_at_breakeven:
        final_spend = optimal_alloc[account]
        b_spend = b_allocs[account].monthly_spend

        if account in to_cap:
            warnings.append(
                f"{account}: capped at €{final_spend:,.0f}/mo "
                f"(was €{b_spend:,.0f} in Scenario B, {min_mroas:.1f}x breakeven)"
            )
        else:
            warnings.append(
                f"{account}: optimized to breakeven €{final_spend:,.0f}/mo "
                f"(was €{b_spend:,.0f} in Scenario B, {min_mroas:.1f}x floor)"
            )

    return warnings



def scenario_c(
    scenario_a: Scenario,
    scenario_b: Scenario,
    predict_fns: dict,
    model_info: dict,
    min_mroas: float
) -> Scenario:
    """
    Generate Scenario C: Recommended (breakeven-capped reallocation).

    Uses constrained optimization (Phase 3) to cap accounts above breakeven and
    optimally reallocate freed budget. Applies stability rules in build_scenarios().

    Args:
        scenario_a: Baseline scenario (for C1 detection)
        scenario_b: Target budget scenario (starting point for reallocation)
        predict_fns: Dict of account -> callable(monthly_spend) -> monthly_revenue
        model_info: Dict of account -> (fn, params, r2, model_name)
        min_mroas: Minimum instantaneous mROAS floor (typically 2.5x)

    Returns:
        Scenario C or C1 with breakeven-capped allocations
    """
    # Compute breakevens for all accounts
    breakevens = _compute_breakevens(model_info, min_mroas)

    # Build reference dict for Scenario B allocations
    b_allocs = {alloc.account: alloc for alloc in scenario_b.allocations}

    # Identify accounts that need capping (B spend > breakeven)
    to_cap = [acc for acc, b_alloc in b_allocs.items()
              if b_alloc.monthly_spend > breakevens[acc]]

    # ─────────────────────────────────────────────────────────────────────────
    # Case 1: No cuts needed — return B's allocation unchanged
    # ─────────────────────────────────────────────────────────────────────────
    if not to_cap:
        allocations = [
            _build_allocation_from_spend(
                account=b_alloc.account,
                monthly_spend=b_alloc.monthly_spend,
                predict_fns=predict_fns,
                model_info=model_info,
                b_alloc=b_alloc
            )
            for b_alloc in scenario_b.allocations
        ]

        is_c1 = _is_budget_neutral(scenario_a.budget_monthly, scenario_b.budget_monthly)
        scenario_id = "C1" if is_c1 else "C"
        scenario_name = "Budget-Neutral Reallocation" if is_c1 else "Recommended"

        total_breakeven_ceiling = sum(breakevens.values())
        breakeven_headroom = total_breakeven_ceiling - scenario_b.budget_monthly

        constraint_note = (
            f"BUDGET CAP binds — all accounts operating well below {min_mroas:.1f}x floor. "
            f"Breakeven headroom: €{breakeven_headroom:,.0f} "
            f"(portfolio ceiling: €{total_breakeven_ceiling:,.0f})."
        )

        description = (
            f"No cuts required — all accounts in Scenario B are below the {min_mroas:.1f}x "
            f"instantaneous mROAS floor. Allocation matches Scenario B exactly. "
            f"{constraint_note}"
        )

        return Scenario(
            id=scenario_id,
            name=scenario_name,
            description=description,
            budget_monthly=scenario_b.budget_monthly,
            revenue_monthly=scenario_b.revenue_monthly,
            blended_roas=scenario_b.blended_roas,
            portfolio_discrete_mroas=None if is_c1 else scenario_b.portfolio_discrete_mroas,
            allocations=allocations,
            warnings=[],
            recommended=True
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Case 2: Cuts needed — run constrained optimization
    # ─────────────────────────────────────────────────────────────────────────

    # Fix capped accounts at breakeven, optimize the rest
    fixed_allocations = {acc: breakevens[acc] for acc in to_cap}
    max_spend_dict = breakevens.copy()

    try:
        optimal_alloc, success, total_rev, diagnosis = optimize_with_inequality_constraint(
            predict_fns=predict_fns,
            max_budget=scenario_b.budget_monthly,
            fixed_accounts=fixed_allocations,
            min_spend=None,
            max_spend=max_spend_dict
        )
    except (ValueError, RuntimeError) as exc:
        raise RuntimeError(
            f"Scenario C optimization failed: {exc}. "
            f"This may indicate infeasible constraints or numerical issues."
        )

    # Build allocations from optimized spend levels
    allocations = [
        _build_allocation_from_spend(
            account=account,
            monthly_spend=optimal_alloc[account],
            predict_fns=predict_fns,
            model_info=model_info,
            b_alloc=b_allocs[account]
        )
        for account in sorted(predict_fns.keys())
    ]

    # Portfolio totals
    total_monthly_spend = sum(a.monthly_spend for a in allocations)
    total_monthly_revenue = sum(a.monthly_revenue for a in allocations)
    blended_roas = total_monthly_revenue / total_monthly_spend if total_monthly_spend > 0 else 0.0

    # Portfolio discrete mROAS vs B
    portfolio_disc_mroas = discrete_mroas(
        rev_from=scenario_b.revenue_monthly,
        rev_to=total_monthly_revenue,
        spend_from=scenario_b.budget_monthly,
        spend_to=total_monthly_spend
    )

    # C1 detection
    is_c1 = _is_budget_neutral(scenario_a.budget_monthly, scenario_b.budget_monthly)
    scenario_id = "C1" if is_c1 else "C"
    scenario_name = "Budget-Neutral Reallocation" if is_c1 else "Recommended"

    # Build description
    capped_names = ", ".join(to_cap)
    constraint_note = _format_constraint_diagnosis(diagnosis, breakevens, min_mroas, scenario_b.budget_monthly)

    description = (
        f"Breakeven-capped reallocation using constrained optimization (Phase 3). "
        f"Capped {len(to_cap)} account(s) at {min_mroas:.1f}x floor: {capped_names}. "
        f"Reallocated freed budget optimally across all non-capped accounts. "
        f"{constraint_note}"
    )

    # Generate warnings
    warnings = _generate_breakeven_warnings(
        diagnosis['accounts_at_breakeven'],
        optimal_alloc,
        b_allocs,
        to_cap,
        min_mroas
    )

    return Scenario(
        id=scenario_id,
        name=scenario_name,
        description=description,
        budget_monthly=total_monthly_spend,
        revenue_monthly=total_monthly_revenue,
        blended_roas=blended_roas,
        portfolio_discrete_mroas=None if is_c1 else portfolio_disc_mroas,
        allocations=allocations,
        warnings=warnings,
        recommended=True
    )


def build_scenarios(
    df: pd.DataFrame,
    predict_fns: dict,
    model_info: dict,
    target_budget: float,
    min_mroas: float = 2.5,
    baseline_window_days: int = 7,
    max_account_changes: int = 2,
    wow_cap: float = 0.20,
    apply_stability: bool = True,
) -> ScenarioSet:
    """
    Build complete scenario set (A, B, C, D) with optional stability rules.

    Phase 4: This orchestrator applies stability rules to Scenario C by default.

    Args:
        df: Input dataframe
        predict_fns: Response curve predictors
        model_info: Model parameters
        target_budget: Monthly budget target for Scenario B
        min_mroas: Minimum instantaneous mROAS floor (default 2.5x)
        baseline_window_days: Days for Scenario A baseline (default 7)
        max_account_changes: Maximum account changes in C vs B (default 2, 0=unlimited)
        wow_cap: Week-over-week change cap for phasing warnings (default 0.20)
        apply_stability: Apply stability rules to C (default True)

    Returns:
        ScenarioSet with A, B, C (with stability), D
    """
    # Generate base scenarios
    scen_a = scenario_a(df, predict_fns, model_info, min_mroas, baseline_window_days)
    scen_b = scenario_b(scen_a, target_budget, predict_fns, model_info, min_mroas)
    scen_c_raw = scenario_c(scen_a, scen_b, predict_fns, model_info, min_mroas)

    # Apply stability rules to C
    if apply_stability:
        # Apply change limit (top-N ranking)
        scen_c = apply_change_limit(
            scen_c_raw, scen_b, predict_fns, model_info, min_mroas, max_account_changes
        )

        # Detect if C matches B exactly (no reallocation needed)
        # Build lookup dicts
        b_allocs_dict = {a.account: a for a in scen_b.allocations}
        c_allocs_dict = {a.account: a for a in scen_c.allocations}

        # Check if all accounts in C match B within €1
        matches_b = all(
            abs(c_allocs_dict[acc].monthly_spend - b_allocs_dict[acc].monthly_spend) < 1.0
            for acc in c_allocs_dict.keys()
        )

        if matches_b:
            # C matches B exactly — set flag
            from dataclasses import replace
            scen_c = replace(scen_c, no_reallocation=True)

        # Add phasing warnings (phase from A, not B)
        phasing_warns = phasing_warnings(scen_c, scen_a, wow_cap)
        scen_c.warnings.extend(phasing_warns)

        # Check for recent churn
        churn, churn_msg = detect_recent_churn(df, threshold=0.25)
        if churn:
            scen_c.warnings.insert(0, churn_msg)
    else:
        # No stability rules — use raw C
        scen_c = scen_c_raw

    # Scenario D (all accounts at breakeven)
    scen_d = scenario_d(predict_fns, model_info, min_mroas, scenario_b=scen_c)

    return ScenarioSet(
        scenarios=[scen_a, scen_b, scen_c, scen_d],
        predict_fns=predict_fns,
        model_info=model_info,
        min_mroas=min_mroas,
        generated_at=datetime.now()
    )
