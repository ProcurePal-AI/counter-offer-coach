"""Resolver tests for the post-2.3/2.4 state: benzene resolves (USITC), NULL unit
values are skipped, hydrogen derives from gas, nitric_acid stays unavailable."""
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "pipeline"))
import prices  # noqa: E402
import storage  # noqa: E402

PRICE_COLS = ["chemical_id", "source", "region", "period", "price_usd_per_kg",
              "fetched_at", "hts_code", "grade", "assessment_type"]


def _row(chemical, period, price):
    return {"chemical_id": chemical, "source": "USITC", "region": "US_IMPORTS_ALL_ORIGINS",
            "period": period, "price_usd_per_kg": price,
            "fetched_at": "2026-06-05T00:00:00+00:00", "hts_code": "29022000",
            "grade": None, "assessment_type": None}


def test_benzene_resolves_and_skips_nulls(db_conn):
    # a NULL-price month (USITC keeps these) then a real one; resolver must
    # ignore the NULL and return the real $/ton.
    storage.write_price_observations([_row("benzene", "2025-01", None)], conn=db_conn)
    storage.write_price_observations([_row("benzene", "2025-01", 0.95)], conn=db_conn)
    assert prices.resolve_price_usd_per_ton("benzene", "2025-01", "US", db_conn) == pytest.approx(950.0)


def test_all_null_benzene_raises(db_conn):
    storage.write_price_observations([_row("benzene", "2025-01", None)], conn=db_conn)
    with pytest.raises(prices.PriceUnavailable):
        prices.resolve_price_usd_per_ton("benzene", "2025-01", "US", db_conn)


def test_hydrogen_from_gas(db_conn):
    storage.write_utility_observations([{
        "utility": "natural_gas_henry_hub", "source": "EIA", "unit": "MMBtu",
        "region": "US", "period": "2025-01", "price_usd_per_unit": 4.0,
        "fetched_at": "2025-02-01T00:00:00+00:00"}], conn=db_conn)
    assert prices.resolve_price_usd_per_ton("hydrogen", "2025-01", "US", db_conn) == pytest.approx(660.0)


def test_nitric_acid_still_unavailable(db_conn):
    with pytest.raises(prices.PriceUnavailable):
        prices.resolve_price_usd_per_ton("nitric_acid", "2025-01", "US", db_conn)
