#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
"""
Budget Solver — Google Ads API Data Pull (v25)
───────────────────────────────────────────────
Pulls 24 months of daily account-level performance data from Google Ads API
for the 6 core markets. Queries at campaign level so filters can be applied,
then aggregates to account + date before writing to CSV.

FILTERS:
  - Campaign type = SEARCH only
  - Exclude campaigns containing "| BR" or "| PK" in the name
  - Core markets only: Landal BE/NL/DE, Roompot BE/NL/DE (see CORE_ACCOUNTS)

CONVERSION LAG CORRECTION:
  Conversions are attributed back to the click date, so recent days have
  incomplete data — not all conversions have arrived yet. Rather than
  excluding the last 30 days, we apply a daily multiplier derived from
  the observed conversion lag profile. Days older than 30 days are fully
  settled (multiplier = 1.0). The adjusted columns are:
    conversion_value_adj  — lag-corrected revenue (used by optimizer)
    conversions_adj       — lag-corrected conversions
    lag_factor            — the multiplier applied (1/cumulative_pct)

OUTPUT:
  output/core_markets.csv  — 6 core markets, one row per account per day (feeds into optimizer)

SETUP:
  1. pip install -e .
  2. Create google-ads.yaml (see README or bottom of this file)
  3. Run: budget-solver-pull
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from dateutil.relativedelta import relativedelta

import numpy as np
import pandas as pd
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# Micros conversion constant
MICROS_PER_UNIT = 1_000_000

# ─────────────────────────────────────────────────────────────
# CONVERSION LAG PROFILES — PER ACCOUNT
# ─────────────────────────────────────────────────────────────
# Each entry is a 30-element list of CUMULATIVE % of conversion value
# expected to have arrived by days_elapsed (index 0 = same day, 29 = day 29).
#
# Source: Google Ads "Days to conversion" path metrics report, Q1 2026,
# last-interaction attribution model, per-account, conversion value cumulative %.
#
# Granular data supplied for days 0-13 (individual day buckets).
# Days 14-20 and 21-29 are linearly interpolated from the weekly bucket totals:
#   - "14-21 days" bucket → cumulative at end of day 20
#   - "21-30 days" bucket → cumulative at end of day 29 (= 100%)
#
ACCOUNT_LAG_PROFILES: dict[str, list[float]] = {
    "Landal NL": [
        #  d0     d1     d2     d3     d4     d5     d6     d7     d8     d9
        42.00, 49.80, 54.50, 58.00, 61.10, 64.10, 67.00, 69.70, 71.50, 73.50,
        # d10    d11    d12    d13   (d14-d20: interp 80.2→89.4)  (d21-d29: interp 89.4→100)
        75.20, 76.90, 78.40, 80.20, 81.51, 82.83, 84.14, 85.46, 86.77, 88.09,
        89.40, 90.58, 91.76, 92.93, 94.11, 95.29, 96.47, 97.64, 98.82, 100.00,
    ],
    "Roompot NL": [
        44.30, 51.80, 56.10, 59.80, 62.70, 65.60, 68.40, 71.00, 73.00, 75.10,
        # (d14-d20: interp 81.3→90.3)  (d21-d29: interp 90.3→100)
        76.90, 78.30, 79.80, 81.30, 82.59, 83.87, 85.16, 86.44, 87.73, 89.01,
        90.30, 91.38, 92.46, 93.53, 94.61, 95.69, 96.77, 97.84, 98.92, 100.00,
    ],
    "Landal DE": [
        39.30, 47.90, 53.40, 57.20, 61.30, 64.40, 67.10, 70.10, 72.20, 74.10,
        # (d14-d20: interp 81.4→90.9)  (d21-d29: interp 90.9→100)
        76.00, 78.20, 79.90, 81.40, 82.76, 84.11, 85.47, 86.83, 88.19, 89.54,
        90.90, 91.91, 92.92, 93.93, 94.94, 95.96, 96.97, 97.98, 98.99, 100.00,
    ],
    "Roompot DE": [
        48.30, 56.70, 61.30, 64.90, 67.80, 70.90, 73.50, 76.10, 78.00, 79.30,
        # (d14-d20: interp 85.2→92.5)  (d21-d29: interp 92.5→100)
        80.60, 82.30, 83.90, 85.20, 86.24, 87.29, 88.33, 89.37, 90.41, 91.46,
        92.50, 93.33, 94.17, 95.00, 95.83, 96.67, 97.50, 98.33, 99.17, 100.00,
    ],
    "Landal BE": [
        40.80, 49.20, 53.70, 56.90, 60.00, 63.00, 65.90, 68.50, 70.70, 72.60,
        # (d14-d20: interp 79.9→88.7)  (d21-d29: interp 88.7→100)
        74.50, 76.70, 77.80, 79.90, 81.16, 82.41, 83.67, 84.93, 86.19, 87.44,
        88.70, 89.96, 91.21, 92.47, 93.72, 94.98, 96.23, 97.49, 98.74, 100.00,
    ],
    "Roompot BE": [
        47.00, 54.60, 58.20, 61.30, 65.10, 67.90, 70.70, 73.10, 75.00, 76.70,
        # (d14-d20: interp 82.5→89.9)  (d21-d29: interp 89.9→100)
        78.30, 79.90, 81.10, 82.50, 83.56, 84.61, 85.67, 86.73, 87.79, 88.84,
        89.90, 91.02, 92.14, 93.27, 94.39, 95.51, 96.63, 97.76, 98.88, 100.00,
    ],
}

# Portfolio average profile — fallback for any account not in ACCOUNT_LAG_PROFILES.
# Computed as the mean of all 6 accounts at each day index.
DEFAULT_LAG_PROFILE: list[float] = [
    43.62, 51.67, 56.20, 59.68, 63.00, 65.98, 68.77, 71.42, 73.40, 75.22,
    76.92, 78.72, 80.15, 81.75, 82.97, 84.19, 85.41, 86.63, 87.85, 89.06,
    90.28, 91.36, 92.44, 93.52, 94.60, 95.68, 96.76, 97.84, 98.92, 100.00,
]


def account_lag_factor(days_elapsed: int, account_name: str) -> float:
    """
    Return the lag multiplier for a specific account.

    Looks up the account-specific cumulative arrival profile.
    Falls back to the portfolio average for unknown accounts.

    days_elapsed = (pull_date - click_date).days

    Examples (Landal NL):
      0 days → 42.0% arrived → multiply by 2.38
      7 days → 69.7% arrived → multiply by 1.43
     14 days → 81.5% arrived → multiply by 1.23
     30+ days → 100% arrived → multiply by 1.00
    """
    if days_elapsed >= 30:
        return 1.0
    profile = ACCOUNT_LAG_PROFILES.get(account_name, DEFAULT_LAG_PROFILE)
    cum_pct = profile[days_elapsed]
    return 100.0 / cum_pct if cum_pct > 0 else 1.0


def lag_factor(days_elapsed: int) -> float:
    """
    Return the portfolio-average lag multiplier (backward-compatible wrapper).
    Prefer account_lag_factor() for per-account accuracy.
    """
    return account_lag_factor(days_elapsed, "")


def apply_lag_correction(df: pd.DataFrame, pull_date: datetime) -> pd.DataFrame:
    """
    Add lag_factor, conversion_value_adj, and conversions_adj columns.

    Uses per-account conversion arrival profiles from ACCOUNT_LAG_PROFILES.
    Spend and clicks are NOT adjusted (they are fully attributed same-day).
    """
    df = df.copy()
    df["date_dt"] = pd.to_datetime(df["date"])
    pull_day = pd.Timestamp(pull_date.date())
    df["days_elapsed"] = (pull_day - df["date_dt"]).dt.days
    df["lag_factor"] = df.apply(
        lambda row: account_lag_factor(int(row["days_elapsed"]), row["account_name"]),
        axis=1,
    )
    df["conversion_value_adj"] = df["conversion_value"] * df["lag_factor"]
    df["conversions_adj"]      = df["conversions"]      * df["lag_factor"]
    df.drop(columns=["date_dt"], inplace=True)
    return df

# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────

# Root MCC that owns the developer token with Standard Access.
# Auction insight metrics (and other restricted metrics) require login_customer_id
# to be the account whose developer token has Standard Access — not a sub-MCC.
ROOT_MCC_ID = "2322660480"   # RP - LDL MCC (232-266-0480)

# Sub-MCCs to enumerate child accounts from
CHILD_MCCS = {
    "8265762094": "Landal MCC",
    "6917028372": "Roompot MCC",
}

# Core markets to include in output (all others filtered out)
# These are the 6 accounts used for budget optimization
CORE_ACCOUNTS = [
    "Landal BE",
    "Landal NL",
    "Landal DE",
    "Roompot BE",
    "Roompot NL",
    "Roompot DE",
]

OUTPUT_CSV      = "output/core_markets.csv"
YAML_PATH       = Path(os.getenv(
    "GOOGLE_ADS_YAML_PATH",
    Path.home() / ".config" / "landal" / "google-ads.yaml"
))

# ─────────────────────────────────────────────────────────────
# GAQL — campaign-level daily data (1 year)
# Filters:
#   • SEARCH campaigns only
#   • Exclude "| BR" and "| PK" in campaign name
#   • Only days with spend > 0
# ─────────────────────────────────────────────────────────────

def build_query(start_date: str, end_date: str) -> str:
    return f"""
        SELECT
            customer.descriptive_name,
            customer.id,
            customer.currency_code,
            segments.date,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.clicks,
            metrics.impressions,
            metrics.search_impression_share,
            metrics.search_budget_lost_impression_share,
            metrics.search_rank_lost_impression_share
        FROM campaign
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
          AND campaign.advertising_channel_type = 'SEARCH'
          AND campaign.name NOT LIKE '%| BR%'
          AND campaign.name NOT LIKE '%| PK%'
          AND metrics.cost_micros > 0
        ORDER BY segments.date DESC
    """


# ─────────────────────────────────────────────────────────────
# LIST CHILD ACCOUNTS UNDER MCC
# ─────────────────────────────────────────────────────────────

def get_child_accounts(client: GoogleAdsClient, mcc_id: str) -> list[dict]:
    """Return list of {id, name} for all non-manager accounts under the MCC."""
    ga_service = client.get_service("GoogleAdsService")

    query = """
        SELECT
            customer_client.id,
            customer_client.descriptive_name,
            customer_client.manager,
            customer_client.status
        FROM customer_client
        WHERE customer_client.manager = FALSE
          AND customer_client.status = 'ENABLED'
    """

    accounts = []
    try:
        response = ga_service.search(customer_id=mcc_id, query=query)
        for row in response:
            accounts.append({
                "id":   str(row.customer_client.id),
                "name": row.customer_client.descriptive_name
            })
    except GoogleAdsException as ex:
        print(f"ERROR listing child accounts: {ex.error.code().name}")
        for error in ex.failure.errors:
            print(f"  {error.message}")
        sys.exit(1)

    return accounts


# ─────────────────────────────────────────────────────────────
# PULL DATA FOR ONE ACCOUNT
# ─────────────────────────────────────────────────────────────

def _safe_is(val) -> float:
    """Convert IS proto value to float, returning NaN if unavailable."""
    try:
        v = float(val)
        return v if 0.0 <= v <= 1.0 else float('nan')
    except (TypeError, ValueError):
        return float('nan')


def pull_account_data(
    client: GoogleAdsClient,
    account_id: str,
    account_name: str,
    query: str
) -> list[dict]:
    """Run GAQL query against one account and return rows as dicts."""
    ga_service = client.get_service("GoogleAdsService")
    rows = []

    try:
        response = ga_service.search(customer_id=account_id, query=query)
        for row in response:
            rows.append({
                "account_id":        account_id,
                "account_name":      account_name,
                "date":              row.segments.date,
                "cost":              row.metrics.cost_micros / MICROS_PER_UNIT,
                "conversions":       row.metrics.conversions,
                "conversion_value":  row.metrics.conversions_value,
                "clicks":            row.metrics.clicks,
                "impressions":       row.metrics.impressions,
                "search_impression_share":            _safe_is(row.metrics.search_impression_share),
                "search_impression_share_lost_budget": _safe_is(row.metrics.search_budget_lost_impression_share),
                "search_impression_share_lost_rank":  _safe_is(row.metrics.search_rank_lost_impression_share),
                "currency":          row.customer.currency_code,
            })
    except GoogleAdsException as ex:
        code = ex.error.code().name
        print(f"  WARNING: skipping {account_name} ({account_id}) — {code}")
        for error in ex.failure.errors:
            print(f"    {error.message}")

    return rows


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    # Ensure output directory exists
    Path("output").mkdir(exist_ok=True)

    # Date range: 24 months lookback ending today.
    # Conversion lag is handled by per-day multipliers (lag_factor) rather
    # than a hard cutoff, so we pull through to today and correct each day.
    pull_date  = datetime.today()
    end_date   = pull_date
    start_date = end_date - relativedelta(months=24)
    start_str  = start_date.strftime("%Y-%m-%d")
    end_str    = end_date.strftime("%Y-%m-%d")

    print(f"Budget Solver — Data Pull")
    print(f"Date range : {start_str} to {end_str}  (24-month lookback, lag-corrected)")
    print(f"Filters    : SEARCH campaigns only | excluding '| BR' and '| PK' | IS + CPC included")
    print()

    # Authenticate
    if not Path(YAML_PATH).exists():
        print(f"ERROR: credentials file not found at '{YAML_PATH}'")
        print("See the CREDENTIALS SETUP section at the bottom of this file.")
        sys.exit(1)

    client = GoogleAdsClient.load_from_storage(YAML_PATH, version="v23")

    # List child accounts across all MCCs.
    # Track which MCC each account belongs to — auction insight metrics require
    # login_customer_id to be the direct parent MCC (cross-manager access denied).
    accounts = []
    account_mcc_map: dict[str, str] = {}  # account_name → mcc_id
    for mcc_id, mcc_name in CHILD_MCCS.items():
        print(f"Fetching child accounts under {mcc_name} ({mcc_id})...")
        found = get_child_accounts(client, mcc_id)
        print(f"  Found {len(found)} account(s)")
        for acc in found:
            account_mcc_map[acc["name"]] = mcc_id
        accounts.extend(found)
    print(f"\nTotal accounts: {len(accounts)}\n")

    query = build_query(start_str, end_str)

    # Pull data for each account
    all_rows = []
    for acc in accounts:
        print(f"  Pulling: {acc['name']} ({acc['id']})", end="", flush=True)
        rows = pull_account_data(client, acc["id"], acc["name"], query)
        all_rows.extend(rows)
        print(f" → {len(rows):,} campaign-day rows")

    if not all_rows:
        print("\nNo data returned. Check your filters and date range.")
        sys.exit(1)

    # Build DataFrame
    df = pd.DataFrame(all_rows)

    # Aggregate campaign-level rows → account + date level
    # (multiple campaigns per account per day collapse into one row)
    df_daily = (
        df.groupby(["account_id", "account_name", "date", "currency"])
        .agg(
            cost=("cost", "sum"),
            conversions=("conversions", "sum"),
            conversion_value=("conversion_value", "sum"),
            clicks=("clicks", "sum"),
            impressions=("impressions", "sum"),
        )
        .reset_index()
        .sort_values(["account_name", "date"])
    )

    # IS: impression-weighted mean (weighted by impressions per campaign-day)
    for col in ['search_impression_share', 'search_impression_share_lost_budget', 'search_impression_share_lost_rank']:
        if col in df.columns:
            # Compute weighted IS at account+date level
            df_is = (
                df[df[col].notna() & (df['impressions'] > 0)]
                .groupby(['account_id', 'account_name', 'date', 'currency'])
                .apply(lambda g: np.average(g[col], weights=g['impressions']))
                .reset_index(name=col)
            )
            df_daily = df_daily.merge(df_is, on=['account_id', 'account_name', 'date', 'currency'], how='left')

    # CPC: derived from aggregated cost and clicks
    df_daily['cpc'] = np.where(
        df_daily['clicks'] > 0,
        df_daily['cost'] / df_daily['clicks'],
        float('nan')
    )

    # Remove any residual zero-spend rows post-aggregation
    df_daily = df_daily[df_daily["cost"] > 0]

    # Apply conversion lag correction
    df_daily = apply_lag_correction(df_daily, pull_date)

    # Filter to core markets only
    df_daily = df_daily[df_daily["account_name"].isin(CORE_ACCOUNTS)]
    if len(df_daily) == 0:
        print("\nERROR: No data found for core markets after filtering.")
        print(f"Core markets configured: {CORE_ACCOUNTS}")
        print("\nCheck account names in the output above and update CORE_ACCOUNTS list if needed.")
        sys.exit(1)

    # Write CSV (already filtered to core markets)
    df_daily.to_csv(OUTPUT_CSV, index=False)

    # Summary — show both raw and lag-adjusted ROAS for the last 30 days
    print()
    print("── FULL PERIOD SUMMARY ──────────────────────────────────────────")
    summary = (
        df_daily.groupby("account_name")
        .agg(
            days=("date", "nunique"),
            total_spend=("cost", "sum"),
            total_revenue_adj=("conversion_value_adj", "sum"),
        )
        .reset_index()
    )
    for _, r in summary.iterrows():
        roas = r.total_revenue_adj / r.total_spend if r.total_spend > 0 else 0
        print(f"  {r.account_name:<35} {r.days:>3} days  "
              f"spend=€{r.total_spend:>10,.0f}  "
              f"rev_adj=€{r.total_revenue_adj:>10,.0f}  "
              f"ROAS={roas:.2f}")

    # Trailing 30-day summary: lag-adjusted vs raw (shows lag impact)
    recent_start = (pull_date - timedelta(days=29)).strftime("%Y-%m-%d")
    recent_end = pull_date.strftime("%Y-%m-%d")
    recent = df_daily[(df_daily["date"] >= recent_start) & (df_daily["date"] <= recent_end)]
    if len(recent):
        print()
        print(f"── TRAILING 30 DAYS ({recent_start} to {recent_end}, lag-adjusted vs raw) ──")
        rec_sum = (
            recent.groupby("account_name")
            .agg(
                spend=("cost", "sum"),
                rev_raw=("conversion_value", "sum"),
                rev_adj=("conversion_value_adj", "sum"),
            )
            .reset_index()
        )
        for _, r in rec_sum.iterrows():
            roas_raw = r.rev_raw / r.spend if r.spend > 0 else 0
            roas_adj = r.rev_adj / r.spend if r.spend > 0 else 0
            print(f"  {r.account_name:<35}  "
                  f"ROAS raw={roas_raw:.2f}x  adj={roas_adj:.2f}x  "
                  f"(lag uplift: +{roas_adj - roas_raw:.2f}x)")

    print()
    print(f"Total rows : {len(df_daily):,} (filtered to {len(CORE_ACCOUNTS)} core markets)")
    print(f"Saved to   : {Path(OUTPUT_CSV).resolve()}")

    # ── Keyword demand index ──────────────────────────────────
    # Pull top-500 exact match keywords per account from generic campaigns,
    # query Keyword Planner for 12-month historical search volumes, and build
    # a per-account seasonal demand index. Saved alongside core_markets.csv
    # so the optimizer can use it as the highest-priority demand signal.
    # Fails gracefully: if Keyword Planner is unavailable the optimizer falls
    # back to Google Trends → internal ROAS-derived index.
    core_account_map = {
        acc["name"]: acc["id"]
        for acc in accounts
        if acc["name"] in CORE_ACCOUNTS
    }
    first_mcc_id = list(CHILD_MCCS.keys())[0]
    try:
        from budget_solver.keyword_demand import (
            pull_keyword_demand_index,
            OUTPUT_DEMAND_CSV,
            OUTPUT_KEYWORD_CSV,
        )
        demand_df, keyword_df = pull_keyword_demand_index(
            client, core_account_map, mcc_id=first_mcc_id
        )
        if not demand_df.empty:
            demand_df.to_csv(OUTPUT_DEMAND_CSV, index=False)
            keyword_df.to_csv(OUTPUT_KEYWORD_CSV, index=False)
            print(f"\nKeyword demand index saved: {OUTPUT_DEMAND_CSV.resolve()}")
            print(f"Keyword list saved        : {OUTPUT_KEYWORD_CSV.resolve()}")
        else:
            print("\nWARNING: keyword demand index empty — optimizer will use fallback index.")
    except Exception as exc:
        print(f"\nWARNING: keyword demand index failed ({exc})")
        print("         Optimizer will fall back to Google Trends / internal demand index.")

    # ── Auction Insights ─────────────────────────────────────
    # Pull trailing vs prior 30-day competitor impression share per account.
    # Saved to output/auction_insights.csv and loaded by the optimizer at
    # run time to display competitive pressure flags alongside recommendations.
    print("\nPulling auction insights (competitor impression share)...")
    try:
        from budget_solver.auction_insights import (
            pull_all_auction_insights,
            OUTPUT_INSIGHTS_CSV,
        )
        insights_df = pull_all_auction_insights(
            client, core_account_map, root_mcc_id=ROOT_MCC_ID
        )
        if not insights_df.empty:
            insights_df.to_csv(OUTPUT_INSIGHTS_CSV, index=False)
            print(f"Auction insights saved    : {OUTPUT_INSIGHTS_CSV.resolve()}")
        else:
            print("WARNING: auction insights returned no data.")
    except Exception as exc:
        print(f"WARNING: auction insights failed ({exc.__class__.__name__}: {exc})")
        print("         Competitive pressure flags will be unavailable in optimizer output.")

    # ── Bid Simulator ─────────────────────────────────────────
    # Pull campaign-level BUDGET simulation points for all core accounts.
    # Google's simulator predicts (spend → conversion_value) using its internal
    # auction model — forward-looking vs our historically-fitted curves.
    # Used at run time as a cross-check: large divergence (> 20%) flags that
    # market conditions may have shifted or the model is extrapolating.
    print("\nPulling bid simulator data...")
    try:
        from budget_solver.bid_simulator import (
            pull_all_simulator_data,
            OUTPUT_SIMULATOR_CSV,
        )
        sim_df = pull_all_simulator_data(client, core_account_map)
        if not sim_df.empty:
            sim_df.to_csv(OUTPUT_SIMULATOR_CSV, index=False)
            print(f"Bid simulator data saved  : {OUTPUT_SIMULATOR_CSV.resolve()}")
        else:
            print("WARNING: bid simulator returned no data.")
    except Exception as exc:
        print(f"WARNING: bid simulator failed ({exc.__class__.__name__}: {exc})")
        print("         Simulator cross-check will be unavailable in optimizer output.")

    print()
    print("Lag correction applied. 'conversion_value_adj' is the column used by optimizer")
    print("Next step  : budget-solver --budget <amount> --data output/core_markets.csv --scenarios")


if __name__ == "__main__":
    main()


# ═════════════════════════════════════════════════════════════
# CREDENTIALS SETUP
# ═════════════════════════════════════════════════════════════
#
# Create a file called google-ads.yaml in the same folder as this script:
#
#   developer_token: YOUR_DEVELOPER_TOKEN
#   client_id:       YOUR_OAUTH_CLIENT_ID
#   client_secret:   YOUR_OAUTH_CLIENT_SECRET
#   refresh_token:   YOUR_REFRESH_TOKEN
#   login_customer_id: YOUR_MCC_CUSTOMER_ID   # digits only, no dashes
#   use_proto_plus:  True
#
# To get these credentials:
#   1. Developer token — Google Ads UI → Admin → API Center
#   2. OAuth credentials — Google Cloud Console → APIs & Services → Credentials
#      Create an OAuth 2.0 Client ID (Desktop app)
#   3. Refresh token — run the OAuth flow once using google-auth-oauthlib:
#      https://developers.google.com/google-ads/api/docs/oauth/overview
#      Or use the generate_user_credentials.py helper in the google-ads-python examples repo
#
# IMPORTANT: add google-ads.yaml to .gitignore — never commit credentials
