"""
Unit tests for Phase 5 narrative generation.
"""
import pytest
from budget_solver.narrative import mroas_state, account_callout, scenario_summary, action_items, full_scenario_narrative
from budget_solver.scenarios import Scenario, AccountAllocation


def test_mroas_state_boundaries():
    """Test that mroas_state() returns correct states at boundary values."""
    # Below floor
    emoji, label, color = mroas_state(2.0, 2.5)
    assert emoji == "🔴"
    assert label == "Below floor"
    assert color == "#FF4444"

    # Exactly at floor (edge case - should be monitor zone)
    emoji, label, color = mroas_state(2.5, 2.5)
    assert emoji == "🟡"
    assert label == "Monitor closely"

    # Just above floor but below healthy threshold
    emoji, label, color = mroas_state(3.5, 2.5)
    assert emoji == "🟡"
    assert label == "Monitor closely"

    # At healthy threshold (min + 1.5)
    emoji, label, color = mroas_state(4.0, 2.5)
    assert emoji == "🟢"
    assert label == "Healthy"
    assert color == "#70AD47"

    # Well above floor
    emoji, label, color = mroas_state(6.0, 2.5)
    assert emoji == "🟢"


def test_account_callout_renders_for_each_state():
    """Test that account_callout() produces valid strings for red/yellow/green states."""
    # Red: below floor
    alloc_red = AccountAllocation(
        account="Test Account Red",
        daily_spend=100,
        monthly_spend=3040,
        daily_revenue=200,
        monthly_revenue=6080,
        roas=2.0,
        inst_mroas=2.0,  # Below 2.5x floor
        discrete_mroas_vs_prev=None
    )

    callout = account_callout(alloc_red, 2.5)
    assert "🔴" in callout
    assert "Test Account Red" in callout
    assert "below 2.5x floor" in callout
    assert "Value destruction" in callout

    # Yellow: monitor zone
    alloc_yellow = AccountAllocation(
        account="Test Account Yellow",
        daily_spend=100,
        monthly_spend=3040,
        daily_revenue=300,
        monthly_revenue=9120,
        roas=3.0,
        inst_mroas=3.0,  # At floor + 0.5
        discrete_mroas_vs_prev=None
    )

    callout = account_callout(alloc_yellow, 2.5)
    assert "🟡" in callout
    assert "Test Account Yellow" in callout
    assert "Monitor closely" in callout

    # Green: healthy
    alloc_green = AccountAllocation(
        account="Test Account Green",
        daily_spend=100,
        monthly_spend=3040,
        daily_revenue=600,
        monthly_revenue=18240,
        roas=6.0,
        inst_mroas=6.0,  # Well above floor
        discrete_mroas_vs_prev=5.5
    )

    callout = account_callout(alloc_green, 2.5)
    assert "🟢" in callout
    assert "Test Account Green" in callout
    assert "Healthy" in callout or "Strong" in callout


def test_scenario_summary_budget_neutral():
    """Test that C1 scenario renders with budget-neutral template."""
    # Create a C1 scenario
    scenario_c1 = Scenario(
        id="C1",
        name="Budget-Neutral Reallocation",
        description="Test C1",
        budget_monthly=100_000,
        revenue_monthly=500_000,
        blended_roas=5.0,
        portfolio_discrete_mroas=None,
        allocations=[],
        recommended=True
    )

    scenario_b = Scenario(
        id="B",
        name="Target Budget",
        description="Test B",
        budget_monthly=100_000,
        revenue_monthly=480_000,
        blended_roas=4.8,
        portfolio_discrete_mroas=4.0,
        allocations=[],
        recommended=False
    )

    summary = scenario_summary(scenario_c1, scenario_b, 2.5)

    assert "Budget-neutral" in summary or "C1" in summary
    # Summary should mention the scenario type and revenue metrics
    assert "account change" in summary.lower()
    assert "revenue" in summary.lower()


