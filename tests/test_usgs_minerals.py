"""Tests for pipeline/usgs_minerals.py (USGS ammonia connector, pipe 2.4).

Covers unit detection/conversion, table + narrative extraction, regression guards
for the two silent-wrong-data bugs the previous version had (wrong-year leak,
wrong-commodity leak), the schema-aligned row shape, and the full bridge:
connector row -> storage.price_observations -> resolver reads ammonia as $/ton.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from pipeline import usgs_minerals as u
from pipeline import storage
from engine import prices


# --- unit detection & conversion ------------------------------------------
def test_short_ton_conversion():
    per_kg, unit, note = u._to_usd_per_kg(600.0, "short ton")
    assert unit == "short ton"
    assert note == ""
    assert per_kg == pytest.approx(600.0 / 907.18474, rel=1e-9)


def test_metric_ton_conversion():
    per_kg, unit, note = u._to_usd_per_kg(600.0, "metric ton")
    assert unit == "metric ton"
    assert per_kg == pytest.approx(0.6, rel=1e-9)


def test_bare_ton_assumed_short_and_flagged():
    per_kg, unit, note = u._to_usd_per_kg(600.0, "ton")
    assert "short" in unit          # assumed short ton
    assert note                      # carries a verify-me flag
    assert per_kg == pytest.approx(600.0 / 907.18474, rel=1e-9)


# --- table extraction ------------------------------------------------------
def _salient_table():
    return [
        ["Salient Statistics", "2020", "2021", "2022", "2023", "2024e"],
        ["Production", "14000", "14200", "14100", "13900", "14050"],
        ["Price, average, anhydrous ammonia, f.o.b. Gulf Coast, dollars per short ton",
         "300", "500", "1250", "600", "550"],
    ]


def test_table_extraction_maps_years_to_prices():
    out = u._prices_from_tables([_salient_table()])
    assert out[2023] == (600.0, "short ton")
    assert out[2022] == (1250.0, "short ton")


def test_table_years_are_scoped_to_that_table():
    # A stray year mentioned elsewhere must not create a phantom column.
    out = u._prices_from_tables([_salient_table()])
    assert set(out) == {2020, 2021, 2022, 2023, 2024}


# --- narrative extraction --------------------------------------------------
def test_narrative_requires_ammonia_and_year():
    text = "In 2023, the average anhydrous ammonia price was $600 per short ton."
    assert u._extract_via_narrative(text, 2023) == {2023: (600.0, "short ton")}


def test_regression_no_wrong_year_leak():
    # OLD BUG: returned the first match regardless of year. Asking for 2024 when
    # only a 2022 figure is present must NOT return that 2022 price as 2024.
    text = "In 2022, the average ammonia price was $1,250 per short ton. Later it eased."
    assert u._extract_via_narrative(text, 2024) == {}


def test_regression_no_wrong_commodity_leak():
    # OLD BUG: broad fallback grabbed the last "$N per short ton" anywhere, even a
    # urea/DAP figure. With no ammonia+year sentence, we must return nothing.
    text = "Urea sold for $600 per short ton. DAP moved at $720 per short ton in 2023."
    assert u._extract_via_narrative(text, 2023) == {}


# --- row shape -------------------------------------------------------------
def test_build_price_row_shape_and_period():
    rec = u._build_record(2023, 600.0, "short ton", "file://x", "table")
    row = u.build_price_row(rec, fetched_at="2026-01-01T00:00:00+00:00")
    assert row["chemical_id"] == "ammonia"
    assert row["source"] == "USGS"
    assert row["period"] == "2023-12"            # annual avg -> year-end YYYY-MM slot
    assert row["price_usd_per_kg"] == pytest.approx(600.0 / 907.18474, rel=1e-6)
    # source-specific columns are absent -> storage stores them NULL
    assert "hts_code" not in row


def test_row_period_matches_schema_pattern():
    import re
    rec = u._build_record(2024, 550.0, "short ton", "file://x", "table")
    row = u.build_price_row(rec)
    assert re.match(r"^[0-9]{4}-[0-9]{2}$", row["period"])


# --- end-to-end bridge: connector -> storage -> resolver -------------------
def test_bridge_resolver_reads_ammonia(db_conn):
    rec = u._build_record(2023, 600.0, "short ton", "file://x", "table")
    storage.write_price_observations([u.build_price_row(rec)], conn=db_conn)
    per_ton = prices.resolve_price_usd_per_ton("ammonia", "2023-12", "US", db_conn)
    # 600 $/short ton == 661.39 $/metric ton
    assert per_ton == pytest.approx(600.0 / 0.90718474, rel=1e-6)
