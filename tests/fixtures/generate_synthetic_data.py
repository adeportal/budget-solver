#!/usr/bin/env python3
"""
Generate deterministic synthetic test data for golden-file testing.
Uses fixed random seed for reproducibility.
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# Fixed seed for reproducibility
np.random.seed(42)

accounts = [
    "Landal NL",
    "Landal DE",
    "Landal BE",
    "Roompot NL",
    "Roompot DE",
    "Roompot BE",
]

# Generate 10 weeks of data
start_date = datetime(2024, 1, 1)
dates = [start_date + timedelta(days=7*i) for i in range(10)]

rows = []
for account in accounts:
    # Each account has different base characteristics
    base_spend = np.random.uniform(5000, 15000)
    base_roas = np.random.uniform(2.0, 6.0)

    for week_idx, date in enumerate(dates):
        # Add some weekly variation (±20%)
        spend_variation = np.random.uniform(0.8, 1.2)
        roas_variation = np.random.uniform(0.85, 1.15)

        # Seasonal trend (higher ROAS in earlier weeks)
        seasonal_factor = 1.0 + (10 - week_idx) * 0.03

        weekly_spend = base_spend * spend_variation
        weekly_roas = base_roas * roas_variation * seasonal_factor
        weekly_revenue = weekly_spend * weekly_roas

        rows.append({
            'account_name': account,
            'date': date.strftime('%Y-%m-%d'),
            'cost': round(weekly_spend, 2),
            'conversion_value': round(weekly_revenue, 2),
            'currency': 'EUR',
            'clicks': int(weekly_spend / 2),  # ~€2 CPC
            'impressions': int(weekly_spend / 2 * 50),  # ~2% CTR
        })

df = pd.DataFrame(rows)
df = df.sort_values(['account_name', 'date'])
df.to_csv('tests/fixtures/synthetic_10weeks.csv', index=False)

print(f"Generated {len(df)} rows")
print(f"Accounts: {df['account_name'].nunique()}")
print(f"Date range: {df['date'].min()} to {df['date'].max()}")
print("\nPer-account summary:")
summary = df.groupby('account_name').agg({
    'cost': 'sum',
    'conversion_value': 'sum',
}).reset_index()
summary['roas'] = summary['conversion_value'] / summary['cost']
print(summary.to_string(index=False))
