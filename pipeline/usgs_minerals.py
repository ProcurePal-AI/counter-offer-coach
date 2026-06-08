"""
usgs_minerals.py -- USGS ammonia annual price connector (PostgreSQL-backed).

Sources
-------
* Mineral Commodity Summaries (MCS) — published each February, covers prior year.
  URL pattern: https://pubs.usgs.gov/periodicals/mcs{pub_year}/mcs{pub_year}-nitrogen.pdf
  where pub_year = data_year + 1.

* Minerals Yearbook (MYB) — published ~18 months after data year, more granular.
  URL pattern: https://minerals.usgs.gov/minerals/pubs/commodity/nitrogen/myb1-{data_year}-nitro.pdf

Parse strategies (tried in order)
----------------------------------
1. **table**   — pdfplumber table extraction; reliable when PDF has proper table structure.
2. **regex**   — scan raw page text for the price row + a column-aligned year header.
3. **narrative** — scan sentences for explicit "$NNN per short ton" mentions.

Public API
----------
* ``fetch_ammonia_price(data_year, *, pdf_path=None, prefer_myb=False)``
* ``ingest_ammonia_prices(start_year, end_year, *, db_path, overwrite=False)``
* ``AmmoniaPriceRecord`` — named dataclass
* ``USGSParseError`` — raised on unrecoverable failure
"""

from __future__ import annotations

import io
import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pdfplumber
import requests

try:
    from pipeline.storage import connect
except ModuleNotFoundError:  # allow `python pipeline/usgs_minerals.py` from the repo root
    from storage import connect

logger = logging.getLogger(__name__)

# Public exceptions & data types
class USGSParseError(RuntimeError):
    """Raised when price data cannot be extracted from a USGS PDF."""
    
@dataclass
class AmmoniaPriceRecord:
    data_year: int
    price_usd_per_short_ton: float
    source_url: str
    parse_strategy: str  # "table" | "regex" | "narrative"
    notes: str = ""

_MCS_BASE = "https://pubs.usgs.gov/periodicals/mcs{pub_year}/mcs{pub_year}-nitrogen.pdf"
_MYB_BASE = ( "https://minerals.usgs.gov/minerals/pubs/commodity/nitrogen/myb1-{data_year}-nitro.pdf")


def _resolve_url(data_year: int, *, prefer_myb: bool = False) -> Tuple[str, str]:
    """Return (url, description) for the preferred source for *data_year*."""
    pub_year = data_year + 1
    mcs_url = _MCS_BASE.format(pub_year=pub_year)
    myb_url = _MYB_BASE.format(data_year=data_year)
    if prefer_myb:
        return myb_url, f"MYB {data_year}"
    return mcs_url, f"MCS {pub_year}"

def _download_pdf(url: str, timeout: int = 30) -> bytes:
    """Download *url* and return raw bytes.  Raises USGSParseError on failure."""
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    except requests.RequestException as exc:
        raise USGSParseError(f"Download failed for {url}: {exc}") from exc
        
#text helpers
_YEAR_RE = re.compile(r"\b(20\d{2})e?\b")
_PRICE_LABEL_KEYWORDS = ("price", "gulf coast", "short ton", "f.o.b")

def _is_price_row(cell_text: str) -> bool:
    """Return True if *cell_text* looks like the ammonia price label row."""
    lower = cell_text.lower()
    # Must contain "price" AND at least one of the other keywords
    return "price" in lower and any(kw in lower for kw in _PRICE_LABEL_KEYWORDS[1:])


