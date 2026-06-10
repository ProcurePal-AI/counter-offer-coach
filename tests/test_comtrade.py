"""Tests for pipeline/comtrade.py.

All offline -- normalization runs against canned Comtrade-shaped dicts. Focus:
the unit-value math, the region-tag convention (imports vs exports never blend),
period handling for annual vs monthly, NULL-on-missing-weight, and the
multi-record accumulation that keeps a period's unit value a true average.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from comtrade import (  # noqa: E402
    normalize_rows,
    region_tag,
    _period,
    _period_list,
)


def _doc(records):
    return {"data": records}


def test_region_tag_distinguishes_flows():
    assert region_tag("DE", "M") == "DE_IMPORTS"
    assert region_tag("CN", "X") == "CN_EXPORTS"
    assert region_tag("jp", "m") == "JP_IMPORTS"  # case-insensitive
    with pytest.raises(ValueError):
        region_tag("DE", "Z")


def test_annual_unit_value_and_period_slotting():
    doc = _doc([{"period": "2023", "primaryValue": 1000.0, "netWgt": 500.0}])
    rows = normalize_rows("aniline", "292141", "DE", "M", doc, freq="A")
    assert len(rows) == 1
    r = rows[0]
    assert r["period"] == "2023-12"          # annual stored at year slot
    assert r["price_usd_per_kg"] == pytest.approx(2.0)  # 1000/500
    assert r["region"] == "DE_IMPORTS"
    assert r["source"] == "COMTRADE"
    assert r["hts_code"] == "292141"
    assert r["grade"] is None and r["assessment_type"] is None


def test_monthly_period_parsing():
    doc = _doc([{"period": "202306", "primaryValue": 750.0, "netWgt": 300.0}])
    rows = normalize_rows("benzene", "290220", "CN", "X", doc, freq="M")
    assert rows[0]["period"] == "2023-06"
    assert rows[0]["region"] == "CN_EXPORTS"
    assert rows[0]["price_usd_per_kg"] == pytest.approx(2.5)


def test_missing_weight_yields_null_price():
    doc = _doc([{"period": "2023", "primaryValue": 1000.0, "netWgt": None}])
    rows = normalize_rows("aniline", "292141", "DE", "M", doc, freq="A")
    assert rows[0]["price_usd_per_kg"] is None


def test_zero_weight_yields_null_price():
    doc = _doc([{"period": "2023", "primaryValue": 1000.0, "netWgt": 0.0}])
    rows = normalize_rows("aniline", "292141", "DE", "M", doc, freq="A")
    assert rows[0]["price_usd_per_kg"] is None


def test_duplicate_period_records_accumulate_to_average():
    """Two records for the same period sum value and weight, so the unit value
    is the combined average, not whichever record came last."""
    doc = _doc([
        {"period": "2023", "primaryValue": 1000.0, "netWgt": 500.0},   # 2.0/kg
        {"period": "2023", "primaryValue": 3000.0, "netWgt": 500.0},   # 6.0/kg
    ])
    rows = normalize_rows("aniline", "292141", "DE", "M", doc, freq="A")
    assert len(rows) == 1
    # combined: 4000 / 1000 = 4.0, not 2.0 or 6.0
    assert rows[0]["price_usd_per_kg"] == pytest.approx(4.0)


def test_empty_data_returns_no_rows():
    assert normalize_rows("aniline", "292141", "DE", "M", {"data": []}, freq="A") == []
    assert normalize_rows("aniline", "292141", "DE", "M", {}, freq="A") == []


def test_rows_sorted_by_period():
    doc = _doc([
        {"period": "2023", "primaryValue": 100.0, "netWgt": 100.0},
        {"period": "2021", "primaryValue": 100.0, "netWgt": 100.0},
        {"period": "2022", "primaryValue": 100.0, "netWgt": 100.0},
    ])
    rows = normalize_rows("aniline", "292141", "DE", "M", doc, freq="A")
    assert [r["period"] for r in rows] == ["2021-12", "2022-12", "2023-12"]


def test_period_list_annual_inclusive():
    periods = _period_list(years=2, freq="A").split(",")
    # current year minus 2 .. current year => 3 entries
    assert len(periods) == 3
    assert all(len(p) == 4 for p in periods)


def test_period_helper_rejects_garbage():
    assert _period("not-a-date", "A") is None
    assert _period(None, "M") is None
    assert _period("2023", "A") == "2023-12"
    assert _period("202312", "M") == "2023-12"


def test_schema_columns_present():
    """Every row must carry exactly the 9 price_observations columns."""
    doc = _doc([{"period": "2023", "primaryValue": 1000.0, "netWgt": 500.0}])
    rows = normalize_rows("aniline", "292141", "DE", "M", doc, freq="A")
    expected = {"chemical_id", "source", "region", "period", "price_usd_per_kg",
                "fetched_at", "hts_code", "grade", "assessment_type"}
    assert set(rows[0]) == expected
