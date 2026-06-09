"""
usitc.py -- Monthly USITC DataWeb trade benchmark connector.

Part 2.3: pulls monthly import benchmark observations for the aniline demo chain:
  1. Benzene  HTS 2902.20.00, queried/stored as 29022000
  2. Aniline  HTS 2921.41.20, queried/stored as 29214120

The connector follows the EIA pattern: fetch raw source data, normalize into
schema-aligned dictionaries, then hand rows to a writer seam. Raw DataWeb responses
are preserved under data/raw/usitc/ for audit/debugging. The data/ directory is
git-ignored.

Unit values are derived locally as customs value / first unit quantity in kg.
Rows with missing value, missing quantity, zero quantity, or unknown units are retained with
price_usd_per_kg=None; downstream calibration owns outlier, low-volume, and holdout
logic.

Run examples:
  python pipeline/usitc.py --dry-run
  USITC_API_TOKEN=... python pipeline/usitc.py --dry-run
  USITC_API_TOKEN=... python pipeline/usitc.py --write
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    from pipeline.storage import write_price_observations
except ModuleNotFoundError:  # Allows `python pipeline/usitc.py` from the repo root.
    from storage import write_price_observations

USITC_BASE = "https://datawebws.usitc.gov/dataweb"
SOURCE = "USITC"
DEFAULT_MONTHS = 122

CHEMICALS = {
    "benzene": {"display_hts_code": "2902.20.00", "hts_code": "29022000"},
    "aniline": {"display_hts_code": "2921.41.20", "hts_code": "29214120"},
}

DATA_TO_REPORT = [
    "CONS_CUSTOMS_VALUE",  # imports for consumption customs value
    "CONS_FIR_UNIT_QUANT",  # first unit quantity; kg for the target HTS lines
]

MONTHS = {
    "january": "01",
    "february": "02",
    "march": "03",
    "april": "04",
    "may": "05",
    "june": "06",
    "july": "07",
    "august": "08",
    "september": "09",
    "october": "10",
    "november": "11",
    "december": "12",
}

# DataWeb reports benzene first-unit quantity as liters. The model calibration
# expects USD/kg, so convert liters to kg with a fixed liquid-density assumption.
LITERS_TO_KG = {
    "benzene": 0.8765,
}

BASIC_QUERY: dict[str, Any] = {
    "savedQueryName": "",
    "savedQueryDesc": "",
    "isOwner": True,
    "runMonthly": False,
    "reportOptions": {"tradeType": "Import", "classificationSystem": "HTS"},
    "searchOptions": {
        "MiscGroup": {
            "districts": {
                "aggregation": "Aggregate District",
                "districtGroups": {"userGroups": []},
                "districts": [],
                "districtsExpanded": [{"name": "All Districts", "value": "all"}],
                "districtsSelectType": "all",
            },
            "importPrograms": {
                "aggregation": None,
                "importPrograms": [],
                "programsSelectType": "all",
            },
            "extImportPrograms": {
                "aggregation": "Aggregate CSC",
                "extImportPrograms": [],
                "extImportProgramsExpanded": [],
                "programsSelectType": "all",
            },
            "provisionCodes": {
                "aggregation": "Aggregate RPCODE",
                "provisionCodesSelectType": "all",
                "rateProvisionCodes": [],
                "rateProvisionCodesExpanded": [],
            },
        },
        "commodities": {
            "aggregation": "Aggregate Commodities",
            "codeDisplayFormat": "YES",
            "commodities": [],
            "commoditiesExpanded": [],
            "commoditiesManual": "",
            "commodityGroups": {"systemGroups": [], "userGroups": []},
            "commoditySelectType": "list",
            "granularity": "8",
            "groupGranularity": None,
            "searchGranularity": "8",
        },
        "componentSettings": {
            "dataToReport": DATA_TO_REPORT,
            "scale": "1",
            "timeframeSelectType": "specificDateRange",
            "years": [],
            "startDate": None,
            "endDate": None,
            "startMonth": None,
            "endMonth": None,
            "yearsTimeline": "Monthly",
        },
        "countries": {
            "aggregation": "Aggregate Countries",
            "countries": [],
            "countriesExpanded": [{"name": "All Countries", "value": "all"}],
            "countriesSelectType": "all",
            "countryGroups": {"systemGroups": [], "userGroups": []},
        },
    },
    "sortingAndDataFormat": {
        "DataSort": {"columnOrder": [], "fullColumnOrder": [], "sortOrder": []},
        "reportCustomizations": {
            "exportCombineTables": False,
            "showAllSubtotal": True,
            "subtotalRecords": "",
            "totalRecords": "20000",
            "exportRawData": False,
        },
    },
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _date_range(months: int) -> tuple[str, str]:
    """Return DataWeb date strings as MM/YYYY for the trailing month window."""
    now = datetime.now(timezone.utc)
    end_total = now.year * 12 + (now.month - 1)
    start_total = end_total - months
    start_year, start_month = divmod(start_total, 12)
    end_year, end_month = divmod(end_total, 12)
    return f"{start_month + 1:02d}/{start_year:04d}", f"{end_month + 1:02d}/{end_year:04d}"


def build_query(hts_code: str, months: int = DEFAULT_MONTHS) -> dict[str, Any]:
    """Build a minimal official DataWeb runReport payload for one HTS code."""
    query = copy.deepcopy(BASIC_QUERY)
    start_date, end_date = _date_range(months)
    normalized_hts = normalize_hts_code(hts_code)
    commodities = query["searchOptions"]["commodities"]
    commodities["commodities"] = [normalized_hts]
    commodities["commoditiesExpanded"] = [{"name": normalized_hts, "value": normalized_hts}]
    commodities["commoditiesManual"] = normalized_hts
    settings = query["searchOptions"]["componentSettings"]
    settings["startDate"] = start_date
    settings["endDate"] = end_date
    return query


def normalize_hts_code(hts_code: str) -> str:
    return re.sub(r"[^0-9]", "", hts_code)


def _headers(token: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {token}",
    }


def _raw_path(chemical_id: str, hts_code: str, raw_dir: Path | None = None) -> Path:
    raw_dir = raw_dir or _repo_root() / "data" / "raw" / "usitc"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return raw_dir / f"{timestamp}_{chemical_id}_{hts_code}_runReport.json"


def preserve_raw_response(
    response_json: dict[str, Any],
    chemical_id: str,
    hts_code: str,
    raw_dir: Path | None = None,
) -> Path:
    """Write raw DataWeb JSON without overwriting any existing file."""
    path = _raw_path(chemical_id, hts_code, raw_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = path
    for suffix in range(1, 1000):
        try:
            with candidate.open("x") as f:
                json.dump(response_json, f, indent=2, sort_keys=True)
                f.write("\n")
            return candidate
        except FileExistsError:
            candidate = path.with_name(f"{path.stem}_{suffix}{path.suffix}")
    raise FileExistsError(f"Could not find a free raw response path for {path}")


def _columns(column_groups: list[Any], found: list[str] | None = None) -> list[str]:
    found = [] if found is None else found
    for group in column_groups:
        if isinstance(group, dict) and "columns" in group:
            _columns(group["columns"], found)
        elif isinstance(group, dict) and "label" in group:
            found.append(str(group["label"]))
        elif isinstance(group, list):
            _columns(group, found)
    return found


def _table_rows(table: dict[str, Any]) -> list[dict[str, Any]]:
    cols = _columns(table.get("column_groups", []))
    row_groups = table.get("row_groups", [])
    if not row_groups:
        return []
    raw_rows = row_groups[0].get("rowsNew", [])
    rows = []
    for raw in raw_rows:
        values = [entry.get("value") for entry in raw.get("rowEntries", [])]
        rows.append(dict(zip(cols, values)))
    return rows


def _find_value(row: dict[str, Any], patterns: tuple[str, ...]) -> Any:
    for label, value in row.items():
        normalized = label.casefold()
        if all(pattern in normalized for pattern in patterns):
            return value
    return None


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    if cleaned in {"", "-", "."}:
        return None
    return float(cleaned)


def _period(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if re.match(r"^\d{4}-\d{2}$", text):
        return text
    month_year = re.match(r"^(\d{1,2})/(\d{4})$", text)
    if month_year:
        month, year = month_year.groups()
        return f"{year}-{int(month):02d}"
    year_month = re.match(r"^(\d{4})\s+M(\d{1,2})$", text, flags=re.IGNORECASE)
    if year_month:
        year, month = year_month.groups()
        return f"{year}-{int(month):02d}"
    return None


def _unit_value(value_usd: float | None, quantity_kg: float | None) -> float | None:
    if value_usd is None or quantity_kg is None or quantity_kg <= 0:
        return None
    return round(value_usd / quantity_kg, 6)


def _table_measure(table: dict[str, Any]) -> str | None:
    label = (
        table.get("tab_name")
        or table.get("tableInfo", {}).get("dataToReportDesc")
        or table.get("name")
        or ""
    ).casefold()
    if "customs value" in label:
        return "value_usd"
    if "first unit" in label or "quantity" in label:
        return "quantity"
    return None


def _quantity_kg(chemical_id: str, quantity: float | None, unit: Any) -> float | None:
    if quantity is None:
        return None
    unit_text = "" if unit is None else str(unit).casefold()
    if "kilogram" in unit_text:
        return quantity
    if "liter" in unit_text and chemical_id in LITERS_TO_KG:
        return quantity * LITERS_TO_KG[chemical_id]
    return None


def _normalize_wide_tables(
    chemical_id: str,
    hts_code: str,
    tables: list[dict[str, Any]],
    fetched_at: str,
) -> list[dict[str, Any]]:
    by_period: dict[str, dict[str, Any]] = {}
    for table in tables:
        measure = _table_measure(table)
        if measure is None:
            continue
        for row in _table_rows(table):
            year = str(row.get("Year") or "").strip()
            if not re.match(r"^\d{4}$", year):
                continue
            unit_description = row.get("Quantity Description")
            for month_name, month_number in MONTHS.items():
                raw_value = row.get(month_name.capitalize()) or row.get(month_name)
                value = _number(raw_value)
                if value is None:
                    continue
                period = f"{year}-{month_number}"
                observation = by_period.setdefault(
                    period,
                    {
                        "chemical_id": chemical_id,
                        "source": SOURCE,
                        "hts_code": normalize_hts_code(hts_code),
                        "region": "US_IMPORTS_ALL_ORIGINS",
                        "period": period,
                        "value_usd": None,
                        "quantity": None,
                        "quantity_unit": None,
                        "fetched_at": fetched_at,
                    },
                )
                observation[measure] = value
                if measure == "quantity":
                    observation["quantity_unit"] = unit_description

    rows = []
    for period in sorted(by_period):
        observation = by_period[period]
        quantity_kg = _quantity_kg(
            observation["chemical_id"],
            observation["quantity"],
            observation["quantity_unit"],
        )
        rows.append(
            {
                "chemical_id": observation["chemical_id"],
                "source": observation["source"],
                "hts_code": observation["hts_code"],
                "region": observation["region"],
                "period": observation["period"],
                "price_usd_per_kg": _unit_value(observation["value_usd"], quantity_kg),
                "fetched_at": observation["fetched_at"],
            }
        )
    return rows


def _normalize_long_table(
    chemical_id: str,
    hts_code: str,
    table: dict[str, Any],
    fetched_at: str,
) -> list[dict[str, Any]]:
    rows = []
    for row in _table_rows(table):
        period = _period(
            _find_value(row, ("period",))
            or _find_value(row, ("month",))
            or _find_value(row, ("time",))
        )
        value_usd = _number(
            _find_value(row, ("customs", "value"))
            or _find_value(row, ("import", "value"))
            or _find_value(row, ("value",))
        )
        quantity_kg = _number(
            _find_value(row, ("first", "quantity")) or _find_value(row, ("quantity",))
        )

        if period is None:
            continue

        rows.append(
            {
                "chemical_id": chemical_id,
                "source": SOURCE,
                "hts_code": normalize_hts_code(hts_code),
                "region": "US_IMPORTS_ALL_ORIGINS",
                "period": period,
                "price_usd_per_kg": _unit_value(value_usd, quantity_kg),
                "fetched_at": fetched_at,
            }
        )
    return rows


def normalize_rows(
    chemical_id: str,
    hts_code: str,
    response_json: dict[str, Any],
    fetched_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize DataWeb rows while retaining incomplete observations."""
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    tables = response_json.get("dto", {}).get("tables", [])
    if not tables:
        return []

    wide_rows = _normalize_wide_tables(chemical_id, hts_code, tables, fetched_at)
    if wide_rows:
        return wide_rows
    return _normalize_long_table(chemical_id, hts_code, tables[0], fetched_at)


