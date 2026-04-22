#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
"""
CLI entry point for budget optimizer.
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from budget_solver.constants import TRAILING_WINDOW_DAYS, WEEKS_PER_MONTH, DATA_PATH
from budget_solver.data import (
    load_data,
    aggregate_weekly,
    build_demand_index,
    apply_demand_normalization,
    remove_outliers,
)
from budget_solver.curves import fit_portfolio_curves
from budget_solver.solver import prepare_bounds, optimize_budget, run_sensitivity
from budget_solver.excel import build_excel
from budget_solver.utils import parse_kv_arg, resolve_forecast_period
from budget_solver.scenarios import scenario_a, scenario_b, scenario_c, scenario_d, build_scenarios
from budget_solver.narrative import full_scenario_narrative

def main():
    parser = argparse.ArgumentParser(
        description='Budget Solver — optimize Google Ads budget allocation across accounts'
    )

    parser.add_argument('--budget',  type=float, required=True,
                        help='Total budget to allocate (same currency as data)')
    parser.add_argument('--target',  default='conversion_value',
                        choices=['conversion_value', 'conversions'],
                        help='Metric to optimize (default: conversion_value / revenue)')
    parser.add_argument('--min',     default='',
                        help='Minimum spend per account, e.g. "Account A:500,Account B:1000"')
    parser.add_argument('--max',     default='',
                        help='Maximum spend per account, e.g. "Account A:20000"')
    parser.add_argument('--output',  default='',
                        help='Output Excel file path (default: budget_solver_YYYYMMDD.xlsx)')
    parser.add_argument('--no-outlier-removal', action='store_true',
                        help='Skip automatic outlier/anomaly removal before curve fitting')
    parser.add_argument('--normalize-demand', action='store_true',
                        help='Normalize revenue by a seasonal demand index before fitting curves '
                             '(separates demand seasonality from spend efficiency)')
    parser.add_argument('--demand-index-csv', default='',
                        help='Optional CSV (columns: week, index) to override the derived '
                             'demand index. Use SimilarWeb organic traffic or internal bookings.')
    forecast = parser.add_mutually_exclusive_group()
    forecast.add_argument('--forecast-month', default='',
                          help='Forecast month in YYYY-MM. Used to derive the month midpoint '
                               'ISO week for demand scaling.')
    forecast.add_argument('--forecast-week', type=int, default=None,
                          help='Explicit ISO week number (1-53) for demand scaling. '
                               'If omitted, the solver infers the next calendar month after '
                               'the latest date in the input data.')
    parser.add_argument('--training-months', type=int, default=6,
                        help='Months of history to use for curve fitting (default: 6). '
                             'Use 0 to fit on the full input history.')
    parser.add_argument('--no-calibrate', action='store_true',
                        help='Skip calibrating response curves to the actual lag-adjusted '
                             'ROAS from the trailing 30-day window. By default curves are '
                             'anchored so the predicted ROAS at current spend matches '
                             'observed performance.')

    # Phase 2: Scenario generation
    parser.add_argument('--scenarios', dest='scenarios', action='store_true', default=True,
                        help='Generate 4-scenario framework (A/B/C/D) in addition to '
                             'single-optimum allocation (default: enabled)')
    parser.add_argument('--no-scenarios', dest='scenarios', action='store_false',
                        help='Disable scenario generation, show only single-optimum allocation')
    parser.add_argument('--baseline-window', type=int, default=7,
                        help='Days to use for Scenario A baseline (default: 7)')
    parser.add_argument('--min-mroas', type=float, default=2.5,
                        help='Minimum instantaneous mROAS floor for Scenario C/D (default: 2.5)')

    # Phase 4: Stability rules
    parser.add_argument('--max-account-changes', type=int, default=0,
                        help='Limit optional reallocations to top-N accounts by move value '
                             '(0 = no limit, default: 0). Use 2-3 after major portfolio '
                             'disruptions to preserve Smart Bidding stability.')
    parser.add_argument('--wow-cap', type=float, default=0.20,
                        help='Week-over-week change cap for phasing warnings (default: 0.20 = 20%%)')
    parser.add_argument('--no-stability-rules', dest='apply_stability', action='store_false',
                        help='Disable stability rules (equivalent to --max-account-changes 0)')

    args = parser.parse_args()

    # Ensure output directory exists
    Path('output').mkdir(exist_ok=True)

    output_path = args.output or f'output/budget_solver_{datetime.now().strftime("%Y%m%d_%H%M")}.xlsx'
    min_spend   = parse_kv_arg(args.min)
    max_spend   = parse_kv_arg(args.max)

    # ── Load & validate ──────────────────────────────────────
    if not DATA_PATH.exists():
        print(f"No data found at {DATA_PATH}. Run 'budget-solver-pull' first.", file=sys.stderr)
        sys.exit(1)

    print(f'Loading data from: {DATA_PATH}')
    df = load_data(str(DATA_PATH))

    if args.target == 'conversions' and 'conversions' in df.columns:
        if 'conversions_adj' in df.columns:
            df['conversion_value'] = pd.to_numeric(df['conversions_adj'], errors='coerce').fillna(0)
            print('  Using lag-adjusted conversions (conversions_adj).')
        else:
            df['conversion_value'] = pd.to_numeric(df['conversions'], errors='coerce').fillna(0)
            print('  Using raw conversions (conversions); lag-adjusted conversions not found.')

    print(f'Loaded {len(df):,} rows across {df["account_name"].nunique()} accounts.')
    print()

    # ── Aggregate weekly (full history — used for spend caps) ─
    account_data = aggregate_weekly(df)

    # ── Training window filter ────────────────────────────────
    # Curves are fitted on a recent window only (default: 6 months) so they
    # reflect current market conditions rather than historical periods with
    # different ROAS levels (e.g. January 2025 had 19–32× ROAS for Landal BE
    # at moderate spend — including that in the fit inflates predictions by ~45%).
    _date_col_early = next((c for c in ('date', 'week_start', 'week') if c in df.columns), None)
    if _date_col_early and args.training_months > 0:
        _latest = pd.to_datetime(df[_date_col_early], errors='coerce').max().normalize()
        _training_cutoff = _latest - pd.DateOffset(months=args.training_months)
        training_df = df[pd.to_datetime(df[_date_col_early], errors='coerce') >= _training_cutoff].copy()
        training_data = aggregate_weekly(training_df)
        print(f'Training window: last {args.training_months} months '
              f'({_training_cutoff.date()} to {_latest.date()})')
    else:
        training_data = account_data
        print('Training window: full history')
    print()

    # ── Outlier removal (applied to training window only) ────
    removal_log = []
    if not args.no_outlier_removal:
        training_data, removal_log = remove_outliers(training_data)
        total_removed = len(removal_log)
        if total_removed:
            by_acc = {}
            for r in removal_log:
                by_acc.setdefault(r['account'], []).append(r)
            print(f'Outlier removal: {total_removed} week(s) excluded across '
                  f'{len(by_acc)} account(s)')
            for acc, rows in sorted(by_acc.items()):
                print(f'  {acc:<30}  {len(rows)} week(s) removed')
                for r in rows:
                    print(f'    {r["week"]}  spend=€{r["spend"]:>10,.0f}  '
                          f'ROAS={r["roas"]:.2f}  [{r["reason"]}]')
        else:
            print('Outlier removal: no anomalies detected.')
        print()
    else:
        print('Outlier removal: skipped (--no-outlier-removal).')
        print()

    # ── Spend caps + feasibility check ───────────────────────
    # Build these before curve fitting so impossible min/max settings fail fast.
    auto_max = {}
    for acc, data in account_data.items():
        # Highest single weekly spend observed × WEEKS_PER_MONTH ≈ monthly max
        hist_monthly_max = float(np.max(data['spend'])) * WEEKS_PER_MONTH
        # Allow up to 2× that, or at minimum 10% of the total budget
        auto_max[acc] = max(hist_monthly_max * 2, args.budget * 0.02)

    effective_max = {**auto_max, **max_spend}
    effective_min = min_spend
    try:
        prepare_bounds(list(account_data.keys()), args.budget, effective_min, effective_max)
    except ValueError as exc:
        sys.exit(f'ERROR: {exc}')

    # ── Demand index ─────────────────────────────────────────
    demand_index = build_demand_index(
        account_data,
        external_csv=args.demand_index_csv or None
    )
    source_label = f'external ({args.demand_index_csv})' if args.demand_index_csv else 'derived from data'
    print(f'Demand index built ({source_label}). Sample multipliers:')
    sample_weeks = [1, 7, 14, 17, 26, 32, 40, 44, 50]
    for w in sample_weeks:
        mult = demand_index.get(w, 1.0)
        bar  = '█' * int(mult * 10)
        print(f'  Week {w:>2}  {mult:.3f}  {bar}')
    print()

    # Determine the forecast period for demand scaling
    forecast_week, forecast_label, forecast_source = resolve_forecast_period(
        df,
        forecast_month=args.forecast_month or None,
        forecast_week=args.forecast_week
    )
    forecast_demand = demand_index.get(forecast_week, 1.0)
    print(f'Forecast period: {forecast_label} ({forecast_source})')
    print(f'Forecast week: {forecast_week}  demand multiplier: {forecast_demand:.3f}')
    print()

    # Normalise revenue by demand index before curve fitting (optional)
    fitting_data = training_data
    if args.normalize_demand:
        fitting_data = apply_demand_normalization(training_data, demand_index)
        print('Demand normalization applied: revenue ÷ demand index before curve fitting.')
        print()

    # ── Fit curves ───────────────────────────────────────────
    print('Fitting response curves (portfolio-wide consistency):')
    portfolio_results = fit_portfolio_curves(fitting_data, preferred_model='log')

    predict_fns  = {}
    model_info   = {}

    for acc in sorted(portfolio_results.keys()):
        fn, params, r2, mname = portfolio_results[acc]
        predict_fns[acc] = fn
        model_info[acc]  = (fn, params, r2, mname)
        n = len(fitting_data[acc]['spend'])
        print(f'  {acc:<30}  model={mname:<15}  R²={r2:.3f}  n={n}')

    # If demand-normalized, wrap predict_fns to scale output back up by forecast demand
    if args.normalize_demand:
        predict_fns = {
            acc: (lambda x, fn=fn, d=forecast_demand: fn(x) * d)
            for acc, fn in predict_fns.items()
        }

    # ── Monthly scaling fix ───────────────────────────────────
    # Curves are fitted on WEEKLY aggregates, but current_alloc and budget are
    # MONTHLY totals. Plugging monthly spend directly into a weekly curve asks
    # "what revenue does one week generate at €X monthly spend?" — wrong scale.
    # Correct formula: monthly_revenue = WEEKS_PER_MONTH × weekly_fn(monthly_spend / WEEKS_PER_MONTH)
    predict_fns = {
        acc: (lambda x, fn=fn, wpm=WEEKS_PER_MONTH: wpm * fn(x / wpm))
        for acc, fn in predict_fns.items()
    }

    print()

    # ── Current spend + actual ROAS (trailing 30-day window = actual baseline) ──
    date_col = next((c for c in ('date', 'week_start', 'week') if c in df.columns), None)
    actual_window_label = 'full input range'
    actual_window_detail = actual_window_label
    if date_col:
        df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
        latest_date = df[date_col].max().normalize()
        cutoff = latest_date - pd.Timedelta(days=TRAILING_WINDOW_DAYS - 1)
        recent = df[(df[date_col] >= cutoff) & (df[date_col] <= latest_date)]
        current_alloc      = recent.groupby('account_name')['cost'].sum().to_dict()
        actual_rev_30d     = recent.groupby('account_name')['conversion_value'].sum().to_dict()
        # Raw (as-reported) revenue: before lag correction, for dual ROAS display
        if 'conversion_value_raw' in df.columns:
            actual_rev_30d_raw = recent.groupby('account_name')['conversion_value_raw'].sum().to_dict()
        else:
            actual_rev_30d_raw = actual_rev_30d
        actual_window_label = f'30d ending {latest_date.date()}'
        actual_window_detail = f'{cutoff.date()} to {latest_date.date()}'
    else:
        current_alloc      = df.groupby('account_name')['cost'].sum().to_dict()
        actual_rev_30d     = df.groupby('account_name')['conversion_value'].sum().to_dict()
        actual_rev_30d_raw = actual_rev_30d

    # Lag-adjusted ROAS (conversion_value = adj after load_data overwrite)
    actual_roas = {
        acc: actual_rev_30d.get(acc, 0) / current_alloc[acc]
        for acc in current_alloc if current_alloc[acc] > 0
    }
    # Raw ROAS (as reported by Google, underreported for recent days)
    actual_roas_raw = {
        acc: actual_rev_30d_raw.get(acc, 0) / current_alloc[acc]
        for acc in current_alloc if current_alloc[acc] > 0
    }

    # ── Calibrate curves to actual lag-adjusted ROAS ─────────
    # The fitted curve predicts revenue at current spend. We scale it so
    # the prediction at current spend exactly matches the observed lag-adj ROAS.
    # This anchors the absolute level to reality while preserving the curve shape
    # (diminishing returns slope) derived from historical spend variation.
    calibration_factors = {}
    if not args.no_calibrate:
        for acc in list(predict_fns):
            curr_sp  = current_alloc.get(acc, 0)
            adj_roas = actual_roas.get(acc, 0)
            if curr_sp > 0 and adj_roas > 0:
                model_pred = predict_fns[acc](curr_sp)
                model_roas = model_pred / curr_sp
                if model_roas > 0:
                    scale = adj_roas / model_roas
                    calibration_factors[acc] = scale
                    predict_fns[acc] = (lambda x, fn=predict_fns[acc], s=scale: fn(x) * s)
                    # Update model name in model_info to flag calibration
                    fn_i, params_i, r2_i, mname_i = model_info[acc]
                    model_info[acc] = (fn_i, params_i, r2_i, f'{mname_i}+cal')

    # ── Print per-market spend + ROAS table ──────────────────
    cal_label = '' if args.no_calibrate else '  cal.factor'
    print(f'── CURRENT PERFORMANCE ({actual_window_detail}) ──────────────────────────────')
    print(f'  {"Account":<35} {"Spend":>12} {"ROAS (raw)":>12} {"ROAS (lag-adj)":>15}{cal_label}')
    print('  ' + '─' * (78 + (12 if not args.no_calibrate else 0)))
    for acc in sorted(current_alloc):
        sp   = current_alloc[acc]
        rraw = actual_roas_raw.get(acc, 0)
        radj = actual_roas.get(acc, 0)
        cal_str = f'  {calibration_factors.get(acc, 1.0):>8.3f}x' if not args.no_calibrate else ''
        print(f'  {acc:<35} €{sp:>10,.0f}  {rraw:>10.2f}x  {radj:>13.2f}x{cal_str}')
    total_sp   = sum(current_alloc.values())
    total_rraw = sum(actual_rev_30d_raw.values()) / total_sp if total_sp > 0 else 0
    total_radj = sum(actual_rev_30d.values())     / total_sp if total_sp > 0 else 0
    print('  ' + '─' * (78 + (12 if not args.no_calibrate else 0)))
    print(f'  {"TOTAL":<35} €{total_sp:>10,.0f}  {total_rraw:>10.2f}x  {total_radj:>13.2f}x')
    print()

    # Ensure all accounts in model have a current allocation
    for acc in predict_fns:
        if acc not in current_alloc:
            current_alloc[acc] = 0.0

    # ── Optimize ─────────────────────────────────────────────
    print(f'Optimizing budget: €{args.budget:,.0f}')
    print(f'Auto spend caps applied (2x historical monthly max per account):')
    for acc in sorted(auto_max):
        user_override = f'  [user override: €{max_spend[acc]:,.0f}]' if acc in max_spend else ''
        print(f'  {acc:<30}  max=€{effective_max[acc]:>10,.0f}{user_override}')
    print()

    try:
        optimal_alloc, success, optimal_rev = optimize_budget(
            predict_fns, args.budget, effective_min, effective_max
        )
    except (ValueError, RuntimeError) as exc:
        sys.exit(f'ERROR: {exc}')

    current_actual_rev = sum(actual_rev_30d.values())
    current_actual_spend = sum(current_alloc.values())
    current_actual_roas = current_actual_rev / current_actual_spend if current_actual_spend > 0 else 0
    uplift      = optimal_rev - current_actual_rev
    uplift_pct  = uplift / current_actual_rev * 100 if current_actual_rev > 0 else 0

    print()
    print('── RESULTS ──────────────────────────────────────────────')
    print(f'{"Account":<30} {"Current":>12} {"Recommended":>14} {"Change":>10}')
    print('─' * 70)
    for acc in sorted(optimal_alloc):
        curr = current_alloc.get(acc, 0)
        opt  = optimal_alloc[acc]
        chg  = opt - curr
        chg_pct = chg / curr * 100 if curr > 0 else 0
        arrow = '▲' if chg > 0 else ('▼' if chg < 0 else '─')
        print(f'{acc:<30} €{curr:>10,.0f}  €{opt:>12,.0f}  {arrow} {chg:>+8,.0f} ({chg_pct:+.1f}%)')
    print('─' * 70)
    print(f'{"TOTAL":<30} €{sum(current_alloc.values()):>10,.0f}  €{args.budget:>12,.0f}')
    print()
    print(f'Actual revenue ({actual_window_detail}):  €{current_actual_rev:,.0f}')
    print(f'Actual ROAS ({actual_window_detail}):     {current_actual_roas:.2f}x')
    print(f'Projected revenue ({forecast_label}):    €{optimal_rev:,.0f}')
    print(f'Projected ROAS ({forecast_label}):       {optimal_rev / args.budget:.2f}x')
    print(f'Revenue uplift:                          €{uplift:+,.0f}  ({uplift_pct:+.1f}%)')
    print()

    # ── Sensitivity analysis ─────────────────────────────────
    print('Running sensitivity analysis...')
    sensitivity_df = run_sensitivity(predict_fns, args.budget, effective_min, effective_max)

    # ══════════════════════════════════════════════════════════
    # PHASE 2: Scenario Generation (A/B/C/D)
    # ══════════════════════════════════════════════════════════
    if args.scenarios:
        print()
        print('=' * 100)
        print('SCENARIOS')
        print('=' * 100)
        print()

        # Generate all scenarios with Phase 4 stability rules
        scenario_set = build_scenarios(
            df=df,
            predict_fns=predict_fns,
            model_info=model_info,
            target_budget=args.budget,
            min_mroas=args.min_mroas,
            baseline_window_days=args.baseline_window,
            max_account_changes=args.max_account_changes,
            wow_cap=args.wow_cap,
            apply_stability=args.apply_stability
        )

        scenarios = scenario_set.scenarios

        # Print stability settings
        if args.apply_stability:
            if args.max_account_changes == 0:
                print('Stability limit: none (all account moves applied)')
            else:
                print(f'Stability limit: top-{args.max_account_changes} optional moves by move value '
                      f'(use --max-account-changes 0 to remove limit)')
            print()

        # Print each scenario with narrative
        prev_scenario = None
        for scen in scenarios:
            narrative = full_scenario_narrative(scen, prev_scenario, args.min_mroas)
            print(narrative)
            prev_scenario = scen

        # Scenario comparison table
        print()
        print("=" * 100)
        print("SCENARIO COMPARISON")
        print("=" * 100)
        print()
        print(f"{'ID':<4} {'Name':<30} {'Budget':>15} {'Revenue':>15} {'ROAS':>8} {'Portfolio Disc. mROAS'}")
        print("─" * 100)

        for scen in scenarios:
            recommended_mark = " ✅" if scen.recommended else "   "
            disc_str = f"{scen.portfolio_discrete_mroas:.2f}x" if scen.portfolio_discrete_mroas else "—"

            # Special cases for display
            if scen.id == "A":
                disc_str = "— (baseline)"
            elif scen.id in ["C", "C1"] and scen.portfolio_discrete_mroas is None:
                disc_str = "— (no change from B)"

            print(f"{scen.id:<4} {scen.name:<30} €{scen.budget_monthly:>13,.0f}  "
                  f"€{scen.revenue_monthly / 1_000_000:>12.2f}M  {scen.blended_roas:>6.2f}x  {disc_str}{recommended_mark}")

        print()
        print()

    # ── Build Excel report ───────────────────────────────────
    print(f'Building Excel report → {output_path}')
    if args.scenarios:
        # Multi-scenario Excel (Phase 6)
        build_excel(
            scenario_set,
            df,
            output_path,
            model_info=model_info,
            account_data=account_data,
            removal_log=removal_log,
            demand_index=demand_index,
            demand_normalized=args.normalize_demand,
            forecast_week=forecast_week,
            forecast_label=forecast_label,
            actual_window_label=actual_window_label,
            actual_window_detail=actual_window_detail,
        )
    else:
        # Old single-scenario mode (backward compat) - TODO: remove or map to minimal ScenarioSet
        raise NotImplementedError("Single-scenario Excel mode not yet migrated to Phase 6 format")
    print(f'Done. Report saved to: {output_path}')


if __name__ == '__main__':
    main()
