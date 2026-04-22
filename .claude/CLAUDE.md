# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Budget Solver is a Google Ads budget optimization tool for Landal and Roompot vacation rental brands. It pulls historical performance data from Google Ads API, fits diminishing-returns response curves to each account, and uses constrained nonlinear optimization (SLSQP) to allocate a total monthly budget across accounts to maximize projected revenue.

**Active markets:** Landal BE/NL/DE, Roompot BE/NL/DE (6 accounts across 2 MCCs)

## Core Commands

### Data Pull (Step 1)
```bash
budget-solver-pull
```
Pulls 24 months of daily account-level data from Google Ads API. Requires `~/.config/landal/google-ads.yaml` with OAuth credentials. Outputs `core_markets.csv` with lag-adjusted conversion values. Runtime: 2-5 minutes.

**Post-pull filtering:** Open `core_markets.csv`, manually filter to the 6 core markets, save as `core_markets.csv`.

### Budget Optimization (Step 2)
```bash
# Basic run
python optimizer.py --budget 1710000 --data output/core_markets.csv

# With spend constraints
python optimizer.py --budget 1710000 --data output/core_markets.csv \
  --min "Landal BE:500,Roompot NL:1000" --max "Landal DE:20000"

# Optimize conversions instead of revenue
python optimizer.py --budget 1710000 --data output/core_markets.csv --target conversions

# Adjust training window
python optimizer.py --budget 1710000 --data output/core_markets.csv --training-months 12

# Diagnostic modes
python optimizer.py --budget 1710000 --data output/core_markets.csv --no-calibrate
python optimizer.py --budget 1710000 --data output/core_markets.csv --no-outlier-removal
```

Outputs timestamped Excel file: `budget_solver_YYYYMMDD_HHMM.xlsx` with 8 sheets (Results, Sensitivity, Curves, Raw Data, Model Parameters, Outlier Log, Demand Index, ROAS-Based Allocation).

## Architecture

### Data Pipeline (mcc_data_pull.py)

1. **Multi-MCC account discovery**: Queries child accounts from Landal MCC (8265762094) and Roompot MCC (6917028372)
2. **Campaign-level pull**: Retrieves 24 months of daily metrics (cost, conversions, conversion_value) for SEARCH campaigns, excluding "| BR" and "| PK" campaigns
3. **Conversion lag correction**: Applies daily multipliers to recent conversion data (last 30 days) to correct for attribution lag. Days 0-29 are uplifted based on a 30-day cumulative arrival curve (day 0 = 38.3% settled → 2.61x multiplier, day 29 = 100% settled → 1.0x). Output columns: `conversion_value_adj`, `conversions_adj`, `lag_factor`
4. **Aggregation**: Rolls up campaign-level rows to account+date grain

**Configuration constants:**
- `CHILD_MCCS`: dict of MCC IDs to query (optimizer.py:122-125)
- `YAML_PATH`: path to Google Ads credentials (optimizer.py:127)
- `_DAILY_INCREMENTS_PCT`: 30-element list defining conversion arrival profile (optimizer.py:57-74)

### Optimization Engine (optimizer.py)

#### 1. Data Loading & Preprocessing (optimizer.py:301-367)
- `load_data()`: Reads CSV/Excel or Google Sheets export URL, normalizes column names, substitutes `conversion_value_adj` as primary revenue metric if present
- `aggregate_weekly()`: Groups daily data into weekly buckets per account (curves are fitted on weekly aggregates to smooth noise)

#### 2. Demand Seasonality (optimizer.py:370-446)
- `build_demand_index()`: Computes ISO week 1-53 demand multipliers (mean=1.0) from median weekly ROAS across accounts. High-demand weeks (e.g., Easter week ≈ 1.4x) vs low-demand (e.g., November ≈ 0.7x)
- `apply_demand_normalization()`: Divides weekly revenue by demand index before curve fitting (optional via `--normalize-demand` flag). Prevents conflating "high ROAS due to seasonality" with "high ROAS due to efficient spend"
- **Forecast period resolution** (optimizer.py:1240-1279): Determines which ISO week's demand multiplier to scale predictions by. Priority: explicit `--forecast-week` > `--forecast-month` converted to midpoint week > inferred next month after latest input date > current month fallback

