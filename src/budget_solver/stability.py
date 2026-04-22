"""
Stability rules for budget optimization.

Phase 4: Limit algorithmic churn by restricting the number of account changes
and providing phasing guidance for large week-over-week moves.
"""
from typing import TYPE_CHECKING
import pandas as pd
import numpy as np
from dataclasses import replace

if TYPE_CHECKING:
    from budget_solver.scenarios import Scenario, AccountAllocation

from budget_solver.mroas import discrete_mroas
from budget_solver.constants import DAYS_PER_MONTH


def apply_change_limit(
    scenario_c: "Scenario",
    scenario_b: "Scenario",
    predict_fns: dict,
    model_info: dict,
    min_mroas: float,
    max_changes: int = 2,
) -> "Scenario":
    """
    Restrict Scenario C optional moves to at most `max_changes` vs Scenario B.

    CRITICAL: Distinguishes between mandatory floor caps and optional reallocations.

    Strategy:
    1. Compute breakevens for all accounts
    2. Identify MANDATORY moves: accounts where B allocation > breakeven
       → These are floor-enforced caps and ALWAYS kept (non-negotiable)
    3. Identify OPTIONAL moves: all other reallocations (budget redistribution)
    4. Rank optional moves by move value = |discrete_mroas| × |ΔSpend|
    5. Keep ALL mandatory + top `max_changes` optional moves
    6. Revert remaining optional moves to B's allocation
    7. Redistribute budget if needed

    This ensures Scenario C's core guarantee (all accounts ≥ min_mroas) is never
    violated for stability reasons. Mandatory floor caps are preserved regardless
    of max_changes setting.

    Args:
        scenario_c: Raw Scenario C from Phase 3 SQP solver
        scenario_b: Scenario B (proportional scaling baseline)
        predict_fns: Response curve predictors
        model_info: Model parameters for inst. mROAS calculation
        min_mroas: Minimum instantaneous mROAS floor
        max_changes: Maximum OPTIONAL account changes (default 2)
                     Mandatory caps are always kept in addition to this

    Returns:
        Scenario C with stability rules applied (floor guarantee preserved)
    """
    from budget_solver.scenarios import Scenario, AccountAllocation

    if max_changes == 0:
        # Unlimited changes — return raw C unchanged
        return scenario_c

    # Build lookup dicts
    b_allocs = {a.account: a for a in scenario_b.allocations}
    c_allocs = {a.account: a for a in scenario_c.allocations}

    # Compute breakevens for all accounts
    from budget_solver.constants import WEEKS_PER_MONTH
    breakevens = {}
    for account in c_allocs.keys():
        _, params, _, _ = model_info[account]
        a = params[0]
        weekly_breakeven = a / min_mroas
        monthly_breakeven = weekly_breakeven * WEEKS_PER_MONTH
        breakevens[account] = monthly_breakeven

    # Categorize moves into mandatory (floor-enforced caps) vs optional (reallocations)
    mandatory_moves = []
    optional_moves = []

    for account in sorted(c_allocs.keys()):
        b_alloc = b_allocs[account]
        c_alloc = c_allocs[account]

        delta_spend = c_alloc.monthly_spend - b_alloc.monthly_spend

        # Skip if no change
        if abs(delta_spend) < 100:
            continue

        # Discrete mROAS vs B
        disc_mroas = discrete_mroas(
            rev_from=b_alloc.monthly_revenue,
            rev_to=c_alloc.monthly_revenue,
            spend_from=b_alloc.monthly_spend,
            spend_to=c_alloc.monthly_spend
        )

        # Move value = |discrete_mroas| × |ΔSpend|
        # If disc_mroas is None (zero delta), skip
        if disc_mroas is None:
            continue

        move_value = abs(disc_mroas) * abs(delta_spend)

        move_data = {
            'account': account,
            'delta_spend': delta_spend,
            'disc_mroas': disc_mroas,
            'move_value': move_value,
            'c_alloc': c_alloc,
            'b_alloc': b_alloc
        }

        # Determine if this is a mandatory cap (B allocation > breakeven)
        # Mandatory caps are floor-enforced: account is above breakeven in B, must cap in C
        if b_alloc.monthly_spend > breakevens[account] * 1.01:  # 1% tolerance
            # This is a mandatory cap - B is above breakeven, C caps it
            mandatory_moves.append(move_data)
        else:
            # This is an optional reallocation (budget redistribution among below-floor accounts)
            optional_moves.append(move_data)

    # Sort optional moves by move value (descending)
    optional_moves.sort(key=lambda x: x['move_value'], reverse=True)

    # Keep: ALL mandatory moves + top N optional moves
    kept_moves = mandatory_moves + optional_moves[:max_changes]
    reverted_moves = optional_moves[max_changes:]

    # Build new allocations: kept moves from C, reverted moves from B
    new_allocations = []
    kept_accounts = {m['account'] for m in kept_moves}
    reverted_accounts = {m['account'] for m in reverted_moves}

    for account in sorted(c_allocs.keys()):
        if account in kept_accounts:
            # Keep C's allocation
            new_allocations.append(c_allocs[account])
        elif account in reverted_accounts:
            # Revert to B's allocation
            b_alloc = b_allocs[account]
            new_allocations.append(AccountAllocation(
                account=account,
                daily_spend=b_alloc.daily_spend,
                monthly_spend=b_alloc.monthly_spend,
                daily_revenue=b_alloc.daily_revenue,
                monthly_revenue=b_alloc.monthly_revenue,
                roas=b_alloc.roas,
                inst_mroas=b_alloc.inst_mroas,
                discrete_mroas_vs_prev=None,  # Reverted, so no change vs B
                change_vs_prev=0.0,
                change_label="— reverted to B"
            ))
        else:
            # Account didn't move in raw C — keep unchanged from B
            b_alloc = b_allocs[account]
            new_allocations.append(AccountAllocation(
                account=account,
                daily_spend=b_alloc.daily_spend,
                monthly_spend=b_alloc.monthly_spend,
                daily_revenue=b_alloc.daily_revenue,
                monthly_revenue=b_alloc.monthly_revenue,
                roas=b_alloc.roas,
                inst_mroas=b_alloc.inst_mroas,
                discrete_mroas_vs_prev=b_alloc.discrete_mroas_vs_prev,
                change_vs_prev=b_alloc.change_vs_prev,
                change_label=b_alloc.change_label
            ))

    # Check budget constraint
    new_total_spend = sum(a.monthly_spend for a in new_allocations)
    target_budget = scenario_b.budget_monthly
    budget_discrepancy = target_budget - new_total_spend

    # If reverting broke the budget constraint, redistribute the discrepancy
    # proportionally across kept accounts (within their breakeven caps)
    if abs(budget_discrepancy) > 100:
        # Compute breakevens for kept accounts
        from budget_solver.constants import WEEKS_PER_MONTH

        breakevens = {}
        for move in kept_moves:
            account = move['account']
            _, params, _, _ = model_info[account]
            a = params[0]
            weekly_breakeven = a / min_mroas
            monthly_breakeven = weekly_breakeven * WEEKS_PER_MONTH
            breakevens[account] = monthly_breakeven

        # Try to redistribute proportionally
        if budget_discrepancy > 0:
            # Need to add budget to kept accounts
            total_headroom = sum(
                max(0, breakevens[m['account']] - m['c_alloc'].monthly_spend)
                for m in kept_moves
            )

            if total_headroom < budget_discrepancy:
                # Cannot redistribute within breakeven caps — fall back to raw C
                description_note = (
                    f"⚠️ Stability limit ({max_changes} accounts) would require reverting moves "
                    f"that break budget constraint. Falling back to unconstrained allocation. "
                    f"Consider increasing --max-account-changes."
                )
                return replace(
                    scenario_c,
                    description=scenario_c.description + " " + description_note
                )

            # Redistribute proportionally to headroom
            for i, alloc in enumerate(new_allocations):
                if alloc.account in kept_accounts:
                    # Find this account in kept_moves
                    move = next(m for m in kept_moves if m['account'] == alloc.account)
                    current_spend = move['c_alloc'].monthly_spend
                    headroom = max(0, breakevens[alloc.account] - current_spend)

                    if total_headroom > 0:
                        proportion = headroom / total_headroom
                        add_amount = budget_discrepancy * proportion
                        new_monthly = min(current_spend + add_amount, breakevens[alloc.account])
                    else:
                        new_monthly = current_spend

                    new_daily = new_monthly / DAYS_PER_MONTH

                    # Recompute revenue, ROAS, inst. mROAS at new spend
                    new_monthly_rev = predict_fns[alloc.account](new_monthly)
                    new_daily_rev = new_monthly_rev / DAYS_PER_MONTH
                    new_roas = new_monthly_rev / new_monthly if new_monthly > 0 else 0.0

                    from budget_solver.mroas import instantaneous_mroas
                    _, params, _, mname = model_info[alloc.account]
                    weekly_sp = new_daily * 7
                    new_inst_mroas = instantaneous_mroas(params, mname, weekly_sp)

                    # Update allocation
                    new_allocations[i] = AccountAllocation(
                        account=alloc.account,
                        daily_spend=new_daily,
                        monthly_spend=new_monthly,
                        daily_revenue=new_daily_rev,
                        monthly_revenue=new_monthly_rev,
                        roas=new_roas,
                        inst_mroas=new_inst_mroas,
                        discrete_mroas_vs_prev=alloc.discrete_mroas_vs_prev,
                        change_vs_prev=new_monthly - b_allocs[alloc.account].monthly_spend,
                        change_label=f"▲ +€{abs(new_monthly - b_allocs[alloc.account].monthly_spend):,.0f}" if new_monthly > b_allocs[alloc.account].monthly_spend else f"▼ −€{abs(new_monthly - b_allocs[alloc.account].monthly_spend):,.0f}"
                    )

    # Recalculate portfolio totals
    total_monthly_spend = sum(a.monthly_spend for a in new_allocations)
    total_monthly_revenue = sum(a.monthly_revenue for a in new_allocations)
    blended_roas = total_monthly_revenue / total_monthly_spend if total_monthly_spend > 0 else 0.0

    # Portfolio discrete mROAS vs B
    portfolio_disc_mroas = discrete_mroas(
        rev_from=scenario_b.revenue_monthly,
        rev_to=total_monthly_revenue,
        spend_from=scenario_b.budget_monthly,
        spend_to=total_monthly_spend
    )

    # Build description with change summary
    mandatory_summary = ", ".join(m['account'] for m in mandatory_moves) if mandatory_moves else "none"
    optional_kept_summary = ", ".join(m['account'] for m in optional_moves[:max_changes]) if optional_moves[:max_changes] else "none"
    reverted_summary = ", ".join(m['account'] for m in reverted_moves) if reverted_moves else "none"

    if mandatory_moves and optional_moves[:max_changes]:
        stability_note = (
            f"Stability rules applied: kept {len(mandatory_moves)} mandatory floor cap(s) "
            f"({mandatory_summary}) + top {len(optional_moves[:max_changes])} optional move(s) "
            f"({optional_kept_summary}). Reverted optional: {reverted_summary}."
        )
    elif mandatory_moves:
        stability_note = (
            f"Stability rules applied: kept {len(mandatory_moves)} mandatory floor cap(s) "
            f"({mandatory_summary}), no optional moves. Reverted: {reverted_summary}."
        )
    elif optional_moves[:max_changes]:
        stability_note = (
            f"Stability rules applied: no mandatory caps, kept top {len(optional_moves[:max_changes])} optional move(s) "
            f"({optional_kept_summary}). Reverted: {reverted_summary}."
        )
    else:
        stability_note = f"Stability limit applied: no moves (matched Scenario B)."

    description = f"{scenario_c.description} {stability_note}"

    # Build warnings list
    warnings = scenario_c.warnings.copy()

    # Add note about reverted optional moves
    if reverted_moves:
        reverted_details = []
        for move in reverted_moves:
            reverted_details.append(
                f"{move['account']}: would have moved {move['delta_spend']:+,.0f} "
                f"(move value: {move['move_value']:,.0f}, disc. mROAS: {move['disc_mroas']:.2f}x)"
            )
        warnings.append(
            f"Reverted {len(reverted_moves)} optional reallocation(s) to Scenario B (below top-{max_changes} cutoff): "
            + "; ".join(reverted_details)
        )

    return Scenario(
        id=scenario_c.id,
        name=scenario_c.name,
        description=description,
        budget_monthly=total_monthly_spend,
        revenue_monthly=total_monthly_revenue,
        blended_roas=blended_roas,
        portfolio_discrete_mroas=portfolio_disc_mroas,
        allocations=new_allocations,
        warnings=warnings,
        recommended=True
    )


