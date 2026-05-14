# Budget Solver v3 — Intelligent Budget Allocator

A Python tool that pulls Google Ads performance data and generates multi-scenario budget allocation strategies using diminishing-returns response curves, a stack of market intelligence signals, and constrained nonlinear optimization.

**Core philosophy:** Allocate budget to maximize revenue while ensuring every marginal euro spent returns at least your breakeven threshold (default: 2.5× mROAS). Avoid over-investing in high-ROAS accounts that cannot scale beyond their current level. Surface all assumptions and risks transparently so a human can override the model when market conditions demand it.

---

## What it does

### Step 1 — Data pull (`budget-solver-pull`)

Queries the Google Ads API for 24 months of daily, account-level search campaign data across your MCCs. On each pull it also:

- Applies **conversion lag correction** to recent days (per-account arrival profiles)
- Pulls the **top-500 exact-match keywords** per account and fetches monthly search volumes from Keyword Planner → builds a per-account seasonal demand index
- Pulls **auction insights** (competitor impression share, trailing vs prior 30 days) → saves for competitive pressure flags
- Pulls **bid simulator** data (Google's own budget simulation points per campaign) → saves for model cross-check

### Step 2 — Scenario generation (`budget-solver --scenarios`)

Fits response curves per account, applies a stack of contextual corrections, and generates four strategic scenarios:

| Scenario | Description |
|----------|-------------|
| **A — Current Run Rate** | Baseline. Spend extrapolated from last 7 days |
| **B — Target Budget** | Proportional allocation of your `--budget` |
| **C — Recommended ✅** | Optimized reallocation with stability rules |
| **D — Max Justified** | All accounts at mROAS floor (theoretical ceiling) |

### Step 3 — Excel report

A multi-sheet workbook with every decision supported by evidence:

| Sheet | Contents |
|-------|----------|
| Executive Summary | Headline KPIs + applied correction table |
| Overview | Side-by-side A/B/C/D with corrections |
| Scenario A/B/C/D | Per-account allocation, mROAS, move labels |
| Extended Budget | Efficient frontier C → D |
| **Market Intelligence** | Forecast adjustments, calibration quality, competitive landscape, simulator cross-check |
| Curve Diagnostics | R², model type, breakeven spend per account |
| Outlier Log | Excluded weeks and reason |
| Demand Index | ISO week demand multipliers + chart |
| Model Accuracy | Prediction error vs actuals (from feedback log) |
| CPC Diagnostics | Spend/CPC trend per account |

---

## Accounts covered

| MCC | Name |
|-----|------|
| 8265762094 | Landal MCC |
| 6917028372 | Roompot MCC |

**Active markets:** Landal BE/NL/DE, Roompot BE/NL/DE (6 accounts, 2 MCCs)

---

## Prerequisites

- Python 3.10+
- Google Ads API developer token and OAuth credentials
- `pip install -e .`

---

## Installation

```bash
git clone <repo-url>
cd budget-solver-v3-optimized
pip install -e .

# Verify
budget-solver --help
budget-solver-pull --help
```

Dependencies: `google-ads`, `scipy`, `numpy`, `pandas`, `openpyxl`

---

## Google Ads API Setup

### 1. Create credentials file

```bash
mkdir -p ~/.config/landal
```

Create `~/.config/landal/google-ads.yaml`:

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
- **Refresh token:** Run OAuth flow once:

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

**IMPORTANT:** `google-ads.yaml` is in `.gitignore` — never commit it.

---

## Typical Monthly Workflow

```bash
# 1. Pull fresh data (runs ~5-10 minutes — also pulls keywords, auction insights, simulator)
budget-solver-pull

# 2. Generate scenarios for next month
budget-solver --budget 1231200 --scenarios --forecast-month 2026-06

# 3. Review output/budget_solver_YYYYMMDD_HHMM.xlsx
#    Start with Executive Summary, then Scenario C, then Market Intelligence
```

---

## CLI Reference

### Core flags

| Flag | Default | Description |
|------|---------|-------------|
| `--budget` | *required* | Total monthly budget (EUR) |
| `--scenarios` | on | Generate 4-scenario A/B/C/D report |
| `--min-mroas` | 2.5 | Minimum instantaneous mROAS floor |
| `--training-months` | 6 | Months of history used for curve fitting |
| `--forecast-month YYYY-MM` | inferred | Month to forecast |
| `--forecast-week 1-53` | inferred | ISO week for demand scaling |

### Spend constraints

```bash
budget-solver --budget 1231200 --scenarios \
  --min "Landal BE:500,Roompot NL:1000" \
  --max "Landal DE:20000"
```

### Stability controls

| Flag | Default | Description |
|------|---------|-------------|
| `--max-account-changes` | 0 (all) | Limit optional moves to top-N by move value |
| `--wow-cap` | 0.20 | Max week-over-week spend change per account |
| `--no-apply-stability` | off | Disable stability rules entirely |

### Model controls

| Flag | Default | Description |
|------|---------|-------------|
| `--two-stage` | off | Use spend → clicks → revenue model instead of direct |
| `--normalize-demand` | off | Apply demand index before curve fitting |
| `--no-calibrate` | off | Skip trailing ROAS calibration (diagnostics) |
| `--no-outlier-removal` | off | Skip outlier filter |
| `--demand-index-csv path` | auto | Override demand index with external CSV |

### Common examples

```bash
# Standard monthly run
budget-solver --budget 1231200 --scenarios --forecast-month 2026-06

# With floor constraints
budget-solver --budget 1231200 --scenarios \
  --min "Landal BE:30000" --max "Landal DE:150000"

# Wider training window (more stable curves)
budget-solver --budget 1231200 --scenarios --training-months 12

# Diagnostic: see raw curves before calibration
budget-solver --budget 1231200 --scenarios --no-calibrate

# Two-stage model (separates CPC efficiency from conversion quality)
budget-solver --budget 1231200 --scenarios --two-stage
```

---

## How the Model Works

### 1. Response curve fitting

Each account gets a **log curve**: `revenue = a × ln(spend) + b`

Marginal ROAS = `a / spend` — declines hyperbolically as spend increases. This matches observed diminishing returns in small, saturating markets better than linear or power curves.

**Fallback chain:** log → power (`revenue = a × spend^b`) → linear proxy (if n < 3 weeks). All accounts in a portfolio must use the same curve family (log or power) to allow valid cross-account mROAS comparisons.

**Two-stage model** (`--two-stage`): optionally fits `clicks = g(spend)` first, then multiplies by revenue-per-click. Separates CPC efficiency from conversion quality — important because Smart Bidding pushes into pricier auctions as spend increases, making the spend→clicks relationship deteriorate independently from click quality (CVR × AOV).

### 2. Outlier removal

Two-pass filter per account:
1. Drop weeks with spend < 20% of median (low-spend periods where organic traffic dominates, inflating ROAS)
2. Drop weeks with ROAS outside [Q1 − 2×IQR, Q3 + 2×IQR] (tracking outages, attribution anomalies)

### 3. Signal stack applied to every forecast

After fitting, the raw curve prediction is multiplied by a chain of contextual corrections:

```
raw_curve(weekly_spend)
  × demand_index[forecast_week]     ← keyword search volume seasonality
  × holiday_correction              ← this year's holiday density vs historical average
  × weather_correction              ← forecast sunshine hours vs historical average
  × WEEKS_PER_MONTH                 ← monthly scale conversion
  × extrapolation_damping           ← discount beyond observed max spend
  × calibration_factor              ← confidence-weighted trailing ROAS anchor
  × bias_correction                 ← systematic over/under-prediction from accuracy log
```

Each signal is described below.

#### Demand index

**Source:** Google Keyword Planner (top-500 exact-match keywords per account from generic campaigns, refreshed on every pull).

Converts monthly search volumes → ISO week 1-53 multipliers, normalized to mean = 1.0. Each account gets its own index (NL/DE/BE have different peak weeks — e.g., NL school holidays differ from DE). Falls back to Google Trends → internal ROAS-derived index if Keyword Planner is unavailable.

#### Holiday correction

**Source:** Hardcoded 5-year calendar (2024–2028) for NL, DE, BE school holidays and public holidays including Easter-derived moveable feasts.

Counts holiday days in the forecast month vs the 2024–2025 average baked into the response curves. Returns a per-account correction factor (e.g., April 2026 with Easter gets ×1.52 for NL). Capped [0.60, 1.80]. Always applied — Easter timing shifts are among the most common causes of forecast error.

#### Weather correction

**Source:** Open-Meteo API (free, no API key required).

Compares forecast-month sunshine hours (from the 16-day forecast API + ERA5 archive for elapsed days) to the historical average for the same calendar month across 2023–2025. Sunshine hours are the strongest single predictor of outdoor leisure booking intent. Capped [0.80, 1.20]. Only activates within 30 days of the forecast month — beyond that, weather forecasts are unreliable and the factor returns 1.0.

#### Extrapolation dampening

When recommended spend exceeds the maximum spend observed in the training data, the model is extrapolating. Beyond `max_obs_monthly`, incremental revenue is dampened exponentially:

`decay = e^(−2 × (spend / max_obs − 1))`

At 1.5× max observed: only 37% of incremental revenue survives. At 2×: 14%. The curve remains smooth (no discontinuity), but the optimizer faces rapidly declining marginal returns in uncharted territory. Accounts where recommended spend exceeds 120% of max observed get an explicit `⚠ EXTRAPOLATION WARNING` in the console.

#### Confidence-weighted calibration

After fitting, each curve is anchored to the account's trailing 14-day lag-adjusted ROAS. The blend weight is scaled by a confidence score:

`confidence = active_ratio × (1 − min(CV_of_daily_ROAS, 1.0))`
`blend = 0.25 × confidence`

- **active_ratio:** fraction of window days with positive spend (campaign paused = 0)
- **CV:** coefficient of variation of daily ROAS (volatile window = low confidence)

When confidence → 0 (paused campaigns, noisy window), the calibration factor collapses to 1.0 — the fitted curve stands on its own rather than anchoring to bad data. Calibration is capped at [0.70, 1.30] regardless.

#### Bias correction

On each run, the recommended allocation and predicted revenue are saved to `output/prediction_log.csv`. When actuals arrive the following month, prediction errors are scored. If a model has systematically over- or under-predicted for the last 2+ months, a correction factor (capped ±20%) is applied automatically.

### 4. Optimization

`scipy.optimize.minimize` with SLSQP method:
- **Objective:** Maximize Σ predict_fn(spend_i) across accounts
- **Constraint:** Σ spend_i = total_budget
- **Bounds:** Per-account min/max (from `--min`/`--max` + auto-derived caps)

**Auto spend caps:** default = 2× (max observed weekly spend × 4.33 weeks/month), or 2% of total budget (whichever is higher). Tightened further when impression pool ceiling analysis is available (see below).

**mROAS floor:** The optimizer runs unconstrained, then accounts exceeding breakeven spend are capped at their breakeven point. Breakeven = spend where instantaneous mROAS = `--min-mroas`.

### 5. Impression share ceiling

If `search_impression_share` and `impressions` are in the data, the total available impression pool is estimated:

`pool = impressions_won / avg_impression_share`

Maximum achievable monthly revenue = `pool × CTR × revenue_per_click`. Binary search (brentq) finds the spend level where the curve hits this ceiling. If tighter than the auto cap, `effective_max` is updated before optimization runs — the optimizer cannot recommend spend that would require buying auctions that don't physically exist.

### 6. Stability rules (Scenario C)

Phase 4 stability rules prevent the optimizer from recommending operationally infeasible changes:

- **Move value ranking:** Optional moves sorted by `|ΔSpend| × discrete_mROAS`. Top moves applied first.
- **WoW cap:** Default 20% max change per account per week (phased implementation)
- **Mandatory moves:** Accounts below mROAS floor always capped, regardless of stability limits

Use `--max-account-changes N` to limit how many optional moves are applied (useful during volatile periods when you want to minimize account disruption).

---

## Market Intelligence

Every run produces a **Market Intelligence** section (console + Excel sheet) with four components:

### Competitive landscape (auction insights)

Pulls trailing vs prior 30-day competitor impression share per account from `auction_insight_index`. A competitor surging > +10pp in IS is a leading indicator that CPCs and CVR will come under pressure — not captured by response curves at all. Displayed as a risk flag next to the recommended allocation. **No math is changed; this is informational only.**

```
── COMPETITIVE LANDSCAPE ─────────────────────────────────────────────────
  Account                        Competitor                  Trailing IS  Prior IS    Δ
  Landal NL                      booking.com                      35%       15%   +20%  ⚠ SURGE
```

### Simulator cross-check

Pulls `campaign_simulation` (type=BUDGET) from Google Ads. Each simulation point is Google's own prediction of (spend → conversion_value) from their internal auction model — forward-looking, aware of current competitor bids and Quality Scores. At recommended spend, compares model prediction vs simulator:

- < 10% divergence: good agreement ✓
- 10–20%: monitor
- > 20%: `⚠ diverge` — treat recommendation with extra caution

### CPC trend diagnostic

Always-on comparison of trailing 30-day CPC vs training-period CPC per account. Rising CPC > 15% means the spend→clicks relationship is becoming less efficient — the response curve may be over-optimistic at higher spend levels.

---

## Interpreting Key Metrics

| Metric | Formula | Meaning |
|--------|---------|---------|
| **Blended ROAS** | Total revenue ÷ total spend | Portfolio-level return |
| **Inst. mROAS** | `dRevenue/dSpend` at a given spend | Marginal return on the next euro (instantaneous derivative) |
| **Discrete mROAS** | `ΔRevenue / ΔSpend` between scenarios | Average incremental return on a spend change |
| **Actual ROAS** | Lag-adj. revenue (last 30d) ÷ spend (last 30d) | Observed baseline performance |
| **Projected ROAS** | Predicted revenue ÷ recommended spend | Model expectation at new allocation |
| **Calibration factor** | Actual ROAS / model ROAS at current spend | Scale applied to correct absolute curve level |
| **Confidence** | active_ratio × (1 − CV) | Reliability of the trailing calibration window |

**Inst. mROAS thresholds:**
- `< 2.5×`: Below floor — cap immediately
- `2.5–4.0×`: Monitor — above floor but limited headroom
- `≥ 4.0×`: Healthy — room to scale

---

## Caveats & Limitations

1. **Log curve extrapolation:** No hard saturation ceiling. Scenario D is theoretical — real curves may plateau before predicted revenue. Extrapolation dampening applies a discount, but treat recommendations beyond 1.5× observed max spend with care.

2. **Incrementality not measured:** Curves encode observed revenue (paid + organic halo). ROAS may be inflated by brand search attribution. The optimizer works on relative efficiency across accounts, which is less sensitive to absolute ROAS level.

3. **Bid strategy translation:** The optimizer outputs monthly spend targets. In Google Ads you implement these as daily budget caps and/or tROAS/tCPA adjustments. Large changes (> 20% budget) should be phased over 2–4 weeks to allow Smart Bidding to adapt.

4. **Calibration window risk:** If the trailing 14 days are unrepresentative (paused campaigns, promo spike, tracking outage), the confidence-weighted calibration suppresses the anchor — but the confidence score will show low in the Market Intelligence sheet. Run with `--no-calibrate` to compare.

5. **Weather / holiday corrections:** Both are based on demand proxies (sunshine hours, calendar days). They capture the expected effect of external conditions on booking intent but not one-off events (a viral social post, a major competitor sale). Use your own judgement when market conditions are unusual.

6. **Auction insights lag:** Pulled at data-pull time (not at run time). If you run the optimizer several days after the pull, the competitive picture may have shifted.

7. **SEARCH campaigns only:** Pmax, DemandGen, Display, Shopping not in scope.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `AuthenticationError` | Check `google-ads.yaml`, refresh token may have expired |
| No data returned | Confirm MCC IDs in `CHILD_MCCS`, check SEARCH campaign filter |
| Negative revenue predicted | Run `--no-calibrate` to inspect raw curve; check for zero/negative conversion_value rows |
| `scipy.optimize failed` | Relax `--min`/`--max` constraints or increase `--training-months` |
| ROAS seems too high | Use `--training-months 12` or `--no-calibrate` to check without trailing ROAS anchor |
| R² < 0.4 | Insufficient spend variation in training window; try extending `--training-months` |
| `⚠ low conf` on calibration | Campaign paused or volatile trailing window; model is falling back to curve-only (expected) |
| `⚠ EXTRAPOLATION WARNING` | Recommended spend >> observed max; dampening applied, but verify with simulator cross-check |
| Keyword demand pull fails | Keyword Planner quota may be exhausted; optimizer falls back to Google Trends → internal index |
| Weather correction skipped | Network issue or month > 30 days ahead; falls back to 1.0 (no adjustment) |

---

## File Structure

```
budget-solver-v3-optimized/
├── src/budget_solver/
│   ├── cli.py                  # Main CLI entry point + orchestration
│   ├── data_pull.py            # Google Ads API data pull
│   ├── data.py                 # Data loading, aggregation, outlier removal
│   ├── curves.py               # Response curve fitting (log/power/2-stage)
│   ├── mroas.py                # Marginal ROAS calculations
│   ├── scenarios.py            # Scenario generation (A/B/C/D)
│   ├── stability.py            # Phase 4 stability rules
│   ├── solver.py               # SLSQP optimization engine
│   ├── prediction_log.py       # Prediction persistence + accuracy feedback loop
│   ├── keyword_demand.py       # Keyword Planner demand index
│   ├── holiday_calendar.py     # NL/DE/BE holiday calendar 2024-2028
│   ├── weather.py              # Open-Meteo sunshine correction
│   ├── auction_insights.py     # Competitor IS delta (auction insights)
│   ├── bid_simulator.py        # Google bid simulator cross-check
│   ├── trends.py               # Google Trends fallback demand index
│   ├── narrative.py            # Scenario narrative generation
│   ├── constants.py            # Color codes, weeks/month, shared constants
│   └── excel/
│       ├── __init__.py         # build_excel() orchestrator
│       ├── builders.py         # Overview + scenario sheets
│       ├── phase6b.py          # Extended Budget + curve diagnostics
│       ├── diagnostics.py      # Model accuracy + CPC diagnostics sheets
│       ├── market_intelligence.py  # Market Intelligence sheet
│       └── styling.py          # Shared cell formatting helpers
├── output/                     # Generated files (git-ignored)
│   ├── core_markets.csv        # Data pull output
│   ├── keyword_demand_index.csv
│   ├── keyword_list.csv
│   ├── auction_insights.csv
│   ├── bid_simulator.csv
│   ├── prediction_log.csv
│   └── budget_solver_*.xlsx
├── tests/
├── pyproject.toml
└── requirements.txt
```

---

## Development

```bash
# Install in editable mode
pip install -e .

# Run tests
pytest tests/

# Type check
mypy src/budget_solver
```
