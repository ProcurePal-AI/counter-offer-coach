"""
edgar_financials.py -- derive markup anchors (SG&A%, D&A%, EBIT%) from SEC XBRL.

Bridges pipe 2.5 and layer 3.3. sec_edgar.py (2.5) catalogs and downloads filing
DOCUMENTS; this module pulls the NUMBERS -- via SEC's structured XBRL
"companyfacts" API (https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json)
-- so no HTML/PDF parsing is needed. Every figure is an audited, machine-tagged
income-statement / cash-flow line; nothing is scraped from prose.

What it derives (per company, per fiscal year, from 10-K facts):
    sga_pct           = SG&A expense / revenue
    da_pct            = total D&A / revenue          (cash-flow statement)
    ebit_margin_pct   = operating income / revenue
    ebitda_margin_pct = (operating income + D&A) / revenue
    gross_margin_pct  = 1 - COGS / revenue           (when COGS is tagged)

What it deliberately does NOT attempt (per the methodology note):
    * labor or maintenance per ton -- not a GAAP line; buried in COGS.
    * any aniline-specific allocation -- filings are portfolio-wide. The ratios
      are PROXIES; the Monte Carlo band carries the allocation error.

Reality check on the configured producer set: of the four companies in
config/producer_sources.yaml, only HUNTSMAN is a live US SEC filer with XBRL
facts. BASF deregistered from the SEC in 2007; Covestro and Tosoh have no CIK.
So the default peer set here is Huntsman plus optional extra US commodity-chem
tickers (--tickers DOW OLN EMN ...) to form a peer-median anchor instead of
betting the wedge on one company's product mix.

Outputs:
    data/edgar_financials.csv          -- one row per (company, fiscal year)
    data/edgar_financials_summary.csv  -- per-company medians + a PEER_MEDIAN row
                                          (what engine/markup.py loads)

Run:
    python pipeline/edgar_financials.py                  # Huntsman only
    python pipeline/edgar_financials.py --tickers DOW OLN EMN
    python pipeline/edgar_financials.py --years 5
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import requests

# Reuse 2.5's request conventions (User-Agent policy, CIK padding, index lookup).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sec_edgar import (  # noqa: E402
    DEFAULT_USER_AGENT,
    REQUEST_SLEEP_SECONDS,
    USER_AGENT_ENV,
    _headers,
    _padded_cik,
    fetch_company_index,
)

SEC_COMPANYFACTS_BASE = "https://data.sec.gov/api/xbrl/companyfacts"

# us-gaap tag fallbacks, in preference order. Companies switch tags across years
# (ASC 606 renamed revenue tags), so each concept is a list tried left to right
# PER FISCAL YEAR -- a company can use different tags in different years.
REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)
SGA_TAGS = ("SellingGeneralAndAdministrativeExpense",)
DA_TAGS = (
    "DepreciationDepletionAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
    "DepreciationAndAmortization",
)
EBIT_TAGS = ("OperatingIncomeLoss",)
COGS_TAGS = ("CostOfGoodsAndServicesSold", "CostOfRevenue")

# A full-year flow fact should span roughly one year. Filters out FY-labeled
# quarterly slices and other oddities.
MIN_ANNUAL_DAYS, MAX_ANNUAL_DAYS = 300, 400


@dataclass(frozen=True)
class AnnualRatios:
    """One company-fiscal-year row of derived revenue-share ratios."""

    company_key: str
    company_name: str
    cik: str
    fiscal_year: int
    revenue_usd: float
    sga_pct: float | None
    da_pct: float | None
    ebit_margin_pct: float | None
    ebitda_margin_pct: float | None
    gross_margin_pct: float | None


def _duration_days(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None


def _annual_usd_by_fy(facts: dict, tags: tuple[str, ...]) -> dict[int, float]:
    """{fiscal_year: value} for the first tag (per year) that has a clean 10-K fact.

    A fact qualifies when: unit is USD, fp == "FY", form is a 10-K (incl. /A),
    and (when dated) its start..end duration is ~1 year. The same fiscal year
    appears in multiple filings (comparative columns, amendments); the row with
    the latest `end`, then latest `filed`, wins.
    """
    gaap = facts.get("facts", {}).get("us-gaap", {})
    out: dict[int, tuple[tuple, float]] = {}  # fy -> (sort_key, value)
    for tag_rank, tag in enumerate(tags):
        for item in gaap.get(tag, {}).get("units", {}).get("USD", []):
            if item.get("fp") != "FY" or not str(item.get("form", "")).startswith("10-K"):
                continue
            days = _duration_days(item.get("start"), item.get("end"))
            if days is not None and not (MIN_ANNUAL_DAYS <= days <= MAX_ANNUAL_DAYS):
                continue
            fy = item.get("fy")
            val = item.get("val")
            if fy is None or val is None:
                continue
            fy = int(fy)
            # Prefer earlier tags in the fallback list; within a tag, the most
            # recently ended / most recently filed observation of that year.
            key = (-tag_rank, item.get("end") or "", item.get("filed") or "")
            if fy not in out or key > out[fy][0]:
                out[fy] = (key, float(val))
    return {fy: val for fy, (_, val) in out.items()}


def compute_annual_ratios(facts: dict, company_key: str,
                          last_n_years: int = 5) -> list[AnnualRatios]:
    """Derive the revenue-share ratios for the most recent N fiscal years.

    Pure function of the companyfacts JSON -- unit-testable with a canned dict.
    Years with no usable revenue are dropped (every ratio needs a denominator);
    any other missing concept yields None for that ratio only.
    """
    name = str(facts.get("entityName", company_key))
    cik = _padded_cik(str(facts.get("cik", "")))

    revenue = _annual_usd_by_fy(facts, REVENUE_TAGS)
    sga = _annual_usd_by_fy(facts, SGA_TAGS)
    da = _annual_usd_by_fy(facts, DA_TAGS)
    ebit = _annual_usd_by_fy(facts, EBIT_TAGS)
    cogs = _annual_usd_by_fy(facts, COGS_TAGS)

    rows: list[AnnualRatios] = []
    for fy in sorted(revenue, reverse=True)[:last_n_years]:
        rev = revenue[fy]
        if rev <= 0:
            continue
        ebit_v, da_v = ebit.get(fy), da.get(fy)
        rows.append(AnnualRatios(
            company_key=company_key,
            company_name=name,
            cik=cik,
            fiscal_year=fy,
            revenue_usd=rev,
            sga_pct=(sga[fy] / rev) if fy in sga else None,
            da_pct=(da_v / rev) if da_v is not None else None,
            ebit_margin_pct=(ebit_v / rev) if ebit_v is not None else None,
            ebitda_margin_pct=((ebit_v + da_v) / rev)
            if (ebit_v is not None and da_v is not None) else None,
            gross_margin_pct=(1.0 - cogs[fy] / rev) if fy in cogs else None,
        ))
    return rows


RATIO_FIELDS = ("sga_pct", "da_pct", "ebit_margin_pct", "ebitda_margin_pct",
                "gross_margin_pct")
PEER_KEY = "PEER_MEDIAN"


def summarize(rows: list[AnnualRatios]) -> list[dict]:
    """Per-company median of each ratio across its years, plus a PEER_MEDIAN row.

    Medians (not means) on purpose: commodity-chemical margins swing with the
    cycle, and the anchor should be a mid-cycle level, not last year's peak or
    trough. The peer median is the median of the company medians, so one company
    with many years cannot dominate.
    """
    by_company: dict[str, list[AnnualRatios]] = {}
    for r in rows:
        by_company.setdefault(r.company_key, []).append(r)

    out: list[dict] = []
    for key, company_rows in sorted(by_company.items()):
        line = {"company_key": key,
                "company_name": company_rows[0].company_name,
                "n_years": len(company_rows)}
        for f in RATIO_FIELDS:
            vals = [getattr(r, f) for r in company_rows if getattr(r, f) is not None]
            line[f] = statistics.median(vals) if vals else None
        out.append(line)

    peer = {"company_key": PEER_KEY, "company_name": "median across companies",
            "n_years": sum(line["n_years"] for line in out)}
    for f in RATIO_FIELDS:
        vals = [line[f] for line in out if line[f] is not None]
        peer[f] = statistics.median(vals) if vals else None
    out.append(peer)
    return out


def load_peer_median_ratios(summary_path: Path) -> dict[str, float]:
    """The PEER_MEDIAN row from edgar_financials_summary.csv, as floats.

    This is what engine/markup.py consumes for the EDGAR-anchored mode. Raises
    if the row or a needed ratio is missing -- a markup anchored to nothing
    would be worse than the labeled placeholder.
    """
    with summary_path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["company_key"] == PEER_KEY:
                needed = ("sga_pct", "da_pct", "ebit_margin_pct")
                missing = [k for k in needed if not row.get(k)]
                if missing:
                    raise ValueError(
                        f"{summary_path}: PEER_MEDIAN row lacks {missing}; "
                        f"re-run pipeline/edgar_financials.py with filers that report them"
                    )
                return {k: float(row[k]) for k in RATIO_FIELDS if row.get(k)}
    raise ValueError(f"{summary_path}: no {PEER_KEY} row found")


# --- live pull --------------------------------------------------------------

def fetch_companyfacts(cik: str, user_agent: str) -> dict:
    url = f"{SEC_COMPANYFACTS_BASE}/CIK{_padded_cik(cik)}.json"
    r = requests.get(url, headers=_headers(user_agent), timeout=60)
    r.raise_for_status()
    return r.json()


def _resolve_tickers_to_ciks(tickers: list[str], user_agent: str) -> dict[str, str]:
    """{TICKER: cik} via SEC's company index (same source 2.5 uses)."""
    index = fetch_company_index(user_agent)
    fields, data = index.get("fields", []), index.get("data", [])
    records = [dict(zip(fields, row)) for row in data]
    wanted = {t.upper() for t in tickers}
    found: dict[str, str] = {}
    for rec in records:
        t = str(rec.get("ticker", "")).upper()
        if t in wanted:
            found[t] = str(rec["cik"])
    missing = wanted - set(found)
    if missing:
        print(f"WARNING: no CIK for ticker(s): {', '.join(sorted(missing))}",
              file=sys.stderr)
    return found


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Derive SG&A/D&A/EBIT revenue ratios from SEC XBRL companyfacts.")
    parser.add_argument("--tickers", nargs="*", default=["HUN"],
                        help="US-filer tickers to anchor on (default: HUN). "
                             "Add commodity-chem peers, e.g. --tickers HUN DOW OLN EMN.")
    parser.add_argument("--years", type=int, default=5,
                        help="Most recent fiscal years to keep per company.")
    args = parser.parse_args()

    user_agent = os.environ.get(USER_AGENT_ENV, DEFAULT_USER_AGENT)
    base_dir = Path(__file__).resolve().parents[1]

    ciks = _resolve_tickers_to_ciks(args.tickers, user_agent)
    all_rows: list[AnnualRatios] = []
    for ticker, cik in ciks.items():
        print(f"  pulling companyfacts for {ticker} (CIK {cik})...")
        facts = fetch_companyfacts(cik, user_agent)
        rows = compute_annual_ratios(facts, company_key=ticker, last_n_years=args.years)
        print(f"    {len(rows)} fiscal years with usable revenue")
        all_rows.extend(rows)
        time.sleep(REQUEST_SLEEP_SECONDS)

    if not all_rows:
        print("ERROR: no usable annual facts pulled; nothing written.", file=sys.stderr)
        return 1

    detail_path = base_dir / "data" / "edgar_financials.csv"
    summary_path = base_dir / "data" / "edgar_financials_summary.csv"
    _write_csv(detail_path, [asdict(r) for r in all_rows],
               list(AnnualRatios.__dataclass_fields__))
    summary = summarize(all_rows)
    _write_csv(summary_path, summary,
               ["company_key", "company_name", "n_years", *RATIO_FIELDS])

    print(f"Wrote {len(all_rows)} rows -> {detail_path}")
    print(f"Wrote summary       -> {summary_path}")
    peer = next(r for r in summary if r["company_key"] == PEER_KEY)
    pretty = ", ".join(f"{f}={peer[f]:.3f}" for f in RATIO_FIELDS if peer[f] is not None)
    print(f"PEER_MEDIAN: {pretty}")
    print("Reminder: these are COMPANY-WIDE ratios used as aniline proxies. "
          "Freeze them as the markup anchor; band them wide in the Monte Carlo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
