"""
Google Trends demand index builder.

Fetches weekly search interest for vacation-related keywords per market (NL/DE/BE)
using the pytrends library (unofficial Google Trends API). Builds an ISO-week
demand index (week 1-53 → multiplier, mean=1.0) to replace the internally-derived
demand index in build_demand_index().

Why external? The internal index is computed from median weekly ROAS, which is
shaped by the team's own spend decisions — a circular dependency. External search
volume is exogenous: it reflects actual consumer demand independent of ad spend.

Fallback: if pytrends is unavailable or rate-limited, returns None and the caller
falls back to the internal demand index.
"""
from __future__ import annotations

import time
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


# ── Market keyword definitions ────────────────────────────────────────────────
# Core generic vacation search terms per market (language + locale specific).
# These represent the generic consideration layer — no brand names.
MARKET_KEYWORDS: dict[str, list[str]] = {
    "NL": ["vakantiepark", "vakantie nederland", "bungalowpark", "vakantiehuis huren"],
    "DE": ["ferienhaus", "ferienpark", "ferienwohnung", "ferienhaus mieten"],
    "BE": ["vakantiepark", "bungalowpark belgie", "vakantie belgie", "vakantiehuisje"],
}

# Geo codes for pytrends (ISO 3166-1 alpha-2)
MARKET_GEO: dict[str, str] = {
    "NL": "NL",
    "DE": "DE",
    "BE": "BE",
}

# Cache file: stored next to the data output so it refreshes with each pull
_CACHE_PATH = Path("output/trends_demand_index.csv")

# Rate limit: sleep between pytrends calls to avoid 429s
_SLEEP_BETWEEN_MARKETS = 5  # seconds


def _try_import_pytrends():
    """Return pytrends TrendReq class or None if not installed."""
    try:
        from pytrends.request import TrendReq  # type: ignore
        return TrendReq
    except ImportError:
        return None


def _fetch_trends_for_market(TrendReq, market: str, keywords: list[str], geo: str) -> pd.Series | None:
    """
    Fetch weekly search interest for a market's keywords and return
    an ISO-week indexed Series of average normalised interest (0-100 scale → normalised).

    Returns None on failure.
    """
    try:
        pytrends = TrendReq(hl="nl-NL" if market == "NL" else ("de-DE" if market == "DE" else "nl-BE"),
                            tz=60, timeout=(10, 25), retries=2, backoff_factor=0.5)
        # Pull 5 years of weekly data for stable seasonal pattern
        pytrends.build_payload(keywords[:5], cat=0, timeframe="today 5-y", geo=geo)
        df = pytrends.interest_over_time()
        if df.empty:
            return None
        df = df.drop(columns=["isPartial"], errors="ignore")
        # Average across keywords (handles missing terms gracefully)
        series = df.mean(axis=1)
        return series
    except Exception as exc:
        warnings.warn(f"pytrends fetch failed for market {market}: {exc}")
        return None


def _series_to_iso_week_index(series: pd.Series) -> dict[int, float]:
    """
    Convert a datetime-indexed weekly interest Series to an ISO-week demand index.

    Method: compute median interest per ISO week number (1-53) across all years,
    then normalise to mean=1.0. Missing weeks filled with 1.0.
    """
    df = series.reset_index()
    df.columns = ["date", "interest"]
    df["date"] = pd.to_datetime(df["date"])
    df["iso_week"] = df["date"].dt.isocalendar().week.astype(int)

    weekly_median = df.groupby("iso_week")["interest"].median()

    # Fill any missing ISO weeks (1-53) with the overall median
    all_weeks = pd.RangeIndex(1, 54)
    weekly_median = weekly_median.reindex(all_weeks, fill_value=weekly_median.median())

    mean_val = weekly_median.mean()
    if mean_val <= 0:
        return {w: 1.0 for w in range(1, 54)}

    normalised = (weekly_median / mean_val).to_dict()
    return {int(k): float(v) for k, v in normalised.items()}


def _blend_market_indices(market_indices: dict[str, dict[int, float]]) -> dict[int, float]:
    """
    Blend per-market demand indices into a single portfolio index.

    Uses equal weighting across markets. The portfolio index is what the
    solver uses when --forecast-week or --forecast-month is resolved.
    """
    if not market_indices:
        return {}

    all_weeks = range(1, 54)
    blended = {}
    for w in all_weeks:
        vals = [idx.get(w, 1.0) for idx in market_indices.values()]
        blended[w] = float(np.mean(vals))

    # Re-normalise blended index to mean=1.0
    mean_val = np.mean(list(blended.values()))
    if mean_val > 0:
        blended = {w: v / mean_val for w, v in blended.items()}

    return blended


def build_trends_demand_index(
    markets: list[str] | None = None,
    cache_path: Path | None = None,
    force_refresh: bool = False,
) -> dict[int, float] | None:
    """
    Build a demand index from Google Trends data for the given markets.

    Args:
        markets: List of market codes to fetch (e.g. ['NL', 'DE', 'BE']).
                 Defaults to all three markets.
        cache_path: Where to read/write the cached index CSV.
                    Defaults to output/trends_demand_index.csv.
        force_refresh: If True, ignore cached data and re-fetch.

    Returns:
        Dict {iso_week: multiplier} with mean=1.0, or None if fetch failed.

    On success, writes result to cache_path so subsequent runs don't re-fetch.
    On failure, returns None — caller should fall back to internal demand index.
    """
    markets = markets or list(MARKET_KEYWORDS.keys())
    cache_path = cache_path or _CACHE_PATH

    # ── Try cache first ───────────────────────────────────────────────────────
    if not force_refresh and cache_path.exists():
        try:
            cached = pd.read_csv(cache_path)
            cached.columns = [c.lower().strip() for c in cached.columns]
            if {"week", "index"}.issubset(cached.columns):
                index = dict(zip(cached["week"].astype(int), cached["index"].astype(float)))
                print(f"  Demand index: loaded from cache ({cache_path})")
                return index
        except Exception:
            pass  # Cache corrupt — re-fetch

    # ── Try pytrends ─────────────────────────────────────────────────────────
    TrendReq = _try_import_pytrends()
    if TrendReq is None:
        warnings.warn(
            "pytrends not installed — using internal demand index. "
            "Install with: pip install pytrends"
        )
        return None

    print(f"  Demand index: fetching Google Trends for markets {markets}...")
    market_indices: dict[str, dict[int, float]] = {}

    for i, market in enumerate(markets):
        if market not in MARKET_KEYWORDS:
            continue
        keywords = MARKET_KEYWORDS[market]
        geo = MARKET_GEO[market]

        if i > 0:
            time.sleep(_SLEEP_BETWEEN_MARKETS)

        series = _fetch_trends_for_market(TrendReq, market, keywords, geo)
        if series is not None:
            idx = _series_to_iso_week_index(series)
            market_indices[market] = idx
            print(f"    {market}: fetched {len(series)} weeks of data")
        else:
            print(f"    {market}: fetch failed, skipping")

    if not market_indices:
        warnings.warn("All Google Trends fetches failed — using internal demand index.")
        return None

    blended = _blend_market_indices(market_indices)

    # ── Cache result ─────────────────────────────────────────────────────────
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_df = pd.DataFrame([
            {"week": w, "index": v} for w, v in sorted(blended.items())
        ])
        cache_df.to_csv(cache_path, index=False)
        print(f"  Demand index: cached to {cache_path}")
    except Exception as exc:
        warnings.warn(f"Could not cache trends index: {exc}")

    return blended
