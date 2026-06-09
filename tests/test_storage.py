"""Storage layer tests: the Postgres columns must match the schema contracts, and
the writers must round-trip rows into their tables.

All DB-backed tests use the `db_conn` fixture (tests/conftest.py), which runs them
in an isolated throwaway schema and skips when DATABASE_URL is unset.
"""

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import storage  # noqa: E402
from conftest import TEST_SCHEMA  # noqa: E402

SCHEMA_DIR = ROOT / "docs" / "schema"


def _db_columns(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            (TEST_SCHEMA, table),
        )
        return [r[0] for r in cur.fetchall()]


def test_observation_columns_match_schema(db_conn):
    """utility/price_observations columns == the schema's item properties, exactly."""
    schema = yaml.safe_load((SCHEMA_DIR / "market_observations.schema.yaml").read_text())
    for table in ("price_observations", "utility_observations"):
        expected = set(schema[table]["items"]["properties"].keys())
        actual = set(_db_columns(db_conn, table))
        assert actual == expected, f"{table}: {actual ^ expected} differ"


def test_chemicals_columns_match_schema(db_conn):
    """chemicals columns == schema properties plus the structural `name` key."""
    schema = yaml.safe_load((SCHEMA_DIR / "chemical_registry.schema.yaml").read_text())
    expected = set(schema["chemicals"]["additionalProperties"]["properties"].keys())
    expected.add("name")  # registry map key -> primary-key column
    actual = set(_db_columns(db_conn, "chemicals"))
    assert actual == expected, f"chemicals: {actual ^ expected} differ"


def test_utility_round_trip(db_conn):
    row = {"utility": "electricity_industrial", "source": "EIA", "unit": "kWh",
           "region": "CA", "period": "2026-01", "price_usd_per_unit": 0.1234,
           "fetched_at": "2026-02-01T00:00:00+00:00"}
    assert storage.write_utility_observations([row], db_conn) == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT region, price_usd_per_unit FROM utility_observations")
        region, price = cur.fetchone()
    assert region == "CA" and price == 0.1234


def test_chemicals_upsert_and_json_hts(db_conn):
    """chemicals upserts on name (no dupes) and stores hts_codes as JSON text."""
    registry = {"chemicals": {"aniline": {
        "cas": "62-53-3", "pubchem_cid": 6115, "iupac_name": "aniline",
        "molecular_formula": "C6H7N", "molecular_weight_g_per_mol": 93.13,
        "hts_codes": ["2921.41.20"], "status": "active"}}}
    storage.write_chemicals(registry, db_conn)
    storage.write_chemicals(registry, db_conn)  # second pull must not duplicate
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM chemicals")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT hts_codes FROM chemicals")
        assert json.loads(cur.fetchone()[0]) == ["2921.41.20"]


def test_producer_filings_round_trip(db_conn):
    """SEC EDGAR filing metadata writes into producer_filings (append-only)."""
    row = {"company_name": "BASF SE", "ticker": "BAS", "source": "SEC_EDGAR",
           "filing_type": "10-K", "filing_date": "2025-02-28",
           "period_end_date": "2024-12-31", "source_url": "https://sec.gov/x",
           "local_file_path": None, "fetched_at": "2026-06-09T00:00:00+00:00",
           "notes": None}
    assert storage.write_producer_filings([row], db_conn) == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT company_name, filing_type, source FROM producer_filings")
        assert cur.fetchone() == ("BASF SE", "10-K", "SEC_EDGAR")