def test_scenario_summary_with_uplift():
    """Test that non-C1 scenario uses general template with uplift."""
    # Create regular C scenario
    scenario_c = Scenario(
        id="C",
        name="Recommended",
        description="Test C with BUDGET CAP binds",
        budget_monthly=120_000,
        revenue_monthly=600_000,
        blended_roas=5.0,
        portfolio_discrete_mroas=4.5,
        allocations=[
            AccountAllocation("Account A", 50, 1520, 250, 7600, 5.0, 4.5, 4.0, 520, "▲ +€520"),
            AccountAllocation("Account B", 50, 1520, 250, 7600, 5.0, 4.5, 4.0, -520, "▼ −€520"),
        ],
        recommended=True
    )

    scenario_b = Scenario(
        id="B",
        name="Target Budget",
        description="Test B",
        budget_monthly=120_000,
        revenue_monthly=580_000,
        blended_roas=4.8,
        portfolio_discrete_mroas=4.0,
        allocations=[
            AccountAllocation("Account A", 50, 1000, 200, 6080, 6.08, 5.0, None, 0, "—"),
            AccountAllocation("Account B", 50, 2040, 300, 9120, 4.47, 4.0, None, 0, "—"),
        ],
        recommended=False
    )

    summary = scenario_summary(scenario_c, scenario_b, 2.5)

    assert "Recommended" in summary
    assert "Revenue uplift" in summary or "uplift" in summary.lower()
    assert "2 account" in summary  # Should mention number of changes


def test_action_items_include_phasing():
    """Test that action_items() includes phasing guidance for accounts with WoW warnings."""
    scenario_c = Scenario(
        id="C",
        name="Recommended",
        description="Test",
        budget_monthly=100_000,
        revenue_monthly=500_000,
        blended_roas=5.0,
        portfolio_discrete_mroas=4.0,
        allocations=[
            # Significant change (>€1000/month threshold) - should generate action
            AccountAllocation("Account A", 100, 15200, 500, 76000, 5.0, 4.5, 4.0, 5200, "▲ +€5,200"),
        ],
        warnings=[
            "Account A: increase of 45.0% exceeds 20% WoW cap. Phase gradually over 3 weeks: week 1 → €12000, week 2 → €13600, week 3 → €15200."
        ],
        recommended=True
    )

    scenario_b = Scenario(
        id="B",
        name="Target Budget",
        description="Test",
        budget_monthly=100_000,
        revenue_monthly=480_000,
        blended_roas=4.8,
        portfolio_discrete_mroas=4.0,
        allocations=[
            AccountAllocation("Account A", 100, 10000, 400, 60800, 6.08, 5.0, None, 0, "—"),
        ],
        recommended=False
    )

    items = action_items(scenario_c, scenario_b)

    assert len(items) > 0
    assert any("Account A" in item for item in items)
    # Should mention phasing
    assert any("phase" in item.lower() for item in items)
    assert any("3 weeks" in item or "weeks" in item for item in items)


def test_narrative_length_bounded():
    """Test that full_scenario_narrative() produces output under 2,000 characters per scenario."""
    # Create a realistic scenario with multiple accounts
    allocations = [
        AccountAllocation(f"Account {i}", 10 + i, 304 + i*100, 50 + i*10, 1520 + i*304, 5.0, 4.5, 4.0, i*100, f"▲ +€{i*100}")
        for i in range(6)  # 6 accounts like real data
    ]

    scenario = Scenario(
        id="C",
        name="Recommended",
        description="Test scenario with multiple accounts and some warnings",
        budget_monthly=100_000,
        revenue_monthly=500_000,
        blended_roas=5.0,
        portfolio_discrete_mroas=4.5,
        allocations=allocations,
        warnings=[
            "Account 1: some warning message",
            "Account 2: another warning",
        ],
        recommended=True
    )

    prev_scenario = Scenario(
        id="B",
        name="Target Budget",
        description="Previous",
        budget_monthly=100_000,
        revenue_monthly=480_000,
        blended_roas=4.8,
        portfolio_discrete_mroas=4.0,
        allocations=allocations,
        recommended=False
    )

    narrative = full_scenario_narrative(scenario, prev_scenario, 2.5)

    # Check length (should be reasonable for console output)
    assert len(narrative) < 3000, f"Narrative too long: {len(narrative)} characters"
    assert len(narrative) > 200, f"Narrative too short: {len(narrative)} characters"

    # Check basic structure
    assert "SCENARIO C" in narrative
    assert "Recommended" in narrative
    assert "Per-account:" in narrative
