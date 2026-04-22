# Budget Solver v2 — Strategic Budget Allocator

A Python tool that pulls Google Ads performance data and generates multi-scenario budget allocation strategies using diminishing-returns response curves constrained by a minimum marginal ROAS (mROAS) floor.

**Core philosophy:** Allocate budget to maximize revenue while ensuring every marginal euro spent returns at least your breakeven threshold (default: 2.5x mROAS). Avoid over-investing in high-ROAS accounts that can't scale beyond their current level.

---

## What it does

1. **Data pull** (`budget-solver-pull`) — queries the Google Ads API for 24 months of daily, account-level search campaign spend and revenue across your MCCs. Applies conversion lag correction to recent days.

2. **Scenario generation** (`budget-solver --scenarios`) — fits log response curves per account, calibrates against actual recent ROAS, and generates four strategic scenarios:
   - **Scenario A:** Current run rate (baseline)
   - **Scenario B:** Target budget allocated proportionally
   - **Scenario C:** Recommended allocation (optimized with stability rules)
   - **Scenario D:** Max justified spend (all accounts at mROAS floor)

3. **Output** — a 10-sheet Excel report with:
   - Executive Summary (stakeholder brief)
   - Scenario comparison table
   - Per-scenario allocation details
   - Extended Budget efficient frontier (C → D sweep)
   - Curve diagnostics, outlier log, demand index

---

## Accounts covered

| MCC | Name |
|-----|------|
| 8265762094 | Landal MCC |
| 6917028372 | Roompot MCC |

**Active markets:** Landal BE/NL/DE, Roompot BE/NL/DE (6 accounts across 2 MCCs)

---

## Prerequisites

- Python 3.10+
- Google Ads API developer token and OAuth credentials (see setup below)
- pip-installable package: `pip install -e .`

---

## Installation

```bash
# Clone repo
git clone <repo-url>
cd budget-solver-v2

# Install in editable mode
pip install -e .

# Verify installation
budget-solver --help
budget-solver-pull --help
```

Dependencies: `google-ads`, `scipy`, `numpy`, `pandas`, `openpyxl`, `requests`

---

## Google Ads API Setup

### 1. Create credentials folder

```bash
mkdir -p ~/.config/landal
```

### 2. Create `google-ads.yaml`

Create file at `~/.config/landal/google-ads.yaml`:

```yaml
developer_token: YOUR_DEVELOPER_TOKEN
client_id: YOUR_CLIENT_ID.apps.googleusercontent.com
client_secret: YOUR_CLIENT_SECRET
refresh_token: YOUR_REFRESH_TOKEN
login_customer_id: YOUR_MCC_ID_NO_DASHES
use_proto_plus: True
```

**How to obtain:**
- **Developer token:** Google Ads UI → Admin → API Centre
- **OAuth client ID/secret:** Google Cloud Console → APIs & Services → Credentials (Desktop app)
- **Refresh token:** Run OAuth flow once using `google-auth-oauthlib` (see below)

### 3. Generate refresh token (first time only)

```bash
python -c "
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_config(
    {'installed': {'client_id': 'YOUR_CLIENT_ID', 'client_secret': 'YOUR_CLIENT_SECRET',
     'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
     'token_uri': 'https://oauth2.googleapis.com/token'}},
    scopes=['https://www.googleapis.com/auth/adwords']
)
creds = flow.run_local_server(port=0)
print('Refresh token:', creds.refresh_token)
"
```

Paste the printed token into `google-ads.yaml`.

**IMPORTANT:** Add `google-ads.yaml` to `.gitignore` — never commit credentials.

---

## Typical Monthly Workflow

### Step 1: Pull data

```bash
budget-solver-pull
```

Outputs `output/core_markets.csv` (24 months, 6 core markets, lag-adjusted). Runtime: 2-5 minutes.

### Step 2: Generate scenarios

```bash
budget-solver --budget 1231200 --scenarios
```

Outputs timestamped Excel file: `output/budget_solver_20260422_1133.xlsx`

### Step 3: Review output

Open Excel file. Key sheets:
- **Executive Summary** — stakeholder brief with KPI table
- **Overview** — portfolio view of all 4 scenarios
- **Scenario C** — recommended allocation with action items
- **Extended Budget** — efficient frontier showing where returns diminish

### Step 4: Present to stakeholders

Share Excel file. Flag any seasonality caveats (Easter timing, demand index shifts).

---

## CLI Reference

### Basic usage

```bash
budget-solver --budget <monthly_eur>
```

### Scenario mode (recommended)

```bash
budget-solver --budget 1231200 --scenarios
```

