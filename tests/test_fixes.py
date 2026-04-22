"""
Tests for the four targeted fixes.

Fix 1: Enforce consistent curve family
Fix 2: Remove hard 2-account change limit
Fix 3: "C matches B" edge case
Fix 4: Correct mROAS thresholds
"""
import numpy as np
import pandas as pd
import pytest

from budget_solver.curves import fit_portfolio_curves
from budget_solver.stability import apply_change_limit, phasing_warnings
from budget_solver.narrative import scenario_summary, mroas_state, action_items
from budget_solver.scenarios import Scenario, AccountAllocation


# ══════════════════════════════════════════════════════════════════════════════
# Fix 1: Curve Consistency
# ══════════════════════════════════════════════════════════════════════════════

def test_curve_consistency():
    """
    Test that fit_portfolio_curves enforces consistent curve family.

    When one account fails log fit, ALL accounts should be refitted with power curve.
    """
    # Create synthetic data where one account has degenerate spend pattern
    np.random.seed(42)

    account_data = {
        'Account A': {
            'spend': np.array([1000, 1200, 1100, 1300, 1250, 1150]),
            'revenue': np.array([5000, 5800, 5400, 6100, 5900, 5600])
        },
        'Account B': {
            'spend': np.array([2000, 2100, 2050, 2150, 2080, 2120]),
            'revenue': np.array([8000, 8200, 8100, 8300, 8150, 8250])
        },
        # Account C has degenerate pattern that may fail log fit
        'Account C': {
            'spend': np.array([100, 100, 100, 100, 100, 100]),  # No variation
            'revenue': np.array([200, 210, 205, 200, 195, 205])
        }
    }

    # Fit portfolio curves
    results = fit_portfolio_curves(account_data, preferred_model='log')

    # Get model names
    model_names = {acc: results[acc][3] for acc in account_data.keys()}

    # Count non-linear-fallback models
    non_linear = [name for name in model_names.values() if name != 'linear_fallback']

    # All non-linear models should be the same (either all log or all power)
    if len(non_linear) > 1:
        assert len(set(non_linear)) == 1, (
            f"Mixed curve families detected: {model_names}. "
            f"All non-linear accounts should use the same model."
        )


# ══════════════════════════════════════════════════════════════════════════════
# Fix 2: Max Changes Zero Applies All Moves
# ══════════════════════════════════════════════════════════════════════════════

def test_max_changes_zero_applies_all_moves():
    """
    Test that max_changes=0 applies all moves without limit.
    """
    # Create scenario B and C with 6 changed accounts
    allocations_b = [
        AccountAllocation(
            account=f"Account {i}",
            daily_spend=100.0 * i,
            monthly_spend=3040.0 * i,
            daily_revenue=500.0 * i,
            monthly_revenue=15200.0 * i,
            roas=5.0,
            inst_mroas=3.0
        )
        for i in range(1, 7)
    ]

    allocations_c = [
        AccountAllocation(
            account=f"Account {i}",
            daily_spend=120.0 * i,  # All changed
            monthly_spend=3648.0 * i,
            daily_revenue=600.0 * i,
            monthly_revenue=18240.0 * i,
            roas=5.0,
            inst_mroas=3.5,
            change_vs_prev=608.0 * i,
            change_label=f"▲ +€{608.0 * i:,.0f}"
        )
        for i in range(1, 7)
    ]

    scenario_b = Scenario(
        id="B",
        name="Target Budget",
        description="Proportional scaling",
        budget_monthly=sum(a.monthly_spend for a in allocations_b),
        revenue_monthly=sum(a.monthly_revenue for a in allocations_b),
        blended_roas=5.0,
        portfolio_discrete_mroas=None,
        allocations=allocations_b,
        recommended=False
    )

    scenario_c = Scenario(
        id="C",
        name="Recommended",
        description="Optimized allocation",
        budget_monthly=sum(a.monthly_spend for a in allocations_c),
        revenue_monthly=sum(a.monthly_revenue for a in allocations_c),
        blended_roas=5.0,
        portfolio_discrete_mroas=4.0,
        allocations=allocations_c,
        recommended=True
    )

    # Create dummy predict_fns and model_info
    predict_fns = {f"Account {i}": lambda x: x * 5.0 for i in range(1, 7)}
    model_info = {f"Account {i}": (predict_fns[f"Account {i}"], [3000.0, 0.5], 0.8, 'log') for i in range(1, 7)}

    # Apply change limit with max_changes=0
    result = apply_change_limit(
        scenario_c=scenario_c,
        scenario_b=scenario_b,
        predict_fns=predict_fns,
        model_info=model_info,
        min_mroas=2.5,
        max_changes=0
    )

    # Assert all 6 accounts kept their changed allocations
    # (max_changes=0 should return scenario_c unchanged)
    assert result == scenario_c, "max_changes=0 should apply all moves"


