# Model Limitations & Known Issues

This document outlines the constraints, assumptions, and known limitations of Budget Solver v2. Read this before presenting results to stakeholders.

---

## 1. Log Curve Extrapolation (No Saturation Ceiling)

### What it means

The log curve `revenue = a × ln(spend) + b` has no upper bound — it continues growing (slowly) as spend increases indefinitely. Real markets saturate when you've exhausted qualified demand.

### Why it matters

**Scenario D (Max Justified)** is theoretical. It shows "spend where every euro returns exactly 2.5x mROAS" but doesn't account for:
- Finite audience size (e.g., "only 10,000 people in Belgium search for 'vakantiepark' each month")
- Creative fatigue (ad CTR declines with increased frequency)
- Competitive pressure (CPCs rise as you chase tail keywords)

### What to do

- **Use Scenario D as an upper bound,** not a target. It answers "how much could we theoretically scale?" but assumes perfect conditions.
- **Monitor diminishing returns carefully.** If Extended Budget sheet shows incremental ROAS dropping below 2.5x before reaching D, that's a signal the log curve is over-extrapolating.
- **Test incrementally.** Don't jump from C to D in one week. Scale in 10-20% increments and watch actual ROAS.

### Example

Landal NL currently spends €600k/month at 11x ROAS. Scenario D suggests €1.6M/month at 2.5x mROAS. But the Belgian market may only support €900k before ROAS drops below 2.5x. The log curve doesn't know this — it only sees historical diminishing returns *within the observed range*.

---

## 2. Incrementality Not Measured (Paid vs Organic Halo)

### What it means

The optimizer maximizes *observed revenue* attributed to paid search, not *incremental revenue caused by ads*. It cannot distinguish:
- Direct paid effect (user clicked ad, booked)
- Organic halo (user saw ad, later searched brand organically, booked)
- Baseline (would have booked anyway, ad did nothing)

### Why it matters

ROAS may be inflated by:
- **Brand search:** High ROAS on "Landal" keyword doesn't mean the ad created demand — users were searching for you anyway.
- **Remarketing overlap:** Users who see search ads *and* display/email may be double-counted.
- **Attribution window:** 30-day click attribution captures conversions that would have happened without the click.

### What to do

- **Segment brand vs generic.** If 80% of ROAS comes from brand search, high ROAS doesn't mean ads are efficient — it means you have strong brand equity.
- **Run incrementality tests** (geo holdouts, CausalImpact) annually to validate that paid search ROI is real.
- **Don't over-index on ROAS.** A 15x ROAS on brand search doesn't mean you should cut generic (3x ROAS) — generic may be driving upper-funnel demand that later converts via brand.

### Recommendation

Treat Scenario C allocations as "maximize revenue within observed performance" not "maximize true incrementality." If you suspect significant baseline erosion, deflate your breakeven threshold (e.g., use `--min-mroas 4.0` instead of 2.5).

---

## 3. Bid Strategy Translation Is Approximate

### What it means

The optimizer outputs **daily budget caps** (e.g., "Landal NL: €20,268/day"). You must manually translate this to Google Ads bid settings:
- tROAS (target ROAS) bid adjustments
- tCPA (target CPA) caps
- Manual CPC adjustments
- Budget pacing (standard vs accelerated)

### Why it matters

Google Ads doesn't spend exactly to your daily cap if:
- Bidding algorithm over-delivers early in the day (accelerated pacing)
- tROAS target is set too high (underdelivery)
- Auction volatility spikes (weekend demand surge)

### What to do

- **Use budget caps as guardrails,** not precision targets. If optimizer says "€20,268/day", set Google Ads budget to €20,500/day (2% buffer).
- **Adjust tROAS incrementally.** Don't change from 8x → 10x overnight. Move in 0.5x steps over 7-14 days to let Smart Bidding adapt.
- **Monitor delivery pacing.** If an account consistently spends 70% of budget, either raise tROAS target or increase daily cap.
- **Check search impression share.** If you're losing impressions due to budget (not rank), you can safely increase spend.

### Known gap

The tool doesn't output tROAS targets directly because the relationship between budget and tROAS is nonlinear and bidding-algorithm-dependent. A future version could integrate Google Ads API bid simulation data to recommend tROAS adjustments.

---

## 4. 30-Day Calibration Window Sensitivity

### What it means

After fitting curves on 6-month history, the tool anchors each curve to actual lag-adjusted ROAS from the last 30 days of input data. If those 30 days are unrepresentative, calibration propagates the anomaly into forecasts.