def fetch_hts(
    token: str,
    chemical_id: str,
    hts_code: str,
    months: int = DEFAULT_MONTHS,
    raw_dir: Path | None = None,
) -> list[dict[str, Any]]:
    url = f"{USITC_BASE}/api/v2/report2/runReport"
    response = requests.post(
        url,
        headers=_headers(token),
        json=build_query(hts_code, months),
        timeout=60,
    )
    response.raise_for_status()
    response_json = response.json()
    raw_path = preserve_raw_response(
        response_json, chemical_id, normalize_hts_code(hts_code), raw_dir
    )
    print(f"    raw response saved -> {raw_path}")
    errors = response_json.get("dto", {}).get("errors") or []
    if errors:
        raise ValueError(f"USITC query validation failed for {chemical_id}: {'; '.join(errors)}")
    return normalize_rows(chemical_id, hts_code, response_json)


def load_mock_response(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return json.load(f)


def pull_all(
    token: str | None,
    months: int = DEFAULT_MONTHS,
    raw_dir: Path | None = None,
    mock_response: Path | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chemical_id, meta in CHEMICALS.items():
        hts_code = meta["hts_code"]
        print(f"  pulling {chemical_id} ({meta['display_hts_code']} -> {hts_code})...")
        if mock_response:
            response_json = load_mock_response(mock_response)
            preserve_raw_response(response_json, chemical_id, hts_code, raw_dir)
            fetched = normalize_rows(chemical_id, hts_code, response_json)
        else:
            if token is None:
                raise ValueError("USITC_API_TOKEN is required unless --mock-response is used.")
            fetched = fetch_hts(token, chemical_id, hts_code, months, raw_dir)
        print(f"    {len(fetched)} normalized rows")
        rows.extend(fetched)
    return rows


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pull USITC monthly import benchmarks.")
    parser.add_argument("--months", type=int, default=DEFAULT_MONTHS)
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=_repo_root() / "data" / "raw" / "usitc",
        help="Ignored directory where raw DataWeb JSON responses are preserved.",
    )
    parser.add_argument(
        "--mock-response",
        type=Path,
        help="Local raw DataWeb JSON file for validation without calling the API.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Fetch/normalize/preserve raw files, but do not write normalized storage.",
    )
    parser.add_argument(
        "--write",
        action="store_false",
        dest="dry_run",
        help="Write normalized observations to PostgreSQL (DATABASE_URL).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    token = os.environ.get("USITC_API_TOKEN")

    if not token and not args.mock_response:
        print(
            "ERROR: set USITC_API_TOKEN first, or pass --mock-response for local validation.\n"
            "Create/login to DataWeb, open the API tab, and copy your API token.",
            file=sys.stderr,
        )
        return 1

    print("Pulling chemical import price signals from USITC DataWeb...")
    try:
        rows = pull_all(
            token=token,
            months=args.months,
            raw_dir=args.raw_dir,
            mock_response=args.mock_response,
        )
    except requests.HTTPError as exc:
        print(f"ERROR: USITC request failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not rows:
        print("WARNING: no normalized rows returned; nothing written.", file=sys.stderr)
        return 1

    write_price_observations(rows, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