def phasing_warnings(
    scenario: "Scenario",
    baseline: "Scenario",
    wow_cap: float = 0.20
) -> list[str]:
    """
    Generate phasing warnings for accounts exceeding week-over-week cap.

    For each account where |ΔSpend / baseline_spend| > wow_cap, emit a phasing plan:
    "Phase {account}: currently €X/day. Target €Z/day. Move gradually:
     week 1 → €Y1, week 2 → €Y2, ..., target in week N."

    Args:
        scenario: Target scenario (typically C)
        baseline: Current run rate (typically A)
        wow_cap: Maximum week-over-week change (default 0.20 = 20%)

    Returns:
        List of phasing warning strings
    """
    # No need to import Scenario here since we're only accessing attributes
    warnings = []

    baseline_allocs = {a.account: a for a in baseline.allocations}
    scenario_allocs = {a.account: a for a in scenario.allocations}

    for account in sorted(scenario_allocs.keys()):
        current = baseline_allocs[account]
        target = scenario_allocs[account]

        if current.monthly_spend == 0:
            # Cannot phase from zero — special case
            if target.monthly_spend > 0:
                warnings.append(
                    f"{account}: currently €0/mo, target €{target.monthly_spend:,.0f}/mo. "
                    f"Starting from zero — ramp gradually over 2-3 weeks to allow algorithm learning."
                )
            continue

        # Calculate change percentage
        change_pct = (target.monthly_spend - current.monthly_spend) / current.monthly_spend

        if abs(change_pct) <= wow_cap:
            # Within cap, no warning
            continue

        # Exceeds cap — generate phasing plan
        n_weeks = int(np.ceil(abs(change_pct) / wow_cap))

        phasing_plan = []
        for week in range(1, n_weeks + 1):
            # Linear interpolation from current to target
            progress = week / n_weeks
            interim_spend = current.monthly_spend + (target.monthly_spend - current.monthly_spend) * progress
            phasing_plan.append(f"week {week} → €{interim_spend:,.0f}/mo")

        direction = "increase" if change_pct > 0 else "decrease"
        warnings.append(
            f"{account}: {direction} of {abs(change_pct)*100:.1f}% exceeds {wow_cap*100:.0f}% WoW cap. "
            f"Phase gradually over {n_weeks} weeks: " + ", ".join(phasing_plan) + "."
        )

    return warnings


