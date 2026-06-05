"""
storage.py -- PostgreSQL (Neon) store for the should-cost demo pipeline.

Connection is read from the DATABASE_URL environment variable (a Neon
`postgresql://...` connection string), loaded from the project-root .env via
python-dotenv. psycopg2 is the driver.

Three tables, columns taken verbatim from the schema contracts:
  * utility_observations  <- docs/schema/market_observations.schema.yaml   (eia.py)
  * price_observations    <- docs/schema/market_observations.schema.yaml   (usitc.py)
  * chemicals             <- docs/schema/chemical_registry.schema.yaml      (pubchem.py)

Append-only tables (the two *_observations) take raw INSERTs -- no dedup, no
filtering, no outlier removal. Cleaning is a downstream/calibration concern.
`chemicals` is reference identity (one row per substance), so it upserts on name
via ON CONFLICT.

Run directly to create the tables (idempotent) and print a verification sample:
    python pipeline/storage.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extensions import connection as PgConnection

# Load DATABASE_URL from the project-root .env regardless of the current working
# directory (this file lives in pipeline/, .env lives one level up).
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# Backwards-compatible label: eia.py / pubchem.py print `storage.DB_PATH` in their
# log lines. We must NOT print the real DATABASE_URL (it contains the password), so
# this is just a safe human-readable target name.
DB_PATH = "PostgreSQL (Neon)"

# --- DDL -------------------------------------------------------------------
# Columns mirror docs/schema/*.yaml EXACTLY. The only addition is structural:
#   - chemicals.name: the registry is a YAML map keyed by chemical name, so the
#     key has to become a column (it is the natural primary key).
# Array fields (chemicals.hts_codes) are stored as JSON text -- kept as TEXT for
# parity with the SQLite version (Postgres JSONB is a possible future upgrade).
# NOT NULL mirrors each schema's `required` list; nullable fields stay nullable.

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS utility_observations (
    utility            TEXT             NOT NULL,
    source             TEXT             NOT NULL,
    unit               TEXT             NOT NULL,
    region             TEXT             NOT NULL,
    period             TEXT             NOT NULL,
    price_usd_per_unit DOUBLE PRECISION NOT NULL,
    fetched_at         TEXT             NOT NULL
);

CREATE TABLE IF NOT EXISTS price_observations (
    chemical_id      TEXT             NOT NULL,
    source           TEXT             NOT NULL,
    region           TEXT             NOT NULL,
    period           TEXT             NOT NULL,
    price_usd_per_kg DOUBLE PRECISION,
    fetched_at       TEXT             NOT NULL,
    hts_code         TEXT,                 -- nullable: USITC only
    grade            TEXT,                 -- nullable: ICIS only
    assessment_type  TEXT                  -- nullable: 'spot' | 'contract' | NULL
);

CREATE TABLE IF NOT EXISTS chemicals (
    name                       TEXT             PRIMARY KEY,   -- registry map key
    cas                        TEXT,                           -- nullable: PubChem may have no CAS synonym
    pubchem_cid                INTEGER          NOT NULL,
    iupac_name                 TEXT             NOT NULL,
    molecular_formula          TEXT             NOT NULL,
    molecular_weight_g_per_mol DOUBLE PRECISION NOT NULL,
    hts_codes                  TEXT             NOT NULL,       -- JSON array of HTS code strings
    status                     TEXT             NOT NULL
);
"""

# Column order per table -- the INSERT/verification helpers and the schema-match
# test all read these, so the column contract lives in one place.
UTILITY_COLUMNS = [
    "utility",
    "source",
    "unit",
    "region",
    "period",
    "price_usd_per_unit",
    "fetched_at",
]
PRICE_COLUMNS = [
    "chemical_id",
    "source",
    "region",
    "period",
    "price_usd_per_kg",
    "fetched_at",
    "hts_code",
    "grade",
    "assessment_type",
]
CHEMICAL_COLUMNS = [
    "name",
    "cas",
    "pubchem_cid",
    "iupac_name",
    "molecular_formula",
    "molecular_weight_g_per_mol",
    "hts_codes",
    "status",
]


def _dsn() -> str:
    """Return the Neon connection string, or fail with a clear message."""
    try:
        return os.environ["DATABASE_URL"]
    except KeyError:
        raise RuntimeError(
            "DATABASE_URL is not set. Add it to the project-root .env file, e.g.\n"
            "  DATABASE_URL=postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require"
        ) from None


