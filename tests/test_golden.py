"""
Golden-file regression test for budget optimizer.

This test ensures that the refactored code produces identical results to the
baseline established before refactoring. Any changes to this test's expectations
must be intentional and documented.
"""
import json
from pathlib import Path

import pytest
import numpy as np
import pandas as pd

from budget_solver.data import load_data, aggregate_weekly, build_demand_index, remove_outliers
from budget_solver.curves import fit_response_curve
from budget_solver.solver import optimize_budget, prepare_bounds
from budget_solver.utils import resolve_forecast_period


def test_golden_allocation():
    """
    Current behavior snapshot. Any logic change must update this intentionally.

    This test runs the full optimization pipeline on synthetic_10weeks.csv
    and compares the results to the golden baseline generated from the
    unrefactored optimizer.py.
    """
    # Load test fixtures
    fixtures_dir = Path(__file__).parent / "fixtures"
    data_path = fixtures_dir / "synthetic_10weeks.csv"
    expected_path = fixtures_dir / "expected_allocation.json"

    with open(expected_path) as f:
        expected = json.load(f)

    # Run the optimization pipeline
    budget = expected["budget"]

    # 1. Load data
    df = load_data(data_path)

    # 2. Aggregate weekly
    account_data = aggregate_weekly(df)

    # 3. Training window (use full history for synthetic data)
    training_data = account_data

    # 4. Remove outliers
    training_data, removal_log = remove_outliers(training_data)

    # 5. Build demand index
    demand_index = build_demand_index(account_data)

    # 6. Resolve forecast period
    forecast_week, forecast_label, _ = resolve_forecast_period(df)
    forecast_demand = demand_index.get(forecast_week, 1.0)

    # 7. Fit curves
    predict_fns = {}
    for acc, data in sorted(training_data.items()):
        fn, params, r2, mname = fit_response_curve(data['spend'], data['revenue'])
        predict_fns[acc] = fn

    # 8. Monthly scaling (4.33 weeks/month)
    predict_fns = {
        acc: (lambda x, fn=fn: 4.33 * fn(x / 4.33))
        for acc, fn in predict_fns.items()
    }

    # 9. Calibrate to actual ROAS (trailing 30 days)
    # Calculate actual ROAS from trailing window
    from budget_solver.constants import TRAILING_WINDOW_DAYS
    date_col = next((c for c in ('date', 'week_start', 'week') if c in df.columns), None)
    if date_col:
        df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
        latest_date = df[date_col].max().normalize()
        cutoff = latest_date - pd.Timedelta(days=TRAILING_WINDOW_DAYS - 1)
        recent = df[(df[date_col] >= cutoff) & (df[date_col] <= latest_date)]
        current_alloc = recent.groupby('account_name')['cost'].sum().to_dict()
        actual_rev_30d = recent.groupby('account_name')['conversion_value'].sum().to_dict()
    else:
        current_alloc = df.groupby('account_name')['cost'].sum().to_dict()
        actual_rev_30d = df.groupby('account_name')['conversion_value'].sum().to_dict()

    actual_roas = {
        acc: actual_rev_30d.get(acc, 0) / current_alloc[acc]
        for acc in current_alloc if current_alloc[acc] > 0
    }

    # Apply calibration
    for acc in list(predict_fns):
        curr_sp = current_alloc.get(acc, 0)
        adj_roas = actual_roas.get(acc, 0)
        if curr_sp > 0 and adj_roas > 0:
            model_pred = predict_fns[acc](curr_sp)
            model_roas = model_pred / curr_sp
            if model_roas > 0:
                scale = adj_roas / model_roas
                predict_fns[acc] = (lambda x, fn=predict_fns[acc], s=scale: fn(x) * s)

    # 10. Auto spend caps
    auto_max = {}
    for acc, data in account_data.items():
        hist_monthly_max = float(np.max(data['spend'])) * 4.33
        auto_max[acc] = max(hist_monthly_max * 2, budget * 0.02)

    # 11. Optimize
    optimal_alloc, success, optimal_rev = optimize_budget(
        predict_fns, budget, min_spend={}, max_spend=auto_max
    )

    # Compare results
    assert success, "Optimization should succeed"

    # Check allocations match within tolerance (0.01% relative error)
    for acc, expected_spend in expected["allocations"].items():
        actual_spend = optimal_alloc[acc]
        assert actual_spend == pytest.approx(expected_spend, rel=1e-4), \
            f"{acc}: expected {expected_spend:.2f}, got {actual_spend:.2f}"

    # Check total revenue matches
    assert optimal_rev == pytest.approx(expected["total_revenue"], rel=1e-4), \
        f"Total revenue: expected {expected['total_revenue']:.2f}, got {optimal_rev:.2f}"

    print(f"\n✓ Golden test passed! Allocations and revenue match baseline.")
    print(f"  Budget: €{budget:,.0f}")
    print(f"  Projected revenue: €{optimal_rev:,.0f}")
    print(f"  ROAS: {optimal_rev / budget:.2f}x")
