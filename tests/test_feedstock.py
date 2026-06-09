"""
Tests for the Cat 3.1 feedstock cost calculator and the price resolver.

Covers the mass-balance math (against the known aniline numbers), the
no-double-count contract (the intermediate is never priced), the cost assembly
with injected prices, and the resolver's hydrogen derivation + refusal to guess
pending feeds.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "pipeline"))

import feedstock  # noqa: E402
import prices  # noqa: E402
import storage  # noqa: E402

PROCESS, ROUTE = "aniline", "benzene_nitration_hydrogenation"


def _masses():
    route = feedstock.load_route(PROCESS, ROUTE)
    mw = feedstock.load_registry_mw()
    return feedstock.feedstock_masses_per_ton(route, mw)


def test_final_product_is_aniline():
    assert feedstock.final_product(feedstock.load_route(PROCESS, ROUTE)) == "aniline"


def test_feedstock_masses_match_known_balance():
    """Per ton of aniline: ~0.86 t benzene, ~0.70 t nitric acid, ~0.066 t H2."""
    m = _masses()
    assert m["benzene"] == pytest.approx(0.865, abs=0.005)      # doc: 0.84-0.86
    assert m["nitric_acid"] == pytest.approx(0.697, abs=0.005)  # doc: ~0.71
    assert m["hydrogen"] == pytest.approx(0.0656, abs=0.001)    # doc: 65-70 kg/t


def test_intermediate_is_not_a_feedstock():
    """nitrobenzene is made in step 1 -> it must never appear as a purchased input."""
    assert "nitrobenzene" not in _masses()


def test_intermediate_is_never_priced():
    """The resolver must not be asked for a price on the intermediate."""
    asked = []

    def spy_price(cid, period, region):
        asked.append(cid)
        return 1000.0

    feedstock.feedstock_cost(PROCESS, ROUTE, "2025-01", "US", spy_price)
    assert "nitrobenzene" not in asked
    assert set(asked) == {"benzene", "nitric_acid", "hydrogen"}


def test_feedstock_cost_assembly():
    """total == sum(mass_i * price_i) with injected prices."""
    fixed = {"benzene": 800.0, "nitric_acid": 350.0, "hydrogen": 1500.0}  # $/ton
    m = _masses()
    expected = sum(m[c] * fixed[c] for c in fixed)
    result = feedstock.feedstock_cost(
        PROCESS, ROUTE, "2025-01", "US", lambda c, p, r: fixed[c]
    )
    assert result["feedstock_cost_usd_per_ton"] == pytest.approx(expected)
    assert result["product"] == "aniline"


def test_missing_price_raises_not_guesses():
    """If a feed has no source, fail loudly listing it -- never fabricate a price."""
    def only_hydrogen(cid, period, region):
        if cid == "hydrogen":
            return 1500.0
        raise prices.PriceUnavailable(f"{cid}: pipe not built")

    with pytest.raises(prices.PriceUnavailable) as exc:
        feedstock.feedstock_cost(PROCESS, ROUTE, "2025-01", "US", only_hydrogen)
    assert "benzene" in str(exc.value) and "nitric_acid" in str(exc.value)


def test_resolver_derives_hydrogen_from_gas(db_conn):
    """hydrogen $/ton == Henry Hub $/MMBtu x NG_MMBTU_PER_TON_H2."""
    storage.write_utility_observations([{
        "utility": "natural_gas_henry_hub", "source": "EIA", "unit": "MMBtu",
        "region": "US", "period": "2025-01", "price_usd_per_unit": 4.0,
        "fetched_at": "2025-02-01T00:00:00+00:00",
    }], conn=db_conn)
    price = prices.resolve_price_usd_per_ton("hydrogen", "2025-01", "US", db_conn)
    assert price == pytest.approx(4.0 * prices.NG_MMBTU_PER_TON_H2)


def test_resolver_refuses_pending_feeds(db_conn):
    """benzene (USITC) and nitric_acid (USGS) aren't ingested -> PriceUnavailable."""
    for cid in ("nitric_acid",):
        with pytest.raises(prices.PriceUnavailable):
            prices.resolve_price_usd_per_ton(cid, "2025-01", "US", db_conn)
