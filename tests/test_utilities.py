"""Tests for engine/utilities.py (Cat 3.2 utility cost) and the utility resolver."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine import utilities, feedstock, prices
from pipeline import storage

PROC, ROUTE = "aniline", "benzene_nitration_hydrogenation"


def _route_and_mw():
    route = feedstock.load_route(PROC, ROUTE)
    return route, feedstock.load_registry_mw()


# --- step-output mass tracing ---------------------------------------------
def test_step_output_masses():
    route, mw = _route_and_mw()
    masses = utilities.step_output_masses_per_ton(route, mw)
    # Final step (hydrogenation -> aniline) is 1 ton output per ton product.
    assert masses["hydrogenation"] == pytest.approx(1.0, rel=1e-9)
    # Nitrobenzene per ton aniline = MW_nb/MW_aniline / yield_hydrogenation.
    expected_nb = (mw["nitrobenzene"] / mw["aniline"]) / 0.99
    assert masses["nitration"] == pytest.approx(expected_nb, rel=1e-6)


# --- cost assembly with an injected (deterministic) price fn ---------------
def _fixed_prices(kind, period, region):
    return {"electricity": 0.075, "steam": 3.00}[kind]  # $/kWh, $/GJ


def test_utility_cost_assembly():
    route, mw = _route_and_mw()
    masses = utilities.step_output_masses_per_ton(route, mw)
    res = utilities.utility_cost(PROC, ROUTE, "2025-01", "US", _fixed_prices)

    # Electricity qty = 120*nitration_mass + 180*1.0 ; steam = 1.5*nb + 2.5*1.0
    exp_kwh = 120 * masses["nitration"] + 180 * masses["hydrogenation"]
    exp_gj = 1.5 * masses["nitration"] + 2.5 * masses["hydrogenation"]
    assert res["by_utility"]["electricity"]["qty_per_ton"] == pytest.approx(exp_kwh, rel=1e-6)
    assert res["by_utility"]["steam"]["qty_per_ton"] == pytest.approx(exp_gj, rel=1e-6)

    exp_total = exp_kwh * 0.075 + exp_gj * 3.00
    assert res["utility_cost_usd_per_ton"] == pytest.approx(exp_total, rel=1e-9)


def test_catalyst_reported_separately():
    route, mw = _route_and_mw()
    masses = utilities.step_output_masses_per_ton(route, mw)
    res = utilities.utility_cost(PROC, ROUTE, "2025-01", "US", _fixed_prices)
    exp_cat = 3.6 * masses["nitration"] + 10.0 * masses["hydrogenation"]
    assert res["catalyst_cost_usd_per_ton"] == pytest.approx(exp_cat, rel=1e-6)
    # Catalyst is NOT folded into the metered-utility total.
    assert "catalyst" not in res["by_utility"]


def test_missing_utility_price_raises():
    def only_electricity(kind, period, region):
        if kind == "steam":
            raise prices.PriceUnavailable("steam: gas not in store")
        return 0.075
    with pytest.raises(prices.PriceUnavailable) as exc:
        utilities.utility_cost(PROC, ROUTE, "2025-01", "US", only_electricity)
    assert "steam" in str(exc.value)


# --- steam derivation in the resolver --------------------------------------
def test_steam_derives_from_gas():
    conn = storage.connect(Path(tempfile.mktemp(suffix=".db")))
    try:
        storage.write_utility_observations([{
            "utility": "natural_gas_henry_hub", "source": "EIA", "unit": "MMBtu",
            "region": "US", "period": "2025-01", "price_usd_per_unit": 2.50,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }], conn=conn)
        steam = prices.steam_price_usd_per_gj(conn, "2025-01", "US")
        # 2.50 * 0.9478171 / 0.80
        assert steam == pytest.approx(2.50 * prices.MMBTU_PER_GJ / prices.BOILER_EFFICIENCY, rel=1e-9)
    finally:
        conn.close()


# --- end-to-end through storage + the real resolver ------------------------
def test_utility_cost_end_to_end():
    conn = storage.connect(Path(tempfile.mktemp(suffix=".db")))
    now = datetime.now(timezone.utc).isoformat()
    try:
        storage.write_utility_observations([
            {"utility": "natural_gas_henry_hub", "source": "EIA", "unit": "MMBtu",
             "region": "US", "period": "2025-01", "price_usd_per_unit": 2.50, "fetched_at": now},
            {"utility": "electricity_industrial", "source": "EIA", "unit": "kWh",
             "region": "US", "period": "2025-01", "price_usd_per_unit": 0.075, "fetched_at": now},
        ], conn=conn)
        res = utilities.utility_cost(
            PROC, ROUTE, "2025-01", "US",
            lambda k, p, r: prices.resolve_utility_price_usd_per_unit(k, p, r, conn),
        )
        assert res["utility_cost_usd_per_ton"] > 0
        assert set(res["by_utility"]) == {"electricity", "steam"}
    finally:
        conn.close()