def test_phasing_warnings_independent_of_change_limit():
    """
    Test that phasing warnings are generated regardless of max_changes setting.

    6-account reallocation with max_changes=0 should still generate phasing warnings
    where WoW change exceeds cap.
    """
    # Create scenario A (baseline) and C (target) with large moves
    allocations_a = [
        AccountAllocation(
            account=f"Account {i}",
            daily_spend=100.0 * i,
            monthly_spend=3040.0 * i,
            daily_revenue=500.0 * i,
            monthly_revenue=15200.0 * i,
            roas=5.0,
            inst_mroas=3.0
        )
        for i in range(1, 7)
    ]

    allocations_c = [
        AccountAllocation(
            account=f"Account {i}",
            daily_spend=150.0 * i,  # 50% increase → exceeds 20% WoW cap
            monthly_spend=4560.0 * i,
            daily_revenue=750.0 * i,
            monthly_revenue=22800.0 * i,
            roas=5.0,
            inst_mroas=3.5,
            change_vs_prev=1520.0 * i,
            change_label=f"▲ +€{1520.0 * i:,.0f}"
        )
        for i in range(1, 7)
    ]

    scenario_a = Scenario(
        id="A",
        name="Current Run Rate",
        description="Baseline",
        budget_monthly=sum(a.monthly_spend for a in allocations_a),
        revenue_monthly=sum(a.monthly_revenue for a in allocations_a),
        blended_roas=5.0,
        portfolio_discrete_mroas=None,
        allocations=allocations_a,
        recommended=False
    )

    scenario_c = Scenario(
        id="C",
        name="Recommended",
        description="Optimized allocation",
        budget_monthly=sum(a.monthly_spend for a in allocations_c),
        revenue_monthly=sum(a.monthly_revenue for a in allocations_c),
        blended_roas=5.0,
        portfolio_discrete_mroas=4.0,
        allocations=allocations_c,
        recommended=True
    )

    # Generate phasing warnings
    warnings = phasing_warnings(scenario_c, scenario_a, wow_cap=0.20)

    # All 6 accounts should have phasing warnings (50% change > 20% cap)
    assert len(warnings) == 6, f"Expected 6 phasing warnings, got {len(warnings)}"

    # Verify each warning mentions the account and phasing plan
    for i, warning in enumerate(warnings, 1):
        assert f"Account {i}" in warning
        assert "exceeds 20% WoW cap" in warning or "week" in warning.lower()


# ══════════════════════════════════════════════════════════════════════════════
# Fix 3: No Reallocation Note
# ══════════════════════════════════════════════════════════════════════════════

def test_no_reallocation_note():
    """
    Test that scenario_summary() and action_items() handle no_reallocation=True.
    """
    # Create Scenario C with no_reallocation=True
    allocations = [
        AccountAllocation(
            account="Account 1",
            daily_spend=100.0,
            monthly_spend=3040.0,
            daily_revenue=500.0,
            monthly_revenue=15200.0,
            roas=5.0,
            inst_mroas=3.0
        )
    ]

    scenario_c = Scenario(
        id="C",
        name="Recommended",
        description="No cuts needed",
        budget_monthly=3040.0,
        revenue_monthly=15200.0,
        blended_roas=5.0,
        portfolio_discrete_mroas=None,
        allocations=allocations,
        recommended=True,
        no_reallocation=True  # Key flag
    )

    scenario_b = Scenario(
        id="B",
        name="Target Budget",
        description="Proportional",
        budget_monthly=3040.0,
        revenue_monthly=15200.0,
        blended_roas=5.0,
        portfolio_discrete_mroas=None,
        allocations=allocations,
        recommended=False
    )

    # Test scenario_summary
    summary = scenario_summary(scenario_c, scenario_b, min_mroas=2.5)
    assert "No reallocation required" in summary
    assert "Implement Scenario B" in summary
    assert "action items" not in summary.lower()  # Should NOT contain action items formatting

    # Test action_items
    items = action_items(scenario_c, scenario_b)
    assert len(items) == 1
    assert items[0] == "No action required. Implement Scenario B daily caps as-is."


# ══════════════════════════════════════════════════════════════════════════════
# Fix 4: mROAS State Boundaries
# ══════════════════════════════════════════════════════════════════════════════

def test_mroas_state_boundaries():
    """
    Test that mroas_state() uses correct thresholds.

    With min_mroas=2.5:
    - < 2.5x → 🔴 Below floor
    - 2.5x - 4.0x → 🟡 Monitor closely
    - ≥ 4.0x → 🟢 Healthy

    With min_mroas=3.0:
    - < 3.0x → 🔴 Below floor
    - 3.0x - 4.5x → 🟡 Monitor closely
    - ≥ 4.5x → 🟢 Healthy
    """
    # Test with min_mroas=2.5
    assert mroas_state(2.4, 2.5)[0] == '🔴'   # just below floor
    assert mroas_state(2.5, 2.5)[0] == '🟡'   # exactly at floor
    assert mroas_state(3.9, 2.5)[0] == '🟡'   # in monitor band
    assert mroas_state(4.0, 2.5)[0] == '🟢'   # exactly at healthy threshold
    assert mroas_state(4.1, 2.5)[0] == '🟢'   # healthy

    # Test with min_mroas=3.0 (confirm thresholds shift with floor)
    assert mroas_state(2.9, 3.0)[0] == '🔴'   # below floor
    assert mroas_state(3.0, 3.0)[0] == '🟡'   # at floor
    assert mroas_state(4.4, 3.0)[0] == '🟡'   # monitor band
    assert mroas_state(4.5, 3.0)[0] == '🟢'   # healthy threshold
    assert mroas_state(5.0, 3.0)[0] == '🟢'   # healthy

    # Verify labels
    assert mroas_state(2.4, 2.5)[1] == 'Below floor'
    assert mroas_state(3.0, 2.5)[1] == 'Monitor closely'
    assert mroas_state(4.5, 2.5)[1] == 'Healthy'