### Why it matters

Recent anomalies that skew calibration:
- **Promo spike:** Black Friday week with 3x normal ROAS → calibration inflates all predictions
- **Tracking outage:** GA4 or Google Ads attribution bug → ROAS drops to 2x for 10 days → calibration deflates predictions
- **Seasonal trough:** Calibration window = early November (low demand) → predictions anchored to weak baseline

### What to do

- **Check calibration factors** in Curve Diagnostics sheet. If cal. factor = 0.4x, it means model predicted 10x ROAS but actual was 4x — big red flag.
- **Run with `--no-calibrate`** to see raw fitted curve. If uncalibrated predictions seem reasonable, the issue is the 30-day window.
- **Manually override calibration window** (future enhancement: add `--calibration-start`/`--calibration-end` flags).
- **Exclude outlier days.** If Black Friday distorted ROAS, filter those dates from input CSV before running.

### Example

Input data: Oct 1 – Apr 22. Calibration window: Mar 24 – Apr 22 (last 30 days). But Apr 1-14 was Easter surge (ROAS 15x), Apr 15-22 was post-Easter slump (ROAS 6x). Average = 10.5x. If fitted curve predicted 8x at current spend, calibration scales everything by 1.31x (10.5/8). Now May forecast (non-Easter) is inflated by 31%.

---

## 5. Excluded Campaign Types (Pmax / DemandGen / Display)

### What it means

The data pull queries only **SEARCH campaigns** (campaign type filter in `data_pull.py`). Excluded:
- Performance Max (Pmax)
- Demand Gen (YouTube/Discover)
- Display Network
- Shopping (if not search-based)

Campaigns with `| BR` or `| PK` suffixes are also excluded (brand-only or remarketing).

### Why it matters

If 40% of your Google Ads budget goes to Pmax, the optimizer only sees 60% of total spend. Reallocations assume the excluded 40% stays constant.

### What to do

- **Run Pmax separately.** Pmax has its own asset groups and doesn't accept daily budget optimization in the same way. Treat it as a fixed allocation outside Budget Solver.
- **Filter input data post-pull.** If you want to include Shopping, modify `data_pull.py` campaign type filter to include `SHOPPING`.
- **Document mixed strategies.** If presenting Scenario C to CFO, note "these allocations apply to €X search budget; €Y Pmax budget held constant."

### Roadmap

Future version could support Pmax by:
1. Pulling Pmax data separately
2. Fitting curves at asset group level (not campaign)
3. Generating Pmax-specific budget recommendations

But Pmax bid strategies are opaque (Google controls targeting), so curve fitting may not be meaningful.

---

## 6. Easter Timing Shifts (Year-on-Year Seasonality Mismatch)

### What it means

Easter moves between March 22 and April 25 depending on the year. The demand index captures "ISO week X had high ROAS in historical data," but if Easter was week 15 last year and week 17 this year, the index won't align.

### Why it matters

April forecast may be conservative or over-optimistic depending on whether Easter overlaps with the forecast period.

### What to do

- **Check forecast week** in Demand Index sheet. If forecasting April 2026 (week 16) but Easter is week 14, you're projecting non-Easter demand at Easter-level spend.
- **Manually adjust demand multiplier.** If Easter shifts significantly, consider using `--demand-index-csv` to supply a corrected seasonal curve (e.g., from internal bookings data or SimilarWeb traffic patterns).
- **Flag in stakeholder presentation.** "Note: Easter was week 15 in 2025 but week 17 in 2026. April projections assume no Easter overlap; adjust if needed."

### Example

Scenario: Forecasting May 2026 (weeks 18-22). Historical data shows week 15 (typical Easter) had 1.4x demand multiplier. But this year Easter was week 14. May forecast uses week 20 multiplier (0.76x, low demand). If stakeholder asks "why is May ROAS so low?", answer: "May is post-Easter trough; historical data from 2024-2025 shows weak demand in weeks 18-22."

---

## 7. Account-Level Aggregation (No Campaign Granularity)

### What it means

Curves are fitted at the account level (e.g., "Landal NL" = all NL search campaigns aggregated). The optimizer cannot recommend within-account reallocations (e.g., "cut BE brand, increase BE generic").

### Why it matters

A single account may have high-ROAS brand campaigns and low-ROAS generic campaigns. Aggregated ROAS = 10x doesn't tell you which to cut.

