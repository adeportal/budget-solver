"""
Weather-based demand correction using Open-Meteo (free, no API key).

Compares forecast-month sunshine hours to the historical average (ERA5 archive)
for the same calendar month. Returns a per-country multiplier, capped [0.80, 1.20].

Sunshine duration is the primary metric for outdoor leisure (vacation parks):
it correlates with temperature, absence of rain, and booking impulse intent.
A month forecast to have 20% more sunshine than average gets a ×1.20 uplift;
a gloomier month gets a ×0.80 floor.

Only activates within a 30-day lookahead window. Beyond that window, weather
forecasts are unreliable and we return 1.0 (no correction). For months that are
partially elapsed, past days come from the ERA5 archive and future days from the
Open-Meteo forecast API (16-day horizon); days beyond the forecast horizon are
filled with the historical daily average so the full month is always estimated.

Falls back to (1.0, reason) gracefully on any network error or missing data.
"""
import calendar
import datetime
import json
import urllib.parse
import urllib.request
from datetime import date

COUNTRY_COORDS = {
    "NL": (52.10, 5.18),   # De Bilt — KNMI reference station
    "DE": (52.37, 9.73),   # Hannover — central for Landal DE parks
    "BE": (50.85, 4.35),   # Brussels
}

ACCOUNT_COUNTRY = {
    "Landal NL":  "NL",
    "Roompot NL": "NL",
    "Landal DE":  "DE",
    "Roompot DE": "DE",
    "Landal BE":  "BE",
    "Roompot BE": "BE",
}

_FORECAST_URL     = "https://api.open-meteo.com/v1/forecast"
_ARCHIVE_URL      = "https://archive-api.open-meteo.com/v1/archive"
_TIMEOUT          = 10       # seconds per HTTP request
_HIST_YEARS       = [2023, 2024, 2025]
_MULT_LO          = 0.90   # back-tested: ±20% caused systematic over-prediction
_MULT_HI          = 1.10   # sunshine hours correlate with bookings but effect is modest
_LOOKAHEAD_DAYS   = 30       # skip correction if month starts more than this far ahead
_ARCHIVE_LAG_DAYS = 2        # ERA5 archive typically lags ~2 days behind today


def _fetch_sunshine(url: str, lat: float, lon: float, start: str, end: str) -> list[float]:
    """
    Fetch daily sunshine_duration (seconds) for a date range from Open-Meteo.
    Returns a list of floats (one per day). Raises on HTTP / parse errors.
    """
    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "daily": "sunshine_duration",
        "start_date": start,
        "end_date": end,
        "timezone": "UTC",
    })
    req = urllib.request.Request(
        f"{url}?{params}",
        headers={"User-Agent": "budget-solver/1.0"},
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read())
    return [float(v) if v is not None else 0.0
            for v in data.get("daily", {}).get("sunshine_duration", [])]


def compute_weather_multipliers(
    accounts: list[str],
    forecast_year: int,
    forecast_month: int,
    today: date | None = None,
) -> dict[str, tuple[float, str]]:
    """
    Return {account: (multiplier, explanation)} for each account.

    multiplier = forecast_sunshine_total / historical_avg_sunshine_total
    historical avg  = mean over _HIST_YEARS for the same calendar month.

    The multiplier is capped to [_MULT_LO, _MULT_HI] before being returned.
    """
    today = today or date.today()

    days_in_month = calendar.monthrange(forecast_year, forecast_month)[1]
    month_start   = date(forecast_year, forecast_month, 1)
    month_end     = date(forecast_year, forecast_month, days_in_month)

    # Only activate within the useful lookahead window
    days_to_start = (month_start - today).days
    if days_to_start > _LOOKAHEAD_DAYS:
        reason = f'outside {_LOOKAHEAD_DAYS}d lookahead — no weather adjustment'
        return {acc: (1.0, reason) for acc in accounts}

    needed_countries = {ACCOUNT_COUNTRY[acc] for acc in accounts if acc in ACCOUNT_COUNTRY}
    country_results: dict[str, tuple[float, str]] = {}

    for country in needed_countries:
        lat, lon = COUNTRY_COORDS[country]
        try:
            # ── Historical average (same month, past years) ───────
            hist_totals: list[float] = []
            for yr in _HIST_YEARS:
                h_days  = calendar.monthrange(yr, forecast_month)[1]
                h_start = date(yr, forecast_month, 1).isoformat()
                h_end   = date(yr, forecast_month, h_days).isoformat()
                vals    = _fetch_sunshine(_ARCHIVE_URL, lat, lon, h_start, h_end)
                if vals:
                    hist_totals.append(sum(vals))

            if not hist_totals or sum(hist_totals) == 0:
                country_results[country] = (1.0, 'no historical baseline — skipped')
                continue

            hist_avg           = sum(hist_totals) / len(hist_totals)
            hist_daily_avg     = hist_avg / days_in_month

            # ── Current forecast for the target month ─────────────
            # Past days (archive) + upcoming days (forecast API) +
            # days beyond forecast horizon (filled with hist daily avg)
            archive_end    = min(today - datetime.timedelta(days=_ARCHIVE_LAG_DAYS), month_end)
            forecast_start = archive_end + datetime.timedelta(days=1)

            forecast_total = 0.0

            # Past elapsed days from archive
            if month_start <= archive_end:
                past_vals = _fetch_sunshine(
                    _ARCHIVE_URL, lat, lon,
                    month_start.isoformat(), archive_end.isoformat(),
                )
                forecast_total += sum(past_vals)

            # Remaining days: try forecast API, fill remainder with historical avg
            if forecast_start <= month_end:
                try:
                    future_vals = _fetch_sunshine(
                        _FORECAST_URL, lat, lon,
                        forecast_start.isoformat(), month_end.isoformat(),
                    )
                    forecast_total += sum(future_vals)
                    covered_end = month_end
                except Exception:
                    # Forecast API can't reach that far — use archive where available,
                    # fill the rest with the historical daily average
                    covered_end = forecast_start - datetime.timedelta(days=1)

                days_uncovered = (month_end - covered_end).days
                if days_uncovered > 0:
                    forecast_total += hist_daily_avg * days_uncovered

            # ── Compute and cap multiplier ────────────────────────
            raw_mult  = forecast_total / hist_avg if hist_avg > 0 else 1.0
            capped    = float(max(_MULT_LO, min(_MULT_HI, raw_mult)))
            pct       = (capped - 1.0) * 100
            direction = f'+{pct:.0f}%' if pct >= 0 else f'{pct:.0f}%'
            explanation = (
                f'sunshine {forecast_total / 3600:.0f}h vs hist avg {hist_avg / 3600:.0f}h'
                f' → {direction}'
            )
            country_results[country] = (capped, explanation)

        except Exception as exc:
            country_results[country] = (1.0, f'API error ({type(exc).__name__}) — no adjustment')

    # Map country results back to individual accounts
    out: dict[str, tuple[float, str]] = {}
    for acc in accounts:
        country = ACCOUNT_COUNTRY.get(acc)
        if country and country in country_results:
            out[acc] = country_results[country]
        else:
            out[acc] = (1.0, 'country not mapped')
    return out
