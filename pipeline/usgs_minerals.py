"""
usgs_minerals.py -- USGS ammonia price connector (pipe 2.4).

Pulls the annual average anhydrous-ammonia price from the USGS Mineral Commodity
Summaries (MCS) nitrogen data sheet and normalizes it into a `price_observations`
row (chemical_id="ammonia") -- the SAME store every other connector writes to.

Why this exists
---------------
Nitric acid has no reliable public spot series, so the resolver derives it from
ammonia (HNO3 = ammonia x K, see engine/prices.py). That derivation can only read
an ammonia price if one actually lands in `price_observations`. Landing it there
is this connector's entire job. Writing to a private side table (the previous
design) leaves the resolver blind, so all output goes through
`storage.write_price_observations`.

Unit handling
-------------
USGS has reported this price per short ton in its nitrogen series, but wording has
shifted across editions. The parser DETECTS the unit from the source text instead
of assuming it, records the raw unit for audit, and converts to USD/kg. A bare
"per ton" with no qualifier is treated as a short ton (the long-standing USGS
nitrogen convention) and flagged in `notes` for human verification.

Cadence / period
----------------
Annual. The schema requires period = "YYYY-MM", so the annual figure is stored at
"{data_year}-12" (year-end). It is an ANNUAL AVERAGE, not a December spot -- the
year-end month is only a schema-compatible slot.

Postgres note
-------------
This connector contains no SQL and never opens its own DB connection. Persistence
goes through `storage.write_price_observations`, so the SQLite -> Postgres move
touches storage.py only; this file is unchanged.

Run examples
------------
  python pipeline/usgs_minerals.py --dry-run                  # download + parse + print
  python pipeline/usgs_minerals.py --write                    # write to the Postgres store (DATABASE_URL)
  python pipeline/usgs_minerals.py --start-year 2021 --end-year 2024 --write
  python pipeline/usgs_minerals.py --pdf path/to.pdf --year 2023 --dry-run
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from pipeline.storage import write_price_observations
except ModuleNotFoundError:  # Allows `python pipeline/usgs_minerals.py` from repo root.
    from storage import write_price_observations

# NOTE: pdfplumber is imported lazily inside the two functions that open a PDF
# (_extract_via_table, _extract_full_text). It is a declared dependency, but the
# pure parsing/conversion logic and the storage->resolver bridge don't need it,
# so the module (and the whole test suite) imports fine without it installed.


# --- Constants -------------------------------------------------------------
SOURCE = "USGS"
CHEMICAL_ID = "ammonia"
REGION = "US"  # USGS price is a national f.o.b. Gulf Coast average.

KG_PER_SHORT_TON = 907.18474
KG_PER_METRIC_TON = 1000.0

# Plausible ammonia price band, USD per (short or metric) ton. Rejects parse
# noise (page numbers, footnote indices) and absurd values from mis-detection.
_PRICE_MIN = 50.0
_PRICE_MAX = 5000.0

# Each MCS edition's salient-statistics table spans ~5 prior data years, so a
# data_year appears in editions pub_year = data_year+1 .. data_year+5. We try the
# earliest (most authoritative) edition first, then a couple of later editions as
# fallbacks -- all on the confirmed-live pubs.usgs.gov host. (The legacy
# minerals.usgs.gov Minerals-Yearbook paths are dead and are NOT used.)
_MCS_URL = "https://pubs.usgs.gov/periodicals/mcs{pub_year}/mcs{pub_year}-nitrogen.pdf"
_FALLBACK_EDITIONS = 3  # try pub_year = data_year+1, +2, +3


_SHORT_TON_TO_METRIC_TON = 0.90718474

# Public exceptions & data types
class USGSParseError(RuntimeError):
    """Raised when an ammonia price cannot be extracted from a USGS PDF."""


@dataclass
class AmmoniaPriceRecord:
    data_year: int
    price_usd_per_kg: float          # normalized
    raw_price: float                 # as printed in the source
    raw_unit: str                    # detected: "short ton" | "metric ton" | "ton (assumed short)"
    source_url: str
    parse_strategy: str              # "table" | "narrative"
    notes: str = ""


# --- Unit detection & conversion ------------------------------------------
def _unit_to_kg_divisor(unit_phrase: str) -> Tuple[float, str, str]:
    """Map a detected unit phrase to (kg_per_unit, canonical_unit, note).

    A bare "ton" is treated as a short ton (USGS nitrogen convention) but flagged.
    """
    u = unit_phrase.lower()
    if "short" in u:
        return KG_PER_SHORT_TON, "short ton", ""
    if "metric" in u or "tonne" in u:
        return KG_PER_METRIC_TON, "metric ton", ""
    return (
        KG_PER_SHORT_TON,
        "ton (assumed short)",
        "source said 'per ton' without qualifier; assumed short ton -- verify against the PDF",
    )


def _to_usd_per_kg(raw_price: float, unit_phrase: str) -> Tuple[float, str, str]:
    kg_per_unit, canonical_unit, note = _unit_to_kg_divisor(unit_phrase)
    return raw_price / kg_per_unit, canonical_unit, note


# --- Text helpers ----------------------------------------------------------
_YEAR_RE = re.compile(r"\b(20\d{2})e?\b")

# "$ 1,234.5 per short ton" -> ("1,234.5", "short ton"). Tolerant of metric/tonne.
_PRICE_UNIT_RE = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s+per\s+((?:short|metric)\s+ton|tonne|ton)\b",
    re.IGNORECASE,
)


def _parse_price_cell(value: Any) -> Optional[float]:
    """Parse a table cell or string to a float price; None for missing/sentinel."""
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("$", "")
    if s in ("", "--", "-", "NA", "W", "XX", "(1)", "(2)"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _in_band(price: Optional[float]) -> bool:
    return price is not None and _PRICE_MIN <= price <= _PRICE_MAX


def _price_row_unit(label: str) -> Optional[str]:
    """If `label` is an ammonia price-row label, return its unit phrase, else None.

    Accepts a row whose label mentions a price and a ton-based unit. The MCS
    nitrogen data sheet is ammonia-specific, so a "Price ... per ton" row there is
    the ammonia price; we still prefer labels that name a ton unit explicitly.
    """
    low = label.lower()
    if "price" not in low:
        return None
    m = re.search(r"((?:short|metric)\s+ton|tonne|per\s+ton|ton)\b", low)
    if not m:
        return None
    phrase = m.group(1).replace("per ", "").strip()
    return phrase


# --- Strategy 1: structured tables ----------------------------------------
def _find_year_header(table: List[List]) -> Tuple[int, Dict[int, int]]:
    """Return (row_index, {col_index: year}) for the first row with >=2 years."""
    for ridx, row in enumerate(table):
        year_cols: Dict[int, int] = {}
        for cidx, cell in enumerate(row):
            m = _YEAR_RE.match(str(cell or "").strip())
            if m:
                year_cols[cidx] = int(m.group(1))
        if len(year_cols) >= 2:
            return ridx, year_cols
    return -1, {}


def _prices_from_tables(tables: List[List[List]]) -> Dict[int, Tuple[float, str]]:
    """Extract {year: (price, unit_phrase)} from already-parsed table structures.

    Pure (no PDF/IO) so it is unit-testable with synthetic rows. Years come from
    THIS table's own header row -- never from a document-wide year scan.
    """
    results: Dict[int, Tuple[float, str]] = {}
    for table in tables:
        if not table:
            continue
        header_idx, year_cols = _find_year_header(table)
        if not year_cols:
            continue
        for row in table[header_idx + 1:]:
            if not row:
                continue
            unit_phrase = _price_row_unit(str(row[0] or ""))
            if unit_phrase is None:
                continue
            for cidx, year in year_cols.items():
                if cidx < len(row):
                    price = _parse_price_cell(row[cidx])
                    if _in_band(price):
                        results[year] = (price, unit_phrase)
            if results:
                return results
    return results


def _extract_via_table(pdf_bytes: bytes) -> Dict[int, Tuple[float, str]]:
    import pdfplumber  # heavy dep; only needed when actually opening a PDF

    tables: List[List[List]] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            tables.extend(page.extract_tables() or [])
    return _prices_from_tables(tables)


# --- Strategy 2: narrative sentences --------------------------------------
def _extract_via_narrative(text: str, data_year: int) -> Dict[int, Tuple[float, str]]:
    """Find a price in a sentence that mentions BOTH 'ammonia' AND `data_year`.

    Deliberately strict: no document-wide "grab the last dollar amount" fallback.
    A price is only accepted when its own sentence ties it to ammonia and the
    requested year, so a urea/DAP figure or another year's number can't leak in.
    Returns {} (caller raises) rather than guessing.
    """
    year_str = str(data_year)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for sent in sentences:
        low = sent.lower()
        if "ammonia" not in low or year_str not in sent:
            continue
        for m in _PRICE_UNIT_RE.finditer(sent):
            price = _parse_price_cell(m.group(1))
            if _in_band(price):
                return {data_year: (price, m.group(2))}
    return {}


def _extract_full_text(pdf_bytes: bytes) -> str:
    import pdfplumber  # heavy dep; only needed when actually opening a PDF

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


# --- Parse orchestrator ----------------------------------------------------
def _build_record(
    data_year: int, raw_price: float, unit_phrase: str, source_url: str, strategy: str
) -> AmmoniaPriceRecord:
    price_per_kg, canonical_unit, note = _to_usd_per_kg(raw_price, unit_phrase)
    return AmmoniaPriceRecord(
        data_year=data_year,
        price_usd_per_kg=round(price_per_kg, 6),
        raw_price=raw_price,
        raw_unit=canonical_unit,
        source_url=source_url,
        parse_strategy=strategy,
        notes=note,
    )


def _parse_pdf(pdf_bytes: bytes, *, data_year: int, source_url: str) -> AmmoniaPriceRecord:
    table_prices = _extract_via_table(pdf_bytes)
    if data_year in table_prices:
        raw_price, unit_phrase = table_prices[data_year]
        return _build_record(data_year, raw_price, unit_phrase, source_url, "table")

    full_text = _extract_full_text(pdf_bytes)
    narrative = _extract_via_narrative(full_text, data_year)
    if data_year in narrative:
        raw_price, unit_phrase = narrative[data_year]
        return _build_record(data_year, raw_price, unit_phrase, source_url, "narrative")

    raise USGSParseError(
        f"No ammonia price for {data_year} found in {source_url} "
        f"(years seen in tables: {sorted(table_prices) or 'none'})"
    )


# --- Download --------------------------------------------------------------
def _download_pdf(url: str, timeout: int = 30) -> bytes:
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    except requests.RequestException as exc:
        raise USGSParseError(f"Download failed for {url}: {exc}") from exc


def _raw_path(data_year: int, raw_dir: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return raw_dir / f"{timestamp}_ammonia_{data_year}_mcs-nitrogen.pdf"


def _preserve_raw(pdf_bytes: bytes, data_year: int, raw_dir: Path) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = _raw_path(data_year, raw_dir)
    for suffix in range(0, 1000):
        target = path if suffix == 0 else path.with_name(f"{path.stem}_{suffix}{path.suffix}")
        try:
            with target.open("xb") as f:
                f.write(pdf_bytes)
            return target
        except FileExistsError:
            continue
    raise FileExistsError(f"Could not find a free raw path for {path}")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def fetch_ammonia_price(
    data_year: int,
    *,
    pdf_path: Optional[Path] = None,
    raw_dir: Optional[Path] = None,
) -> AmmoniaPriceRecord:
    """Return an :class:`AmmoniaPriceRecord` for `data_year`.

    If `pdf_path` is given, parse that local PDF (no download). Otherwise download
    the MCS nitrogen sheet, trying successive editions whose 5-year salient-stats
    table still covers `data_year`. Downloaded PDFs are preserved under raw_dir.
    """
    if pdf_path is not None:
        pdf_bytes = Path(pdf_path).read_bytes()
        return _parse_pdf(pdf_bytes, data_year=data_year, source_url=Path(pdf_path).as_uri())

    raw_dir = raw_dir or _repo_root() / "data" / "raw" / "usgs"
    errors: List[str] = []
    for edition_offset in range(1, _FALLBACK_EDITIONS + 1):
        pub_year = data_year + edition_offset
        url = _MCS_URL.format(pub_year=pub_year)
        try:
            pdf_bytes = _download_pdf(url)
        except USGSParseError as exc:
            errors.append(str(exc))
            continue
        _preserve_raw(pdf_bytes, data_year, raw_dir)
        try:
            return _parse_pdf(pdf_bytes, data_year=data_year, source_url=url)
        except USGSParseError as exc:
            errors.append(str(exc))
    raise USGSParseError(f"Could not fetch ammonia price for {data_year}: " + "; ".join(errors))


# --- Normalize to a price_observations row --------------------------------
def build_price_row(record: AmmoniaPriceRecord, fetched_at: Optional[str] = None) -> dict:
    """Turn a record into a schema-aligned `price_observations` dict.

    period = "{data_year}-12" (year-end slot for an annual average; schema needs
    YYYY-MM). hts_code / grade / assessment_type are omitted -> stored NULL.
    """
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    return {
        "chemical_id": CHEMICAL_ID,
        "source": SOURCE,
        "region": REGION,
        "period": f"{record.data_year}-12",
        "price_usd_per_kg": record.price_usd_per_kg,
        "fetched_at": fetched_at,
    }


def ingest_ammonia_prices(
    start_year: int,
    end_year: int,
    *,
    dry_run: bool = True,
    pdf_path: Optional[Path] = None,
    raw_dir: Optional[Path] = None,
) -> List[AmmoniaPriceRecord]:
    """Fetch `start_year..end_year` and hand normalized rows to storage.

    Append-only, like every other connector. Years that fail to parse are logged
    and skipped (never written as guesses). With `pdf_path`, only `start_year` is
    read from that file.
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    records: List[AmmoniaPriceRecord] = []
    rows: List[dict] = []

    years = [start_year] if pdf_path is not None else range(start_year, end_year + 1)
    for year in years:
        try:
            rec = fetch_ammonia_price(year, pdf_path=pdf_path, raw_dir=raw_dir)
        except USGSParseError as exc:
            print(f"  WARNING: {year} skipped -- {exc}", file=sys.stderr)
            continue

      sql = """
        INSERT OR REPLACE INTO ammonia_prices 
        (data_year, price_usd_per_metric_ton, source_url, parse_strategy, notes) 
        VALUES (?, ?, ?, ?, ?)
        """ if overwrite else """
        INSERT OR IGNORE INTO ammonia_prices 
        (data_year, price_usd_per_metric_ton, source_url, parse_strategy, notes) 
        VALUES (?, ?, ?, ?, ?)
        """
        
        db_conn.execute(
            sql,
            (rec.data_year, rec.price_usd_per_metric_ton,
             rec.source_url, rec.parse_strategy, rec.notes),
        )
        db_conn.commit()
        records.append(rec)
        logger.info(
            "Ingested %s: $%.2f/t via %s", year,
            rec.price_usd_per_metric_ton, rec.parse_strategy
        )

    return records


