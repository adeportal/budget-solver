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

from budget_solver.constants import TRAILING_WINDOW_DAYS, WEEKS_PER_MONTH, DAYS_PER_MONTH, DATA_PATH
from budget_solver.data import (
    load_data,
    aggregate_weekly,
    build_demand_index,
    apply_demand_normalization,
    remove_outliers,
    select_training_window_by_cv,
)
from budget_solver.prediction_log import (
    save_predictions,
    load_and_score_history,
    compute_portfolio_accuracy_summary,
)
from budget_solver.curves import fit_portfolio_curves
from budget_solver.solver import prepare_bounds
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
    parser.add_argument('--data',    default='',
                        help='Path to input CSV (default: output/core_markets.csv). '
                             'Useful for backtesting on a filtered dataset.')
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
    parser.add_argument('--training-months-override', default='',
                        help='Per-account training window overrides, e.g. '
                             '"Landal BE:12,Landal DE:12". Overrides --training-months '
                             'for the specified accounts only.')
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
    parser.add_argument('--two-stage', action='store_true', default=False,
                        help='Use two-stage spend → clicks → revenue model instead of direct '
                             'spend → revenue. Separates click efficiency (CPC dynamics) from '
                             'click quality (CVR × AOV). Recommended when CPCs are trending up.')

    args = parser.parse_args()

    # Ensure output directory exists
    Path('output').mkdir(exist_ok=True)

    output_path = args.output or f'output/budget_solver_{datetime.now().strftime("%Y%m%d_%H%M")}.xlsx'
    min_spend   = parse_kv_arg(args.min)
    max_spend   = parse_kv_arg(args.max)

    # ── Load & validate ──────────────────────────────────────
    data_path = Path(args.data) if args.data else DATA_PATH
    if not data_path.exists():
        print(f"No data found at {data_path}. Run 'budget-solver-pull' first.", file=sys.stderr)
        sys.exit(1)

    print(f'Loading data from: {data_path}')
    df = load_data(str(data_path))

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

    # ── Per-account training window selection via cross-validation ───────────
    # For each account, test windows [3, 6, 9, 12] months using a 1-month holdout.
    # The window with the lowest out-of-sample WAPE is selected automatically.
    # Explicit --training-months-override still wins over the CV result.
    explicit_overrides = {k: int(v) for k, v in parse_kv_arg(args.training_months_override).items()}

    if _date_col_early:
        _latest_date = pd.to_datetime(df[_date_col_early], errors='coerce').max().normalize()
        print('Training window selection (cross-validation per account):')
        cv_windows = select_training_window_by_cv(df, _date_col_early, _latest_date)
        combined_overrides = {**cv_windows, **explicit_overrides}

        for acc, months in combined_overrides.items():
            _cutoff = _latest_date - pd.DateOffset(months=months)
            acc_df = df[
                (df['account_name'] == acc) &
                (pd.to_datetime(df[_date_col_early], errors='coerce') >= _cutoff)
            ].copy()
            acc_weekly = aggregate_weekly(acc_df)
            if acc in acc_weekly:
                training_data[acc] = acc_weekly[acc]
                tag = ' (explicit override)' if acc in explicit_overrides else ' (CV-selected)'
                print(f'  {acc:<30}  {months} months{tag}')
            else:
                print(f'  WARNING: training override for "{acc}" matched no data — check account name.')
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
    # IS Lost to Budget < 5%: account is near its search ceiling → tighten cap to 1.2×.
    # IS Lost to Budget > 30%: genuinely budget-constrained → keep cap at 2.0×.
    # IS data requires a recent window — use the same 30-day window established below.
    _is_ltb_trailing: dict[str, float] = {}
    _is_col = 'search_impression_share_lost_budget'
    _date_col_cap = next((c for c in ('date', 'week_start', 'week') if c in df.columns), None)
    if _is_col in df.columns and _date_col_cap:
        _latest_cap  = pd.to_datetime(df[_date_col_cap], errors='coerce').max().normalize()
        _cutoff_cap  = _latest_cap - pd.Timedelta(days=29)
        _recent_cap  = df[
            (pd.to_datetime(df[_date_col_cap], errors='coerce') >= _cutoff_cap)
        ]
        for _acc, _grp in _recent_cap.groupby('account_name'):
            _vals = pd.to_numeric(_grp[_is_col], errors='coerce').dropna()
            if len(_vals) > 0:
                _is_ltb_trailing[_acc] = float(_vals.median())

    auto_max = {}
    for acc, data in account_data.items():
        hist_monthly_max = float(np.max(data['spend'])) * WEEKS_PER_MONTH
        is_ltb = _is_ltb_trailing.get(acc, float('nan'))
        if not np.isnan(is_ltb) and is_ltb < 0.05:
            cap_mult = 1.2  # near IS ceiling — tighter cap
        else:
            cap_mult = 2.0  # default
        auto_max[acc] = max(hist_monthly_max * cap_mult, args.budget * 0.02)

    effective_max = {**auto_max, **max_spend}
    effective_min = min_spend
    try:
        prepare_bounds(list(account_data.keys()), args.budget, effective_min, effective_max)
    except ValueError as exc:
        sys.exit(f'ERROR: {exc}')

    # ── Demand index ─────────────────────────────────────────
    # Priority:
    #   1. explicit --demand-index-csv (user override, portfolio-wide)
    #   2. keyword_demand_index.csv (per-account, from last budget-solver-pull)
    #   3. Google Trends (portfolio-wide, non-circular)
    #   4. internal ROAS-derived index (circular fallback)
    #
    # per_account_demand_index: {account_name: {iso_week: multiplier}}
    # Used to apply per-account demand multipliers when --normalize-demand is set.
    per_account_demand_index: dict = {}

    if args.demand_index_csv:
        demand_index = build_demand_index(account_data, external_csv=args.demand_index_csv)
        source_label = f'external CSV ({args.demand_index_csv})'
    else:
        # Try keyword-based per-account index first (most precise)
        try:
            from budget_solver.keyword_demand import load_keyword_demand_index, OUTPUT_DEMAND_CSV
            kw_per_account, kw_portfolio = load_keyword_demand_index(OUTPUT_DEMAND_CSV)
        except Exception:
            kw_per_account, kw_portfolio = {}, {}

        if kw_portfolio:
            demand_index           = kw_portfolio
            per_account_demand_index = kw_per_account
            source_label = (
                f'keyword Planner (per-account exact match, '
                f'{len(kw_per_account)} accounts with individual indices)'
            )
        else:
            # Fallback: Google Trends
            try:
                from budget_solver.trends import build_trends_demand_index
                trends_index = build_trends_demand_index()
            except Exception:
                trends_index = None
            if trends_index:
                demand_index = trends_index
                source_label = 'Google Trends (external, non-circular)'
            else:
                demand_index = build_demand_index(account_data)
                source_label = 'derived from data (Google Trends unavailable)'

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
    if per_account_demand_index:
        print('  Per-account demand multipliers for forecast week:')
        for acc in sorted(per_account_demand_index.keys()):
            acc_mult = per_account_demand_index[acc].get(forecast_week, forecast_demand)
            print(f'    {acc:<30}  {acc_mult:.3f}')
    print()

    # ── Holiday correction ────────────────────────────────────
    # Correct for year-specific holiday density differences vs the historical average
    # baked into the response curves. Applied to all accounts independently.
    # Most impactful for Easter timing shifts and school holiday calendar variation.
    from budget_solver.holiday_calendar import compute_holiday_corrections, forecast_week_to_ym
    _forecast_year, _forecast_month = forecast_week_to_ym(forecast_week)
    holiday_corrections = compute_holiday_corrections(
        accounts=list(account_data.keys()),
        forecast_year=_forecast_year,
        forecast_month=_forecast_month,
        lookback_years=[2024, 2025],
    )
    print(f'Holiday corrections ({_forecast_year}-{_forecast_month:02d}):')
    for acc in sorted(holiday_corrections.keys()):
        factor, explanation = holiday_corrections[acc]
        flag = '  ▲' if factor > 1.05 else ('  ▼' if factor < 0.95 else '   ')
        print(f'  {acc:<30}  {factor:.2f}×{flag}  {explanation}')
    print()

    # ── Weather correction ────────────────────────────────────
    # Compares forecast-month sunshine hours (Open-Meteo) to historical average
    # (ERA5 archive, same month 2023-2025). Outdoor leisure demand tracks sunshine
    # strongly; a sunnier-than-average month justifies slightly more spend.
    # Only activates within 30 days of the forecast month start; graceful fallback.
    weather_corrections: dict[str, tuple[float, str]] = {}
    try:
        from budget_solver.weather import compute_weather_multipliers
        weather_corrections = compute_weather_multipliers(
            accounts=list(account_data.keys()),
            forecast_year=_forecast_year,
            forecast_month=_forecast_month,
        )
        print(f'Weather corrections ({_forecast_year}-{_forecast_month:02d}):')
        for acc in sorted(weather_corrections.keys()):
            factor, explanation = weather_corrections[acc]
            flag = '  ☀' if factor > 1.02 else ('  ☁' if factor < 0.98 else '   ')
            print(f'  {acc:<30}  {factor:.2f}×{flag}  {explanation}')
        print()
    except Exception as exc:
        print(f'Weather corrections: skipped ({exc.__class__.__name__})')
        print()

    # Normalise revenue by demand index before curve fitting (optional)
    fitting_data = training_data
    if args.normalize_demand:
        fitting_data = apply_demand_normalization(training_data, demand_index)
        print('Demand normalization applied: revenue ÷ demand index before curve fitting.')
        print()

    # Resolve date column early — needed for CPC trends and calibration blocks below
    date_col = next((c for c in ('date', 'week_start', 'week') if c in df.columns), None)
    if date_col:
        df[date_col] = pd.to_datetime(df[date_col], errors='coerce')

    # ── CPC trend diagnostic (always computed) ───────────────
    # Compares trailing 30-day CPC to training-period CPC per account.
    # A rising CPC means the spend→clicks relationship is becoming less efficient —
    # the model may be over-optimistic at higher spend levels if this isn't accounted for.
    cpc_trends: dict[str, float] = {}
    if date_col and 'clicks' in df.columns:
        train_df_for_cpc = df[pd.to_datetime(df[date_col], errors='coerce') >= _training_cutoff] \
            if args.training_months > 0 else df
        train_cpc_data = train_df_for_cpc.groupby('account_name').agg(
            cost=('cost', 'sum'), clicks=('clicks', 'sum')
        )
        recent_cpc_data = recent.groupby('account_name').agg(
            cost=('cost', 'sum'), clicks=('clicks', 'sum')
        ) if 'recent' in dir() else pd.DataFrame()

        for acc in sorted(account_data.keys()):
            t = train_cpc_data.loc[acc] if acc in train_cpc_data.index else None
            r = recent_cpc_data.loc[acc] if (not recent_cpc_data.empty and acc in recent_cpc_data.index) else None
            if t is not None and r is not None and t['clicks'] > 0 and r['clicks'] > 0:
                cpc_train   = t['cost'] / t['clicks']
                cpc_recent  = r['cost'] / r['clicks']
                cpc_trends[acc] = (cpc_recent / cpc_train) - 1.0

    if cpc_trends:
        print('CPC trends (trailing 30d vs training period):')
        for acc in sorted(cpc_trends.keys()):
            trend = cpc_trends[acc]
            flag  = '  ⚠ CPC inflation' if trend > 0.15 else ('  ↓' if trend < -0.10 else '')
            print(f'  {acc:<30}  {trend:+.1%}{flag}')
        print()

    # ── Fit curves ───────────────────────────────────────────
    if args.two_stage:
        from budget_solver.curves import fit_two_stage_curves
        print('Fitting two-stage response curves (spend → clicks → revenue):')
        portfolio_results = fit_two_stage_curves(fitting_data, preferred_model='log')
    else:
        print('Fitting response curves (portfolio-wide consistency):')
        portfolio_results = fit_portfolio_curves(fitting_data, preferred_model='log')

    predict_fns  = {}
    model_info   = {}

    # Max observed weekly spend per account (from training data, before outlier removal
    # is irrelevant here — we want the actual observed upper bound for extrapolation logic)
    max_obs_weekly: dict[str, float] = {}

    for acc in sorted(portfolio_results.keys()):
        fn, params, r2, mname = portfolio_results[acc]
        predict_fns[acc] = fn
        model_info[acc]  = (fn, params, r2, mname)
        sp_arr = fitting_data[acc]['spend']
        n = len(sp_arr)
        max_obs_weekly[acc] = float(sp_arr.max()) if n > 0 else 0.0
        cpc_str = f'  CPC {cpc_trends[acc]:+.1%}' if acc in cpc_trends else ''
        print(f'  {acc:<30}  model={mname:<15}  R²={r2:.3f}  n={n}{cpc_str}')

    # Wrap predict_fns with:
    #   1. demand normalization de-scaling (per-account if available, else portfolio)
    #      — only when --normalize-demand is set
    #   2. holiday correction (always) — adjusts for forecast month's holiday density
    #      vs the historical average baked into the response curves
    #   3. weather correction (always, graceful fallback to 1.0) — sunshine-hours
    #      ratio for forecast month vs historical average
    for acc in list(predict_fns.keys()):
        fn = predict_fns[acc]
        hc = holiday_corrections.get(acc, (1.0, ''))[0]
        wc = weather_corrections.get(acc, (1.0, ''))[0]
        if args.normalize_demand:
            d = per_account_demand_index.get(acc, {}).get(forecast_week, forecast_demand)
            predict_fns[acc] = lambda x, fn=fn, d=d, hc=hc, wc=wc: fn(x) * d * hc * wc
        else:
            predict_fns[acc] = lambda x, fn=fn, hc=hc, wc=wc: fn(x) * hc * wc

    # ── Monthly scaling fix ───────────────────────────────────
    # Curves are fitted on WEEKLY aggregates, but current_alloc and budget are
    # MONTHLY totals. Plugging monthly spend directly into a weekly curve asks
    # "what revenue does one week generate at €X monthly spend?" — wrong scale.
    # Correct formula: monthly_revenue = WEEKS_PER_MONTH × weekly_fn(monthly_spend / WEEKS_PER_MONTH)
    predict_fns = {
        acc: (lambda x, fn=fn, wpm=WEEKS_PER_MONTH: wpm * fn(x / wpm))
        for acc, fn in predict_fns.items()
    }

    # ── Extrapolation dampening ───────────────────────────────
    # Beyond max observed monthly spend, the curve is extrapolating outside
    # its training range. Confidence in log/power curves decays rapidly here.
    # We dampen the INCREMENTAL revenue above the observed max using an
    # exponential decay: decay = e^(-k × (x/max_obs − 1)), k=2.
    #   At 1.1× max: ~82% of incremental survives (gentle nudge)
    #   At 1.5× max: ~37% of incremental survives (meaningful skepticism)
    #   At 2.0× max: ~14% of incremental survives (strong discount)
    # Revenue at max_obs is preserved exactly — only uncharted territory is discounted.
    _EXTRAP_K = 2.0
    max_obs_monthly = {acc: v * WEEKS_PER_MONTH for acc, v in max_obs_weekly.items()}
    for acc in list(predict_fns.keys()):
        _max = max_obs_monthly.get(acc, 0.0)
        if _max <= 0:
            continue
        _fn = predict_fns[acc]
        _rev_at_max = _fn(_max)

        def _damped(x, fn=_fn, max_m=_max, rev_max=_rev_at_max, k=_EXTRAP_K):
            if x <= max_m:
                return fn(x)
            incremental = fn(x) - rev_max
            decay = float(np.exp(-k * (x / max_m - 1.0)))
            return rev_max + incremental * decay

        predict_fns[acc] = _damped

    print()

    # ── Current spend + actual ROAS (trailing 30-day window = actual baseline) ──
    actual_window_label = 'full input range'
    actual_window_detail = actual_window_label
    if date_col:
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
    # Uses a 14-day window (more recent, less contaminated by start-of-month effects).
    # Blend weight is confidence-weighted: full weight (0.25) only when the trailing
    # window is clean. Confidence degrades when:
    #   (a) many days have zero spend (campaign paused mid-window) → active_ratio ↓
    #   (b) daily ROAS is highly volatile → CV of daily ROAS ↑
    # Formula: confidence = active_ratio × (1 − min(cv, 1.0))
    #          blend = _CAL_BLEND × confidence
    # When confidence → 0 the calibration scale collapses to 1.0 (no adjustment),
    # letting the fitted curve stand on its own rather than anchoring to bad data.
    # Backtest (April 2026) showed calibration issues:
    # - cal_roas was using raw conversion_value (not lag-adjusted) — understates ROAS for
    #   recent days with incomplete attribution. Fixed: use actual_roas (lag-adjusted 30d).
    # - High blend (0.75) over-anchored to the trailing-window ROAS and degraded forecasts
    #   when the forecast period operates at a different spend level than the calibration
    #   anchor. April backtest showed uncalibrated curves were more accurate than 0.75-blend
    #   calibrated curves. Settled on 0.40 as a light touch that helps without over-fitting.
    # - 14-day window kept for confidence/volatility signal only.
    _CAL_WINDOW_DAYS = 14
    _CAL_BLEND       = 0.40   # 0.75 overshot — April backtest showed uncalibrated curves accurate;
                               # high blend over-anchored to March ROAS and degraded April forecasts
    _CAL_CAP_LO      = 0.7
    _CAL_CAP_HI      = 1.3
    _CAL_CV_MAX      = 1.0    # CV above this → confidence saturates to 0 contribution

    cal_roas: dict[str, float] = {}
    cal_confidence: dict[str, float] = {}
    if date_col and not args.no_calibrate:
        cal_cutoff = latest_date - pd.Timedelta(days=_CAL_WINDOW_DAYS - 1)
        cal_recent = df[(df[date_col] >= cal_cutoff) & (df[date_col] <= latest_date)]
        cal_spend  = cal_recent.groupby('account_name')['cost'].sum().to_dict()

        # Per-account confidence score from daily spend activity + ROAS volatility
        # (14-day window still used for confidence; ROAS anchor uses the lag-adjusted 30d figure)
        for acc in cal_spend:
            if cal_spend[acc] <= 0:
                continue
            cal_roas[acc] = actual_roas.get(acc, 0)   # lag-adjusted 30d — more stable than raw 14d

            acc_days = cal_recent[cal_recent['account_name'] == acc]
            daily_sp  = acc_days.groupby(date_col)['cost'].sum()
            active_days   = int((daily_sp > 0).sum())
            total_days    = int(len(daily_sp)) or _CAL_WINDOW_DAYS
            active_ratio  = active_days / total_days

            daily_rev  = acc_days.groupby(date_col)['conversion_value'].sum()
            active_sp  = daily_sp[daily_sp > 0]
            active_rev = daily_rev[daily_sp > 0]
            if len(active_sp) >= 3:
                daily_roas = active_rev / active_sp
                cv = float(daily_roas.std() / daily_roas.mean()) if daily_roas.mean() > 0 else 1.0
            else:
                cv = 1.0  # too few days to estimate — treat as maximum uncertainty

            confidence = active_ratio * (1.0 - min(cv, _CAL_CV_MAX))
            cal_confidence[acc] = max(confidence, 0.0)

    calibration_factors = {}
    cal_confidence_out: dict[str, float] = {}
    if not args.no_calibrate:
        for acc in list(predict_fns):
            curr_sp    = current_alloc.get(acc, 0)
            adj_roas   = cal_roas.get(acc, actual_roas.get(acc, 0))
            confidence = cal_confidence.get(acc, 1.0)
            blend      = _CAL_BLEND * confidence
            if curr_sp > 0 and adj_roas > 0:
                model_pred = predict_fns[acc](curr_sp)
                model_roas = model_pred / curr_sp
                if model_roas > 0:
                    raw_scale     = adj_roas / model_roas
                    blended_scale = (1.0 - blend) + blend * raw_scale
                    capped_scale  = float(np.clip(blended_scale, _CAL_CAP_LO, _CAL_CAP_HI))
                    calibration_factors[acc] = capped_scale
                    cal_confidence_out[acc]  = confidence
                    predict_fns[acc] = (lambda x, fn=predict_fns[acc], s=capped_scale: fn(x) * s)
                    fn_i, params_i, r2_i, mname_i = model_info[acc]
                    model_info[acc] = (fn_i, params_i, r2_i, f'{mname_i}+cal')

    # ── Walk-forward bias check (diagnostic only) ────────────
    # Evaluate calibrated predictions against last 3 complete settled months and
    # print the per-account over/under-prediction ratio for transparency.
    # We do NOT apply a flat divisor: backtesting shows the ratio is spend-level
    # dependent and applying a constant multiplier degrades rather than improves
    # accuracy when forecast-period spend differs from the calibration anchor.
    _DEBIAS_MONTHS   = 3
    _DEBIAS_LAG_DAYS = 30

    if not args.no_calibrate and date_col:
        col_rv = 'conversion_value_adj' if 'conversion_value_adj' in df.columns else 'conversion_value'
        settled_cutoff = (latest_date - pd.Timedelta(days=_DEBIAS_LAG_DAYS)).normalize()

        debias_months: list[pd.Timestamp] = []
        candidate = pd.Timestamp(settled_cutoff.year, settled_cutoff.month, 1)
        while len(debias_months) < _DEBIAS_MONTHS:
            m_end = candidate + pd.offsets.MonthEnd(0)
            if m_end.normalize() <= settled_cutoff:
                debias_months.append(candidate)
            candidate -= pd.DateOffset(months=1)
            if candidate < latest_date - pd.DateOffset(months=18):
                break

        if debias_months:
            acc_ratios: dict[str, list] = {acc: [] for acc in predict_fns}
            for m_start in debias_months:
                m_end = (m_start + pd.offsets.MonthEnd(0)).normalize()
                mdf   = df[
                    (pd.to_datetime(df[date_col]) >= m_start) &
                    (pd.to_datetime(df[date_col]) <= m_end)
                ]
                for acc, fn in predict_fns.items():
                    sp = mdf[mdf['account_name'] == acc]['cost'].sum()
                    rv = mdf[mdf['account_name'] == acc][col_rv].sum()
                    if sp > 0 and rv > 0:
                        pred = fn(sp)
                        if pred > 0:
                            acc_ratios[acc].append(pred / rv)

            print('Model accuracy vs settled months (calibrated predictions / actual):')
            for acc, ratios in acc_ratios.items():
                if ratios:
                    geo_mean = float(np.exp(np.mean(np.log(ratios))))
                    tag = '⚠ over ' if geo_mean > 1.15 else ('⚠ under' if geo_mean < 0.85 else '  ok  ')
                    months_str = ', '.join(f'{r:.2f}x' for r in ratios)
                    print(f'  {acc:<30}  {tag}  geo={geo_mean:.3f}x  [{months_str}]')
            print()

    # ── Print per-market spend + ROAS table ──────────────────
    if args.no_calibrate:
        cal_label = ''
    else:
        cal_label = '  cal.factor  confidence'
    print(f'── CURRENT PERFORMANCE ({actual_window_detail}) ──────────────────────────────')
    print(f'  {"Account":<35} {"Spend":>12} {"ROAS (raw)":>12} {"ROAS (lag-adj)":>15}{cal_label}')
    print('  ' + '─' * (78 + (22 if not args.no_calibrate else 0)))
    for acc in sorted(current_alloc):
        sp   = current_alloc[acc]
        rraw = actual_roas_raw.get(acc, 0)
        radj = actual_roas.get(acc, 0)
        if not args.no_calibrate:
            conf = cal_confidence_out.get(acc, cal_confidence.get(acc, 1.0))
            conf_flag = '  ⚠ low conf' if conf < 0.4 else ''
            cal_str = f'  {calibration_factors.get(acc, 1.0):>8.3f}x  {conf:>8.0%}{conf_flag}'
        else:
            cal_str = ''
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

    # ── Competitive landscape (auction insights) ──────────────
    # Loads pre-pulled auction_insights.csv (written by budget-solver-pull).
    # Displays trailing vs prior 30-day competitor IS per account and flags
    # any competitor that surged > +10pp — a leading indicator of CPC pressure
    # not visible in the response curves. No math changes; informational only.
    try:
        from budget_solver.auction_insights import load_auction_insights, format_insights_table
        insights_df = load_auction_insights()
        if not insights_df.empty:
            print(format_insights_table(insights_df))
    except Exception:
        pass

    # ── Impression pool analysis ──────────────────────────────
    # Derives the physical impression ceiling per account from trailing 30-day data:
    #   total_pool = impressions_won / impression_share
    #   max_revenue = total_pool × CTR × revenue_per_click  (monthly-scaled)
    #
    # When the impression pool ceiling is tighter than the auto spend cap, the cap
    # is updated. Prevents the optimizer from recommending spend that would require
    # buying auctions that don't exist in that market.
    if date_col and 'search_impression_share' in df.columns and 'impressions' in df.columns:
        from scipy.optimize import brentq as _brentq

        is_col = 'search_impression_share'
        print('── IMPRESSION POOL ANALYSIS (trailing 30d) ───────────────────────────────')
        print(f'  {"Account":<30} {"IS":>6} {"Pool/mo":>10} {"Max rev/mo":>12} {"Spend ceiling":>15}')
        print('  ' + '─' * 78)

        for acc in sorted(predict_fns.keys()):
            acc_recent = recent[recent['account_name'] == acc] if 'recent' in dir() and not recent.empty else pd.DataFrame()
            if acc_recent.empty:
                continue

            imp_total  = acc_recent['impressions'].sum()
            clk_total  = acc_recent['clicks'].sum() if 'clicks' in acc_recent.columns else 0
            rev_total  = acc_recent['conversion_value'].sum()

            # IS: impression-weighted average
            is_vals = acc_recent[is_col].dropna()
            imp_vals = acc_recent.loc[is_vals.index, 'impressions']
            if len(is_vals) == 0 or imp_total <= 0 or clk_total <= 0:
                continue
            avg_is = float(np.average(is_vals, weights=imp_vals)) if imp_vals.sum() > 0 else float(is_vals.mean())

            if avg_is <= 0.01:
                continue

            # Derive impression pool and revenue ceiling (scaled to one month)
            days_in_window   = (acc_recent[date_col].max() - acc_recent[date_col].min()).days + 1
            scale_to_month   = DAYS_PER_MONTH / max(days_in_window, 1)
            pool_monthly     = (imp_total / avg_is) * scale_to_month
            ctr              = clk_total / imp_total
            rpc              = rev_total / clk_total if clk_total > 0 else 0.0
            max_rev_monthly  = pool_monthly * ctr * rpc

            # Find implied spend ceiling: spend at which curve hits max_rev_monthly
            spend_ceiling_str = 'not binding'
            try:
                current_max = effective_max.get(acc, args.budget)
                if predict_fns[acc](current_max) > max_rev_monthly > 0:
                    # Pool ceiling is tighter — find the crossover spend
                    lo, hi = 0.0, current_max
                    sp_ceil = _brentq(
                        lambda s: predict_fns[acc](s) - max_rev_monthly,
                        lo, hi, xtol=100, maxiter=50
                    )
                    if sp_ceil < current_max:
                        effective_max[acc] = max(sp_ceil, effective_min.get(acc, 0))
                        spend_ceiling_str = f'€{sp_ceil:>8,.0f}  ← cap tightened'
            except Exception:
                pass

            pool_label = f'{pool_monthly/1e6:.1f}M' if pool_monthly >= 1e6 else f'{pool_monthly/1e3:.0f}k'
            print(
                f'  {acc:<30} {avg_is:>5.0%} {pool_label:>10} '
                f'€{max_rev_monthly:>10,.0f}  {spend_ceiling_str}'
            )

        print()

    # ── Feedback loop: load history + apply bias corrections ─────────────────
    accuracy_df, bias_corrections = load_and_score_history(df, date_col=date_col)
    if bias_corrections:
        print('Bias corrections (from historical prediction accuracy):')
        for acc, factor in sorted(bias_corrections.items()):
            direction = 'up' if factor > 1.0 else 'down'
            print(f'  {acc:<30}  ×{factor:.3f}  (model was systematically {direction}-predicting)')
            predict_fns[acc] = (lambda x, fn=predict_fns[acc], f=factor: fn(x) * f)
        print()
    else:
        accuracy_df = accuracy_df  # empty is fine

    # ══════════════════════════════════════════════════════════
    # Scenario Generation (A/B/C/D)
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
            apply_stability=args.apply_stability,
            max_spend=effective_max,
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

        # ── Extrapolation warnings ────────────────────────────
        # Flag accounts where the recommended spend exceeds 120% of the maximum
        # spend observed in the training data. Predictions in this range are
        # dampened (see above) but the warning makes it explicit to the user.
        _EXTRAP_WARN_RATIO = 1.20
        rec_scenario = next((s for s in scenarios if s.recommended), None)
        if rec_scenario:
            extrap_flags = []
            for alloc in rec_scenario.allocations:
                max_m = max_obs_monthly.get(alloc.account, 0.0)
                if max_m > 0 and alloc.monthly_spend > max_m * _EXTRAP_WARN_RATIO:
                    ratio = alloc.monthly_spend / max_m
                    extrap_flags.append((alloc.account, alloc.monthly_spend, max_m, ratio))
            if extrap_flags:
                print('⚠  EXTRAPOLATION WARNING — recommended spend exceeds observed training range:')
                for acc, rec_sp, max_m, ratio in extrap_flags:
                    print(f'   {acc:<35}  rec €{rec_sp:>8,.0f}  vs max observed €{max_m:>8,.0f}  ({ratio:.1f}× — dampened)')
                print('   Curve predictions beyond observed range are discounted but treat these with care.')
                print()

        # ── Simulator cross-check ─────────────────────────────
        # Compares model predictions vs Google's bid simulator at the recommended
        # spend level. Large divergence (> 20%) flags a calibration risk.
        try:
            from budget_solver.bid_simulator import load_simulator_data, format_simulator_table
            sim_df = load_simulator_data()
            if not sim_df.empty and rec_scenario:
                rec_alloc = {a.account: a.monthly_spend for a in rec_scenario.allocations}
                table = format_simulator_table(sim_df, predict_fns, current_alloc, rec_alloc)
                if table:
                    print(table)
        except Exception:
            pass

        print()

    # ── Build Excel report ───────────────────────────────────
    print(f'Building Excel report → {output_path}')
    if args.scenarios:
        try:
            from budget_solver.auction_insights import load_auction_insights
            _auction_insights_df = load_auction_insights()
        except Exception:
            _auction_insights_df = pd.DataFrame()

        try:
            from budget_solver.bid_simulator import load_simulator_data
            _simulator_df = load_simulator_data()
        except Exception:
            _simulator_df = pd.DataFrame()

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
            accuracy_df=accuracy_df,
            is_ltb_trailing=_is_ltb_trailing,
            holiday_corrections=holiday_corrections,
            weather_corrections=weather_corrections,
            cal_confidence={**cal_confidence, **cal_confidence_out},
            calibration_factors=calibration_factors,
            auction_insights_df=_auction_insights_df,
            simulator_df=_simulator_df,
            current_alloc=current_alloc,
        )
    else:
        raise NotImplementedError("Single-scenario Excel mode not yet migrated to Phase 6 format")
    print(f'Done. Report saved to: {output_path}')

    # ── Save predictions to feedback log ─────────────────────
    # Record the recommended Scenario C allocation for accuracy tracking next month.
    try:
        rec_scenario = next((s for s in scenarios if s.recommended), scenarios[1])
        rec_alloc    = {a.account: a.monthly_spend for a in rec_scenario.allocations}
        save_predictions(
            run_date=datetime.now().strftime('%Y-%m-%d'),
            forecast_period=forecast_label[:7] if len(forecast_label) >= 7 else forecast_label,
            allocations=rec_alloc,
            predict_fns=predict_fns,
        )
        print(f'Predictions saved to prediction log for {forecast_label}.')
    except Exception as _exc:
        print(f'  WARNING: could not save predictions to log: {_exc}')


if __name__ == '__main__':
    main()
