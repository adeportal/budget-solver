"""
Prediction persistence and accuracy feedback loop.

On each run:
  1. load_and_score_history() loads the saved log and computes prediction errors
     for any forecast periods that are now covered by actuals in the input data.
  2. compute_bias_corrections() derives per-account correction multipliers
     from the last 3 runs' signed errors, capped at ±20%.
  3. save_predictions() appends the current run's predictions to the log.

The log is a CSV at output/prediction_log.csv.
Each row represents one account's forecast from one run.
"""
from __future__ import annotations

import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

LOG_PATH = Path("output/prediction_log.csv")

LOG_COLUMNS = [
    "run_date",           # ISO date of the optimizer run (YYYY-MM-DD)
    "forecast_period",    # Month being forecast (YYYY-MM)
    "account_name",
    "recommended_spend",  # Monthly spend recommended by optimizer (€)
    "predicted_revenue",  # Monthly revenue predicted at that spend (€)
    "predicted_roas",     # predicted_revenue / recommended_spend
]


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────

def save_predictions(
    run_date: str,
    forecast_period: str,
    allocations: dict[str, float],
    predict_fns: dict[str, object],
) -> None:
    """
    Append current run's per-account predictions to the log CSV.

    Args:
        run_date:        ISO date string, e.g. "2026-05-01"
        forecast_period: Year-month string, e.g. "2026-06"
        allocations:     {account_name: monthly_spend} — the recommended allocation
        predict_fns:     {account_name: callable(spend) → revenue}
    """
    rows = []
    for acc, spend in allocations.items():
        if acc not in predict_fns:
            continue
        try:
            rev  = float(predict_fns[acc](spend))
            roas = rev / spend if spend > 0 else 0.0
        except Exception:
            rev, roas = 0.0, 0.0
        rows.append({
            "run_date":           run_date,
            "forecast_period":    forecast_period,
            "account_name":       acc,
            "recommended_spend":  round(spend, 2),
            "predicted_revenue":  round(rev, 2),
            "predicted_roas":     round(roas, 4),
        })

    if not rows:
        return

    new_df = pd.DataFrame(rows, columns=LOG_COLUMNS)

    if LOG_PATH.exists():
        try:
            existing = pd.read_csv(LOG_PATH)
            existing.columns = [c.lower().strip() for c in existing.columns]
            # Deduplicate: if same run_date + forecast_period + account exists, overwrite
            key_cols = ["run_date", "forecast_period", "account_name"]
            key_set  = set(map(tuple, new_df[key_cols].values.tolist()))
            existing = existing[~existing[key_cols].apply(tuple, axis=1).isin(key_set)]
            out_df   = pd.concat([existing, new_df], ignore_index=True)
        except Exception:
            out_df = new_df
    else:
        out_df = new_df

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(LOG_PATH, index=False)


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

