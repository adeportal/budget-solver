"""
Narrative generation for budget optimization scenarios.

Phase 5: Human-readable, color-coded summaries with template-based narration.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from budget_solver.scenarios import Scenario, AccountAllocation


def mroas_state(mroas: float, min_mroas: float) -> tuple[str, str, str]:
    """
    Return (emoji, label, color_hex) for an mROAS value.

    Thresholds:
    - < min_mroas              → 🔴 "Below floor"      RED
    - < min_mroas + 1.5        → 🟡 "Monitor closely"   YELLOW
    - ≥ min_mroas + 1.5        → 🟢 "Healthy"          GREEN

    Args:
        mroas: Instantaneous or discrete mROAS value
        min_mroas: Minimum mROAS floor (typically 2.5x)

    Returns:
        Tuple of (emoji, label, color_hex)
    """
    if mroas < min_mroas:
        return ("🔴", "Below floor", "#FF4444")
    elif mroas < min_mroas + 1.5:
        return ("🟡", "Monitor closely", "#FFB800")
    else:
        return ("🟢", "Healthy", "#70AD47")


def account_callout(
    alloc: "AccountAllocation",
    min_mroas: float,
    prev_alloc: "AccountAllocation | None" = None
) -> str:
    """
    Generate a one-line narrated description of an account's state in a scenario.

    Example outputs:
    - "🟢 RP NL (€11,594/day): inst. mROAS 10.23x, discrete mROAS vs B = 10.64x. Strong return."
    - "🔴 LDL DACH (€5,528/day): inst. mROAS 1.06x — 58% below 2.5x floor. Value destruction."
    - "🟡 LDL BE (€1,580/day): inst. mROAS 3.24x. Monitor closely."

    Args:
        alloc: Account allocation for this scenario
        min_mroas: Minimum mROAS floor
        prev_alloc: Previous scenario allocation (for discrete mROAS context)

    Returns:
        One-line narrative string with emoji, metrics, and interpretation
    """
    emoji, state_label, _ = mroas_state(alloc.inst_mroas, min_mroas)

    # Format daily spend
    daily_spend = alloc.monthly_spend / 30.4  # DAYS_PER_MONTH
    spend_str = f"€{daily_spend:,.0f}/day"

    # Format inst. mROAS
    mroas_str = f"inst. mROAS {alloc.inst_mroas:.2f}x"

    # Add discrete mROAS if available and meaningful
    disc_mroas_str = ""
    if alloc.discrete_mroas_vs_prev is not None:
        disc_mroas_str = f", discrete mROAS vs prev = {alloc.discrete_mroas_vs_prev:.2f}x"

    # Build narrative suffix based on state
    if alloc.inst_mroas < min_mroas:
        # Below floor - emphasize the problem
        pct_below = (1 - alloc.inst_mroas / min_mroas) * 100
        narrative = f"{emoji} {alloc.account:20s} ({spend_str:15s}): {mroas_str} — {pct_below:.0f}% below {min_mroas:.1f}x floor{disc_mroas_str}. Value destruction on marginal spend."
    elif alloc.inst_mroas < min_mroas + 1.5:
        # Monitor zone - neutral tone
        narrative = f"{emoji} {alloc.account:20s} ({spend_str:15s}): {mroas_str}{disc_mroas_str}. Monitor closely."
    else:
        # Healthy - positive tone
        if alloc.discrete_mroas_vs_prev and alloc.discrete_mroas_vs_prev > min_mroas + 2:
            narrative = f"{emoji} {alloc.account:20s} ({spend_str:15s}): {mroas_str}{disc_mroas_str}. Strong incremental return."
        else:
            narrative = f"{emoji} {alloc.account:20s} ({spend_str:15s}): {mroas_str}{disc_mroas_str}. Healthy."

    return narrative


def scenario_summary(
    scenario: "Scenario",
    prev: "Scenario | None",
    min_mroas: float
) -> str:
    """
    Generate a 2-4 sentence summary of a scenario.

    Uses template-based generation with data-driven fills (no LLM).

    Args:
        scenario: The scenario to summarize
        prev: Previous scenario in sequence (for deltas)
        min_mroas: Minimum mROAS floor

    Returns:
        Multi-sentence narrative summary
    """
    from budget_solver.constants import DAYS_PER_MONTH

    # Detect scenario type and select template
    if scenario.id == "A":
        # Baseline scenario
        # Check for floor warnings
        floor_status = ""
        if scenario.warnings:
            n_warnings = len(scenario.warnings)
            floor_status = f" ⚠️ {n_warnings} account(s) below {min_mroas:.1f}x floor."
        else:
            floor_status = f" All accounts above {min_mroas:.1f}x floor."

        summary = (
            f"Current run rate based on last 7 days, extrapolated to monthly. "
            f"Portfolio spending €{scenario.budget_monthly:,.0f}/month "
            f"at {scenario.blended_roas:.2f}x blended ROAS.{floor_status}"
        )

    elif scenario.id == "B":
        # Proportional scaling
        if not prev:
            scale = 1.0
            change_pct = 0.0
        else:
            scale = scenario.budget_monthly / prev.budget_monthly if prev.budget_monthly > 0 else 1.0
            change_pct = (scale - 1.0) * 100

        direction = "increased" if scale > 1.0 else ("decreased" if scale < 1.0 else "unchanged")
        pdm_str = ""
        if scenario.portfolio_discrete_mroas:
            pdm_str = f" Portfolio discrete mROAS vs A: {scenario.portfolio_discrete_mroas:.2f}x."

        summary = (
            f"Target budget €{scenario.budget_monthly:,.0f}/month, allocated proportionally "
            f"across accounts (scale factor ×{scale:.4f}, {direction} {abs(change_pct):.1f}%). "
            f"Projected revenue €{scenario.revenue_monthly:,.0f}, blended ROAS {scenario.blended_roas:.2f}x.{pdm_str}"
        )

    elif scenario.id in ["C", "C1"]:
        # Recommended scenario

        # Check for no-reallocation edge case
        if hasattr(scenario, 'no_reallocation') and scenario.no_reallocation:
            return (
                "No reallocation required — all accounts are operating efficiently above the "
                f"{min_mroas:.1f}x mROAS floor at the target budget. Scenario C matches B exactly. "
                "Implement Scenario B targets directly."
            )

        is_c1 = scenario.id == "C1"

        # Count changes vs B (look at allocations)
        if prev:
            n_changes = sum(1 for a in scenario.allocations if abs(a.change_vs_prev) > 100)
        else:
            n_changes = 0

        # Extract binding constraint info from description
        binding_text = ""
        if "BUDGET CAP binds" in scenario.description:
            binding_text = "Budget cap binds — all accounts operating efficiently below floor."
        elif "BREAKEVEN binds" in scenario.description:
            binding_text = "Breakeven floor binds — portfolio ceiling reached."
        elif "MIXED binding" in scenario.description:
            binding_text = "Mixed binding — budget cap with some accounts at floor."

        # Calculate uplift vs B
        if prev:
            uplift = scenario.revenue_monthly - prev.revenue_monthly
            uplift_pct = (uplift / prev.revenue_monthly * 100) if prev.revenue_monthly > 0 else 0.0
            uplift_str = f"Revenue uplift vs B: {uplift:+,.0f} ({uplift_pct:+.1f}%)."
        else:
            uplift_str = ""

        if is_c1:
            summary = (
                f"Budget-neutral reallocation (Scenario C1). "
                f"{n_changes} account change(s) vs B. "
                f"{binding_text} {uplift_str}"
            )
        else:
            summary = (
                f"Recommended allocation. "
                f"{n_changes} account change(s) vs B. "
                f"{binding_text} {uplift_str}"
            )

    elif scenario.id == "D":
        # Max justified
        if prev:
            above_c = scenario.budget_monthly - prev.budget_monthly
            pdm_str = ""
            if scenario.portfolio_discrete_mroas:
                pdm_str = f" Portfolio discrete mROAS vs C: {scenario.portfolio_discrete_mroas:.2f}x."

            summary = (
                f"Theoretical maximum: all accounts at {min_mroas:.1f}x inst. mROAS floor. "
                f"Requires €{scenario.budget_monthly:,.0f}/month — "
                f"€{above_c:,.0f} above recommended. "
                f"Every euro below this level returns above {min_mroas:.1f}x.{pdm_str}"
            )
        else:
            summary = (
                f"Theoretical maximum: all accounts at {min_mroas:.1f}x inst. mROAS floor. "
                f"Requires €{scenario.budget_monthly:,.0f}/month."
            )

    else:
        # Fallback
        summary = f"Scenario {scenario.id}: €{scenario.budget_monthly:,.0f}/month, {scenario.blended_roas:.2f}x ROAS."

    return summary


def action_items(
    scenario: "Scenario",
    prev: "Scenario"
) -> list[str]:
    """
    Generate numbered action items for the recommended scenario.

    Includes:
    - Per-account daily cap adjustments
    - Phasing guidance for large moves
    - Monitoring recommendations for at-floor accounts

    Rule: One action item per account max. If account has both spend change
    and monitoring note, they are combined into a single item.

    Args:
        scenario: Recommended scenario (typically C)
        prev: Previous scenario (typically B)

    Returns:
        List of action item strings (numbered externally)
    """
    from budget_solver.constants import DAYS_PER_MONTH
    import re

    # Check for no-reallocation edge case
    if hasattr(scenario, 'no_reallocation') and scenario.no_reallocation:
        return ["No action required. Implement Scenario B daily caps as-is."]

    # Build action items dict (one per account)
    action_dict = {}

    # Build lookup dict for prev allocations
    prev_allocs = {a.account: a for a in prev.allocations}

    # First pass: add spend change actions for meaningful changes
    for alloc in scenario.allocations:
        prev_alloc = prev_allocs.get(alloc.account)
        if not prev_alloc:
            continue

        delta_monthly = alloc.monthly_spend - prev_alloc.monthly_spend

        # Only generate actions for meaningful changes (> €1000/month = ~€33/day)
        if abs(delta_monthly) < 1000:
            continue

        # Convert to daily
        current_daily = prev_alloc.monthly_spend / DAYS_PER_MONTH
        target_daily = alloc.monthly_spend / DAYS_PER_MONTH
        delta_daily = target_daily - current_daily

        # Format action
        if delta_daily > 0:
            action = f"{alloc.account}: increase daily cap from €{current_daily:,.0f} → €{target_daily:,.0f} (+€{abs(delta_daily):,.0f}/day)"
        else:
            action = f"{alloc.account}: decrease daily cap from €{current_daily:,.0f} → €{target_daily:,.0f} (−€{abs(delta_daily):,.0f}/day)"

        # Check for phasing warnings
        phasing_warning = next(
            (w for w in scenario.warnings if alloc.account in w and "exceeds" in w and "WoW cap" in w),
            None
        )

        if phasing_warning:
            # Extract week count from warning if possible
            if "week" in phasing_warning:
                match = re.search(r'over (\d+) weeks', phasing_warning)
                if match:
                    n_weeks = int(match.group(1))
                    action += f" — phase over {n_weeks} weeks"
                else:
                    action += " — phase gradually"
            else:
                action += " — phase gradually"

        action_dict[alloc.account] = action

    # Second pass: add or merge monitoring recommendations
    for alloc in scenario.allocations:
        if alloc.inst_mroas < 2.6:  # Within 0.1 of 2.5x floor
            monitoring_note = f"Monitor inst. mROAS weekly — currently at {alloc.inst_mroas:.2f}x floor."

            if alloc.account in action_dict:
                # Merge into existing action
                action_dict[alloc.account] += f"\n   {monitoring_note}"
            else:
                # Standalone monitoring item
                action_dict[alloc.account] = f"{alloc.account}: {monitoring_note}"

    # Return as ordered list (by account name for consistency)
    return [action_dict[acc] for acc in sorted(action_dict.keys())]


def full_scenario_narrative(
    scenario: "Scenario",
    prev: "Scenario | None",
    min_mroas: float
) -> str:
    """
    Complete narrative block: summary + per-account callouts + warnings + action items.

    Returns multi-line formatted string ready for console output.

    Args:
        scenario: The scenario to narrate
        prev: Previous scenario in sequence (for context)
        min_mroas: Minimum mROAS floor

    Returns:
        Multi-line narrative string
    """
    lines = []

    # Header: scenario ID and name
    recommended_mark = " ✅ RECOMMENDED" if scenario.recommended else ""
    lines.append(f"── SCENARIO {scenario.id}: {scenario.name}{recommended_mark} " + "─" * 60)
    lines.append("")

    # Portfolio headline (compact, single-line summary)
    import textwrap
    revenue_m = scenario.revenue_monthly / 1_000_000
    headline_parts = [
        f"Budget: €{scenario.budget_monthly:,.0f}/mo",
        f"Revenue: €{revenue_m:.2f}M/mo",
        f"Blended ROAS: {scenario.blended_roas:.2f}x"
    ]

    # Add discrete mROAS
    if scenario.portfolio_discrete_mroas is not None:
        headline_parts.append(f"Discrete mROAS vs prev: {scenario.portfolio_discrete_mroas:.2f}x")
    elif scenario.id in ["C", "C1"]:
        headline_parts.append(f"Discrete mROAS vs prev: n/a (budget-neutral)")

    # Add uplift if applicable
    if prev:
        uplift = scenario.revenue_monthly - prev.revenue_monthly
        uplift_pct = (uplift / prev.revenue_monthly * 100) if prev.revenue_monthly > 0 else 0.0
        if abs(uplift) > 1000:  # Only show if meaningful
            uplift_k = uplift / 1000
            headline_parts.append(f"Uplift vs prev: {uplift_k:+,.0f}k ({uplift_pct:+.1f}%)")

    lines.append(" | ".join(headline_parts))
    lines.append("")

    # Summary paragraph
    summary = scenario_summary(scenario, prev, min_mroas)
    wrapped_summary = textwrap.fill(summary, width=95, initial_indent="→ ", subsequent_indent="  ")
    lines.append(wrapped_summary)
    lines.append("")

    # Warnings (if any)
    if scenario.warnings:
        lines.append("⚠️  WARNINGS:")
        for warning in scenario.warnings:
            # Wrap warnings
            wrapped_warning = textwrap.fill(warning, width=95, initial_indent="  ", subsequent_indent="  ")
            lines.append(wrapped_warning)
        lines.append("")

    # Per-account callouts
    lines.append("Per-account:")
    for alloc in scenario.allocations:
        callout = account_callout(alloc, min_mroas)
        lines.append(f"  {callout}")
    lines.append("")

    # Action items (only for recommended scenario)
    if scenario.recommended and prev:
        items = action_items(scenario, prev)
        if items:
            lines.append("Action items:")
            for i, item in enumerate(items, 1):
                wrapped_item = textwrap.fill(item, width=95, initial_indent=f"  {i}. ", subsequent_indent="     ")
                lines.append(wrapped_item)
            lines.append("")

    return "\n".join(lines)
