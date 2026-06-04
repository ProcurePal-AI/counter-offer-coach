"""
Config + schema integrity tests.

Guard the contracts the should-cost engine depends on:
  1. every config validates against its JSON-Schema in docs/schema/
  2. every chemical_id referenced by a process config exists in the registry
  3. known molecular weights are correct (catches a wrong PubChem CID)
  4. the market-observations schema stays source-agnostic: USITC, ICIS, and EIA
     rows all validate, so re-adding a required field can't silently break a feed

These are cheap and would have caught both the nitrobenzene-CID bug and the
hts_code-required regression.
"""

from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "docs" / "schema"
CONFIG = ROOT / "config"


def _load(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8"))


# ---- 1. configs validate against their schemas ----------------------------

def test_chemical_registry_matches_schema():
    schema = _load(SCHEMA / "chemical_registry.schema.yaml")["chemicals"]
    data = _load(CONFIG / "chemical_registry.yaml")["chemicals"]
    errors = [e.message for e in Draft202012Validator(schema).iter_errors(data)]
    assert not errors, f"registry fails schema: {errors}"


def test_aniline_config_matches_schema():
    schema = _load(SCHEMA / "process_config.schema.yaml")["processes"]
    data = _load(CONFIG / "aniline.yaml")["processes"]
    errors = [e.message for e in Draft202012Validator(schema).iter_errors(data)]
    assert not errors, f"aniline.yaml fails schema: {errors}"


# ---- 2. referential integrity: config chemical_ids subset of registry -----

def test_config_chemical_ids_exist_in_registry():
    registry = set(_load(CONFIG / "chemical_registry.yaml")["chemicals"])
    processes = _load(CONFIG / "aniline.yaml")["processes"]
    referenced: set[str] = set()
    for proc in processes.values():
        for route in proc["routes"].values():
            for step in route["steps"]:
                referenced |= {i["chemical_id"] for i in step["inputs"]}
                referenced.add(step["main_output"])
                referenced |= {c["chemical_id"] for c in step.get("co_products", [])}
    missing = referenced - registry
    assert not missing, f"referenced in config but missing from registry: {sorted(missing)}"


# ---- 3. molecular-weight sanity (catches a wrong CID) ---------------------

KNOWN_MW = {
    "benzene": 78.11,
    "nitrobenzene": 123.11,
    "aniline": 93.13,
    "nitric_acid": 63.01,
    "hydrogen": 2.016,
    "water": 18.015,
}


@pytest.mark.parametrize("name,expected", list(KNOWN_MW.items()))
def test_known_molecular_weights(name, expected):
    chem = _load(CONFIG / "chemical_registry.yaml")["chemicals"]
    assert name in chem, f"{name} missing from registry"
    got = chem[name]["molecular_weight_g_per_mol"]
    assert abs(got - expected) < 0.5, f"{name} MW {got}, expected ~{expected} (wrong CID?)"


# ---- 4. market-observations schema stays source-agnostic ------------------

def _validator(table: str) -> Draft202012Validator:
    s = _load(SCHEMA / "market_observations.schema.yaml")
    return Draft202012Validator(s[table]["items"])


def test_usitc_price_row_validates():
    row = {"chemical_id": "aniline", "source": "USITC", "hts_code": "2921.41.20",
           "region": "US", "period": "2025-01", "price_usd_per_kg": 1.4,
           "fetched_at": "2025-02-01T00:00:00+00:00"}
    assert not list(_validator("price_observations").iter_errors(row))


def test_icis_price_row_validates():
    # ICIS carries grade + assessment_type and NO hts_code -- guards the socket.
    row = {"chemical_id": "aniline", "source": "ICIS", "grade": "tech",
           "assessment_type": "contract", "region": "NWE", "period": "2025-01",
           "price_usd_per_kg": 1.55, "fetched_at": "2025-02-01T00:00:00+00:00"}
    errors = [e.message for e in _validator("price_observations").iter_errors(row)]
    assert not errors, f"ICIS row rejected (is hts_code required again?): {errors}"


def test_eia_utility_row_validates():
    row = {"utility": "natural_gas_henry_hub", "source": "EIA", "unit": "MMBtu",
           "region": "US", "period": "2025-01", "price_usd_per_unit": 3.5,
           "fetched_at": "2025-02-01T00:00:00+00:00"}
    assert not list(_validator("utility_observations").iter_errors(row))


# ---- 5. process-graph integrity: intermediates vs purchased feedstocks -----
# Locks the engine's cost-resolution contract before the engine exists:
#   * an input also produced as an earlier step's main_output is an INTERMEDIATE
#     (cost flows from that step) and must be produced BEFORE it's consumed;
#   * an input produced by no step is a PURCHASED FEEDSTOCK and must have a
#     registry identity so the pricing layer can resolve a price for it.

def test_intermediates_produced_before_consumed():
    processes = _load(CONFIG / "aniline.yaml")["processes"]
    for proc in processes.values():
        for route_name, route in proc["routes"].items():
            produced = {s["main_output"]: s["order"] for s in route["steps"]}
            for step in route["steps"]:
                for inp in step["inputs"]:
                    cid = inp["chemical_id"]
                    if cid in produced:  # intermediate
                        assert produced[cid] < step["order"], (
                            f"{route_name}: '{cid}' consumed at order {step['order']} "
                            f"but produced at order {produced[cid]} (must be earlier)"
                        )


def test_purchased_feedstocks_exist_in_registry():
    registry = set(_load(CONFIG / "chemical_registry.yaml")["chemicals"])
    processes = _load(CONFIG / "aniline.yaml")["processes"]
    for proc in processes.values():
        for route_name, route in proc["routes"].items():
            produced = {s["main_output"] for s in route["steps"]}
            for step in route["steps"]:
                for inp in step["inputs"]:
                    cid = inp["chemical_id"]
                    if cid not in produced:  # purchased feedstock
                        assert cid in registry, (
                            f"{route_name}: feedstock '{cid}' not in registry"
                        )
