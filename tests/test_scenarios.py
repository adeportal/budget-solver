"""
Unit tests for Phase 2 scenario generation.

Tests the 4-scenario framework (A, B, C, D) and validates:
- Baseline window extraction
- Proportional scaling
- Breakeven capping
- Discrete mROAS calculations
- C1 detection
"""
import pytest
import pandas as pd
import numpy as np

from budget_solver.scenarios import scenario_a, scenario_b, scenario_c, scenario_d
from budget_solver.curves import fit_response_curve
from budget_solver.data import aggregate_weekly
from budget_solver.constants import WEEKS_PER_MONTH, DAYS_PER_MONTH
from budget_solver.mroas import discrete_mroas


@pytest.fixture
def synthetic_data():
    """
    Create synthetic fixture with controlled spend/revenue patterns.
    Two accounts with different last-7-day vs prior-7-day spend levels.
    """
    dates = pd.date_range('2024-01-01', periods=20, freq='D')
    data = []

    # Account A: higher spend in last 7 days
    for i, date in enumerate(dates):
        if i < 13:
            spend = 1000  # Days 0-12
        else:
            spend = 2000  # Days 13-19 (last 7 days)
        rev = spend * 5.0  # 5x ROAS
        data.append({'date': date, 'account_name': 'Account A', 'cost': spend, 'conversion_value': rev})

    # Account B: lower spend in last 7 days
    for i, date in enumerate(dates):
        if i < 13:
            spend = 3000  # Days 0-12
        else:
            spend = 1500  # Days 13-19 (last 7 days)
        rev = spend * 4.0  # 4x ROAS
        data.append({'date': date, 'account_name': 'Account B', 'cost': spend, 'conversion_value': rev})

    return pd.DataFrame(data)


@pytest.fixture
def fitted_models(synthetic_data):
    """Fit curves and prepare predict_fns for synthetic data."""
    account_data = aggregate_weekly(synthetic_data)

    predict_fns = {}
    model_info = {}

    for acc, data in account_data.items():
        fn, params, r2, mname = fit_response_curve(data['spend'], data['revenue'])
        predict_fns[acc] = fn
        model_info[acc] = (fn, params, r2, mname)

    # Apply monthly scaling
    predict_fns = {
        acc: (lambda x, fn=fn, wpm=WEEKS_PER_MONTH: wpm * fn(x / wpm))
        for acc, fn in predict_fns.items()
    }

    return predict_fns, model_info


def test_scenario_a_uses_baseline_window(synthetic_data, fitted_models):
    """
    Scenario A should extract spend from the last N days only.

    In synthetic_data:
    - Account A: last 7 days = 2000/day, prior = 1000/day
    - Account B: last 7 days = 1500/day, prior = 3000/day
    """
    predict_fns, model_info = fitted_models

    scen_a = scenario_a(
        df=synthetic_data,
        predict_fns=predict_fns,
        model_info=model_info,
        min_mroas=2.5,
        baseline_window_days=7
    )

    # Find allocations
    alloc_a = next(a for a in scen_a.allocations if a.account == 'Account A')
    alloc_b = next(a for a in scen_a.allocations if a.account == 'Account B')

    # Check daily spend matches last 7 days average
    assert alloc_a.daily_spend == pytest.approx(2000, rel=0.01), \
        f"Account A should use last 7 days (2000/day), got {alloc_a.daily_spend}"

    assert alloc_b.daily_spend == pytest.approx(1500, rel=0.01), \
        f"Account B should use last 7 days (1500/day), got {alloc_b.daily_spend}"

    # Check monthly extrapolation
    assert alloc_a.monthly_spend == pytest.approx(2000 * DAYS_PER_MONTH, rel=0.01)
    assert alloc_b.monthly_spend == pytest.approx(1500 * DAYS_PER_MONTH, rel=0.01)


