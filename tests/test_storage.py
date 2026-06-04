"""Storage layer tests: the DB columns must match the schema contracts, and
the connector writers must round-trip rows into their tables."""

import sys
from pathlib import Path

import yaml

# pipeline/ is on sys.path when the connectors run as scripts; mirror that here.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import storage  # noqa: E402

SCHEMA_DIR = ROOT / "docs" / "schema"


def _db_columns(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def test_observation_columns_match_schema(tmp_path):
    """utility/price_observations columns == the schema's item properties, exactly."""
    schema = yaml.safe_load((SCHEMA_DIR / "market_observations.schema.yaml").read_text())
    conn = storage.connect(tmp_path / "t.db")
    try:
        for table in ("price_observations", "utility_observations"):
            expected = set(schema[table]["items"]["properties"].keys())
            actual = set(_db_columns(conn, table))
            assert actual == expected, f"{table}: {actual ^ expected} differ"
    finally:
        conn.close()


def test_chemicals_columns_match_schema(tmp_path):
    """chemicals columns == schema properties plus the structural `name` key."""
    schema = yaml.safe_load((SCHEMA_DIR / "chemical_registry.schema.yaml").read_text())
    expected = set(schema["chemicals"]["additionalProperties"]["properties"].keys())
    expected.add("name")  # registry map key -> primary-key column
    conn = storage.connect(tmp_path / "t.db")
    try:
        actual = set(_db_columns(conn, "chemicals"))
        assert actual == expected, f"chemicals: {actual ^ expected} differ"
    finally:
        conn.close()


def test_utility_round_trip(tmp_path):
    conn = storage.connect(tmp_path / "t.db")
    try:
        row = {"utility": "electricity_industrial", "source": "EIA", "unit": "kWh",
               "region": "CA", "period": "2026-01", "price_usd_per_unit": 0.1234,
               "fetched_at": "2026-02-01T00:00:00+00:00"}
        assert storage.write_utility_observations([row], conn) == 1
        got = conn.execute("SELECT * FROM utility_observations").fetchone()
        assert got["region"] == "CA" and got["price_usd_per_unit"] == 0.1234
    finally:
        conn.close()


def test_chemicals_upsert_and_json_hts(tmp_path):
    """chemicals upserts on name (no dupes) and stores hts_codes as JSON text."""
    conn = storage.connect(tmp_path / "t.db")
    try:
        registry = {"chemicals": {"aniline": {
            "cas": "62-53-3", "pubchem_cid": 6115, "iupac_name": "aniline",
            "molecular_formula": "C6H7N", "molecular_weight_g_per_mol": 93.13,
            "hts_codes": ["2921.41.20"], "status": "active"}}}
        storage.write_chemicals(registry, conn)
        storage.write_chemicals(registry, conn)  # second pull must not duplicate
        rows = conn.execute("SELECT * FROM chemicals").fetchall()
        assert len(rows) == 1
        import json
        assert json.loads(rows[0]["hts_codes"]) == ["2921.41.20"]
    finally:
        conn.close()
