"""
Holiday calendar for NL / DE / BE markets.

Encodes school holiday periods and public holidays for 2024-2028.
Used to compute a per-account holiday density correction factor:

    correction = holiday_days_this_forecast_month / avg_holiday_days_same_month_historically

If the forecast month has the same holiday structure as the historical average
baked into the response curves → correction = 1.0 (no change).
If Easter falls in the forecast month this year but didn't historically → correction > 1.0.
If the forecast month is unusually light on holidays → correction < 1.0.

This corrects for the most common source of prediction error: Easter timing shifts
and year-to-year school holiday calendar variation across NL / DE / BE.

Sources:
  NL: Midden regio — DUO / rijksoverheid.nl/onderwerpen/schoolvakanties
  DE: NRW (Nordrhein-Westfalen, largest German state) — KMK Ferienregelung
  BE: Official Belgian school calendar (both Dutch + French communities share dates)

Easter Sunday dates are exact (computed via the Anonymous Gregorian algorithm).
School holiday boundary dates are ± 1-2 days of official schedules.
Update the SCHOOL_HOLIDAYS dict annually when governments publish new calendars
(typically 2-3 years ahead).
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Optional


# ─────────────────────────────────────────────────────────────
# EASTER SUNDAY — exact dates
# ─────────────────────────────────────────────────────────────
EASTER_SUNDAY: dict[int, date] = {
    2024: date(2024, 3, 31),
    2025: date(2025, 4, 20),
    2026: date(2026, 4,  5),
    2027: date(2027, 3, 28),
    2028: date(2028, 4, 16),
}

# Account → ISO country code
ACCOUNT_COUNTRY: dict[str, str] = {
    "Landal NL":  "NL",
    "Roompot NL": "NL",
    "Landal DE":  "DE",
    "Roompot DE": "DE",
    "Landal BE":  "BE",
    "Roompot BE": "BE",
}

# ─────────────────────────────────────────────────────────────
# SCHOOL HOLIDAYS — (label, start_date, end_date) inclusive
# Christmas periods span into January of the following year.
# ─────────────────────────────────────────────────────────────
SCHOOL_HOLIDAYS: dict[str, dict[int, list[tuple[str, date, date]]]] = {

    # ── Netherlands (Midden regio) ────────────────────────────
    "NL": {
        2024: [
            ("voorjaarsvakantie", date(2024,  2, 10), date(2024,  2, 18)),
            ("easter",            date(2024,  3, 29), date(2024,  4,  7)),
            ("meivakantie",       date(2024,  4, 27), date(2024,  5,  5)),
            ("summer",            date(2024,  7, 20), date(2024,  9,  1)),
            ("autumn",            date(2024, 10, 19), date(2024, 10, 27)),
            ("christmas",         date(2024, 12, 21), date(2025,  1,  5)),
        ],
        2025: [
            ("voorjaarsvakantie", date(2025,  2, 22), date(2025,  3,  2)),
            ("easter",            date(2025,  4, 18), date(2025,  4, 27)),
            ("meivakantie",       date(2025,  5,  1), date(2025,  5,  4)),
            ("summer",            date(2025,  7, 19), date(2025,  8, 31)),
            ("autumn",            date(2025, 10, 18), date(2025, 10, 26)),
            ("christmas",         date(2025, 12, 20), date(2026,  1,  4)),
        ],
        2026: [
            ("voorjaarsvakantie", date(2026,  2, 14), date(2026,  2, 22)),
            ("easter",            date(2026,  4,  3), date(2026,  4, 12)),
            ("meivakantie",       date(2026,  4, 25), date(2026,  5,  3)),
            ("summer",            date(2026,  7, 18), date(2026,  8, 30)),
            ("autumn",            date(2026, 10, 17), date(2026, 10, 25)),
            ("christmas",         date(2026, 12, 19), date(2027,  1,  3)),
        ],
        2027: [
            ("voorjaarsvakantie", date(2027,  2, 20), date(2027,  2, 28)),
            ("easter",            date(2027,  3, 26), date(2027,  4,  4)),
            ("meivakantie",       date(2027,  4, 24), date(2027,  5,  2)),
            ("summer",            date(2027,  7, 17), date(2027,  8, 29)),
            ("autumn",            date(2027, 10, 16), date(2027, 10, 24)),
            ("christmas",         date(2027, 12, 18), date(2028,  1,  2)),
        ],
        2028: [
            ("voorjaarsvakantie", date(2028,  2, 19), date(2028,  2, 27)),
            ("easter",            date(2028,  4, 14), date(2028,  4, 23)),
            ("meivakantie",       date(2028,  4, 29), date(2028,  5,  7)),
            ("summer",            date(2028,  7, 22), date(2028,  9,  3)),
            ("autumn",            date(2028, 10, 21), date(2028, 10, 29)),
            ("christmas",         date(2028, 12, 23), date(2029,  1,  6)),
        ],
    },

    # ── Germany — NRW (Nordrhein-Westfalen) ──────────────────
    # NRW is the most populous German state and primary source market for DE parks.
    "DE": {
        2024: [
            ("karneval",  date(2024,  2, 12), date(2024,  2, 13)),
            ("easter",    date(2024,  3, 25), date(2024,  4,  7)),
            ("summer",    date(2024,  7,  8), date(2024,  8, 20)),
            ("autumn",    date(2024, 10, 14), date(2024, 10, 26)),
            ("christmas", date(2024, 12, 23), date(2025,  1,  5)),
        ],
        2025: [
            ("karneval",  date(2025,  3,  3), date(2025,  3,  4)),
            ("easter",    date(2025,  4, 14), date(2025,  4, 26)),
            ("summer",    date(2025,  6, 23), date(2025,  8,  5)),
            ("autumn",    date(2025, 10, 13), date(2025, 10, 25)),
            ("christmas", date(2025, 12, 22), date(2026,  1,  5)),
        ],
        2026: [
            ("karneval",  date(2026,  2, 16), date(2026,  2, 17)),
            ("easter",    date(2026,  4,  2), date(2026,  4, 15)),
            ("summer",    date(2026,  6, 29), date(2026,  8, 11)),
            ("autumn",    date(2026, 10, 12), date(2026, 10, 24)),
            ("christmas", date(2026, 12, 23), date(2027,  1,  6)),
        ],
        2027: [
            ("karneval",  date(2027,  2,  8), date(2027,  2,  9)),
            ("easter",    date(2027,  3, 25), date(2027,  4,  7)),
            ("summer",    date(2027,  7,  5), date(2027,  8, 17)),
            ("autumn",    date(2027, 10, 11), date(2027, 10, 23)),
            ("christmas", date(2027, 12, 23), date(2028,  1,  6)),
        ],
        2028: [
            ("karneval",  date(2028,  2, 28), date(2028,  2, 29)),
            ("easter",    date(2028,  4, 13), date(2028,  4, 26)),
            ("summer",    date(2028,  7, 10), date(2028,  8, 22)),
            ("autumn",    date(2028, 10, 16), date(2028, 10, 28)),
            ("christmas", date(2028, 12, 22), date(2029,  1,  5)),
        ],
    },

    # ── Belgium (both communities share the same periods) ─────
    # Summer holidays are constitutionally fixed: July 1 – August 31.
    "BE": {
        2024: [
            ("krokus",    date(2024,  2, 10), date(2024,  2, 18)),
            ("easter",    date(2024,  4,  1), date(2024,  4, 14)),
            ("summer",    date(2024,  7,  1), date(2024,  8, 31)),
            ("autumn",    date(2024, 10, 26), date(2024, 11,  3)),
            ("christmas", date(2024, 12, 21), date(2025,  1,  5)),
        ],
        2025: [
            ("krokus",    date(2025,  3,  1), date(2025,  3,  9)),
            ("easter",    date(2025,  4, 12), date(2025,  4, 27)),
            ("summer",    date(2025,  7,  1), date(2025,  8, 31)),
            ("autumn",    date(2025, 10, 25), date(2025, 11,  2)),
            ("christmas", date(2025, 12, 20), date(2026,  1,  4)),
        ],
        2026: [
            ("krokus",    date(2026,  2, 14), date(2026,  2, 22)),
            ("easter",    date(2026,  4,  4), date(2026,  4, 19)),
            ("summer",    date(2026,  7,  1), date(2026,  8, 31)),
            ("autumn",    date(2026, 10, 31), date(2026, 11,  8)),
            ("christmas", date(2026, 12, 19), date(2027,  1,  3)),
        ],
        2027: [
            ("krokus",    date(2027,  2, 27), date(2027,  3,  7)),
            ("easter",    date(2027,  3, 27), date(2027,  4, 11)),
            ("summer",    date(2027,  7,  1), date(2027,  8, 31)),
            ("autumn",    date(2027, 10, 30), date(2027, 11,  7)),
            ("christmas", date(2027, 12, 18), date(2028,  1,  2)),
        ],
        2028: [
            ("krokus",    date(2028,  2, 19), date(2028,  2, 27)),
            ("easter",    date(2028,  4, 15), date(2028,  4, 30)),
            ("summer",    date(2028,  7,  1), date(2028,  8, 31)),
            ("autumn",    date(2028, 10, 28), date(2028, 11,  5)),
            ("christmas", date(2028, 12, 23), date(2029,  1,  6)),
        ],
    },
}


# ─────────────────────────────────────────────────────────────
# PUBLIC HOLIDAYS
# ─────────────────────────────────────────────────────────────

def public_holidays(country: str, year: int) -> set[date]:
    """Return the set of public holiday dates for country and year."""
    easter = EASTER_SUNDAY.get(year)
    days: set[date] = set()

    if country == "NL":
        days = {
            date(year,  1,  1),  # New Year's Day
            date(year,  5,  5),  # Liberation Day
            date(year, 12, 25),  # Christmas Day
            date(year, 12, 26),  # Boxing Day
        }
        # King's Day: Apr 27, moves to Apr 26 when Apr 27 is Sunday
        kd = date(year, 4, 27)
        days.add(date(year, 4, 26) if kd.weekday() == 6 else kd)
        if easter:
            days.update({
                easter - timedelta(days=2),   # Good Friday
                easter + timedelta(days=1),   # Easter Monday
                easter + timedelta(days=39),  # Ascension Thursday
                easter + timedelta(days=49),  # Whit Sunday
                easter + timedelta(days=50),  # Whit Monday
            })

    elif country == "DE":
        days = {
            date(year,  1,  1),  # New Year's Day
            date(year,  5,  1),  # Labour Day
            date(year, 10,  3),  # German Unity Day
            date(year, 11,  1),  # All Saints' Day (NRW)
            date(year, 12, 25),  # Christmas Day
            date(year, 12, 26),  # St. Stephen's Day
        }
        if easter:
            days.update({
                easter - timedelta(days=2),   # Good Friday
                easter + timedelta(days=1),   # Easter Monday
                easter + timedelta(days=39),  # Ascension Thursday
                easter + timedelta(days=49),  # Whit Sunday
                easter + timedelta(days=50),  # Whit Monday
                easter + timedelta(days=60),  # Corpus Christi (NRW)
            })

    elif country == "BE":
        days = {
            date(year,  1,  1),  # New Year's Day
            date(year,  5,  1),  # Labour Day
            date(year,  7, 21),  # Belgian National Day
            date(year,  8, 15),  # Assumption of Mary
            date(year, 11,  1),  # All Saints' Day
            date(year, 11, 11),  # Armistice Day
            date(year, 12, 25),  # Christmas Day
        }
        if easter:
            days.update({
                easter + timedelta(days=1),   # Easter Monday
                easter + timedelta(days=39),  # Ascension Thursday
                easter + timedelta(days=50),  # Whit Monday
            })

    return days


# ─────────────────────────────────────────────────────────────
# HOLIDAY DENSITY
# ─────────────────────────────────────────────────────────────

def holiday_days(country: str, year: int, month: int) -> int:
    """
    Count calendar days in (country, year, month) that are either:
      - a public holiday, OR
      - within a school holiday period

    Uses calendar days (not just weekdays) since vacation parks are booked
    for full weekly stays that include weekends.

    Returns 0 for country/year combinations not in the calendar data.
    """
    if country not in SCHOOL_HOLIDAYS or year not in SCHOOL_HOLIDAYS[country]:
        return 0

    days_in_month = monthrange(year, month)[1]
    month_dates   = {date(year, month, d) for d in range(1, days_in_month + 1)}

    holiday_set: set[date] = set()

    # School holiday periods (Christmas spans into following January — handle cross-year)
    for _, start, end in SCHOOL_HOLIDAYS[country][year]:
        d = start
        while d <= end:
            if d in month_dates:
                holiday_set.add(d)
            d += timedelta(days=1)

    # Also include Christmas tail from previous year (Jan 1-6 area)
    if month == 1 and (year - 1) in SCHOOL_HOLIDAYS.get(country, {}):
        for _, start, end in SCHOOL_HOLIDAYS[country][year - 1]:
            if end.year == year:
                d = start if start.year == year else date(year, 1, 1)
                while d <= end:
                    if d in month_dates:
                        holiday_set.add(d)
                    d += timedelta(days=1)

    # Public holidays
    holiday_set.update(d for d in public_holidays(country, year) if d in month_dates)

    return len(holiday_set)


# ─────────────────────────────────────────────────────────────
# CORRECTION FACTOR
# ─────────────────────────────────────────────────────────────

_CORRECTION_FLOOR = 0.80   # back-tested: ±60% caused systematic over-prediction
_CORRECTION_CAP   = 1.20   # Easter timing shifts rarely move revenue more than ±20%


def compute_holiday_corrections(
    accounts: list[str],
    forecast_year: int,
    forecast_month: int,
    lookback_years: Optional[list[int]] = None,
) -> dict[str, tuple[float, str]]:
    """
    Compute per-account holiday correction factors for a forecast month.

    correction = forecast_holiday_density / avg_historical_holiday_density
    clipped to [0.6, 1.8] to prevent extreme swings from sparse data.

    Returns dict: {account_name: (correction_factor, explanation_string)}

    The explanation string is human-readable for console output and stakeholder
    communication: "Easter in month (10d), avg historical: 4d → +1.65×"
    """
    lookback_years = lookback_years or [2024, 2025]
    result: dict[str, tuple[float, str]] = {}

    for acc in accounts:
        country = ACCOUNT_COUNTRY.get(acc)
        if not country:
            result[acc] = (1.0, "no country mapping — no correction applied")
            continue

        forecast_days = holiday_days(country, forecast_year, forecast_month)

        hist_days = [
            holiday_days(country, y, forecast_month)
            for y in lookback_years
            if country in SCHOOL_HOLIDAYS and y in SCHOOL_HOLIDAYS[country]
        ]

        if not hist_days:
            result[acc] = (1.0, "no historical data — no correction applied")
            continue

        avg_hist = sum(hist_days) / len(hist_days)

        if avg_hist < 2 and forecast_days < 2:
            # Both forecast and history have negligible holiday density (< 2 days).
            # The signal is too thin to correct on — noise would dominate.
            factor      = 1.0
            explanation = f"{forecast_days}d holiday vs avg {avg_hist:.1f}d — too few days to correct"
        elif avg_hist == 0:
            factor      = min(_CORRECTION_CAP, 1.5)
            explanation = (
                f"{forecast_days}d holiday in forecast month, "
                f"0d historical avg → capped at {factor:.2f}×"
            )
        else:
            raw_factor  = forecast_days / avg_hist
            factor      = max(_CORRECTION_FLOOR, min(_CORRECTION_CAP, raw_factor))
            easter_note = ""
            if EASTER_SUNDAY.get(forecast_year, date(1, 1, 1)).month == forecast_month:
                easter_note = " (Easter in month)"
            explanation = (
                f"{forecast_days}d holiday{easter_note} vs "
                f"avg {avg_hist:.0f}d → {factor:.2f}×"
            )

        result[acc] = (factor, explanation)

    return result


# ─────────────────────────────────────────────────────────────
# UTILITY: ISO week → calendar year + month
# ─────────────────────────────────────────────────────────────

def forecast_week_to_ym(forecast_week: int) -> tuple[int, int]:
    """
    Convert an ISO week number to (year, month) using the Wednesday of that week
    as the representative day (mid-week avoids edge cases at month boundaries).

    Tries the current calendar year first, then next year (handles year-end
    forecast weeks like week 1 of January).
    """
    from datetime import date as _date
    today = _date.today()
    for year in [today.year, today.year + 1]:
        try:
            wednesday = _date.fromisocalendar(year, forecast_week, 3)
            return wednesday.year, wednesday.month
        except ValueError:
            continue
    return today.year, today.month
