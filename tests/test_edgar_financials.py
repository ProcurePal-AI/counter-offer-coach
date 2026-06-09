"""Tests for pipeline/edgar_financials.py.

All tests run against a canned companyfacts-shaped dict -- no network. They
exercise the parts that can silently corrupt the markup anchor: fiscal-year
dedup (comparative columns repeat years), the FY-but-quarterly-duration trap,
revenue tag fallback across years, and the peer-median summary that markup.py
consumes.
"""

import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from edgar_financials import (  # noqa: E402
    PEER_KEY,
    AnnualRatios,
    compute_annual_ratios,
    load_peer_median_ratios,
    summarize,
)


def _fact(fy, val, start, end, form="10-K", fp="FY", filed="2024-02-15"):
    return {"fy": fy, "val": val, "start": start, "end": end,
            "form": form, "fp": fp, "filed": filed}


def _facts_doc(gaap: dict) -> dict:
    return {"entityName": "Test Chem Corp", "cik": 12345,
            "facts": {"us-gaap": gaap}}


def test_basic_ratios_one_year():
    doc = _facts_doc({
        "Revenues": {"units": {"USD": [
            _fact(2023, 1000.0, "2023-01-01", "2023-12-31")]}},
        "SellingGeneralAndAdministrativeExpense": {"units": {"USD": [
            _fact(2023, 110.0, "2023-01-01", "2023-12-31")]}},
        "DepreciationDepletionAndAmortization": {"units": {"USD": [
            _fact(2023, 70.0, "2023-01-01", "2023-12-31")]}},
        "OperatingIncomeLoss": {"units": {"USD": [
            _fact(2023, 80.0, "2023-01-01", "2023-12-31")]}},
        "CostOfGoodsAndServicesSold": {"units": {"USD": [
            _fact(2023, 750.0, "2023-01-01", "2023-12-31")]}},
    })
    rows = compute_annual_ratios(doc, "TEST")
    assert len(rows) == 1
    r = rows[0]
    assert r.fiscal_year == 2023
    assert r.sga_pct == pytest.approx(0.11)
    assert r.da_pct == pytest.approx(0.07)
    assert r.ebit_margin_pct == pytest.approx(0.08)
    assert r.ebitda_margin_pct == pytest.approx(0.15)
    assert r.gross_margin_pct == pytest.approx(0.25)
    assert r.cik == "0000012345"


def test_comparative_duplicates_keep_latest_filed():
    """FY2022 appears in both the 2022 and 2023 10-Ks; latest end/filed wins."""
    doc = _facts_doc({
        "Revenues": {"units": {"USD": [
            _fact(2022, 900.0, "2022-01-01", "2022-12-31", filed="2023-02-10"),
            _fact(2022, 905.0, "2022-01-01", "2022-12-31", filed="2024-02-10"),  # restated
        ]}},
    })
    rows = compute_annual_ratios(doc, "TEST")
    assert len(rows) == 1
    assert rows[0].revenue_usd == pytest.approx(905.0)


def test_fy_labeled_quarter_is_excluded():
    """A fact tagged fp=FY but spanning ~90 days must not pass as annual."""
    doc = _facts_doc({
        "Revenues": {"units": {"USD": [
            _fact(2023, 250.0, "2023-10-01", "2023-12-31"),          # bogus FY slice
            _fact(2023, 1000.0, "2023-01-01", "2023-12-31"),         # real annual
        ]}},
    })
    rows = compute_annual_ratios(doc, "TEST")
    assert rows[0].revenue_usd == pytest.approx(1000.0)


def test_non_10k_forms_excluded():
    doc = _facts_doc({
        "Revenues": {"units": {"USD": [
            _fact(2023, 999.0, "2023-01-01", "2023-12-31", form="10-Q", fp="FY"),
        ]}},
    })
    assert compute_annual_ratios(doc, "TEST") == []


def test_revenue_tag_fallback_across_years():
    """Old years tagged Revenues, new years on the ASC 606 tag -- both resolve."""
    doc = _facts_doc({
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            _fact(2023, 1000.0, "2023-01-01", "2023-12-31")]}},
        "Revenues": {"units": {"USD": [
            _fact(2019, 800.0, "2019-01-01", "2019-12-31")]}},
    })
    rows = compute_annual_ratios(doc, "TEST")
    assert {r.fiscal_year: r.revenue_usd for r in rows} == {2023: 1000.0, 2019: 800.0}


def test_missing_concept_yields_none_not_zero():
    doc = _facts_doc({
        "Revenues": {"units": {"USD": [
            _fact(2023, 1000.0, "2023-01-01", "2023-12-31")]}},
    })
    r = compute_annual_ratios(doc, "TEST")[0]
    assert r.sga_pct is None and r.da_pct is None and r.ebitda_margin_pct is None


def _row(key, fy, sga, da, ebit):
    return AnnualRatios(company_key=key, company_name=key, cik="0000000001",
                        fiscal_year=fy, revenue_usd=1000.0, sga_pct=sga, da_pct=da,
                        ebit_margin_pct=ebit,
                        ebitda_margin_pct=(ebit + da) if (ebit is not None and da is not None) else None,
                        gross_margin_pct=None)


def test_summarize_medians_and_peer_row():
    rows = [
        _row("A", 2023, 0.10, 0.06, 0.08),
        _row("A", 2022, 0.12, 0.08, 0.10),
        _row("A", 2021, 0.11, 0.07, 0.09),
        _row("B", 2023, 0.20, 0.10, 0.05),
    ]
    summary = summarize(rows)
    a = next(s for s in summary if s["company_key"] == "A")
    peer = next(s for s in summary if s["company_key"] == PEER_KEY)
    assert a["sga_pct"] == pytest.approx(0.11)          # median of 3 years
    assert peer["sga_pct"] == pytest.approx(0.155)      # median of company medians
    assert peer["n_years"] == 4


def test_load_peer_median_roundtrip(tmp_path):
    path = tmp_path / "summary.csv"
    fields = ["company_key", "company_name", "n_years", "sga_pct", "da_pct",
              "ebit_margin_pct", "ebitda_margin_pct", "gross_margin_pct"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerow({"company_key": PEER_KEY, "company_name": "x", "n_years": 3,
                    "sga_pct": 0.11, "da_pct": 0.07, "ebit_margin_pct": 0.08,
                    "ebitda_margin_pct": 0.15, "gross_margin_pct": ""})
    ratios = load_peer_median_ratios(path)
    assert ratios["sga_pct"] == pytest.approx(0.11)
    assert "gross_margin_pct" not in ratios  # empty cells are skipped


def test_load_peer_median_missing_required_raises(tmp_path):
    path = tmp_path / "summary.csv"
    fields = ["company_key", "company_name", "n_years", "sga_pct", "da_pct",
              "ebit_margin_pct", "ebitda_margin_pct", "gross_margin_pct"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerow({"company_key": PEER_KEY, "company_name": "x", "n_years": 1,
                    "sga_pct": 0.11, "da_pct": "", "ebit_margin_pct": 0.08})
    with pytest.raises(ValueError):
        load_peer_median_ratios(path)