def test_scenario_b_scales_proportionally(synthetic_data, fitted_models):
    """
    Scenario B should scale every account by the same factor.

    If A total = €1M and target = €2M, every account in B should be 2× A.
    """
    predict_fns, model_info = fitted_models

    scen_a = scenario_a(synthetic_data, predict_fns, model_info, 2.5, 7)

    # Target budget = 2× current
    target = scen_a.budget_monthly * 2.0

    scen_b = scenario_b(scen_a, target, predict_fns, model_info, 2.5)

    # Every account should be scaled by 2×
    for b_alloc in scen_b.allocations:
        a_alloc = next(a for a in scen_a.allocations if a.account == b_alloc.account)
        expected_spend = a_alloc.monthly_spend * 2.0

        assert b_alloc.monthly_spend == pytest.approx(expected_spend, rel=1e-4), \
            f"{b_alloc.account}: expected {expected_spend:.2f}, got {b_alloc.monthly_spend:.2f}"

    # Total should match target exactly
    total_b = sum(a.monthly_spend for a in scen_b.allocations)
    assert total_b == pytest.approx(target, abs=1.0)


def test_scenario_c_caps_below_floor_accounts(synthetic_data, fitted_models):
    """
    Scenario C should cap accounts where B pushes inst. mROAS below floor.

    Test on the stress fixture (core_markets_stress.csv) which has
    Landal DE artificially degraded to ensure capping at normal budgets.
    """
    # Use stress fixture if available, otherwise skip this test
    import os
    stress_path = "tests/fixtures/core_markets_stress.csv"

    if not os.path.exists(stress_path):
        pytest.skip("Stress fixture not available")

    from budget_solver.data import load_data
    from budget_solver.data import remove_outliers

    df = load_data(stress_path)
    account_data = aggregate_weekly(df)

    # Training window
    date_col = 'date'
    df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
    latest = df[date_col].max().normalize()
    training_cutoff = latest - pd.DateOffset(months=6)
    training_df = df[df[date_col] >= training_cutoff].copy()
    training_data = aggregate_weekly(training_df)
    training_data, _ = remove_outliers(training_data)

    # Fit curves
    predict_fns = {}
    model_info = {}

    for acc, data in training_data.items():
        fn, params, r2, mname = fit_response_curve(data['spend'], data['revenue'])
        predict_fns[acc] = fn
        model_info[acc] = (fn, params, r2, mname)

    predict_fns = {
        acc: (lambda x, fn=fn, wpm=WEEKS_PER_MONTH: wpm * fn(x / wpm))
        for acc, fn in predict_fns.items()
    }

    # Calibrate (simplified - skip for test)
    # In real usage this matters but for this test we just need the capping logic

    scen_a = scenario_a(df, predict_fns, model_info, 2.5, 7)

    # Target €1.231M should trigger capping on Landal DE (degraded account)
    scen_b = scenario_b(scen_a, 1_231_200, predict_fns, model_info, 2.5)
    scen_c = scenario_c(scen_a, scen_b, predict_fns, model_info, 2.5)

    # Check that capping was triggered
    assert len(scen_c.warnings) > 0, "Expected at least one capping warning"
    assert "Landal DE" in str(scen_c.warnings), "Expected Landal DE to be capped"

    # All accounts in C should be at or above 2.5x inst. mROAS
    for alloc in scen_c.allocations:
        assert alloc.inst_mroas >= 2.49, \
            f"{alloc.account} has inst. mROAS {alloc.inst_mroas:.2f}x < 2.5x floor"


def test_scenario_d_sums_to_breakeven_ceiling(synthetic_data, fitted_models):
    """
    Scenario D's total budget should equal sum of per-account breakevens.

    And every account should have inst. mROAS = min_mroas exactly.
    """
    predict_fns, model_info = fitted_models

    scen_d = scenario_d(predict_fns, model_info, min_mroas=2.5)

    # Calculate expected total from breakevens
    expected_total = 0.0
    for acc in predict_fns.keys():
        _, params, _, _ = model_info[acc]
        a = params[0]
        weekly_breakeven = a / 2.5
        monthly_breakeven = weekly_breakeven * WEEKS_PER_MONTH
        expected_total += monthly_breakeven

    # Check total matches
    actual_total = sum(a.monthly_spend for a in scen_d.allocations)
    assert actual_total == pytest.approx(expected_total, rel=1e-4), \
        f"Expected total {expected_total:.2f}, got {actual_total:.2f}"

    # Check every account is at 2.5x inst. mROAS
    for alloc in scen_d.allocations:
        assert alloc.inst_mroas == pytest.approx(2.5, abs=0.01), \
            f"{alloc.account} inst. mROAS = {alloc.inst_mroas:.2f}x, expected 2.50x"


