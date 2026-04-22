"""
Constants used throughout the budget solver.
"""
from pathlib import Path

# Data file path (expected output from budget-solver-pull)
DATA_PATH = Path("output/core_markets.csv")

# Time window for trailing actual performance (days)
TRAILING_WINDOW_DAYS = 30

# Monthly conversion factors
DAYS_PER_MONTH = 30.4             # Primary: daily actuals → monthly totals (daily × 30.4)
WEEKS_PER_MONTH = DAYS_PER_MONTH / 7  # Derived: weekly curves → monthly (~4.343, not 4.33)
                                      # Ensures consistency when weekly-fitted curves meet monthly allocations

# Zero threshold for display (values below this are shown as 0)
REPORTING_ZERO_EPSILON = 0.5

# Scenario detection thresholds
BUDGET_NEUTRAL_THRESHOLD = 0.05   # ±5% budget change → label as C1 (budget-neutral)
MIN_CHANGE_FOR_LABEL_EUR = 100    # Minimum €100 change to show ▲/▼ label
BUDGET_SLACK_EPSILON = 1000       # €1k slack threshold for binding constraint detection

# Curve fitting bounds
POWER_B_MIN = 0.01                # Minimum exponent for power curve
POWER_B_MAX = 0.99                # Maximum exponent for power curve

# Google Ads API micros conversion
MICROS_PER_UNIT = 1_000_000

# Excel color palette
NAV = '1F3864'   # Dark navy
BLUE = '2E75B6'   # Accent blue
GRN = '70AD47'   # Green (positive)
RED = 'FF4444'   # Red (negative)
LGRY = 'F5F5F5'   # Light grey row
WHIT = 'FFFFFF'
LBLU = 'EBF3FB'   # Light blue alternating row
