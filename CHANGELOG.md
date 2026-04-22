# Changelog

All notable changes to Budget Solver documented by development phase.

---

## Phase 0: Foundation (Pre-refactor)

**Status:** Baseline implementation

- Single-file `optimizer.py` script with data pull + optimization + Excel output
- Log curve fitting with calibration
- Basic Results, Sensitivity, Curves sheets in Excel output
- Manual filtering workflow (core_markets.csv → core_markets.csv)
- Command: `python optimizer.py --budget <amount> --data <file>`

**Limitations:**
- No multi-scenario framework
- No stability rules (optimizer could recommend -100% cuts)
- No mROAS floor constraint
- Monolithic codebase (hard to test/maintain)
- Excel output limited to 8 sheets

---

## Phase 1: Package Structure & CLI

**Goal:** Modularize codebase into installable package with proper CLI

**Changes:**
- Created `src/budget_solver/` package structure
- Split monolith into modules: `cli.py`, `solver.py`, `curves.py`, `excel/`, `scenarios.py`
- Added `pyproject.toml` for pip-installable package
- Introduced `budget-solver` CLI command (replaces `optimizer.py`)
- Kept `optimizer.py` as temporary compatibility shim

**New behavior:**
```bash
# Old way (still works)
python optimizer.py --budget 1231200 --data output/core_markets.csv

# New way
budget-solver --budget 1231200 --data output/core_markets.csv
```

**Breaking changes:** None (shim maintained backward compatibility)

---

## Phase 2: Breakeven Constraint (mROAS Floor)

**Goal:** Add minimum marginal ROAS floor to prevent over-investment in saturating accounts

**Changes:**
- Introduced `--min-mroas` flag (default: 2.5x)
- Optimizer now enforces `dRevenue/dSpend >= min_mroas` per account
- Accounts that hit mROAS floor are capped (spend limited to breakeven point)
- Excel output shows inst. mROAS with conditional formatting:
  - Red: < 2.5x (over-invested)
  - Yellow: 2.5x - 4.0x (healthy)
  - Green: ≥ 4.0x (strong, consider scaling)

**New behavior:**
```bash
budget-solver --budget 1231200 --data output/core_markets.csv --min-mroas 3.0
```

Before Phase 2: Optimizer could allocate all budget to highest-ROAS account (e.g., Landal NL at 15x blended) even if marginal ROAS was 1.2x (losing money).

After Phase 2: Budget capped at point where inst. mROAS = 2.5x. Excess budget reallocated to other accounts.

**Key insight:** High blended ROAS ≠ scalable. Small markets saturate quickly.

---

## Phase 3: Multi-Scenario Framework (A / B / C / D)

**Goal:** Replace single-point optimization with strategic scenario cascade

**Changes:**
- Introduced 4-scenario structure:
  - **Scenario A:** Current run rate (baseline)
  - **Scenario B:** Target budget allocated proportionally
  - **Scenario C:** Optimized allocation (inequality constraint: Σspend ≤ budget)
  - **Scenario D:** Max justified (all accounts at mROAS floor)
- New `--scenarios` flag generates multi-scenario Excel report
- Added `scenarios.py` module with `ScenarioSet` dataclass
- Implemented `optimize_with_inequality_constraint()` for Scenario C (allows unallocated budget)
- Introduced **discrete mROAS** metric for A→B, B→C, C→D transitions

**New behavior:**
```bash
budget-solver --budget 1231200 --data output/core_markets.csv --scenarios
```

Output: 10-sheet Excel with scenario comparison table, per-scenario details, Extended Budget frontier.

**Conceptual shift:**
- Before: "What allocation maximizes revenue at €X budget?"
- After: "Show me 4 strategic options from baseline (A) to max scale (D), with recommended middle path (C)"

---

## Phase 4: Stability Rules (Feasibility Constraints)

**Goal:** Prevent operational chaos from aggressive reallocations

**Changes:**
- Implemented **top-2 move ranking** system:
  - All account changes ranked by `move_value = |Δspend| × discrete_mROAS`
  - Top-2 optional moves preserved, rest reverted to Scenario B
  - Mandatory floor caps (accounts below mROAS) always applied
- Added **20% WoW change cap** with phasing guidance
- New warnings in Scenario C:
  - "Reverted N optional reallocations to Scenario B (below top-2 cutoff)"
  - "Decrease of X% exceeds 20% WoW cap. Phase gradually over Y weeks."
- Move labels in Excel: "▲ +€X (+Y%)" / "▼ -€X (-Y%)" / "— reverted to B" / "— (already at floor)"

**Example:**

Before Phase 4 (Scenario C):
- Landal BE: -€50k (-60%)
- Landal NL: +€200k (+40%)
- Roompot BE: -€30k (-80%)
- Roompot NL: -€80k (-25%)
- Roompot DE: +€10k (+15%)

