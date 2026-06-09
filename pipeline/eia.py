"""
eia.py -- Monthly pull of utility cost signals from the EIA Open Data API (v2).

Pulls three utility price series that feed the should-cost engine's energy build-up:
  1. Industrial electricity price by state   (USD per kWh)
  2. Henry Hub natural gas spot price         (USD per MMBtu)
  3. Industrial natural gas price (US avg)    (USD per Mcf)

The EIA v2 API is clean JSON. Get a free key at https://www.eia.gov/opendata/
and expose it as the EIA_API_KEY environment variable before running.

Output contract -- every row conforms to `utility_observations` in
docs/schema/market_observations.schema.yaml and is source-tagged (source="EIA")
so the calibration layer can read evidence by source without code changes:

    {utility, source, region, period (YYYY-MM), price_usd_per_unit, fetched_at}

Storage is intentionally decoupled: this module produces normalized rows and hands
them to `write_utility_observations()`. That function is a thin seam owned by the
storage layer (pipeline/storage.py), which appends the rows to the
`utility_observations` table.

Run:  python pipeline/eia.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import requests

EIA_BASE = "https://api.eia.gov/v2"
SOURCE = "EIA"

# How many trailing months to request per series. Monthly refresh only needs the
# latest point, but a small window backfills gaps and is cheap.
DEFAULT_MONTHS = 24

# Series definitions. Each entry describes one EIA v2 query and how to normalize it
# into a utility_observations row. `region_field` names the response field that
# carries the region (per-state series); a literal `region` is used when the series
# is a single national number.
SERIES = {
    "electricity_industrial": {"unit": "kWh",
        "route": "electricity/retail-sales",
        "facets": {"sectorid": ["IND"]},
        "data_column": "price",       # EIA reports cents/kWh
        "frequency": "monthly",
        "region_field": "stateid",
        "scale": 0.01,                # cents -> USD per kWh
    },
    "natural_gas_henry_hub": {"unit": "MMBtu",
        "route": "natural-gas/pri/fut",
        # RNGWHHD is the Henry Hub spot series ($/MMBtu); frequency=monthly returns
        # its monthly average. (The old monthly-only id RNGWHHM is retired in v2.)
        "facets": {"series": ["RNGWHHD"]},
        "data_column": "value",
        "frequency": "monthly",
        "region": "US",
        "scale": 1.0,
    },
    "natural_gas_industrial": {"unit": "Mcf",
        "route": "natural-gas/pri/sum",
        "facets": {"series": ["N3035US3"]},  # US industrial price, monthly, $/Mcf
        "data_column": "value",
        "frequency": "monthly",
        "region": "US",
        "scale": 1.0,
    },
}


def _start_period(months: int) -> str:
    """EIA `start` filter as YYYY-MM, `months` before the current month."""
    now = datetime.now(timezone.utc)
    total = now.year * 12 + (now.month - 1) - months
    year, month = divmod(total, 12)
    return f"{year:04d}-{month + 1:02d}"


def fetch_series(api_key: str, utility: str, months: int = DEFAULT_MONTHS) -> list[dict]:
    """Fetch one EIA series and normalize it to utility_observations rows."""
    spec = SERIES[utility]
    params: list[tuple[str, str]] = [
        ("api_key", api_key),
        ("frequency", spec["frequency"]),
        ("data[0]", spec["data_column"]),
        ("start", _start_period(months)),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "desc"),
        ("length", "5000"),
    ]
    for facet, values in spec["facets"].items():
        for v in values:
            params.append((f"facets[{facet}][]", v))

    url = f"{EIA_BASE}/{spec['route']}/data/"
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    records = r.json()["response"]["data"]

    fetched_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    for rec in records:
        raw = rec.get(spec["data_column"])
        if raw is None or raw == "":
            continue  # EIA emits nulls for unreported months; skip them
        region = rec[spec["region_field"]] if "region_field" in spec else spec["region"]
        rows.append(
            {
                "utility": utility,
                "source": SOURCE,
                "unit": spec["unit"],
                "region": region,
                "period": rec["period"],  # EIA monthly periods are already YYYY-MM
                "price_usd_per_unit": round(float(raw) * spec["scale"], 6),
                "fetched_at": fetched_at,
            }
        )
    return rows


def pull_all(api_key: str, months: int = DEFAULT_MONTHS) -> list[dict]:
    rows: list[dict] = []
    for utility in SERIES:
        print(f"  pulling {utility}...")
        fetched = fetch_series(api_key, utility, months)
        print(f"    {len(fetched)} rows")
        rows.extend(fetched)
    return rows


def write_utility_observations(rows: list[dict]) -> None:
    """Persist normalized rows into the `utility_observations` table.

    Storage seam owned by the Step 1 storage layer (pipeline/storage.py). The row
    dict keys already match the table's columns, so this is a straight append-only
    INSERT -- no cleaning or dedup here (a calibration-phase concern).
    """
    import storage  # local import: keeps the connector importable without a DB

    inserted = storage.write_utility_observations(rows)
    print(f"Wrote {inserted} rows -> {storage.DB_PATH} (utility_observations)")


def main() -> int:
    api_key = os.environ.get("EIA_API_KEY")
    if not api_key:
        print(
            "ERROR: set EIA_API_KEY first (free key at https://www.eia.gov/opendata/).\n"
            "  PowerShell:  $env:EIA_API_KEY = 'your-key'\n"
            "  bash:        export EIA_API_KEY=your-key",
            file=sys.stderr,
        )
        return 1

    print("Pulling utility price signals from EIA Open Data API...")
    rows = pull_all(api_key)
    if not rows:
        print("WARNING: no rows returned; nothing written.", file=sys.stderr)
        return 1
    write_utility_observations(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