#### 3. Outlier Removal (optimizer.py:448-511)
Two-pass filter per account (unless `--no-outlier-removal`):
- **Pass 1**: Drop weeks with spend < 20% of median (low-spend periods where organic/remarketing dominates)
- **Pass 2**: Drop weeks with ROAS outside [Q1 - 2×IQR, Q3 + 2×IQR] (tracking outages, attribution errors)
Removal log is written to Excel "Outlier Log" sheet for review.

#### 4. Response Curve Fitting (optimizer.py:90-161)
`fit_response_curve(spend_arr, revenue_arr)` tries in order:
- **Primary**: Log curve `revenue = a × ln(spend) + b` (requires positive slope)
- **Fallback**: Power curve `revenue = a × spend^b` (b constrained 0.01-0.99)
- **Last resort**: Linear proxy `revenue = avg_roas × spend` (if n < 3)

**Why log curves?** Small saturating markets (Landal BE/NL/DE) exhibit fast diminishing returns; log derivative `a/x` matches empirical R² better than power curves over 6-month windows.

**Safe predictor wrapping** (optimizer.py:57-87): Below observed spend range, curves interpolate linearly from (0,0) to anchor point to prevent negative extrapolations. Above observed range, predictions are floored at zero.

**Training window** (optimizer.py:1350-1366): Curves are fitted on recent N months only (default: 6 via `--training-months`) to reflect current market conditions. Full history is used for spend cap derivation but NOT for fitting. Rationale: prevents over-predicting when historical high-ROAS periods (e.g., January 2025 with 19-32× ROAS) inflate model expectations.

#### 5. Curve Calibration (optimizer.py:1504-1523)
Unless `--no-calibrate`, each curve is anchored to actual lag-adjusted ROAS from the last 30 days of input data. Calibration factor `scale = actual_roas / model_roas_at_current_spend` is applied so the curve predicts exactly what was observed at current spend, preserving the fitted curve's shape (diminishing returns slope) while correcting absolute level.

**Monthly scaling correction** (optimizer.py:1458-1466): Curves are fitted on WEEKLY spend/revenue aggregates, but `current_alloc` and `--budget` are MONTHLY totals. Direct substitution would ask "what revenue does one week generate at €X monthly spend?" (wrong scale). Correct formula: `monthly_revenue = 4.33 × weekly_fn(monthly_spend / 4.33)` where 4.33 ≈ weeks per month.

#### 6. Constrained Optimization (optimizer.py:222-268)
`optimize_budget()` uses `scipy.optimize.minimize` with SLSQP method:
- **Objective**: Maximize Σ predict_fn_i(spend_i) (equivalently, minimize the negation)
- **Constraint**: Σ spend_i = total_budget (equality)
- **Bounds**: Per-account min/max spend (from `--min`/`--max` flags + auto-derived caps)

**Auto spend caps** (optimizer.py:1391-1405): Each account's max defaults to `2 × (highest observed weekly spend × 4.33)`, or 2% of total budget (whichever is higher). Prevents optimizer from over-allocating to accounts beyond their historical scale. User-provided `--max` overrides auto caps.

**Feasibility checks** (optimizer.py:167-199): Pre-optimization validation ensures sum(minimums) ≤ budget ≤ sum(maximums), and per-account bounds are non-empty.

#### 7. Excel Report Generation (optimizer.py:552-1221)
`build_excel()` produces 8-sheet workbook:
- **Sheet 1: Optimization Results**: Main KPI table showing current vs recommended spend, actual vs projected revenue, marginal ROI, model R². Includes "WHY ACCOUNTS GAIN OR LOSE BUDGET" explanation block with marginal ROI comparison.
- **Sheet 2: Sensitivity Analysis**: Budget ±30% in 10% steps, shows predicted revenue and allocation at each level. Includes revenue vs budget line chart.
- **Sheet 3: Response Curves**: Per-account curve tables with 20 interpolated points from 5% to 200% of current/optimal spend. Dual-axis charts: revenue (blue solid line) + ROAS (orange dashed) on secondary y-axis.
- **Sheet 4: Raw Data**: Full input CSV dump for traceability
- **Sheet 5: Model Parameters**: Fitted curve equations, R², data points per account
- **Sheet 6: Outlier Log**: Weeks excluded before fitting, with spend/revenue/ROAS and reason
- **Sheet 7: Demand Index**: 53-week seasonal multiplier table + bar chart, forecast week highlighted
- **Sheet 8: ROAS-Based Allocation**: Alternative naive allocation (budget split proportional to lag-adjusted ROAS from trailing window). Includes caveat warning about non-scalable high ROAS in small markets.