def _parse_price_cell(value) -> Optional[float]:
    """Parse a table cell or string to a float price; return None for missing."""
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("$", "")
    if s in ("", "--", "-", "NA", "W", "XX"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _find_year_header(table: List[List]) -> Tuple[int, Dict[int, int]]:
    """
    Scan *table* rows for a header containing ≥2 consecutive calendar years.
    Returns
    -------
    (row_index, {col_index: year})  — empty dict when not found.
    """
    for ridx, row in enumerate(table):
        year_cols: Dict[int, int] = {}
        for cidx, cell in enumerate(row):
            m = _YEAR_RE.match(str(cell or "").strip())
            if m:
                year_cols[cidx] = int(m.group(1))
        if len(year_cols) >= 2:
            return ridx, year_cols
    return -1, {}

# pdfplumber table extraction
def _extract_via_table(pdf_bytes: bytes) -> Dict[int, float]:
    """
    Try to extract prices from real table structures inside the PDF.

    Returns {year: price} — empty on failure.
    """
    results: Dict[int, float] = {}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                header_idx, year_cols = _find_year_header(table)
                if not year_cols:
                    continue
                # Search rows AFTER the header for price label
                for row in table[header_idx + 1 :]:
                    if not row:
                        continue
                    label = str(row[0] or "")
                    if not _is_price_row(label):
                        continue
                    for cidx, year in year_cols.items():
                        if cidx < len(row):
                            price = _parse_price_cell(row[cidx])
                            if price is not None and 50 <= price <= 5000:
                                results[year] = price
                    if results:
                        return results
    return results

#regex over raw page text
def _infer_candidate_years_from_text(text: str) -> List[int]:
    """Return sorted list of unique 4-digit years found in *text*."""
    raw = [int(m.group(1)) for m in _YEAR_RE.finditer(text)]
    return sorted(set(raw))


def _extract_via_regex(text: str, candidate_years: List[int]) -> Dict[int, float]:
    """
    Locate a price-label line and match the N years following it to prices.

    Expects a column layout where the line AFTER the price label contains
    N numeric values corresponding to *candidate_years* in order.
    """
    lines = text.splitlines()

    for i, line in enumerate(lines):
        if not _is_price_row(line):
            continue

        # Collect numeric tokens from subsequent lines
        tokens: List[str] = []
        for j in range(i + 1, min(i + 6, len(lines))):
            tokens.extend(re.findall(r"[\d,]+(?:\.\d+)?", lines[j]))
            if len(tokens) >= len(candidate_years):
                break

        # Also try tokens on the same line (after the label text)
        same_line_tokens = re.findall(r"[\d,]+(?:\.\d+)?", line)

        for token_source in (tokens, same_line_tokens):
            prices = [_parse_price_cell(t) for t in token_source]
            prices = [p for p in prices if p is not None and 50 <= p <= 10_000]
            if len(prices) == len(candidate_years):
                return dict(zip(candidate_years, prices))

    return {}

# narrative sentence scan
_DOLLAR_PRICE_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s+per\s+short\s+ton", re.IGNORECASE)
_AVG_PRICE_RE = re.compile(r"average\s+ammonia\s+price\s+was\s+\$\s?([\d,]+(?:\.\d+)?)\s+per\s+short\s+ton", re.IGNORECASE,)


def _extract_via_narrative(text: str, data_year: int) -> Dict[int, float]:
    """
    Scan narrative sentences for explicit price mentions referencing *data_year*.

    Returns {data_year: price} or {}.
    """
    # Prefer "average ammonia price was $NNN per short ton in YYYY"
    for m in _AVG_PRICE_RE.finditer(text):
        price = _parse_price_cell(m.group(1))
        if price and 50 <= price <= 10_000:
            return {data_year: price}

    # Fall back: find price mentions in sentences that also contain data_year
    year_str = str(data_year)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    candidates: List[float] = []
    for sent in sentences:
        if year_str not in sent:
            continue
        for m in _DOLLAR_PRICE_RE.finditer(sent):
            price = _parse_price_cell(m.group(1))
            if price and 50 <= price <= 10_000:
                candidates.append(price)

    if candidates:
        # Take the last mention (most likely the annual summary)
        return {data_year: candidates[-1]}

    # Broader fallback: any dollar price in the text (last occurrence)
    all_prices = [
        _parse_price_cell(m.group(1))
        for m in _DOLLAR_PRICE_RE.finditer(text)
        if _parse_price_cell(m.group(1)) is not None
        and 50 <= (_parse_price_cell(m.group(1)) or 0) <= 10_000
    ]
    if all_prices:
        return {data_year: all_prices[-1]}

    return {}


# PDF parsing orchestrator
def _extract_full_text(pdf_bytes: bytes) -> str:
    """Return concatenated text from all pages of the PDF."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def _parse_pdf(pdf_bytes: bytes, *, data_year: int, source_url: str) -> AmmoniaPriceRecord:
    """
    Parse *pdf_bytes* and return an :class:`AmmoniaPriceRecord`.

    Raises :class:`USGSParseError` if no price can be extracted.
    """
    # --- Strategy 1: table ---
    table_prices = _extract_via_table(pdf_bytes)
    if data_year in table_prices:
        return AmmoniaPriceRecord(
            data_year=data_year,
            price_usd_per_short_ton=table_prices[data_year],
            source_url=source_url,
            parse_strategy="table",)

    # Extract full text once for strategies 2 & 3
    full_text = _extract_full_text(pdf_bytes)

    # --- Strategy 2: regex ---
    candidate_years = _infer_candidate_years_from_text(full_text)
    if candidate_years:
        regex_prices = _extract_via_regex(full_text, candidate_years)
        if data_year in regex_prices:
            return AmmoniaPriceRecord(
                data_year=data_year,
                price_usd_per_short_ton=regex_prices[data_year],
                source_url=source_url,
                parse_strategy="regex",)

    # --- Strategy 3: narrative ---
    narrative_prices = _extract_via_narrative(full_text, data_year)
    if data_year in narrative_prices:
        return AmmoniaPriceRecord(
            data_year=data_year,
            price_usd_per_short_ton=narrative_prices[data_year],
            source_url=source_url,
            parse_strategy="narrative",)

    raise USGSParseError(f"Could not extract ammonia price for {data_year} from {source_url}")

# Public fetch function
def fetch_ammonia_price(
    data_year: int,
    *,
    pdf_path: Optional[Path] = None,
    prefer_myb: bool = False,
) -> AmmoniaPriceRecord:
    """
    Return an :class:`AmmoniaPriceRecord` for *data_year*.

    Parameters
    ----------
    data_year:
        Calendar year for which the ammonia price is required.
    pdf_path:
        If supplied, read bytes from this local file instead of downloading.
    prefer_myb:
        When True, try the Minerals Yearbook URL before the MCS URL.
    """
    if pdf_path is not None:
        pdf_bytes = Path(pdf_path).read_bytes()
        source_url = pdf_path.as_uri()
        return _parse_pdf(pdf_bytes, data_year=data_year, source_url=source_url)

    # Try primary URL
    primary_url, _ = _resolve_url(data_year, prefer_myb=prefer_myb)
    primary_err_msg: str = ""
    try:
        pdf_bytes = _download_pdf(primary_url)
        return _parse_pdf(pdf_bytes, data_year=data_year, source_url=primary_url)
    except USGSParseError as exc:
        primary_err_msg = str(exc)
        logger.warning("Primary URL failed (%s): %s", primary_url, exc)

    # Fall back to the other source
    fallback_url, _ = _resolve_url(data_year, prefer_myb=not prefer_myb)
    try:
        pdf_bytes = _download_pdf(fallback_url)
        return _parse_pdf(pdf_bytes, data_year=data_year, source_url=fallback_url)
    except USGSParseError as exc:
        raise USGSParseError(
            f"Both URLs failed for {data_year}: {primary_err_msg}; {exc}"
        ) from exc

# PostgreSQL persistence. The ammonia_price_usgs table DDL lives in
# pipeline/storage.py (the single schema authority); connect() creates it.
def ingest_ammonia_prices(
    start_year: int,
    end_year: int,
    *,
    db_path: Optional[Path] = None,  # deprecated/ignored: data now goes to Postgres
    overwrite: bool = False,
    prefer_myb: bool = False,
) -> List[AmmoniaPriceRecord]:
    """
    Fetch and persist ammonia prices for *start_year* through *end_year* inclusive.

    Parameters
    ----------
    start_year, end_year:
        Inclusive range of data years to ingest.
    db_path:
        Path to the SQLite database file (created if absent).
    overwrite:
        When True, replace existing rows; when False, skip years already present.
    prefer_myb:
        Passed through to :func:`fetch_ammonia_price`.

    Returns
    -------
    List of :class:`AmmoniaPriceRecord` objects that were written.
    """
    conn = connect()
    cur = conn.cursor()
    records: List[AmmoniaPriceRecord] = []

    base_insert = (
        "INSERT INTO ammonia_price_usgs "
        "(data_year, price_usd_per_short_ton, source_url, parse_strategy, notes) "
        "VALUES (%s, %s, %s, %s, %s) "
    )
    if overwrite:
        upsert = base_insert + (
            "ON CONFLICT (data_year) DO UPDATE SET "
            "price_usd_per_short_ton = EXCLUDED.price_usd_per_short_ton, "
            "source_url = EXCLUDED.source_url, "
            "parse_strategy = EXCLUDED.parse_strategy, "
            "notes = EXCLUDED.notes"
        )
    else:
        upsert = base_insert + "ON CONFLICT (data_year) DO NOTHING"

    try:
        for year in range(start_year, end_year + 1):
            # Skip if row exists and overwrite not requested
            if not overwrite:
                cur.execute("SELECT 1 FROM ammonia_price_usgs WHERE data_year = %s", (year,))
                if cur.fetchone():
                    logger.info("Skipping year %s -- already in DB", year)
                    continue

            try:
                rec = fetch_ammonia_price(year, prefer_myb=prefer_myb)
            except USGSParseError as exc:
                logger.error("Failed to fetch %s: %s", year, exc)
                continue

            cur.execute(
                upsert,
                (rec.data_year, rec.price_usd_per_short_ton,
                 rec.source_url, rec.parse_strategy, rec.notes),
            )
            conn.commit()
            records.append(rec)
            logger.info(
                "Ingested %s: $%.0f/st via %s", year,
                rec.price_usd_per_short_ton, rec.parse_strategy)
    finally:
        cur.close()
        conn.close()
    return records