### What to do

- **Segment manually post-optimization.** If optimizer says "cut Landal NL by 20%", review campaign-level ROAS in Google Ads and apply cuts selectively to underperformers.
- **Use campaign-level constraints** (future enhancement: `--campaign-min`/`--campaign-max` flags).
- **Split accounts in input data.** Create separate rows for "Landal NL Brand" vs "Landal NL Generic" if you want campaign-level recommendations.

---

## 8. Training Window Recency Bias

### What it means

Default training window = 6 months. Older data (months 7-24) is ignored for curve fitting (but used for spend caps).

### Why it matters

If market conditions changed radically 6 months ago (new competitor, algorithm update, product launch), 6-month window may miss the shift.

### What to do

- **Experiment with `--training-months`.** Try 3/6/12 months and compare R² and predictions.
- **Visual inspection.** Plot spend vs revenue over 24 months (use Outlier Log + raw data) to spot structural breaks.
- **Adjust if needed.** If a major product launch happened 8 months ago, use `--training-months 8` to include it.

---

## 9. Single-Month Forecast (No Multi-Month Rollout)

### What it means

The tool outputs a single monthly allocation for the forecast period. It doesn't plan "month 1: scale 10%, month 2: scale 20%, month 3: full allocation."

### Why it matters

Stakeholders often want a phased rollout plan, especially for large budget shifts.

### What to do

- **Use Extended Budget sheet.** Scenario C = month 1 target, intermediate steps = month 2-3 targets, Scenario D = long-term max.
- **Manual phasing.** If Scenario C says "cut Roompot NL by €73k/month", implement as:
  - Week 1-2: -20% (€15k)
  - Week 3-4: -40% (€30k)
  - Week 5-6: -60% (€45k)
  - Week 7+: -100% (€73k)
- **Re-run monthly.** After implementing month 1, pull fresh data and regenerate scenarios to see if recommendations shift.

---

## 10. No Confidence Intervals

### What it means

The tool outputs point estimates (€20,268/day revenue projection) without confidence intervals (e.g., "80% CI: €18k-€22k").

### Why it matters

All predictions are uncertain. R² = 0.8 doesn't mean predictions will be exact — it means 80% of historical variance is explained.

### What to do

- **Use R² as a proxy.** R² = 0.4 (Landal BE) → wide confidence bands, treat predictions as ±30% rough estimate. R² = 0.8 (Landal NL) → narrower bands, ±10-15%.
- **Test incrementally.** Don't bet the farm on predictions. Scale in 10-20% steps and validate actual ROAS.
- **Bootstrapping (future enhancement).** Resample historical data, refit curves 1000x, compute 95% CI on predictions.

---

## Summary: What Budget Solver Can and Cannot Do

### ✅ Budget Solver CAN:

- Fit diminishing-returns curves to historical search campaign data
- Allocate budget to maximize revenue subject to mROAS floor
- Generate multi-scenario strategic plans (A/B/C/D)
- Flag accounts below breakeven threshold
- Apply stability rules to avoid operational chaos
- Visualize efficient frontier (Extended Budget)

### ❌ Budget Solver CANNOT:

- Measure true incrementality (requires holdout tests)
- Optimize within-account campaign mix (account-level only)
- Predict saturation ceilings (log curve grows indefinitely)
- Translate budget caps to exact tROAS bid targets
- Account for cross-channel synergies (Pmax/Display/Email)
- Adjust for radically shifted seasonality (Easter year-over-year)
- Provide confidence intervals (point estimates only)
- Replace human judgment (use as decision support, not autopilot)

---

## Recommended Use

Budget Solver v2 is a **decision support tool**, not a black box. Use it to:
1. Identify accounts over/under-invested relative to breakeven
2. Quantify revenue opportunity from reallocation
3. Visualize trade-offs (Extended Budget frontier)
4. Support stakeholder discussions with data-driven scenarios

**Always:**
- Cross-check recommendations against campaign-level Google Ads data
- Validate incrementality assumptions annually (geo tests, CausalImpact)
- Test allocations incrementally (10-20% steps over 4-8 weeks)
- Monitor actual ROAS weekly during rollout
- Re-run monthly with fresh data to adapt to market shifts

**Never:**
- Implement Scenario D blindly (theoretical max, not tested)
- Ignore warnings (accounts below floor, large WoW changes)
- Trust calibration if last 30 days had major anomalies
- Expect predictions to be exact (all models are wrong, some are useful)
