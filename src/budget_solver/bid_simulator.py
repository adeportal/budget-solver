"""
Google Ads Bid Simulator integration.

Pulls campaign-level budget simulation data (type=BUDGET) from the
campaign_simulation resource. Each simulation point represents Google's
own prediction of what happens at a given daily campaign budget:
  cost_micros                → actual predicted spend (may differ from budget if under-delivered)
  biddable_conversions_value → predicted conversion value

This gives us Google's forward-looking response curve, built from the current
auction landscape, competitor bids, and Quality Scores — things our historically-
fitted curves cannot see. Used as a cross-check against the model:

  - Good agreement (< 20% discrepancy): model is well-calibrated, high confidence
  - Large discrepancy (> 20%): market conditions may have shifted, or the model is
    extrapolating; apply extra caution to the recommended allocation

Aggregation: campaigns within an account are simulated independently. To derive
an account-level curve we use proportional scaling — if total account spend
increases by X%, each campaign's budget increases by X%. This matches Scenario B
logic and is a reasonable approximation for Smart Bidding accounts.

Output: output/bid_simulator.csv
Columns: account_name, campaign_name, cost_monthly, conv_value_monthly
"""
from __future__ import annotations

import numpy as np
from pathlib import Path
from typing import Callable

import pandas as pd

OUTPUT_SIMULATOR_CSV = Path("output/bid_simulator.csv")

MICROS_PER_UNIT = 1_000_000
DAYS_PER_MONTH  = 30.44

_DISCREPANCY_WARN = 0.20   # flag when model vs simulator diverges > 20%


def pull_simulator_data(
    client,
    account_id: str,
    account_name: str,
) -> list[dict]:
    """
    Pull BUDGET simulation points for all SEARCH campaigns in the account.
    Returns a list of dicts: account_name, campaign_name, cost_monthly, conv_value_monthly.
    """
    ga_service = client.get_service("GoogleAdsService")

    query = """
        SELECT
            campaign.id,
            campaign.name,
            campaign_simulation.type,
            campaign_simulation.start_date,
            campaign_simulation.end_date,
            campaign_simulation.budget_point_list.points
        FROM campaign_simulation
        WHERE campaign_simulation.type = 'BUDGET'
          AND campaign.advertising_channel_type = 'SEARCH'
          AND campaign.name NOT LIKE '%| BR%'
          AND campaign.name NOT LIKE '%| PK%'
    """

    rows = []
    try:
        response = ga_service.search(customer_id=account_id, query=query)
        for row in response:
            campaign_name = row.campaign.name
            for pt in row.campaign_simulation.budget_point_list.points:
                cost    = pt.cost_micros          / MICROS_PER_UNIT
                revenue = pt.biddable_conversions_value
                if cost <= 0:
                    continue
                rows.append({
                    "account_name":     account_name,
                    "campaign_name":    campaign_name,
                    "cost_monthly":     cost * DAYS_PER_MONTH,
                    "conv_value_monthly": revenue * DAYS_PER_MONTH,
                })
    except Exception as exc:
        print(f"  WARNING: simulator unavailable for {account_name} — {exc.__class__.__name__}")

    return rows


def pull_all_simulator_data(
    client,
    account_map: dict[str, str],
) -> pd.DataFrame:
    """Pull simulator data for all accounts. Returns combined DataFrame."""
    all_rows = []
    for acc_name, acc_id in account_map.items():
        print(f"  Pulling bid simulator: {acc_name} ...", end=" ", flush=True)
        rows = pull_simulator_data(client, acc_id, acc_name)
        print(f"{len(rows)} simulation points across {len({r['campaign_name'] for r in rows})} campaigns")
        all_rows.extend(rows)

    return pd.DataFrame(all_rows) if all_rows else pd.DataFrame(
        columns=["account_name", "campaign_name", "cost_monthly", "conv_value_monthly"]
    )