def detect_recent_churn(
    df: pd.DataFrame,
    threshold: float = 0.25
) -> tuple[bool, str]:
    """
    Detect recent portfolio-level churn (last 14d vs prior 14d).

    If total spend volatility exceeds threshold, return (True, warning_message).
    This informs the reader that the portfolio has recently absorbed volatility
    and may benefit from conservative changes.

    Args:
        df: Input dataframe with date and cost columns
        threshold: Change threshold (default 0.25 = 25%)

    Returns:
        Tuple of (churn_detected: bool, message: str)
    """
    # Find date column
    date_col = next((c for c in ('date', 'week_start', 'week') if c in df.columns), None)
    if not date_col:
        return False, "Cannot detect churn: no date column found"

    # Parse dates
    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
    df = df.dropna(subset=[date_col])

    if len(df) == 0:
        return False, "Cannot detect churn: no valid dates"

    latest = df[date_col].max()

    # Last 14 days
    last_14d_start = latest - pd.Timedelta(days=13)  # Inclusive of latest
    last_14d = df[(df[date_col] >= last_14d_start) & (df[date_col] <= latest)]

    # Prior 14 days (days -27 to -14)
    prior_14d_end = latest - pd.Timedelta(days=14)
    prior_14d_start = latest - pd.Timedelta(days=27)
    prior_14d = df[(df[date_col] >= prior_14d_start) & (df[date_col] <= prior_14d_end)]

    if len(last_14d) == 0 or len(prior_14d) == 0:
        return False, "Insufficient data for churn detection (need 28 days)"

    # Sum total cost in each period
    last_total = last_14d['cost'].sum()
    prior_total = prior_14d['cost'].sum()

    if prior_total == 0:
        return False, "Cannot detect churn: prior 14d spend is zero"

    # Calculate change percentage
    change_pct = (last_total - prior_total) / prior_total

    if abs(change_pct) > threshold:
        direction = "increased" if change_pct > 0 else "decreased"
        message = (
            f"⚠️ RECENT CHURN DETECTED: Portfolio spend {direction} {abs(change_pct)*100:.1f}% "
            f"in last 14 days (€{last_total:,.0f}) vs prior 14 days (€{prior_total:,.0f}). "
            f"System has recently absorbed volatility — consider conservative changes to allow "
            f"Smart Bidding algorithms to stabilize (2-4 week re-learning period)."
        )
        return True, message

    return False, ""