**Color scheme** (optimizer.py:518-524): Dark navy header (`#1F3864`), accent blue (`#2E75B6`), green for positive deltas (`#70AD47`), red for negative (`#FF4444`), light blue alternating rows (`#EBF3FB`).

#### 8. CLI Entry Point (optimizer.py:1282-1613)
`main()` orchestrates: load data → aggregate weekly → build demand index → remove outliers → fit curves → calibrate → optimize → sensitivity analysis → build Excel. Prints per-account performance table, optimization results, and forecast summary to console.

**Key flags:**
- `--training-months 6` (default): Fit curves on recent N months only
- `--no-calibrate`: Skip anchoring curves to actual ROAS (for diagnostics)
- `--forecast-month YYYY-MM`: Specify forecast period (converted to midpoint ISO week)
- `--forecast-week 1-53`: Explicit ISO week for demand scaling
- `--normalize-demand`: Apply demand index normalization before fitting (experimental)
- `--demand-index-csv path.csv`: Override derived demand index with external data (SimilarWeb, internal bookings)

## Key Concepts

### Conversion Lag Correction
Conversions are attributed back to click date, so recent days (0-29 days ago) have incomplete data. Rather than excluding the trailing 30 days, the data pull applies a daily multiplier derived from observed conversion arrival profile. Example: day 0 (today) = 38.3% settled → multiply by 2.61x; day 14 = 78.8% settled → multiply by 1.27x; day 30+ = 100% settled → multiply by 1.0x. This uplifts `conversion_value` and `conversions` to estimated fully-settled totals (`conversion_value_adj`, `conversions_adj`). The optimizer uses `conversion_value_adj` as the primary revenue metric throughout.

### Response Curve Philosophy
The log curve `revenue = a × ln(spend) + b` is used because its derivative (marginal ROAS = `a / spend`) declines hyperbolically as spend increases, matching empirical behavior in small, saturating markets. This is more realistic than power curves for accounts where doubling spend does NOT double revenue. The curve is calibrated to actual trailing 30-day ROAS so it anchors predictions to current reality, while preserving the fitted shape (slope of diminishing returns) derived from historical spend variation.

### Demand Index (Seasonality)
Revenue is a function of both spend efficiency AND external demand (Easter booking spikes, summer high season, November low season). Without separating these, response curves conflate "high ROAS because we spent at peak season" with "high ROAS because spend is efficient at low volumes." The demand index (ISO week → multiplier, mean=1.0) is built from median weekly ROAS across accounts and both years. High-demand weeks (Easter ≈ 1.4x) vs low-demand (November ≈ 0.7x). When `--normalize-demand` is enabled, weekly revenue is divided by demand index before curve fitting, then predictions are scaled back up by the forecast period's demand multiplier. This isolates spend efficiency from seasonality.

### Training Window vs Full History
Curves are fitted on recent N months (default 6) to reflect current market conditions, NOT the full 24-month input. Full history is used for auto spend cap derivation (highest observed weekly spend × 4.33 → monthly max). Rationale: including old high-ROAS periods (e.g., January 2025 with 19-32× ROAS for Landal BE at moderate spend) inflates predictions by ~45%. The training window approach prevents this over-prediction while still allowing long lookback for data stability.

### Actual vs Projected ROAS
- **Actual ROAS**: Lag-adjusted revenue from last 30 days of input data ÷ spend in that window. This is the observed baseline performance.
- **Projected ROAS**: Curve-predicted revenue at recommended spend for forecast period ÷ recommended spend. This is the model's expectation after reallocation.
The optimization maximizes total projected revenue subject to budget constraint; it does NOT directly optimize ROAS (a ratio). Marginal ROI (derivative of revenue curve at optimal spend) is equalized across accounts at optimum.

