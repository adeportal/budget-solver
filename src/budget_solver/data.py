"""
Data loading and preprocessing functions.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def load_data(source):
    """
    Load CSV or Excel from a local file path.

    Applies lag-adjusted conversion values if present and validates currency.
    """
    p = Path(source)
    df = pd.read_excel(p, sheet_name='daily_data') if p.suffix in ('.xlsx', '.xls') \
         else pd.read_csv(p)

    # Normalise column names
    df.columns = [c.lower().strip().replace(' ', '_') for c in df.columns]
    rename = {'revenue': 'conversion_value', 'value': 'conversion_value',
              'spend': 'cost', 'account': 'account_name'}
    df.rename(columns=rename, inplace=True)

    # If lag-corrected column is present (from mcc_data_pull.py), use it as
    # conversion_value so all downstream logic (curves, ROAS) uses settled data.
    # Preserve the original raw column as conversion_value_raw for dual ROAS display.
    if 'conversion_value_adj' in df.columns:
        df['conversion_value_raw'] = pd.to_numeric(df['conversion_value'], errors='coerce').fillna(0)
        df['conversion_value'] = pd.to_numeric(df['conversion_value_adj'], errors='coerce').fillna(0)
        print('  Using lag-adjusted conversion values (conversion_value_adj).')

    for col in ('cost', 'conversion_value'):
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

    missing = {'account_name', 'cost', 'conversion_value'} - set(df.columns)
    if missing:
        sys.exit(f'ERROR: missing columns in data: {missing}')

    # Currency assertion
    if 'currency' in df.columns:
        currencies = df['currency'].dropna().unique()
        if len(currencies) > 1:
            sys.exit(f'ERROR: mixed currencies detected: {list(currencies)}. All data must be in the same currency.')
        if len(currencies) == 1 and currencies[0] != 'EUR':
            print(f'  WARNING: Currency is {currencies[0]}, not EUR. Ensure budget is in {currencies[0]}.')

    return df


def aggregate_weekly(df):
    """
    Group data into weekly buckets per account.
    Returns dict {account_name: {'spend': array, 'revenue': array, '_week': array}}
    """
    date_col = next((c for c in ('date', 'week_start', 'week') if c in df.columns), None)
    if date_col:
        df = df.copy()
        df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
        df['_week'] = df[date_col].dt.to_period('W')
        group_cols = ['account_name', '_week']
    else:
        group_cols = ['account_name']

    agg_dict = {'spend': ('cost', 'sum'), 'revenue': ('conversion_value', 'sum')}
    if 'clicks' in df.columns:
        agg_dict['clicks'] = ('clicks', 'sum')

    agg = df.groupby(group_cols).agg(**agg_dict).reset_index()

    result = {}
    for acc, grp in agg.groupby('account_name'):
        result[acc] = {
            'spend':   grp['spend'].values,
            'revenue': grp['revenue'].values,
            'clicks':  grp['clicks'].values if 'clicks' in grp.columns else np.zeros(len(grp)),
            '_week':   grp['_week'].values if '_week' in grp.columns else np.arange(len(grp)),
        }
    return result


def build_demand_index(account_data, external_csv=None):
    """
    Build a weekly seasonal demand index (ISO week 1-53 → multiplier, mean=1.0).

    Why this matters: revenue = f(spend) × demand(t). Without separating demand
    from spend efficiency, response curves conflate "high ROAS because we spent
    at peak season" with "high ROAS because spend is efficient at low volumes."
    The demand index removes that confound before curve fitting.

    Method (derived from data):
      - Compute median ROAS per ISO week number across all accounts and both years.
        Using median (not mean) dampens account-specific anomalies.
      - Normalize so the annual mean = 1.0.
      - High-demand weeks get a multiplier > 1 (e.g., Easter week ≈ 1.4),
        low-demand weeks < 1 (e.g., November ≈ 0.7).

    external_csv: optional path to a CSV with columns 'week' (1-53) and 'index'.
      Use this to override with SimilarWeb organic traffic data or internal
      booking volumes (more accurate than the derived estimate).

    Returns dict {iso_week_int: float}
    """
    if external_csv:
        idx_df = pd.read_csv(external_csv)
        idx_df.columns = [c.lower().strip() for c in idx_df.columns]
        return dict(zip(idx_df['week'].astype(int), idx_df['index'].astype(float)))

    rows = []
    for acc, data in account_data.items():
        for week, spend, revenue in zip(data['_week'], data['spend'], data['revenue']):
            if spend <= 0:
                continue
            try:
                # Period objects have a .week attribute; strings may be "2024-W01" style
                w = week.week if hasattr(week, 'week') else int(str(week).split('-W')[-1][:2])
            except Exception:
                continue
            rows.append({'week_num': w, 'roas': revenue / spend})

    if not rows:
        return {}

    df_idx = pd.DataFrame(rows)
    weekly_median = df_idx.groupby('week_num')['roas'].median()
    # Fill any missing weeks with the overall median
    all_weeks = pd.Series(range(1, 54), name='week_num')
    weekly_median = weekly_median.reindex(all_weeks, fill_value=weekly_median.median())
    normalized = weekly_median / weekly_median.mean()
    return normalized.to_dict()


def apply_demand_normalization(account_data, demand_index):
    """
    Divide each week's revenue by its demand index multiplier before curve fitting.
    Returns a new account_data dict with normalized revenue values and the
    per-week multipliers stored for later de-normalization.
    """
    if not demand_index:
        return account_data

    normed = {}
    for acc, data in account_data.items():
        norm_rev = []
        for week, spend, revenue in zip(data['_week'], data['spend'], data['revenue']):
            try:
                w = week.week if hasattr(week, 'week') else int(str(week).split('-W')[-1][:2])
            except Exception:
                w = 26  # fallback: mid-year
            mult = demand_index.get(w, 1.0)
            norm_rev.append(revenue / mult if mult > 0 else revenue)
        normed[acc] = {
            'spend':   data['spend'],
            'revenue': np.array(norm_rev),
            '_week':   data['_week'],
        }
    return normed


def remove_outliers(account_data, min_spend_pct=0.20, roas_iqr_mult=2.0):
    """
    Remove anomalous weekly observations before fitting response curves.

    Two-pass filter per account:
      1. Low-spend weeks  — drop weeks where spend < min_spend_pct × median spend.
         Rationale: very low-spend weeks (budget cuts, low season) have misleadingly
         high ROAS because demand is carried by organic/remarketing rather than paid
         incrementality. Fitting curves on these points makes spend look near-linear.
      2. ROAS IQR filter  — drop weeks where ROAS falls outside
         [Q1 − roas_iqr_mult×IQR, Q3 + roas_iqr_mult×IQR].
         Uses 2× (not the standard 1.5×) to be conservative and only catch genuine
         anomalies (tracking outages, attribution errors, promo misfires).

    Returns (cleaned_account_data, removal_log)
    removal_log: list of dicts with keys account, week, spend, revenue, roas, reason
    """
    cleaned = {}
    removal_log = []

    for acc, data in account_data.items():
        spend   = np.array(data['spend'],   dtype=float)
        revenue = np.array(data['revenue'], dtype=float)
        weeks   = np.array(data['_week'])

        keep = np.ones(len(spend), dtype=bool)

        # ── Pass 1: low-spend weeks ──────────────────────────
        median_sp = np.median(spend[spend > 0]) if np.any(spend > 0) else 1.0
        threshold = median_sp * min_spend_pct
        low_mask  = spend < threshold
        for i in np.where(low_mask)[0]:
            roas = revenue[i] / spend[i] if spend[i] > 0 else 0
            removal_log.append({
                'account': acc, 'week': str(weeks[i]),
                'spend': round(spend[i], 2), 'revenue': round(revenue[i], 2),
                'roas': round(roas, 3), 'reason': f'low spend (<{min_spend_pct:.0%} of median)'
            })
        keep &= ~low_mask

        # ── Pass 2: ROAS IQR outliers (on surviving weeks) ───
        roas_all = np.where(spend > 0, revenue / spend, 0)
        roas_ok  = roas_all[keep]
        if len(roas_ok) >= 4:
            q1, q3 = np.percentile(roas_ok, [25, 75])
            iqr    = q3 - q1
            lo, hi = q1 - roas_iqr_mult * iqr, q3 + roas_iqr_mult * iqr
            roas_outlier = keep & ((roas_all < lo) | (roas_all > hi))
            for i in np.where(roas_outlier)[0]:
                removal_log.append({
                    'account': acc, 'week': str(weeks[i]),
                    'spend': round(spend[i], 2), 'revenue': round(revenue[i], 2),
                    'roas': round(roas_all[i], 3),
                    'reason': f'ROAS outlier (IQR×{roas_iqr_mult}: bounds {lo:.2f}–{hi:.2f})'
                })
            keep &= ~roas_outlier

        cleaned[acc] = {
            'spend':   spend[keep],
            'revenue': revenue[keep],
            '_week':   weeks[keep],
        }

    return cleaned, removal_log


def select_training_window_by_cv(
    df: pd.DataFrame,
    date_col: str,
    latest_date,
    candidates: list | None = None,
) -> dict:
    """
    Select the optimal training window per account via 1-month holdout CV.

    For each candidate window W (months), per account:
      - Train on data from (latest_date - W months) to (latest_date - 1 month)
      - Fit a log curve to the weekly spend/revenue in that window
      - Predict revenue for the most recent month using actual weekly spend
      - Compute WAPE = Σ|predicted - actual| / Σ actual  (0 = perfect)
    Pick W with minimum WAPE.  Ties broken by largest W (more data preferred).

    Returns dict {account_name: optimal_months_int}

    Falls back to 6 months for accounts with insufficient data or flat spend.
    """
    from budget_solver.curves import fit_response_curve  # local import avoids circular dep

    candidates = candidates or [3, 6, 9, 12]
    DEFAULT_WINDOW = 6

    latest_date = pd.Timestamp(latest_date).normalize()
    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")

    # Test period: last full calendar month before latest_date
    test_end   = (latest_date.replace(day=1) - pd.Timedelta(days=1)).normalize()
    test_start = test_end.replace(day=1)

    # We need at least 2 months of data before the test period for any window to work
    if test_start <= latest_date - pd.DateOffset(months=max(candidates)):
        pass  # Enough history

    results: dict[str, int] = {}

    for acc in df["account_name"].unique():
        acc_df = df[df["account_name"] == acc].copy()

        # Test period actuals
        test_df = acc_df[
            (acc_df[date_col] >= test_start) & (acc_df[date_col] <= test_end)
        ]
        if len(test_df) == 0 or test_df["cost"].sum() <= 0:
            results[acc] = DEFAULT_WINDOW
            continue

        # Aggregate test period to weekly buckets (same as aggregate_weekly)
        test_df = test_df.copy()
        test_df["_week"] = test_df[date_col].dt.to_period("W")
        test_weekly = (
            test_df.groupby("_week")
            .agg(spend=("cost", "sum"), revenue=("conversion_value", "sum"))
            .reset_index()
        )
        if len(test_weekly) == 0 or test_weekly["spend"].sum() <= 0:
            results[acc] = DEFAULT_WINDOW
            continue

        best_wape   = float("inf")
        best_window = DEFAULT_WINDOW

        for W in sorted(candidates, reverse=True):  # try larger windows first; ties → larger W wins
            train_end   = test_start - pd.Timedelta(days=1)
            train_start = latest_date - pd.DateOffset(months=W)

            train_df = acc_df[
                (acc_df[date_col] >= train_start) & (acc_df[date_col] <= train_end)
            ].copy()
            if len(train_df) == 0:
                continue

            train_df["_week"] = train_df[date_col].dt.to_period("W")
            train_weekly = (
                train_df.groupby("_week")
                .agg(spend=("cost", "sum"), revenue=("conversion_value", "sum"))
                .reset_index()
            )
            if len(train_weekly) < 3:
                continue  # Too few weeks to fit a curve

            try:
                fn, _, _, _ = fit_response_curve(
                    train_weekly["spend"].values,
                    train_weekly["revenue"].values,
                )
                # Predict test weeks
                predicted = np.array([fn(s) for s in test_weekly["spend"].values])
                actual    = test_weekly["revenue"].values
                denom     = np.sum(np.abs(actual))
                if denom <= 0:
                    continue
                wape = float(np.sum(np.abs(predicted - actual)) / denom)
            except Exception:
                continue

            if wape < best_wape:
                best_wape   = wape
                best_window = W

        results[acc] = best_window

    return results
