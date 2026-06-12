"""Step-4 China hydrogen: two-branch derivation, region routing, VAT invariant.

Pure unit tests -- the coal observation is monkeypatched so no DB is needed.
Numbers cross-check the docs/HYDROGEN_CN.md derivation:
  standalone: coal $/GJ x 0.232 / 0.50
  byproduct:  coal $/GJ x 0.120
and the IEA/ICSC 2023 Table 15 falsification window (CN unabated 1.16-2.32 $/kg).
"""
from __future__ import annotations

import math

import pytest

from engine import prices
from pipeline import sunsirs


# A coal price of 700 RMB/t ex-VAT at 7.10 CNY/USD and 23.0 GJ/t:
#   700/7.10/23.0 = 4.2865 $/GJ -> stored as $/kg: 700/7.10/1000 = 0.09859
COAL_USD_PER_KG = 700.0 / 7.10 / 1000.0
COAL_USD_PER_GJ = COAL_USD_PER_KG * 1000.0 / prices.COAL_GJ_PER_TON


@pytest.fixture
def coal_in_store(monkeypatch):
    """Patch the observation reader: thermal coal exists in the CN family."""
    def fake_obs(conn, chemical_id, period, region):
        if chemical_id == prices.THERMAL_COAL_CHEMICAL_ID:
            return COAL_USD_PER_KG
        return None
    monkeypatch.setattr(prices, "_price_observation_usd_per_kg", fake_obs)


def test_standalone_branch_formula(coal_in_store):
    usd_per_ton = prices.hydrogen_price_usd_per_ton_cn(
        None, "2026-05", "CN_SPOT", sourcing="standalone_gasification")
    expected = COAL_USD_PER_GJ * 0.232 / 0.50 * 1000.0
    assert math.isclose(usd_per_ton, expected, rel_tol=1e-9)
    # ~1.99 $/kg at 700 RMB/t -- inside the IEA window
    assert 1.16 <= usd_per_ton / 1000.0 <= 2.32


def test_byproduct_branch_formula_and_ordering(coal_in_store):
    by = prices.hydrogen_price_usd_per_ton_cn(
        None, "2026-05", "CN_SPOT", sourcing="integrated_byproduct")
    stand = prices.hydrogen_price_usd_per_ton_cn(
        None, "2026-05", "CN_SPOT", sourcing="standalone_gasification")
    assert math.isclose(by, COAL_USD_PER_GJ * 0.120 * 1000.0, rel_tol=1e-9)
    # The by-product (fuel-value) branch must ALWAYS sit below standalone:
    # 0.120 < 0.232/0.50 = 0.464 structurally, so switching an integrated
    # supplier to this branch can only lower the floor.
    assert by < stand


def test_falsification_window_over_normal_coal_range(coal_in_store, monkeypatch):
    """Standalone cost stays within ~1.0-3.3 $/kg across 410-1100 RMB/t coal."""
    for rmb in (410, 600, 820, 1100):
        usd_kg_coal = rmb / 7.10 / 1000.0
        monkeypatch.setattr(prices, "_price_observation_usd_per_kg",
                            lambda c, ch, p, r, v=usd_kg_coal:
                            v if ch == prices.THERMAL_COAL_CHEMICAL_ID else None)
        h2 = prices.hydrogen_price_usd_per_ton_cn(
            None, "2026-05", "CN_SPOT", sourcing="standalone_gasification") / 1000.0
        assert 1.0 <= h2 <= 3.3, f"sanity window breach at {rmb} RMB/t: {h2:.2f} $/kg"


def test_resolve_routes_cn_hydrogen_to_coal_branch(coal_in_store, monkeypatch):
    """region_family decides the basis: CN -> coal derivation, US -> gas SMR."""
    monkeypatch.setattr(prices, "gas_price_usd_per_mmbtu",
                        lambda conn, period, region: 3.0)
    cn = prices.resolve_price_usd_per_ton("hydrogen", "2026-05", "CN_SPOT", None)
    us = prices.resolve_price_usd_per_ton("hydrogen", "2026-05", "TX", None)
    assert math.isclose(us, 3.0 * prices.NG_MMBTU_PER_TON_H2, rel_tol=1e-9)
    assert not math.isclose(cn, us, rel_tol=0.25)  # different bases, different numbers
    # default sourcing is the conservative standalone branch
    assert math.isclose(cn, COAL_USD_PER_GJ * 0.232 / 0.50 * 1000.0, rel_tol=1e-9)


def test_missing_coal_series_fails_loudly(monkeypatch):
    monkeypatch.setattr(prices, "_price_observation_usd_per_kg",
                        lambda *a: None)
    with pytest.raises(prices.PriceUnavailable, match="thermal_coal"):
        prices.hydrogen_price_usd_per_ton_cn(None, "2026-05", "CN_SPOT",
                                             sourcing="standalone_gasification")


def test_unknown_sourcing_rejected(coal_in_store):
    with pytest.raises(ValueError, match="sourcing"):
        prices.hydrogen_price_usd_per_ton_cn(None, "2026-05", "CN_SPOT",
                                             sourcing="magic_hydrogen")


def test_config_default_branch_is_standalone():
    assert prices._hydrogen_cn_sourcing_from_config() == "standalone_gasification"


def test_sunsirs_vat_strip_then_fx():
    """Global invariant: VAT stripped in RMB, then FX. 1130 RMB incl. 13% VAT
    at 7.10 CNY/USD must store as 1000/7.10/1000 USD/kg."""
    records = [{"chemical_id": "thermal_coal", "period": "2026-05",
                "grade": None, "price_rmb_per_ton": 1130.0}]
    rows = sunsirs.normalize_rows(records, rate_fn=lambda p: 7.10,
                                  fetched_at="2026-06-11T00:00:00Z")
    assert len(rows) == 1
    assert math.isclose(rows[0]["price_usd_per_kg"], 1000.0 / 7.10 / 1000.0,
                        rel_tol=1e-4)  # connector rounds to 6 dp
    assert rows[0]["region"] == "CN_SPOT"


def test_sunsirs_thermal_coal_commodity_mapping():
    for name in ("thermal coal", "Steam Coal", "power coal"):
        assert sunsirs.CHEMICAL_BY_COMMODITY[name.casefold()] == "thermal_coal"
