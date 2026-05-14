"""
Auction Insights competitive pressure analysis.

Pulls two 30-day windows of auction insight data per account (trailing and prior)
and computes per-competitor impression share deltas. A competitor surging
> +10pp in the trailing window is a structural signal that CPCs and CVR will
be under pressure — the response curves, fitted on historical data, cannot
see this.

Used as a risk flag only: no math changes to predict_fns. The output surfaces
in the console alongside the recommended allocation so the analyst can override
or apply manual caution before sharing with stakeholders.

GAQL note: auction_insight_index aggregates across the date range automatically —
the API returns one row per domain per account for the requested period. No
per-day data is available.

Output: output/auction_insights.csv
Columns: account_name, domain, trailing_is, prior_is, is_delta,
         overlap_rate, outranking_share
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

OUTPUT_INSIGHTS_CSV = Path("output/auction_insights.csv")

_SURGE_THRESHOLD = 0.10     # IS delta above this → competitive pressure flag
_TOP_N           = 5        # competitors to retain per account per window


def pull_auction_insights(
    client,
    account_id: str,
    account_name: str,
    trailing_start: str,
    trailing_end: str,
    prior_start: str,
    prior_end: str,
) -> list[dict]:
    """
    Pull auction insight metrics for two date windows and return merged rows.

    Each returned dict has:
      account_name, domain, trailing_is, prior_is, is_delta,
      overlap_rate, outranking_share
    """
    ga_service = client.get_service("GoogleAdsService")

    def _query_window(start: str, end: str) -> dict[str, dict]:
        """Return {domain: {is, overlap, outranking}} for one window."""
        query = f"""
            SELECT
                auction_insight_index.domain,
                metrics.auction_insight_search_impression_share,
                metrics.auction_insight_search_overlap_rate,
                metrics.auction_insight_search_outranking_share
            FROM auction_insight_index
            WHERE segments.date BETWEEN '{start}' AND '{end}'
              AND campaign.advertising_channel_type = 'SEARCH'
              AND campaign.name NOT LIKE '%| BR%'
              AND campaign.name NOT LIKE '%| PK%'
        """
        results: dict[str, dict] = {}
        try:
            response = ga_service.search(customer_id=account_id, query=query)
            for row in response:
                domain = row.auction_insight_index.domain
                if not domain:
                    continue
                results[domain] = {
                    "is":         _safe(row.metrics.auction_insight_search_impression_share),
                    "overlap":    _safe(row.metrics.auction_insight_search_overlap_rate),
                    "outranking": _safe(row.metrics.auction_insight_search_outranking_share),
                }
        except Exception as exc:
            print(f"  WARNING: auction insights unavailable for {account_name} — {exc.__class__.__name__}")
        return results

    trailing = _query_window(trailing_start, trailing_end)
    prior    = _query_window(prior_start,    prior_end)

    # All domains seen in either window
    all_domains = set(trailing) | set(prior)
    rows = []
    for domain in all_domains:
        t = trailing.get(domain, {})
        p = prior.get(domain, {})
        trailing_is = t.get("is", 0.0)
        prior_is    = p.get("is", 0.0)
        rows.append({
            "account_name":    account_name,
            "domain":          domain,
            "trailing_is":     trailing_is,
            "prior_is":        prior_is,
            "is_delta":        trailing_is - prior_is,
            "overlap_rate":    t.get("overlap",    p.get("overlap",    0.0)),
            "outranking_share": t.get("outranking", p.get("outranking", 0.0)),
        })

    # Keep top-N by trailing IS (most relevant competitors)
    rows.sort(key=lambda r: r["trailing_is"], reverse=True)
    return rows[:_TOP_N]


def pull_all_auction_insights(
    client,
    account_map: dict[str, str],
    lookback_days: int = 30,
) -> pd.DataFrame:
    """
    Pull auction insights for all accounts and return a combined DataFrame.

    account_map: {account_name: account_id}
    """
    today = datetime.today()

    trailing_end   = today.strftime("%Y-%m-%d")
    trailing_start = (today - timedelta(days=lookback_days - 1)).strftime("%Y-%m-%d")
    prior_end      = (today - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    prior_start    = (today - timedelta(days=lookback_days * 2 - 1)).strftime("%Y-%m-%d")

    print(f"  Trailing window : {trailing_start} → {trailing_end}")
    print(f"  Prior window    : {prior_start} → {prior_end}")

    all_rows = []
    for acc_name, acc_id in account_map.items():
        print(f"  Pulling auction insights: {acc_name} ...", end=" ", flush=True)
        rows = pull_auction_insights(
            client, acc_id, acc_name,
            trailing_start, trailing_end,
            prior_start, prior_end,
        )
        all_rows.extend(rows)
        n_surge = sum(1 for r in rows if r["is_delta"] > _SURGE_THRESHOLD)
        print(f"{len(rows)} competitors{f'  ⚠ {n_surge} surging' if n_surge else ''}")

    return pd.DataFrame(all_rows) if all_rows else pd.DataFrame(
        columns=["account_name", "domain", "trailing_is", "prior_is",
                 "is_delta", "overlap_rate", "outranking_share"]
    )


def load_auction_insights(path: Path = OUTPUT_INSIGHTS_CSV) -> pd.DataFrame:
    """Load saved auction insights CSV. Returns empty DataFrame if not found."""
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def format_insights_table(df: pd.DataFrame) -> str:
    """
    Format auction insights as a console table grouped by account.
    Returns the full string ready for print().
    """
    if df.empty:
        return ""

    lines = []
    lines.append("── COMPETITIVE LANDSCAPE (auction insights, trailing 30d vs prior 30d) ──────")
    lines.append(
        f"  {'Account':<30} {'Competitor':<35} {'Trailing IS':>11} {'Prior IS':>9} {'Δ':>7}"
    )
    lines.append("  " + "─" * 95)

    for acc in sorted(df["account_name"].unique()):
        acc_rows = df[df["account_name"] == acc].sort_values("trailing_is", ascending=False)
        for i, row in enumerate(acc_rows.itertuples()):
            surge = row.is_delta > _SURGE_THRESHOLD
            drop  = row.is_delta < -_SURGE_THRESHOLD
            flag  = "  ⚠ SURGE" if surge else ("  ↓ drop" if drop else "")
            delta_str = f"{row.is_delta:+.0%}"
            acc_label = acc if i == 0 else ""
            lines.append(
                f"  {acc_label:<30} {row.domain:<35} "
                f"{row.trailing_is:>9.0%}  {row.prior_is:>7.0%}  {delta_str:>7}{flag}"
            )
        lines.append("")

    return "\n".join(lines)


def _safe(val) -> float:
    """Convert proto float to Python float, returning 0.0 for sentinel 100% IS values."""
    try:
        v = float(val)
        return v if v < 9.9 else 0.0   # Google uses ~9.99 as 'unknown/insufficient data'
    except (TypeError, ValueError):
        return 0.0
