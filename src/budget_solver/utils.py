"""
Pure utility functions (parsing, formatting, date resolution).
"""
import sys
from datetime import datetime

import pandas as pd


def parse_kv_arg(s):
    """Parse 'Account A:500,Account B:1000' → {'Account A': 500.0, 'Account B': 1000.0}"""
    if not s:
        return {}
    result = {}
    for part in s.split(','):
        if ':' not in part:
            continue
        k, v = part.rsplit(':', 1)
        result[k.strip()] = float(v.strip())
    return result


def resolve_forecast_period(df, forecast_month=None, forecast_week=None):
    """
    Resolve the demand-scaling forecast period.

    Priority:
      1. Explicit --forecast-week
      2. Explicit --forecast-month (YYYY-MM), converted to its midpoint ISO week
      3. Inferred next calendar month after the latest input date
      4. Fallback to the current calendar month if no parseable dates exist

    Returns (forecast_week, forecast_label, forecast_source)
    """
    if forecast_week is not None:
        if not 1 <= forecast_week <= 53:
            sys.exit('ERROR: --forecast-week must be between 1 and 53.')
        return forecast_week, f'ISO Week {forecast_week:02d}', 'user-specified ISO week'

    if forecast_month:
        try:
            month_start = pd.Timestamp(f'{forecast_month}-01')
        except ValueError:
            sys.exit('ERROR: --forecast-month must be in YYYY-MM format.')
        forecast_source = f'user-specified month {forecast_month}'
    else:
        date_col = next((c for c in ('date', 'week_start', 'week') if c in df.columns), None)
        parsed_dates = pd.to_datetime(df[date_col], errors='coerce').dropna() if date_col else pd.Series(dtype='datetime64[ns]')
        if not parsed_dates.empty:
            latest_date = parsed_dates.max().normalize()
            month_start = (latest_date + pd.offsets.MonthBegin(1)).normalize()
            forecast_source = f'inferred from latest data date {latest_date.date()}'
        else:
            today = pd.Timestamp(datetime.today().date())
            month_start = today.replace(day=1)
            forecast_source = f'fallback to current month {month_start.strftime("%Y-%m")}'

    month_end = month_start + pd.offsets.MonthEnd(0)
    midpoint = month_start + pd.Timedelta(days=(month_end.day - 1) // 2)
    forecast_week = int(midpoint.isocalendar().week)
    forecast_label = midpoint.strftime('%b %Y')
    return forecast_week, forecast_label, forecast_source
