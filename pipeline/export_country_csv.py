"""
export_country_csv.py -- write one price_observations_<REGION>.csv per country/flow.

Reads price_observations from the store and splits it by `region` (e.g.
US_IMPORTS_ALL_ORIGINS, DE_IMPORTS, CN_EXPORTS), writing a separate CSV per
region into data/. Each file is deduped on (chemical_id, period) keeping the
most recent fetched_at -- the same dedup the Phase-2 calibration loader needs,
because the append-only store accumulates duplicate periods across re-pulls.

This is the "price_observations_country.csv for each country" deliverable. The
filename uses the region tag, so US import data, German imports, Chinese exports,
etc. each land in their own file:
    data/price_observations_US_IMPORTS_ALL_ORIGINS.csv
    data/price_observations_DE_IMPORTS.csv
    data/price_observations_CN_EXPORTS.csv

Run:
    python pipeline/export_country_csv.py
    python pipeline/export_country_csv.py --out-dir data --source COMTRADE
    python pipeline/export_country_csv.py --regions DE_IMPORTS CN_EXPORTS
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

try:
    from pipeline.storage import connect
except ModuleNotFoundError:
    from storage import connect

CSV_COLUMNS = ["chemical_id", "source", "region", "period", "price_usd_per_kg",
               "fetched_at", "hts_code", "grade", "assessment_type"]


def _safe_region_filename(region: str) -> str:
    """Region tag -> safe filename fragment (defensive; region tags are already
    underscore-cased, but a stray character shouldn't escape the data dir)."""
    import re
    return re.sub(r"[^A-Za-z0-9_]+", "_", region)


def fetch_deduped_by_region(conn, source: str | None = None,
                            regions: list[str] | None = None
                            ) -> dict[str, list[tuple]]:
    """{region: [rows]} deduped on (chemical_id, period) keeping latest fetched_at.

    DISTINCT ON does the dedup in-database. Optional source/regions filters narrow
    the export (e.g. only COMTRADE rows, or only specific countries).
    """
    where = []
    params: list = []
    if source:
        where.append("source = %s")
        params.append(source)
    if regions:
        where.append("region = ANY(%s)")
        params.append(regions)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    query = f"""
        SELECT DISTINCT ON (chemical_id, period, region)
            chemical_id, source, region, period, price_usd_per_kg,
            fetched_at, hts_code, grade, assessment_type
        FROM price_observations
        {where_sql}
        ORDER BY chemical_id, period, region, fetched_at DESC
    """
    cur = conn.cursor()
    cur.execute(query, params)
    rows = cur.fetchall()

    by_region: dict[str, list[tuple]] = {}
    for row in rows:
        region = row[2]
        by_region.setdefault(region, []).append(row)
    return by_region


def write_region_csvs(by_region: dict[str, list[tuple]], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for region, rows in sorted(by_region.items()):
        path = out_dir / f"price_observations_{_safe_region_filename(region)}.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_COLUMNS)
            # rows are already sorted by (chemical_id, period) from the query.
            writer.writerows(rows)
        clean = sum(1 for r in rows if r[4] is not None)
        print(f"  {path.name}: {len(rows)} rows ({clean} clean)")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export price_observations to one CSV per country/region.")
    parser.add_argument("--out-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "data")
    parser.add_argument("--source", default=None,
                        help="Filter to one source (e.g. COMTRADE, USITC).")
    parser.add_argument("--regions", nargs="+", default=None,
                        help="Filter to specific region tags (e.g. DE_IMPORTS CN_EXPORTS).")
    args = parser.parse_args(argv or sys.argv[1:])

    conn = connect()
    try:
        by_region = fetch_deduped_by_region(conn, source=args.source,
                                            regions=args.regions)
    finally:
        conn.close()

    if not by_region:
        print("No matching price_observations rows; nothing written.", file=sys.stderr)
        return 1

    print(f"Writing {len(by_region)} per-region CSV file(s) to {args.out_dir}...")
    write_region_csvs(by_region, args.out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