Generates scenarios A/B/C/D with Excel report.

### Common flags

| Flag | Default | Description |
|------|---------|-------------|
| `--budget` | *required* | Total monthly budget (EUR) |
| `--scenarios` | off | Generate 4-scenario report (A/B/C/D) |
| `--min-mroas` | 2.5 | Minimum instantaneous mROAS floor (breakeven constraint) |
| `--min "Landal BE:500,Roompot NL:1000"` | none | Per-account minimum spend (EUR/month) |
| `--max "Landal DE:20000"` | none | Per-account maximum spend (EUR/month) |
| `--training-months` | 6 | Months of history for curve fitting |
| `--forecast-month YYYY-MM` | inferred | Forecast period (e.g., `2026-05`) |
| `--forecast-week 1-53` | inferred | ISO week for demand scaling |
| `--extended-budget-steps` | 6 | Rows in Extended Budget sweep (C → D) |
| `--no-calibrate` | off | Disable ROAS calibration (diagnostics only) |
| `--no-outlier-removal` | off | Disable outlier filter |
| `--normalize-demand` | off | Apply demand index before fitting (experimental) |
| `--target conversions` | revenue | Optimize conversions instead of revenue |
| `--output path.xlsx` | auto | Custom output filename |

### Advanced examples

```bash
# With spend constraints
budget-solver --budget 1231200 --scenarios \
  --min "Landal BE:500,Roompot NL:1000" --max "Landal DE:20000"

# Adjust training window
budget-solver --budget 1231200 --scenarios \
  --training-months 12

# Diagnostic mode (no calibration, no outlier removal)
budget-solver --budget 1231200 \
  --no-calibrate --no-outlier-removal

# Specify forecast period explicitly
budget-solver --budget 1231200 --scenarios \
  --forecast-month 2026-06
```

---

## How the Model Works

### Response curves

Each account gets a **log curve**: `revenue = a × ln(spend) + b`

Marginal ROAS = `a / spend` — declines hyperbolically as spend increases. This matches observed behavior in small, saturating markets better than power curves.

### Breakeven constraint (mROAS floor)

Default: 2.5x minimum instantaneous mROAS. The optimizer won't allocate spend beyond the point where `dRevenue/dSpend < 2.5`.

**Why 2.5x?** Typical breakeven after variable costs (COGS ≈ 60%, so €1 revenue → €0.40 margin → need 2.5x ROAS for €1 return on €1 spend).

Adjustable via `--min-mroas <value>`.

### Conversion lag correction

Conversions are attributed to click date, so recent days (0-29 days ago) have incomplete data. The data pull applies a daily multiplier derived from observed conversion arrival profile:
- Day 0 (today): 38.3% settled → multiply by 2.61x
- Day 14: 78.8% settled → multiply by 1.27x
- Day 30+: 100% settled → multiply by 1.0x

Output columns: `conversion_value_adj`, `conversions_adj`, `lag_factor`.

### Curve calibration

After fitting, each curve is anchored to the account's actual lag-adjusted ROAS from the last 30 days. This prevents over-prediction when historical high-ROAS periods inflate the model.

**Calibration factor** = `actual_roas / model_roas_at_current_spend`. Applied as a multiplicative scale preserving the curve's shape (diminishing returns slope) while correcting absolute level.

### Outlier removal

Two-pass filter per account (unless `--no-outlier-removal`):
1. **Pass 1:** Drop weeks with spend < 20% of median (low-spend periods where organic/remarketing dominates)
2. **Pass 2:** Drop weeks with ROAS outside [Q1 - 2×IQR, Q3 + 2×IQR] (tracking outages, attribution errors)

Removal log written to Excel "Outlier Log" sheet for review.

### Demand seasonality

Weekly demand index (ISO week 1-53) computed from median weekly ROAS across accounts. High-demand weeks (Easter ≈ 1.4x) vs low-demand (November ≈ 0.7x).

Forecast period predictions are scaled by the demand multiplier for that week. Enables like-for-like comparison of "efficiency at fixed demand level."

Optional: `--normalize-demand` applies demand index before curve fitting (experimental, isolates spend efficiency from seasonality).

### Training window

Curves fitted on recent N months only (default: 6 via `--training-months`). Full history used for spend cap derivation but NOT for fitting.

**Rationale:** Prevents over-predicting when historical high-ROAS periods (e.g., January 2025 with 19-32× ROAS) inflate expectations. Training window approach reflects current market conditions while allowing long lookback for data stability.

---

## Scenario Framework

### Scenario A: Current Run Rate