def load_and_score_history(
    df: pd.DataFrame,
    date_col: str | None = None,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """
    Load the prediction log and score any past forecasts against available actuals.

    A forecast is scoreable if the input DataFrame (df) contains a full calendar
    month matching the forecast_period. For each such month, compute:
      - actual_spend   = sum of cost in that month
      - actual_revenue = sum of conversion_value in that month
      - spend_error_pct  = (actual_spend  - recommended_spend)  / recommended_spend
      - revenue_error_pct = (actual_revenue - predicted_revenue) / predicted_revenue

    Returns:
        (accuracy_df, bias_corrections)
        accuracy_df: DataFrame with all scored rows (suitable for the accuracy Excel tab)
        bias_corrections: dict {account_name: correction_factor}
            correction_factor = 1 / (1 + median_signed_error over last 3 scoreable months)
            Capped to [0.80, 1.20] to prevent overcorrection.
            Only applied if >= 2 scored months exist per account.
    """
    empty_result = (pd.DataFrame(), {})

    if not LOG_PATH.exists():
        return empty_result

    try:
        log = pd.read_csv(LOG_PATH)
        log.columns = [c.lower().strip() for c in log.columns]
    except Exception as exc:
        warnings.warn(f"Could not read prediction log: {exc}")
        return empty_result

    required = {"run_date", "forecast_period", "account_name", "recommended_spend", "predicted_revenue"}
    if not required.issubset(set(log.columns)):
        return empty_result

    log["forecast_period"] = log["forecast_period"].astype(str).str[:7]  # ensure YYYY-MM

    # ── Build month-level actuals from df ──────────────────────────────────
    if date_col is None:
        date_col = next((c for c in ("date", "week_start", "week") if c in df.columns), None)
    if date_col is None:
        return empty_result

    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df["_ym"] = df[date_col].dt.to_period("M").astype(str)

    monthly_actuals = (
        df.groupby(["account_name", "_ym"])
        .agg(actual_spend=("cost", "sum"), actual_revenue=("conversion_value", "sum"))
        .reset_index()
        .rename(columns={"_ym": "forecast_period"})
    )

    # Only keep months that are fully represented (not the current month)
    latest_month = df["_ym"].max()
    monthly_actuals = monthly_actuals[monthly_actuals["forecast_period"] < latest_month]

    # ── Merge predictions with actuals ─────────────────────────────────────
    scored = log.merge(monthly_actuals, on=["account_name", "forecast_period"], how="inner")
    if scored.empty:
        return empty_result

    scored["recommended_spend"]  = pd.to_numeric(scored["recommended_spend"],  errors="coerce")
    scored["predicted_revenue"]  = pd.to_numeric(scored["predicted_revenue"],  errors="coerce")

    scored["spend_error_pct"] = np.where(
        scored["recommended_spend"] > 0,
        (scored["actual_spend"] - scored["recommended_spend"]) / scored["recommended_spend"],
        np.nan,
    )
    scored["revenue_error_pct"] = np.where(
        scored["predicted_revenue"] > 0,
        (scored["actual_revenue"] - scored["predicted_revenue"]) / scored["predicted_revenue"],
        np.nan,
    )
    scored["revenue_abs_error_pct"] = scored["revenue_error_pct"].abs()
    scored["actual_roas"] = np.where(
        scored["actual_spend"] > 0,
        scored["actual_revenue"] / scored["actual_spend"],
        np.nan,
    )

    # ── Compute bias corrections ───────────────────────────────────────────
    bias_corrections: dict[str, float] = {}
    for acc, grp in scored.groupby("account_name"):
        grp_sorted = grp.sort_values("forecast_period")
        recent = grp_sorted.tail(3)  # last 3 scored months
        errors = recent["revenue_error_pct"].dropna()
        if len(errors) < 2:
            continue  # Not enough history to correct reliably
        median_err = float(errors.median())
        # Correction factor: if model over-predicts by 15% (error = -0.15), factor = 1/0.85 = 1.176
        raw_correction = 1.0 / (1.0 + median_err) if (1.0 + median_err) > 0 else 1.0
        # Cap to [0.80, 1.20]
        clamped = float(np.clip(raw_correction, 0.80, 1.20))
        if abs(clamped - 1.0) > 0.02:  # Only apply if correction is meaningful (>2%)
            bias_corrections[acc] = clamped

    return scored, bias_corrections


def compute_portfolio_accuracy_summary(scored: pd.DataFrame) -> dict:
    """
    Compute portfolio-level accuracy metrics from the scored accuracy DataFrame.
    Returns a dict with: mape, bias, wape, n_months, n_accounts.
    """
    if scored.empty:
        return {}

    errors = scored["revenue_error_pct"].dropna()
    return {
        "mape":       float(errors.abs().mean()) if len(errors) else float("nan"),
        "bias":       float(errors.mean())       if len(errors) else float("nan"),
        "wape":       float(
            scored["revenue_abs_error_pct"].dropna().mean()
        ) if len(scored) else float("nan"),
        "n_months":   int(scored["forecast_period"].nunique()),
        "n_accounts": int(scored["account_name"].nunique()),
    }