After Phase 4 (with stability rules):
- Landal BE: ±€0 (reverted to B, move value too low)
- Landal NL: +€200k (top-1 move, high incremental ROAS)
- Roompot BE: -€30k (mandatory floor cap, below 2.5x)
- Roompot NL: -€80k (top-2 move, significant revenue impact)
- Roompot DE: ±€0 (reverted to B, move value too low)

**Key insight:** Perfect optimization ≠ implementable. Limit changes to top-2 highest-impact moves.

---

## Phase 5: Narrative Generation

**Goal:** Auto-generate human-readable scenario summaries for stakeholder presentation

**Changes:**
- Added `narrative.py` module with `full_scenario_narrative()` function
- Each scenario gets 4-block narrative:
  1. **Portfolio headline:** Budget/revenue/ROAS summary + warnings
  2. **Summary paragraph:** Strategic context (what this scenario represents)
  3. **Per-account detail:** Inst. mROAS, discrete mROAS, health indicators
  4. **Action items:** (Scenario C only) Specific implementation steps with phasing
- Narratives printed to console and embedded in Excel report
- Health indicators: 🟢 Healthy | 🟡 Monitor | 🔴 Over-invested

**Sample narrative (Scenario C):**

```
Budget: €1,244,726/mo | Revenue: €12.06M/mo | Blended ROAS: 9.69x
Discrete mROAS vs prev: 14.40x | Uplift vs prev: +195k (+1.6%)

Recommended allocation. 3 account change(s) vs B. Mixed binding — budget
cap with some accounts at floor. Revenue uplift vs B: +194,791 (+1.6%).

Per-account:
  🟢 Landal BE (€1,372/day): inst. mROAS 5.16x. Healthy.
  🟡 Landal DE (€3,419/day): inst. mROAS 2.50x. Monitor closely.
  🟢 Landal NL (€20,268/day): inst. mROAS 6.52x. Strong incremental return.

Action items:
  1. Landal DE: decrease daily cap from €3,716 → €3,419 (−€296/day)
  2. Landal NL: increase daily cap from €18,009 → €20,268 (+€2,259/day)
  3. Roompot NL: decrease daily cap from €12,534 → €11,016 (−€1,518/day)
```

**Key insight:** Stakeholders don't read allocation tables. Give them prose summaries.

---

## Phase 6a: Excel Structural Sheets

**Goal:** Rebuild Excel report with professional multi-scenario presentation

**Changes:**
- Complete Excel overhaul: 6 structural sheets
  1. **Executive Summary:** Stakeholder brief with 4-column KPI table (A/B/C/D)
  2. **Overview:** Portfolio view with side-by-side scenario comparison
  3. **Scenario A:** Current run rate details
  4. **Scenario B:** Target budget details
  5. **Scenario C:** Recommended (marked ✅) with action items
  6. **Scenario D:** Max justified @ 2.5x floor