def test_c1_detection(synthetic_data, fitted_models):
    """
    When target budget ≈ current spend (±5%), scenario should be labeled C1.
    """
    predict_fns, model_info = fitted_models

    scen_a = scenario_a(synthetic_data, predict_fns, model_info, 2.5, 7)

    # Target within 5% of current
    target_c1 = scen_a.budget_monthly * 1.03  # +3%

    scen_b = scenario_b(scen_a, target_c1, predict_fns, model_info, 2.5)
    scen_c = scenario_c(scen_a, scen_b, predict_fns, model_info, 2.5)

    # Should be labeled C1
    assert scen_c.id == "C1", f"Expected C1, got {scen_c.id}"
    assert scen_c.name == "Budget-Neutral Reallocation"

    # Portfolio discrete mROAS should be None
    assert scen_c.portfolio_discrete_mroas is None, \
        "C1 should have None portfolio discrete mROAS (budget-neutral)"

    # Now test regular C (target far from current)
    target_c = scen_a.budget_monthly * 2.0  # +100%

    scen_b2 = scenario_b(scen_a, target_c, predict_fns, model_info, 2.5)
    scen_c2 = scenario_c(scen_a, scen_b2, predict_fns, model_info, 2.5)

    # Should be labeled C
    assert scen_c2.id == "C", f"Expected C, got {scen_c2.id}"
    assert scen_c2.name == "Recommended"


def test_discrete_mroas_handles_zero_delta():
    """
    discrete_mroas() should return None when ΔSpend ≈ 0.
    """
    # Zero delta
    result = discrete_mroas(100, 100, 50, 50)
    assert result is None, "Should return None for zero delta"

    # Very small delta (below threshold)
    result = discrete_mroas(100, 100.0001, 50, 50.0000001)
    assert result is None, "Should return None for delta below threshold"

    # Non-zero delta
    result = discrete_mroas(100, 200, 50, 60)
    assert result == pytest.approx(10.0, rel=1e-4), "Should compute (200-100)/(60-50) = 10.0"


def test_scenario_c_no_op_matches_b_exactly(synthetic_data, fitted_models):
    """
    When no cuts are triggered, Scenario C allocations should be identical to B.

    Not just sum-equal, but element-by-element equal (no silent drift).
    """
    predict_fns, model_info = fitted_models

    scen_a = scenario_a(synthetic_data, predict_fns, model_info, 2.5, 7)

    # Use a small target budget where no accounts exceed breakeven
    target = scen_a.budget_monthly * 0.5

    scen_b = scenario_b(scen_a, target, predict_fns, model_info, 2.5)
    scen_c = scenario_c(scen_a, scen_b, predict_fns, model_info, 2.5)

    # Verify no cuts were triggered
    assert "No cuts required" in scen_c.description, \
        "Expected no cuts for this test case"

    # Check element-by-element equality
    b_dict = {alloc.account: alloc for alloc in scen_b.allocations}
    c_dict = {alloc.account: alloc for alloc in scen_c.allocations}

    for account in b_dict.keys():
        b_alloc = b_dict[account]
        c_alloc = c_dict[account]

        # All critical fields should match exactly (within floating point tolerance)
        assert b_alloc.monthly_spend == pytest.approx(c_alloc.monthly_spend, rel=1e-9), \
            f"{account} spend differs: B={b_alloc.monthly_spend}, C={c_alloc.monthly_spend}"

        assert b_alloc.monthly_revenue == pytest.approx(c_alloc.monthly_revenue, rel=1e-9), \
            f"{account} revenue differs"

        assert b_alloc.roas == pytest.approx(c_alloc.roas, rel=1e-9), \
            f"{account} ROAS differs"

        assert b_alloc.inst_mroas == pytest.approx(c_alloc.inst_mroas, rel=1e-9), \
            f"{account} inst. mROAS differs"

    # Portfolio totals should also match
    assert scen_b.budget_monthly == pytest.approx(scen_c.budget_monthly, rel=1e-9)
    assert scen_b.revenue_monthly == pytest.approx(scen_c.revenue_monthly, rel=1e-9)
    assert scen_b.blended_roas == pytest.approx(scen_c.blended_roas, rel=1e-9)