def connect(dsn: str | None = None) -> PgConnection:
    """Open a PostgreSQL connection and ensure the schema exists.

    `dsn` overrides DATABASE_URL (useful for pointing at a separate test database).
    """
    conn = psycopg2.connect(dsn or _dsn())
    # Pin the schema explicitly. Neon's connection pooler can hand back a backend
    # whose session search_path was left pointing at a now-dropped schema by a
    # previous client (e.g. an isolated test), which would otherwise make an
    # unqualified CREATE TABLE fail with "no schema has been selected to create in".
    with conn.cursor() as cur:
        cur.execute("SET search_path TO public")
    conn.commit()
    init_db(conn)
    return conn


def init_db(conn: PgConnection) -> None:
    """Create all tables if they do not already exist (idempotent)."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


def _insert_rows(conn: PgConnection, table: str, columns: list[str], rows: list[dict]) -> int:
    """Append `rows` to `table`, pulling values by column name. Raw insert."""
    if not rows:
        return 0
    placeholders = ", ".join(["%s"] * len(columns))
    col_list = ", ".join(columns)
    sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
    with conn.cursor() as cur:
        cur.executemany(sql, [[r.get(c) for c in columns] for r in rows])
    return len(rows)


def write_utility_observations(rows: list[dict], conn: PgConnection | None = None) -> int:
    """Append normalized EIA utility rows to `utility_observations`. Append-only."""
    own = conn is None
    conn = conn or connect()
    try:
        n = _insert_rows(conn, "utility_observations", UTILITY_COLUMNS, rows)
        if own:
            conn.commit()
        return n
    finally:
        if own:
            conn.close()


def write_price_observations(
    rows: list[dict],
    conn: PgConnection | None = None,
    dry_run: bool = False,
) -> int:
    """Append normalized price rows to `price_observations`. Append-only.

    `dry_run=True` preserves connector validation mode: normalize and print
    counts without mutating the store.
    """
    if dry_run:
        null_count = sum(row.get("price_usd_per_kg") is None for row in rows)
        print(
            f"Dry run: {len(rows)} price observations normalized "
            f"({null_count} with NULL unit value); storage write skipped."
        )
        return 0

    own = conn is None
    conn = conn or connect()
    try:
        n = _insert_rows(conn, "price_observations", PRICE_COLUMNS, rows)
        if own:
            conn.commit()
        return n
    finally:
        if own:
            conn.close()


def write_chemicals(registry: dict, conn: PgConnection | None = None) -> int:
    """Upsert PubChem reference identity into `chemicals`.

    `registry` is the {"chemicals": {name: {...}}} dict pubchem.build_registry()
    returns. Reference identity, not a time series -> upsert on name via
    ON CONFLICT (re-running the PubChem pull refreshes in place, no duplicates).
    """
    own = conn is None
    conn = conn or connect()
    chemicals = registry.get("chemicals", registry)
    rows = []
    for name, rec in chemicals.items():
        row = {"name": name, **rec}
        # hts_codes is a list in the registry; persist as JSON text.
        row["hts_codes"] = json.dumps(rec.get("hts_codes", []))
        rows.append(row)
    placeholders = ", ".join(["%s"] * len(CHEMICAL_COLUMNS))
    col_list = ", ".join(CHEMICAL_COLUMNS)
    update_clause = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in CHEMICAL_COLUMNS if c != "name"
    )
    sql = (
        f"INSERT INTO chemicals ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT (name) DO UPDATE SET {update_clause}"
    )
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, [[r.get(c) for c in CHEMICAL_COLUMNS] for r in rows])
        if own:
            conn.commit()
        return len(rows)
    finally:
        if own:
            conn.close()


def dump_csv(table: str, out_path: Path | str, conn: PgConnection | None = None) -> int:
    """Export an entire table to CSV (header + all rows). Returns the row count.

    Used by CI to attach a human-readable artifact -- a readable projection of one
    table for the run page.
    """
    import csv

    own = conn is None
    conn = conn or connect()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {table}")
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()  # list of tuples in column order
        with out_path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            writer.writerows(rows)
        print(f"Exported {len(rows)} rows from {table} -> {out_path}")
        return len(rows)
    finally:
        if own:
            conn.close()


def verify(conn: PgConnection | None = None, limit: int = 5) -> None:
    """Print row counts and a small sample from each table for confirmation."""
    own = conn is None
    conn = conn or connect()
    try:
        with conn.cursor() as cur:
            for table in ("utility_observations", "price_observations", "chemicals"):
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                count = cur.fetchone()[0]
                print(f"\n== {table}: {count} rows ==")
                cur.execute(f"SELECT * FROM {table} LIMIT %s", (limit,))
                cols = [d[0] for d in cur.description]
                sample = cur.fetchall()
                for row in sample:
                    print("  " + " | ".join(f"{c}={v}" for c, v in zip(cols, row)))
                if not sample:
                    print("  (empty)")
    finally:
        if own:
            conn.close()


def main() -> int:
    conn = connect()
    print(f"Initialized {DB_PATH} store (tables created if missing)")
    verify(conn)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