### Marginal ROI (mROAS)
The first derivative of the response curve at a given spend level: `dRevenue/dSpend`. Represents the incremental revenue generated by the next euro of spend. The optimizer allocates budget until marginal ROI is equalized across all accounts (within constraints). Values below ~1.5x suggest over-investment (you're paying €1 to generate €1.50 in incremental revenue). Central difference is used for numerical differentiation when spend >= eps; forward difference from zero is used for very low spend to avoid asymmetric clamping bugs.

## Important Caveats

### Easter Timing Shifts
If Easter falls in different weeks year-over-year, April projections may be conservative or over-optimistic depending on whether Easter is in the forecast month. Flag this to stakeholders when presenting results. The demand index captures historical Easter spikes, but if this year's Easter is week 15 and last year's was week 17, the index may not align perfectly.

### Small Market Scalability
High ROAS in small markets (e.g., Landal BE with 10x+ ROAS at current spend) does NOT mean you can 10x the budget and maintain that ROAS. Diminishing returns kick in quickly. The response curves and spend caps account for this, but the naive "ROAS-Based Allocation" sheet (Sheet 8) does not—hence the caveat warning. Always compare Sheet 1 (curve-based) vs Sheet 8 (ROAS-proportional) to see the difference.

### Curve Fit Quality (R²)
Log curves for small markets typically achieve R² = 0.6-0.8 on 6-month windows. This is acceptable for noisy marketing data. R² < 0.5 suggests weak fit—check the Outlier Log and consider adjusting `--training-months` or removing anomalous accounts. R² near 1.0 may indicate overfitting or insufficient spend variation in the training window.

### Calibration Side Effects
Calibration anchors curves to actual trailing 30-day ROAS. If the trailing window is unrepresentative (e.g., includes a promo spike or tracking outage), calibration will anchor to that anomaly and propagate it into forecasts. Use `--no-calibrate` to diagnose whether the raw fitted curve is sensible before applying calibration. If raw predictions seem reasonable but calibrated predictions are off, investigate the trailing window data quality.

## Credentials Setup

The data pull requires Google Ads API OAuth credentials. Create `~/.config/landal/google-ads.yaml`:

```yaml
developer_token: YOUR_DEVELOPER_TOKEN
client_id: YOUR_CLIENT_ID.apps.googleusercontent.com
client_secret: YOUR_CLIENT_SECRET
refresh_token: YOUR_REFRESH_TOKEN
login_customer_id: YOUR_MCC_ID_NO_DASHES
use_proto_plus: True
```

**How to obtain:**
1. Developer token: Google Ads UI → Admin → API Centre
2. OAuth client ID/secret: Google Cloud Console → APIs & Services → Credentials (Desktop app)
3. Refresh token: Run OAuth flow once using `google-auth-oauthlib` or the `generate_user_credentials.py` helper from google-ads-python examples repo

**IMPORTANT:** Add `google-ads.yaml` to `.gitignore` — never commit credentials.

## Typical Monthly Workflow

1. `budget-solver-pull` — refresh 24-month data from Google Ads API
2. Open `core_markets.csv`, filter to 6 core markets (Landal BE/NL/DE, Roompot BE/NL/DE), save as `core_markets.csv`
3. `python optimizer.py --budget <monthly_budget> --data output/core_markets.csv [--min/--max constraints]`
4. Open output `budget_solver_YYYYMMDD_HHMM.xlsx`, review Optimization Results and Response Curves sheets
5. Compare Sheet 1 (curve-based) vs Sheet 8 (ROAS-proportional) allocations
6. Share with stakeholders; flag Easter timing or seasonality caveats if relevant

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `GoogleAdsException: AuthenticationError` | Check `google-ads.yaml` paths and credentials validity (refresh token may have expired) |
| No data returned from API | Confirm MCC IDs in `CHILD_MCCS`, check campaign type filter (SEARCH only) |
| Negative revenue predicted | Data quality issue—run with `--no-calibrate` to inspect raw curve fit, check for zero/negative conversion_value rows |
| `scipy.optimize failed` | Too few accounts or extreme min/max constraints—relax bounds or increase `--training-months` for more data |
| Output ROAS seems too high | Try `--training-months 12` to include more varied ROAS periods, or `--no-calibrate` to see raw model without trailing-window anchoring |
| R² very low (< 0.4) | Insufficient spend variation in training window, or account is too small/noisy—consider filtering out or extending `--training-months` |
| Marginal ROI < 1.0 at optimal | Model predicts negative returns at recommended spend—check curve fit quality, may indicate data issue or overfitting |