def load_simulator_data(path: Path = OUTPUT_SIMULATOR_CSV) -> pd.DataFrame:
    """Load saved simulator CSV. Returns empty DataFrame if not found."""
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def build_account_simulator_curve(
    df: pd.DataFrame,
    account_name: str,
    current_alloc: float,
) -> Callable[[float], float] | None:
    """
    Build an account-level simulator response function from campaign-level simulation points.

    Strategy (proportional scaling):
      At total_spend X, each campaign receives X × (campaign_current / account_current) budget.
      We interpolate each campaign's simulated revenue at that budget level, then sum.

    Returns a callable predict_fn(monthly_spend) → monthly_revenue, or None if insufficient data.
    """
    acc_df = df[df["account_name"] == account_name].copy()
    if acc_df.empty or current_alloc <= 0:
        return None

    campaigns = acc_df["campaign_name"].unique()

    # Per-campaign: find "current" spend (point closest to current_alloc/n_campaigns)
    # and build interpolation arrays
    campaign_curves: list[tuple[np.ndarray, np.ndarray, float]] = []
    current_total_sim = 0.0

    for camp in campaigns:
        cdf = acc_df[acc_df["campaign_name"] == camp].sort_values("cost_monthly")
        if len(cdf) < 2:
            continue
        costs    = cdf["cost_monthly"].values
        revenues = cdf["conv_value_monthly"].values

        # "Current" cost for this campaign = midpoint of simulation range
        camp_current = float(costs[len(costs) // 2])
        current_total_sim += camp_current
        campaign_curves.append((costs, revenues, camp_current))

    if not campaign_curves or current_total_sim <= 0:
        return None

    def _simulator_predict(total_monthly_spend: float) -> float:
        scale = total_monthly_spend / current_total_sim
        total_rev = 0.0
        for costs, revenues, camp_current in campaign_curves:
            target_cost = camp_current * scale
            # Clamp to simulation range and interpolate
            target_cost = float(np.clip(target_cost, costs[0], costs[-1]))
            total_rev  += float(np.interp(target_cost, costs, revenues))
        return max(total_rev, 0.0)

    return _simulator_predict


def format_simulator_table(
    df: pd.DataFrame,
    predict_fns: dict[str, Callable],
    current_alloc: dict[str, float],
    recommended_alloc: dict[str, float],
) -> str:
    """
    Format a cross-check table: model prediction vs simulator prediction at recommended spend.
    Returns formatted string for print().
    """
    if df.empty:
        return ""

    lines = []
    lines.append("── SIMULATOR CROSS-CHECK (Google's bid simulator vs fitted model) ───────────")
    lines.append(
        f"  {'Account':<30} {'Rec. spend':>12} {'Model rev':>12} {'Simulator rev':>14} {'Δ':>8}"
    )
    lines.append("  " + "─" * 80)

    any_data = False
    for acc in sorted(predict_fns.keys()):
        sim_fn = build_account_simulator_curve(df, acc, current_alloc.get(acc, 0.0))
        if sim_fn is None:
            continue

        rec_sp  = recommended_alloc.get(acc, current_alloc.get(acc, 0.0))
        model_rev    = predict_fns[acc](rec_sp)
        simulator_rev = sim_fn(rec_sp)

        if model_rev <= 0 or simulator_rev <= 0:
            continue

        delta    = (model_rev - simulator_rev) / simulator_rev
        flag     = ""
        if abs(delta) > _DISCREPANCY_WARN:
            flag = "  ⚠ diverge" if delta > 0 else "  ⚠ model low"

        lines.append(
            f"  {acc:<30} €{rec_sp:>10,.0f}  €{model_rev:>10,.0f}  €{simulator_rev:>12,.0f}  "
            f"{delta:>+6.0%}{flag}"
        )
        any_data = True

    if not any_data:
        return ""

    lines.append("")
    lines.append(
        "  ⚠ diverge = model predicts more than simulator (curve may be optimistic at this spend level)"
    )
    lines.append(
        "  ⚠ model low = model predicts less than simulator (model may be conservative / over-dampened)"
    )
    lines.append("")
    return "\n".join(lines)
