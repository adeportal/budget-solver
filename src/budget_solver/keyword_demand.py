"""
Keyword-based demand index builder.

Pulls the top-N exact match keywords from each account's generic campaigns,
queries Google Keyword Planner for 12 months of historical monthly search
volumes, and builds a per-account ISO-week demand index (mean=1.0).

Why account-specific keywords over generic market terms:
  Generic terms like "vakantiepark" capture the whole Dutch vacation market.
  Each account's top exact-match keywords are the specific queries it actually
  competes for — different seasonal profiles, different total addressable volume.
  Landal DE (summer-holiday peak) and Landal NL (Easter peak) get separate
  demand curves automatically.

Side outputs:
  output/keyword_demand_index.csv — per-account monthly demand multipliers
  output/keyword_list.csv         — the top-N keywords used per account (audit trail)

Fallback chain (in data_pull):
  keyword demand index → Google Trends → internal ROAS-derived index
"""

import time
from calendar import monthrange
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

MICROS       = 1_000_000
TOP_N        = 500
LOOKBACK_DAYS = 365   # pull keyword performance over last 12 months for ranking
BATCH_SIZE   = 1000   # max keywords per Keyword Planner API call (safe limit)

# Google Ads API MonthOfYear enum names → calendar month int (1-12)
_MONTH_NAMES: dict[str, int] = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH":  3, "APRIL":  4,
    "MAY":     5, "JUNE":     6, "JULY":   7, "AUGUST": 8,
    "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
}

# Geo + language targeting per account
# Geo IDs: NL=2528, DE=2276, BE=2056
# Language IDs: Dutch=1010, German=1001
ACCOUNT_GEO_LANG: dict[str, dict] = {
    "Landal NL":  {"geo_id": 2528, "lang_id": 1010},
    "Roompot NL": {"geo_id": 2528, "lang_id": 1010},
    "Landal DE":  {"geo_id": 2276, "lang_id": 1001},
    "Roompot DE": {"geo_id": 2276, "lang_id": 1001},
    "Landal BE":  {"geo_id": 2056, "lang_id": 1010},  # Flanders-first
    "Roompot BE": {"geo_id": 2056, "lang_id": 1010},
}

OUTPUT_DEMAND_CSV  = Path("output/keyword_demand_index.csv")
OUTPUT_KEYWORD_CSV = Path("output/keyword_list.csv")


# ─────────────────────────────────────────────────────────────
# STEP 1 — Pull top keywords per account
# ─────────────────────────────────────────────────────────────

