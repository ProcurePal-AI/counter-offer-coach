"""
fx.py -- monthly FX rates for multi-currency connectors (CNY -> USD first).

Chinese-source price feeds (sunsirs.py and any future CN connector) quote
RMB/ton; the price_observations schema contract is price_usd_per_kg. Currency
conversion is a NORMALIZATION step inside connectors -- prices are converted at
ingest and stored in USD, with the raw native-currency values preserved in the
connector's raw files. No schema change: the store never holds RMB.

Source: ECB euro reference rates via the Frankfurter API
(https://api.frankfurter.dev) -- free, keyless, redistributable, and CNY is in
the ECB reference set. We request a date range with base=USD, symbols=CNY and
average the daily CNY-per-USD fixings within each month; the connector then
divides (RMB/ton) by (CNY per USD) to get USD/ton.

Monthly AVERAGE (not month-end) on purpose: the trade/assessment prices being
converted are themselves monthly averages, so an average-rate conversion is the
consistent pairing.

Raw API responses are cached under data/raw/fx/ (git-ignored) so a backfill
doesn't refetch and the conversion is reproducible/auditable.

Usage from a connector:
    from fx import monthly_cny_per_usd
    rates = monthly_cny_per_usd("2016-01", "2026-06")   # {"2016-01": 6.57, ...}
    usd_per_ton = rmb_per_ton / rates[period]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

FRANKFURTER_BASE = "https://api.frankfurter.dev/v1"


class FxUnavailable(LookupError):
    """No FX rate available for the requested period."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _raw_dir() -> Path:
    return _repo_root() / "data" / "raw" / "fx"


def _cache_path(start_period: str, end_period: str) -> Path:
    return _raw_dir() / f"usd_cny_{start_period}_{end_period}.json"


def _period_bounds(start_period: str, end_period: str) -> tuple[str, str]:
    """('2016-01','2026-06') -> ('2016-01-01','2026-06-30'-ish ISO dates)."""
    start = f"{start_period}-01"
    year, month = (int(x) for x in end_period.split("-"))
    # Last day shortcut: first day of next month minus nothing fancy -- the API
    # accepts any in-month end date and returns fixings up to it; using day 28
    # is always valid and loses at most 2-3 daily fixings of an average.
    end = f"{year:04d}-{month:02d}-28"
    return start, end


def fetch_daily_usd_cny(start_period: str, end_period: str,
                        use_cache: bool = True) -> dict:
    """Raw Frankfurter response for the window (daily CNY-per-USD fixings).

    Cached on disk so repeated backfills are reproducible and offline-friendly.
    """
    cache = _cache_path(start_period, end_period)
    if use_cache and cache.exists():
        with cache.open() as f:
            return json.load(f)

    start_date, end_date = _period_bounds(start_period, end_period)
    url = f"{FRANKFURTER_BASE}/{start_date}..{end_date}"
    response = requests.get(url, params={"base": "USD", "symbols": "CNY"},
                            timeout=60)
    response.raise_for_status()
    payload = response.json()

    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    return payload


def monthly_average_from_daily(payload: dict) -> dict[str, float]:
    """Frankfurter daily-rates payload -> {'YYYY-MM': mean CNY per USD}.

    Pure function (offline-testable). Days without a CNY fixing are skipped;
    a month with zero fixings is simply absent from the result.
    """
    rates = payload.get("rates") or {}
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for date_str, day_rates in rates.items():
        cny = day_rates.get("CNY")
        if cny is None:
            continue
        period = date_str[:7]  # YYYY-MM-DD -> YYYY-MM
        sums[period] = sums.get(period, 0.0) + float(cny)
        counts[period] = counts.get(period, 0) + 1
    return {p: round(sums[p] / counts[p], 6) for p in sorted(sums)}


def monthly_cny_per_usd(start_period: str, end_period: str,
                        use_cache: bool = True) -> dict[str, float]:
    """{'YYYY-MM': average CNY per USD} for the window (the connector entrypoint)."""
    return monthly_average_from_daily(
        fetch_daily_usd_cny(start_period, end_period, use_cache=use_cache)
    )


def rmb_per_ton_to_usd_per_kg(rmb_per_ton: float, cny_per_usd: float) -> float:
    """RMB/metric-ton -> USD/kg (the conversion every CN connector applies).

    USD/ton = RMB/ton / (CNY per USD); USD/kg = USD/ton / 1000.
    """
    if cny_per_usd <= 0:
        raise FxUnavailable(f"non-positive FX rate: {cny_per_usd}")
    return round(rmb_per_ton / cny_per_usd / 1000.0, 6)


def main() -> int:
    """CLI smoke: print the last 12 monthly averages."""
    now = datetime.now(timezone.utc)
    end = f"{now.year:04d}-{now.month:02d}"
    start_total = now.year * 12 + (now.month - 1) - 11
    sy, sm = divmod(start_total, 12)
    start = f"{sy:04d}-{sm + 1:02d}"
    print(f"USD/CNY monthly averages, {start}..{end} (ECB via Frankfurter):")
    for period, rate in monthly_cny_per_usd(start, end).items():
        print(f"  {period}  {rate:.4f} CNY per USD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