- Navy blue header theme (#001F3864), conditional formatting on mROAS columns
- Move labels with delta formatting
- Section headers: "ALLOCATION", "PORTFOLIO HEALTH", "TRANSITIONS", "ACTION ITEMS"
- Discrete mROAS transitions as separate rows (A→B, B→C, C→D)

**Visual improvements:**
- Blended ROAS now bolded in scenario headers
- Conditional formatting: <2.5x red, 2.5-4.0x yellow, ≥4.0x green
- Recommended scenario (C) marked with "✅ RECOMMENDED" in title
- Account change labels: "▲ +€X", "▼ -€X", "— unchanged"

---

## Phase 6b: Excel Supplementary Sheets

**Goal:** Add diagnostic and exploratory sheets for advanced analysis

**Changes:**
- 4 supplementary sheets added:
  7. **Extended Budget:** Efficient frontier sweep from C → D
     - 6 rows (default, configurable via `--extended-budget-steps`)
     - Each row = actual solver run at incremental budget level
     - Chart: Budget (x) vs Revenue (primary y) + Incremental ROAS (secondary y, dashed)
     - Shows where diminishing returns cross 2.5x threshold
  8. **Curve Diagnostics:** Per-account model metadata
     - R², model type (log), data points, calibration factor, breakeven @ 2.5x
     - Flags weak fits (R² < 0.5) for stakeholder caution
  9. **Outlier Log:** Weeks excluded before curve fitting
     - Account, week, spend, revenue, ROAS, reason
  10. **Demand Index:** 53-week seasonal multipliers
     - ISO week → demand multiplier (mean = 1.0)
     - Bar chart showing high/low demand periods
     - Forecast week highlighted

**New CLI flags:**
- `--extended-budget-steps N`: Number of rows in Extended Budget sweep (default: 6)

**Key insight:** CFOs want to see "where does incremental return drop below acceptable?" — Extended Budget visualizes this.

**Implementation note:** Extended Budget initially used linear interpolation (WRONG, constant slope). Fixed to run actual solver at each budget step, producing concave efficient frontier with declining incremental ROAS (4.78x → 3.49x → 2.90x → 2.48x → 2.13x).

---

## Phase 7: Cleanup & Documentation

**Goal:** Production-ready polish — remove shims, comprehensive docs, example workflows

**Changes:**
- **Removed `optimizer.py` shim** (breaking change: `python optimizer.py` no longer works)
- **README rewrite:**
  - New conceptual framing (breakeven allocator, not revenue maximizer)
  - Scenarios A/B/C/D explained with use cases
  - Full CLI reference (all flags documented)
  - "How the Model Works" section (curves, calibration, outlier removal, seasonality)
  - Interpreting the Output section (sheet-by-sheet guide)
- **LIMITATIONS.md created:**
  - 10 documented constraints (log curve extrapolation, incrementality, bid translation, etc.)
  - Practical guidance for each limitation
  - "What Budget Solver Can and Cannot Do" summary
- **Example workflow:**
  - Created `examples/` directory with `run_example.sh`
  - 3 example runs (basic, constrained, diagnostic)
  - Uses existing test fixture data (no API credentials needed)
- **CHANGELOG.md:** This file (phase-by-phase evolution)

**Breaking changes:**
- `python optimizer.py` no longer works (use `budget-solver` CLI)

**Migration guide:**
```bash
# Old command
python optimizer.py --budget 1231200 --data output/core_markets.csv

# New command
budget-solver --budget 1231200 --data output/core_markets.csv --scenarios
```

---

## Summary: v1 → v2 Evolution

| Aspect | v1 (Phase 0) | v2 (Phases 1-7) |
|--------|--------------|-----------------|
| **Conceptual model** | Single-point revenue maximizer | Multi-scenario strategic planner (A/B/C/D) |
| **Constraint** | Budget cap only | Budget cap + mROAS floor + stability rules |
| **Output** | 8-sheet Excel (Results, Sensitivity, Curves) | 10-sheet Excel (Exec Summary, 4 scenarios, diagnostics) |
| **Operational feasibility** | Could recommend -100% cuts | Top-2 moves + 20% WoW cap + phasing guidance |
| **Stakeholder communication** | Allocation tables | Prose narratives + action items + KPI summaries |
| **Command** | `python optimizer.py` | `budget-solver --scenarios` (installable CLI) |
| **Codebase** | 1,600-line monolith | Modular package (7 modules, 2,500 lines) |
| **Testability** | Hard (single file) | Easy (isolated modules) |
| **Documentation** | README only | README + LIMITATIONS.md + examples/ + CHANGELOG.md |

---

## Upgrade Path

If migrating from v1 (Phase 0) to v2 (Phases 1-7):

1. **Install package:**
   ```bash
   pip install -e .
   ```

2. **Replace command:**
   - Old: `python optimizer.py --budget X --data Y`
   - New: `budget-solver --budget X --data Y --scenarios`

3. **Review new Excel structure:**
   - Executive Summary replaces old Results sheet
   - Scenario C (Recommended) replaces old single-point optimization
   - Extended Budget shows efficient frontier (new)

4. **Adjust workflows:**
   - Add `--min-mroas` if your breakeven is not 2.5x
   - Use `--extended-budget-steps` to customize frontier granularity
   - Check LIMITATIONS.md for model constraints

5. **Retrain stakeholders:**
   - v1: "Here's the optimal allocation at €X budget"
   - v2: "Here are 4 scenarios (A/B/C/D). C is recommended. D shows max scale."

---

## Versioning

Budget Solver v2 uses semantic versioning after Phase 7 stabilization:
- **v2.0.0:** Initial release (Phases 1-7 complete)
- **v2.1.x:** Minor enhancements (new flags, bug fixes)
- **v3.0.0:** Breaking changes (future: Pmax support, campaign-level optimization)

---

## Future Roadmap (Post-Phase 7)

Potential enhancements for v2.1+:

- **Confidence intervals:** Bootstrap resampling for prediction uncertainty
- **Pmax support:** Extend data pull to include Performance Max campaigns
- **Campaign-level optimization:** Within-account reallocation (brand vs generic)
- **Multi-month planning:** Phased rollout schedules (month 1 → 2 → 3)
- **Incrementality integration:** Geo holdout test results → deflate ROAS
- **Automated bid strategy translation:** Suggest tROAS targets from budget allocations
- **Web dashboard:** Flask/Streamlit UI for non-technical stakeholders

---

## License

(To be added based on internal policy)

---

## Contributors

(To be added)