def pull_top_keywords(
    client,
    account_id: str,
    account_name: str,
    lookback_days: int = LOOKBACK_DAYS,
    top_n: int = TOP_N,
) -> list[dict]:
    """
    Query top-N exact match keywords from generic campaigns for one account.

    Ranking: conversion_value DESC (primary), cost DESC (tiebreaker).
    Deduplicates keywords that appear in multiple ad groups.
    Excludes | BR and | PK campaigns (brand/park — not generic demand).
    Excludes REMOVED criteria and campaigns.

    Returns list of dicts sorted by conversion_value desc:
        {text, impressions, cost, conversions, conversion_value}
    """
    try:
        from google.ads.googleads.errors import GoogleAdsException
    except ImportError:
        return []

    ga_service = client.get_service("GoogleAdsService")
    end_date   = datetime.today().strftime("%Y-%m-%d")
    start_date = (datetime.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    # keyword_view supports performance metrics; ad_group_criterion does not.
    query = f"""
        SELECT
            ad_group_criterion.keyword.text,
            metrics.impressions,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value
        FROM keyword_view
        WHERE ad_group_criterion.type = 'KEYWORD'
          AND ad_group_criterion.keyword.match_type = 'EXACT'
          AND ad_group_criterion.status != 'REMOVED'
          AND campaign.advertising_channel_type = 'SEARCH'
          AND campaign.name NOT LIKE '%| BR%'
          AND campaign.name NOT LIKE '%| PK%'
          AND campaign.status != 'REMOVED'
          AND ad_group.status != 'REMOVED'
          AND segments.date BETWEEN '{start_date}' AND '{end_date}'
    """

    # Aggregate metrics across ad groups: same keyword text can appear in many ad groups
    agg: dict[str, dict] = {}
    try:
        for row in ga_service.search(customer_id=account_id, query=query):
            text = row.ad_group_criterion.keyword.text.lower().strip()
            if text not in agg:
                agg[text] = {"impressions": 0, "cost": 0.0,
                             "conversions": 0.0, "conversion_value": 0.0}
            m = agg[text]
            m["impressions"]      += row.metrics.impressions
            m["cost"]             += row.metrics.cost_micros / MICROS
            m["conversions"]      += row.metrics.conversions
            m["conversion_value"] += row.metrics.conversions_value
    except GoogleAdsException as ex:
        print(f"    WARNING: keyword pull failed for {account_name} — {ex.error.code().name}")
        return []

    ranked = sorted(
        agg.items(),
        key=lambda x: (x[1]["conversion_value"], x[1]["cost"]),
        reverse=True,
    )[:top_n]

    return [{"text": kw, **metrics} for kw, metrics in ranked]


# ─────────────────────────────────────────────────────────────
# STEP 2 — Keyword Planner historical metrics
# ─────────────────────────────────────────────────────────────

def _fetch_historical_metrics(
    client,
    mcc_id: str,
    keywords: list[str],
    geo_id: int,
    lang_id: int,
) -> dict[str, list[tuple[int, int, int]]]:
    """
    Call GenerateKeywordHistoricalMetrics for a keyword list.

    Batches into BATCH_SIZE chunks with a brief pause between batches to
    stay within rate limits.

    Returns dict: {keyword_text → [(year, month_int, monthly_searches), ...]}
    Months with <10 searches return 0 (Google's reporting threshold).
    """
    try:
        from google.ads.googleads.errors import GoogleAdsException
    except ImportError:
        return {}

    if not keywords:
        return {}

    kp_service = client.get_service("KeywordPlanIdeaService")
    results: dict[str, list] = {}

    for i in range(0, len(keywords), BATCH_SIZE):
        batch = keywords[i: i + BATCH_SIZE]
        try:
            request = client.get_type("GenerateKeywordHistoricalMetricsRequest")
            request.customer_id = mcc_id
            for kw in batch:
                request.keywords.append(kw)
            request.geo_target_constants.append(f"geoTargetConstants/{geo_id}")
            request.language = f"languageConstants/{lang_id}"

            response = kp_service.generate_keyword_historical_metrics(request=request)

            for result in response.results:
                text   = result.text.lower().strip()
                vols   = []
                for mv in result.keyword_metrics.monthly_search_volumes:
                    month_raw = mv.month
                    name      = month_raw.name if hasattr(month_raw, "name") else str(month_raw)
                    month_int = _MONTH_NAMES.get(name, 0)
                    if month_int == 0:
                        continue
                    searches = int(mv.monthly_searches) if mv.monthly_searches else 0
                    vols.append((int(mv.year), month_int, searches))
                if vols:
                    results[text] = vols

        except GoogleAdsException as ex:
            print(f"\n    WARNING: Keyword Planner error — {ex.error.code().name}")
            for err in ex.failure.errors:
                print(f"      {err.message}")

        if i + BATCH_SIZE < len(keywords):
            time.sleep(1)

    return results


# ─────────────────────────────────────────────────────────────
# STEP 3 — Orchestrate pull and build raw monthly volumes
# ─────────────────────────────────────────────────────────────

def pull_keyword_demand_index(
    client,
    account_map: dict[str, str],
    mcc_id: str,
    top_n: int = TOP_N,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Build per-account keyword demand index from Keyword Planner search volumes.

    Steps:
      1. Pull top-N exact match keywords per account (ranked by conversion value)
      2. Group accounts by geo+language (NL/DE/BE) — batch Keyword Planner calls,
         deduplicating keywords shared across accounts in the same market
      3. Fetch 12-month historical search volumes per keyword
      4. Sum volumes across keywords per account per month
      5. Compute demand multipliers (monthly volume / annual average)
      6. Print market size summary (total addressable search volume per account)

    Args:
        client:      GoogleAdsClient instance
        account_map: {account_name: account_id} for core accounts
        mcc_id:      MCC customer ID to use for Keyword Planner calls
        top_n:       Number of top keywords per account

    Returns:
        demand_df:  account_name, year_month, total_volume, demand_multiplier
        keyword_df: account_name, keyword, impressions, cost, conversions, conversion_value
    """
    print("\n── KEYWORD DEMAND INDEX ─────────────────────────────────────────")

    # Step 1: Pull top keywords per account
    account_keywords: dict[str, list[dict]] = {}
    for acc_name, acc_id in account_map.items():
        if acc_name not in ACCOUNT_GEO_LANG:
            continue
        print(f"  Pulling keywords: {acc_name}", end="", flush=True)
        kws = pull_top_keywords(client, acc_id, acc_name, top_n=top_n)
        account_keywords[acc_name] = kws
        print(f" → {len(kws)} exact match keywords")

    if not account_keywords:
        print("  No keyword data retrieved — skipping demand index.")
        return pd.DataFrame(), pd.DataFrame()

    # Step 2: Group by (geo_id, lang_id) to batch Keyword Planner calls
    geo_groups: dict[tuple, list[str]] = {}
    for acc_name in account_keywords:
        cfg = ACCOUNT_GEO_LANG[acc_name]
        key = (cfg["geo_id"], cfg["lang_id"])
        geo_groups.setdefault(key, []).append(acc_name)

    # Step 3: Fetch historical metrics per geo group (deduplicated keywords)
    # acc_vol_data: {account_name: {keyword_text: [(year, month, volume)]}}
    acc_vol_data: dict[str, dict] = {}

    for (geo_id, lang_id), acc_names in geo_groups.items():
        country_label = {2528: "NL", 2276: "DE", 2056: "BE"}.get(geo_id, str(geo_id))

        # Deduplicate keywords across accounts sharing this geo
        geo_keywords: dict[str, set] = {}  # keyword → set of accounts it belongs to
        for acc_name in acc_names:
            for kw in account_keywords.get(acc_name, []):
                geo_keywords.setdefault(kw["text"], set()).add(acc_name)

        unique_kws = sorted(geo_keywords.keys())
        print(
            f"  Keyword Planner [{country_label}]: "
            f"{len(unique_kws)} unique keywords across {acc_names}",
            end="", flush=True
        )

        metrics_map = _fetch_historical_metrics(client, mcc_id, unique_kws, geo_id, lang_id)
        print(f" → {len(metrics_map)} returned with volume data")

        # Map results back to each account (keyword may belong to multiple accounts)
        for acc_name in acc_names:
            acc_kw_texts = {kw["text"] for kw in account_keywords.get(acc_name, [])}
            acc_vol_data[acc_name] = {
                kw: vols
                for kw, vols in metrics_map.items()
                if kw in acc_kw_texts
            }

    # Step 4 & 5: Aggregate to monthly demand multipliers per account
    demand_rows: list[dict] = []
    keyword_rows: list[dict] = []

    for acc_name, vol_data in acc_vol_data.items():
        # Build keyword audit list
        for kw_dict in account_keywords.get(acc_name, []):
            keyword_rows.append({
                "account_name":     acc_name,
                "keyword":          kw_dict["text"],
                "impressions":      int(kw_dict["impressions"]),
                "cost":             round(kw_dict["cost"], 2),
                "conversions":      round(kw_dict["conversions"], 2),
                "conversion_value": round(kw_dict["conversion_value"], 2),
                "has_volume_data":  kw_dict["text"] in vol_data,
            })

        # Sum monthly search volumes across all keywords for this account
        monthly_totals: dict[str, int] = {}  # "YYYY-MM" → total searches
        for kw_text, volumes in vol_data.items():
            for (year, month, searches) in volumes:
                key = f"{year}-{month:02d}"
                monthly_totals[key] = monthly_totals.get(key, 0) + searches

        if not monthly_totals:
            print(f"  WARNING: no volume data for {acc_name} — will use portfolio fallback")
            continue

        volumes_arr = list(monthly_totals.values())
        avg_vol     = float(np.mean(volumes_arr)) if volumes_arr else 1.0
        if avg_vol <= 0:
            continue

        for ym, vol in sorted(monthly_totals.items()):
            demand_rows.append({
                "account_name":      acc_name,
                "year_month":        ym,
                "total_volume":      int(vol),
                "demand_multiplier": round(vol / avg_vol, 4),
            })

        # Market size summary: total addressable search volume
        total_monthly_avg = avg_vol
        yoy = _compute_yoy_momentum(monthly_totals)
        momentum_str = f"  YoY {yoy:+.0%}" if yoy is not None else ""
        print(
            f"  {acc_name:<30}  "
            f"addressable: ~{total_monthly_avg:>8,.0f} searches/month"
            f"{momentum_str}"
        )

    demand_df  = pd.DataFrame(demand_rows)
    keyword_df = pd.DataFrame(keyword_rows)
    return demand_df, keyword_df


# ─────────────────────────────────────────────────────────────
# STEP 4 — Convert monthly data → ISO week demand indices
# ─────────────────────────────────────────────────────────────

def build_weekly_demand_indices(
    demand_df: pd.DataFrame,
) -> tuple[dict[str, dict[int, float]], dict[int, float]]:
    """
    Convert per-account monthly search volumes into ISO week demand indices.

    Algorithm:
      - For each (account, year, month, volume), distribute volume evenly
        across all calendar days in that month.
      - Accumulate daily volumes by ISO week number.
      - Average across years (2024 + 2025 → typical week N pattern).
      - Normalise each account's index to mean=1.0.
      - Portfolio index = volume-weighted average across all accounts.

    Args:
        demand_df: output of pull_keyword_demand_index (account_name, year_month,
                   total_volume, demand_multiplier columns)

    Returns:
        per_account: {account_name: {iso_week_1_to_53: multiplier}}
        portfolio:   {iso_week_1_to_53: multiplier}  (volume-weighted average)
    """
    if demand_df.empty:
        return {}, {}

    per_account: dict[str, dict[int, float]] = {}
    account_avg_volumes: dict[str, float]    = {}

    for acc_name, grp in demand_df.groupby("account_name"):
        # iso_week_acc: {iso_week: [daily_volume_contributions]}
        iso_week_acc: dict[int, list[float]] = {w: [] for w in range(1, 54)}

        for _, row in grp.iterrows():
            try:
                year  = int(row["year_month"].split("-")[0])
                month = int(row["year_month"].split("-")[1])
                vol   = float(row["total_volume"])
            except (ValueError, AttributeError):
                continue

            days_in_month = monthrange(year, month)[1]
            daily_vol     = vol / days_in_month

            for day in range(1, days_in_month + 1):
                iso_week = date(year, month, day).isocalendar()[1]
                iso_week = min(iso_week, 53)
                iso_week_acc[iso_week].append(daily_vol)

        # Average accumulated daily volumes per ISO week
        avg_by_week: dict[int, float] = {}
        for w in range(1, 54):
            vals = iso_week_acc[w]
            if vals:
                avg_by_week[w] = float(np.mean(vals))

        if not avg_by_week:
            continue

        # Fill any missing ISO weeks via linear interpolation
        filled = _fill_iso_weeks(avg_by_week)

        mean_vol = float(np.mean(list(filled.values())))
        if mean_vol <= 0:
            continue

        per_account[acc_name]       = {w: v / mean_vol for w, v in filled.items()}
        account_avg_volumes[acc_name] = mean_vol

    if not per_account:
        return {}, {}

    # Portfolio index: volume-weighted mean across accounts
    total_vol = sum(account_avg_volumes.values())
    portfolio: dict[int, float] = {}
    for w in range(1, 54):
        weighted = sum(
            per_account[acc].get(w, 1.0) * account_avg_volumes.get(acc, 0.0)
            for acc in per_account
        )
        portfolio[w] = weighted / total_vol if total_vol > 0 else 1.0

    # Re-normalise portfolio to mean=1.0
    port_mean = float(np.mean(list(portfolio.values())))
    if port_mean > 0:
        portfolio = {w: v / port_mean for w, v in portfolio.items()}

    return per_account, portfolio


# ─────────────────────────────────────────────────────────────
# LOAD FROM SAVED CSV
# ─────────────────────────────────────────────────────────────

def load_keyword_demand_index(
    path: Path = OUTPUT_DEMAND_CSV,
) -> tuple[dict[str, dict[int, float]], dict[int, float]]:
    """
    Load saved keyword demand index CSV and return (per_account, portfolio) dicts.

    Returns ({}, {}) if the file does not exist or is malformed.
    """
    if not path.exists():
        return {}, {}
    try:
        df = pd.read_csv(path)
        return build_weekly_demand_indices(df)
    except Exception as ex:
        print(f"  WARNING: could not load keyword demand index from {path}: {ex}")
        return {}, {}


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _fill_iso_weeks(avg_by_week: dict[int, float]) -> dict[int, float]:
    """
    Fill missing ISO weeks (1-53) by linear interpolation between known values.
    Unknown edge weeks fall back to the nearest known value.
    """
    filled = {}
    known_weeks = sorted(avg_by_week.keys())
    if not known_weeks:
        return {w: 1.0 for w in range(1, 54)}

    for w in range(1, 54):
        if w in avg_by_week:
            filled[w] = avg_by_week[w]
        else:
            # Find nearest lower and upper known weeks
            lowers = [k for k in known_weeks if k < w]
            uppers = [k for k in known_weeks if k > w]
            if lowers and uppers:
                lo, hi  = max(lowers), min(uppers)
                t       = (w - lo) / (hi - lo)
                filled[w] = avg_by_week[lo] + t * (avg_by_week[hi] - avg_by_week[lo])
            elif lowers:
                filled[w] = avg_by_week[max(lowers)]
            else:
                filled[w] = avg_by_week[min(uppers)]
    return filled


def _compute_yoy_momentum(monthly_totals: dict[str, int]) -> Optional[float]:
    """
    Compute year-over-year growth rate for the most recent 3 months vs same
    period one year prior. Returns None if insufficient data.
    """
    sorted_months = sorted(monthly_totals.keys())
    if len(sorted_months) < 12:
        return None

    recent_3  = sorted_months[-3:]
    prior_3   = []
    for ym in recent_3:
        year, month = int(ym.split("-")[0]), int(ym.split("-")[1])
        prior_ym = f"{year - 1}-{month:02d}"
        if prior_ym in monthly_totals:
            prior_3.append(prior_ym)

    if len(prior_3) < 2:
        return None

    recent_vol = sum(monthly_totals[ym] for ym in recent_3 if ym in monthly_totals)
    prior_vol  = sum(monthly_totals[ym] for ym in prior_3)

    if prior_vol <= 0:
        return None
    return recent_vol / prior_vol - 1.0