# --- CLI -------------------------------------------------------------------
def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pull USGS annual ammonia price into price_observations.")
    p.add_argument("--start-year", type=int, default=datetime.now(timezone.utc).year - 2)
    p.add_argument("--end-year", type=int, default=datetime.now(timezone.utc).year - 1)
    p.add_argument("--year", type=int, help="Shortcut for a single data year.")
    p.add_argument("--pdf", type=Path, help="Parse a local MCS nitrogen PDF instead of downloading.")
    p.add_argument(
        "--raw-dir", type=Path, default=_repo_root() / "data" / "raw" / "usgs",
        help="Ignored directory where downloaded PDFs are preserved.",
    )
    p.add_argument("--dry-run", action="store_true", default=True,
                   help="Download/parse/print but do not write storage (default).")
    p.add_argument("--write", action="store_false", dest="dry_run",
                   help="Write normalized rows to the Postgres store (DATABASE_URL).")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    start = args.year if args.year is not None else args.start_year
    end = args.year if args.year is not None else args.end_year
    print(f"Pulling USGS ammonia price for {start}..{end} (dry_run={args.dry_run})...")
    records = ingest_ammonia_prices(
        start, end, dry_run=args.dry_run, pdf_path=args.pdf, raw_dir=args.raw_dir
    )
    if not records:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
