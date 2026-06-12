"""
sunsirs.py -- licence-gated connector for SunSirs China spot assessments.

STATUS: INGEST-ONLY UNTIL LICENSED. SunSirs is a commercial assessor; its terms
do not permit scraping or building derived products on unlicensed data, and a
time-limited trial cannot be the foundation (the calibrated model would embed
the data past the trial's permitted use). This connector therefore has NO
scraping path and NO speculative API client. What it does today:

  * ingest a LICENSED data export (the CSV/XLSX-style file SunSirs delivers to
    subscribers, re-saved as CSV) via --licensed-file, and
  * normalize it into the standard price_observations schema: monthly average,
    RMB/ton -> USD/kg via fx.py (ECB monthly-average rates), region "CN_SPOT",
    assessment_type "spot", grade passed through when the export carries one.

When the team signs a subscription WITH DERIVED-USE RIGHTS IN WRITING (the
right to store the data and use it as an input to a commercial model), the API
fetch path gets implemented against their real API docs in fetch_api() below --
it currently raises with exactly that instruction rather than guessing at an
undocumented schema.

Expected licensed-file columns (case-insensitive; extra columns ignored):
  date          -- YYYY-MM-DD (daily) or YYYY-MM (already monthly)
  commodity     -- must match a key in CHEMICAL_BY_COMMODITY (e.g. "aniline")
  price         -- RMB per metric ton
  grade         -- optional product grade/spec
If SunSirs' actual export uses different headers, adjust COLUMN_ALIASES --
that is the ONLY place the file format is interpreted.

Run examples:
  python pipeline/sunsirs.py --licensed-file data/licensed/sunsirs_aniline.csv --dry-run
  python pipeline/sunsirs.py --licensed-file ... --write
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from pipeline.storage import write_price_observations
    from pipeline.fx import monthly_cny_per_usd, rmb_per_ton_to_usd_per_kg
except ModuleNotFoundError:  # `python pipeline/sunsirs.py` from the repo root
    from storage import write_price_observations
    from fx import monthly_cny_per_usd, rmb_per_ton_to_usd_per_kg

SOURCE = "SUNSIRS"
REGION = "CN_SPOT"  # foreign-family tag; see engine/prices.py region_family()

# SunSirs commodity name (lowercased) -> our chemical_id.
CHEMICAL_BY_COMMODITY = {
    "aniline": "aniline",
    "benzene": "benzene",
    "pure benzene": "benzene",
    "ammonia": "ammonia",
    "liquid ammonia": "ammonia",
    # Energy feed for the CN hydrogen derivation (engine/prices.py, Step 4).
    # Not a registry chemical (mixture, no CAS/CID) -- like natural gas on the
    # US side, it enters as an energy commodity. Confirm the series grade vs
    # the 23.0 GJ/t (5500 kcal NAR) assumption in config/hydrogen_cn.yaml.
    "thermal coal": "thermal_coal",
    "steam coal": "thermal_coal",
    "power coal": "thermal_coal",
    "动力煤": "thermal_coal",
}

# Column-header aliases (lowercased) -> canonical field.
COLUMN_ALIASES = {
    "date": "date", "day": "date", "period": "date",
    "commodity": "commodity", "product": "commodity", "name": "commodity",
    "price": "price_rmb_per_ton", "price_rmb": "price_rmb_per_ton",
    "price_rmb_per_ton": "price_rmb_per_ton", "rmb/ton": "price_rmb_per_ton",
    "grade": "grade", "spec": "grade", "specification": "grade",
}


class LicenceRequired(RuntimeError):
    """Raised by any path that would touch SunSirs data without a licence."""


def fetch_api(*_args: Any, **_kwargs: Any) -> None:
    """Placeholder for the post-licence API client.

    Deliberately unimplemented: SunSirs' API schema is only available to
    subscribers, and writing a speculative client would invite running it
    against unlicensed access. Implement against their real API docs once the
    subscription (with derived-use rights in writing) is signed.
    """
    raise LicenceRequired(
        "SunSirs API access requires a signed subscription with derived-use "
        "rights. Until then, use --licensed-file with a data export delivered "
        "under licence. Do not scrape."
    )


def _canonical_row(raw_row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in raw_row.items():
        canon = COLUMN_ALIASES.get(str(key).strip().casefold())
        if canon and value not in (None, ""):
            out[canon] = value
    return out


def _period_of(date_text: str) -> str | None:
    text = str(date_text).strip()
    if len(text) >= 7 and text[4] == "-":
        return text[:7]  # YYYY-MM or YYYY-MM-DD
    return None


def read_licensed_file(path: Path) -> list[dict[str, Any]]:
    """Parse a licensed CSV export into canonical raw records (no math yet)."""
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for raw in csv.DictReader(f):
            row = _canonical_row(raw)
            if "date" not in row or "commodity" not in row:
                continue
            commodity = str(row["commodity"]).strip().casefold()
            chemical_id = CHEMICAL_BY_COMMODITY.get(commodity)
            period = _period_of(row["date"])
            try:
                price = float(str(row.get("price_rmb_per_ton", "")).replace(",", ""))
            except ValueError:
                price = None
            if chemical_id is None or period is None or price is None or price <= 0:
                continue
            records.append({
                "chemical_id": chemical_id,
                "period": period,
                "price_rmb_per_ton": price,
                "grade": str(row["grade"]).strip() if "grade" in row else None,
            })
    return records


RateFn = Callable[[str], float]  # period -> CNY per USD

# Chinese published commodity prices (SunSirs included) are VAT-inclusive; the
# engine's cost basis is ex-VAT because input VAT is recoverable for producers
# and is not a real production cost. GLOBAL INVARIANT for every SunSirs series:
# strip VAT in RMB first, THEN convert FX (order is recorded in
# docs/CHINA_TRACK.md and config/hydrogen_cn.yaml). Standard rate for goods
# is 13% (since Apr 2019). Pass vat_rate=0.0 only if a series is documented
# as already ex-VAT.
CN_VAT_RATE = 0.13


def normalize_rows(records: list[dict[str, Any]], rate_fn: RateFn,
                   fetched_at: str | None = None,
                   vat_rate: float = CN_VAT_RATE) -> list[dict[str, Any]]:
    """Daily/periodic licensed records -> monthly schema rows in USD/kg, ex-VAT.

    Per (chemical, period, grade): average the RMB/ton observations within the
    month, strip VAT (divide by 1+vat_rate, in RMB), then convert with that
    month's average FX rate. A period whose FX rate is unavailable is SKIPPED
    LOUDLY (printed), never guessed.
    """
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    groups: dict[tuple, list[float]] = {}
    for rec in records:
        key = (rec["chemical_id"], rec["period"], rec.get("grade"))
        groups.setdefault(key, []).append(rec["price_rmb_per_ton"])

    rows: list[dict[str, Any]] = []
    for (chemical_id, period, grade) in sorted(groups, key=lambda k: (k[0], k[1])):
        rmb_avg = sum(groups[(chemical_id, period, grade)]) / len(
            groups[(chemical_id, period, grade)])
        rmb_ex_vat = rmb_avg / (1.0 + vat_rate)  # strip VAT in RMB, then FX
        try:
            cny_per_usd = rate_fn(period)
        except (KeyError, LookupError):
            print(f"  SKIP {chemical_id} {period}: no FX rate for period",
                  file=sys.stderr)
            continue
        rows.append({
            "chemical_id": chemical_id,
            "source": SOURCE,
            "region": REGION,
            "period": period,
            "price_usd_per_kg": rmb_per_ton_to_usd_per_kg(rmb_ex_vat, cny_per_usd),
            "fetched_at": fetched_at,
            "hts_code": None,            # assessment feed, not a tariff line
            "grade": grade,
            "assessment_type": "spot",   # SunSirs publishes spot assessments
        })
    return rows


def _live_rate_fn(periods: list[str]) -> RateFn:
    start, end = min(periods), max(periods)
    rates = monthly_cny_per_usd(start, end)
    return lambda period: rates[period]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest a LICENSED SunSirs data export (no scraping path exists).")
    parser.add_argument("--licensed-file", type=Path, required=True,
                        help="CSV export delivered under a SunSirs subscription.")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--write", action="store_false", dest="dry_run")
    args = parser.parse_args(argv or sys.argv[1:])

    if not args.licensed_file.exists():
        print(f"ERROR: {args.licensed_file} not found. This connector only "
              f"ingests data exports delivered under a SunSirs licence.",
              file=sys.stderr)
        return 1

    records = read_licensed_file(args.licensed_file)
    if not records:
        print("ERROR: no usable records (check COLUMN_ALIASES against the "
              "export's actual headers).", file=sys.stderr)
        return 1
    print(f"  {len(records)} licensed records parsed")

    periods = [r["period"] for r in records]
    rows = normalize_rows(records, _live_rate_fn(periods))
    clean = sum(r["price_usd_per_kg"] is not None for r in rows)
    print(f"  {len(rows)} monthly rows normalized ({clean} clean), region={REGION}")

    write_price_observations(rows, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