Baseline. Spend extrapolated from last 7 days to monthly run rate. Shows where you are today.

**Use:** Anchor for comparison. "If we keep current daily caps for 30 days, we'll spend €X and generate €Y revenue."

### Scenario B: Target Budget

Proportional allocation. Takes your `--budget` parameter and splits it across accounts in proportion to their current spend.

**Use:** Naive allocation. "If we scale all accounts up/down by 10%, what happens?"

Often includes accounts below mROAS floor (flagged in warnings).

### Scenario C: Recommended ✅

Optimized allocation with Phase 4 stability rules:
- Reallocations ranked by `move_value = |delta_spend| × discrete_mROAS`
- Optional moves ranked by move value (|ΔSpend| × discrete mROAS). All moves applied by default. Use --max-account-changes N to limit during volatile periods.
- Mandatory floor caps always applied (accounts below mROAS breakeven)
- 20% WoW change cap (gradual phase-in recommended for large cuts)

**Use:** What you should implement. Balances revenue upside with operational feasibility.

### Scenario D: Max Justified @ 2.5x Floor

Theoretical maximum. All accounts at mROAS floor (breakeven). Shows total addressable market within profitability constraint.

**Use:** "How much could we scale if budget were uncapped?" Helps CFO understand incremental opportunity beyond C.

**Portfolio discrete mROAS (C → D)** shows incremental return on the gap budget. If C → D discrete mROAS = 3.16x and you have €1.7M headroom, you know every euro between C and D returns 3.16x.

---

## Interpreting the Output

### Executive Summary sheet

- **KPI table:** Budget/Revenue/Blended ROAS for A/B/C/D
- **Uplift vs B:** Revenue gain from reallocation (C vs B)
- **Methodology:** Summary of approach and constraints
- **Scenario findings:** Narrative summaries from Phase 5
- **Top action recommendations:** Extracted from Scenario C

### Overview sheet

- **Portfolio table:** Side-by-side A/B/C/D comparison
- **Key transitions:** Discrete mROAS for A→B, B→C, C→D
- **Binding constraint diagnosis:** Budget vs breakeven caps
- **Conditional formatting:** Inst. mROAS <2.5x highlighted red, >5x highlighted green

### Scenario sheets (A/B/C/D)

- **Allocation table:** Per-account daily/monthly spend, revenue, inst. mROAS
- **Move labels:** "▲ +€X (+Y%)" for increases, "▼ -€X (-Y%)" for decreases
- **Conditional formatting:** mROAS thresholds colored
- **Action items:** (C only) What to change from B, with phasing guidance

### Extended Budget sheet

Efficient frontier sweep from C → D in equal budget increments (default: 6 steps).

**Chart:** Budget (x-axis) vs Revenue (primary y) + Incremental ROAS (secondary y, dashed orange).

**Use:** Visual answer to "where does incremental return drop below acceptable threshold?"

Example: Incremental ROAS sequence 4.78x → 3.49x → 2.90x → 2.48x → 2.13x shows diminishing returns. CFO can see that pushing beyond €2.5M budget yields <2.5x incremental return.

### Curve Diagnostics sheet

Per-account table: R², model type, data points, calibration factor, training window, breakeven @ 2.5x floor.

**What to watch:**
- **R² < 0.5:** Weak fit, treat recommendations with wider confidence bands
- **Calibration factor << 1.0:** Model over-predicted, actual recent ROAS much lower than fitted curve suggested
- **Breakeven well above current spend:** Account can scale significantly before hitting floor

### Key metrics explained

| Metric | Formula | Meaning |
|--------|---------|---------|
| **Blended ROAS** | Total revenue ÷ Total spend | Portfolio-level return |
| **Inst. mROAS** | `dRevenue/dSpend` at current allocation | Marginal return on next euro (instantaneous) |
| **Discrete mROAS** | `ΔRevenue / ΔBudget` between scenarios | Average incremental return on scenario transition |
| **Actual ROAS** | Lag-adjusted revenue (last 30d) ÷ spend (last 30d) | What you're achieving now |
| **Projected ROAS** | Curve-predicted revenue ÷ recommended spend | Model expectation at new allocation |

**Inst. mROAS thresholds:**
- `< 2.5x`: Below floor — value destruction on every marginal euro. Cap immediately.
- `2.5x - 4.0x`: Monitor closely — above floor but limited headroom.
- `≥ 4.0x`: Healthy return — room to scale if budget allows.

---

## Caveats & Limitations

See `LIMITATIONS.md` for full discussion. Key points:

1. **Log curve extrapolation:** No saturation ceiling. Scenario D is theoretical — real curves may plateau before reaching predicted revenue.

2. **Incrementality not measured:** Curves encode observed revenue (paid + organic halo), not incremental revenue from ads. ROAS may be inflated by brand search.

3. **Bid strategy translation:** Optimizer outputs daily budget caps. You must translate to bid adjustments (tROAS targets, tCPA caps) in Google Ads UI.

4. **30-day calibration window:** If trailing 30 days are unrepresentative (promo spike, tracking outage), calibration anchors to that anomaly. Use `--no-calibrate` to diagnose.

5. **Excluded campaign types:** Pmax, DemandGen, Display not in scope. SEARCH campaigns only.

6. **Easter timing shifts:** If Easter falls in different weeks year-over-year, April projections may be conservative or over-optimistic.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `GoogleAdsException: AuthenticationError` | Check `google-ads.yaml` paths and credentials validity (refresh token may have expired) |
| `No data returned` | Confirm MCC IDs in `CHILD_MCCS`, check campaign type filter (SEARCH only) |
| `Negative revenue predicted` | Data quality issue — run with `--no-calibrate` to inspect raw curve fit |
| `scipy.optimize failed` | Too few accounts or extreme min/max constraints — relax bounds or increase `--training-months` |
| `Output ROAS too high` | Try `--training-months 12` to include more varied ROAS periods, or `--no-calibrate` to see raw model |
| `R² very low (< 0.4)` | Insufficient spend variation in training window — consider filtering out or extending `--training-months` |
| `Marginal ROI < 1.0 at optimal` | Model predicts negative returns at recommended spend — check curve fit quality, may indicate data issue |

---

## Example Run

```bash
$ budget-solver --budget 1231200 --scenarios

Loading data from: core_markets.csv
  Using lag-adjusted conversion values (conversion_value_adj).
Loaded 4,386 rows across 6 accounts.

Training window: last 6 months (2025-10-22 to 2026-04-22)

Outlier removal: 10 week(s) excluded across 4 account(s)
  Landal BE                       3 week(s) removed
  Landal NL                       5 week(s) removed
  ...

Fitting response curves:
  Landal BE                       model=log              R²=0.425  n=24
  Landal DE                       model=log              R²=0.776  n=26
  Landal NL                       model=log              R²=0.815  n=22
  ...

SCENARIO COMPARISON
ID   Name                                    Budget         Revenue     ROAS Portfolio Disc. mROAS
────────────────────────────────────────────────────────────────────────────────────────────────────
A    Current Run Rate               €    1,375,590  €       12.55M    9.12x  — (baseline)
B    Target Budget                  €    1,231,200  €       11.87M    9.64x  4.71x
C    Recommended                    €    1,244,726  €       12.06M    9.69x  14.40x ✅
D    Max Justified @ 2.5x Floor     €    2,993,085  €       17.58M    5.87x  3.16x

Building Excel report → budget_solver_20260422_1133.xlsx
Done. Report saved to: budget_solver_20260422_1133.xlsx
```

---

## File Structure

```
budget-solver-v2/
├── src/
│   └── budget_solver/
│       ├── cli.py              # Main CLI entry point
│       ├── data_pull.py        # Google Ads API data pull
│       ├── scenarios.py        # Scenario generation (A/B/C/D)
│       ├── solver.py           # Optimization engine
│       ├── curves.py           # Response curve fitting
│       ├── excel/              # Excel report generation
│       │   ├── __init__.py     # build_excel() orchestrator
│       │   ├── builders.py     # Overview + scenario sheets
│       │   └── phase6b.py      # Extended Budget + diagnostics
│       └── constants.py        # Color codes, weeks/month
├── tests/
│   └── fixtures/
│       └── core_markets_stress.csv  # Example input data
├── output/                     # Generated files (ignored by git)
│   ├── core_markets.csv        # Data pull output
│   └── budget_solver_*.xlsx    # Optimizer reports
├── README.md                   # This file
├── LIMITATIONS.md              # Model constraints and caveats
├── CHANGELOG.md                # Version history
├── pyproject.toml              # Package metadata
└── requirements.txt            # Dependencies
```

---

## Development

### Running tests

```bash
pytest tests/
```

### Type checking

```bash
mypy src/budget_solver
```

### Installing in editable mode

```bash
pip install -e .
```

Changes to `src/budget_solver/` immediately reflected in `budget-solver` command.

---

## Support

Issues and feature requests: <repo-url>/issues

For questions about Google Ads API setup, see official docs: https://developers.google.com/google-ads/api/docs/start
