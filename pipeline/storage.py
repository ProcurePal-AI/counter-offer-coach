"""
storage.py -- SQLite store for the should-cost demo pipeline.

Single-file, zero external setup: stdlib `sqlite3` only, one DB file at
data/market.db (git-ignored). This is the storage seam the connectors hand
their normalized rows to.

Three tables, columns taken verbatim from the schema contracts:
  * utility_observations  <- docs/schema/market_observations.schema.yaml   (eia.py)
  * price_observations    <- docs/schema/market_observations.schema.yaml   (no feed yet)
  * chemicals             <- docs/schema/chemical_registry.schema.yaml      (pubchem.py)

Append-only tables (the two *_observations) take raw INSERTs -- no dedup, no
filtering, no outlier removal. Cleaning is a downstream/calibration concern.
`chemicals` is reference identity (one row per substance), so it upserts on name.

Run directly to (re)create the DB and print a verification sample:
    python pipeline/storage.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# DB lives under data/, which .gitignore already excludes. The data is fully
# re-pullable from the public EIA / PubChem APIs, so it is never committed.
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "market.db"

# --- DDL -------------------------------------------------------------------
# Columns mirror docs/schema/*.yaml EXACTLY. The only additions are structural:
#   - chemicals.name: the registry is a YAML map keyed by chemical name, so the
#     key has to become a column (it is the natural primary key).
# Array fields (chemicals.hts_codes) are stored as JSON text -- SQLite has no
# array type. NOT NULL mirrors each schema's `required` list; schema-optional
# and nullable fields stay nullable.

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS utility_observations (
    utility            TEXT    NOT NULL,
    source             TEXT    NOT NULL,
    unit               TEXT    NOT NULL,
    region             TEXT    NOT NULL,
    period             TEXT    NOT NULL,
    price_usd_per_unit REAL    NOT NULL,
    fetched_at         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS price_observations (
    chemical_id      TEXT    NOT NULL,
    source           TEXT    NOT NULL,
    region           TEXT    NOT NULL,
    period           TEXT    NOT NULL,
    price_usd_per_kg REAL    NOT NULL,
    fetched_at       TEXT    NOT NULL,
    hts_code         TEXT,                 -- nullable: USITC only
    grade            TEXT,                 -- nullable: ICIS only
    assessment_type  TEXT                  -- nullable: 'spot' | 'contract' | NULL
);

CREATE TABLE IF NOT EXISTS chemicals (
    name                       TEXT    PRIMARY KEY,   -- registry map key
    cas                        TEXT,                  -- nullable: PubChem may have no CAS synonym
    pubchem_cid                INTEGER NOT NULL,
    iupac_name                 TEXT    NOT NULL,
    molecular_formula          TEXT    NOT NULL,
    molecular_weight_g_per_mol REAL    NOT NULL,
    hts_codes                  TEXT    NOT NULL,       -- JSON array of HTS code strings
    status                     TEXT    NOT NULL
);
"""

# Column order per table -- the INSERT/verification helpers and the schema-match
# test all read these, so the column contract lives in one place.
UTILITY_COLUMNS = ["utility", "source", "unit", "region", "period",
                   "price_usd_per_unit", "fetched_at"]
PRICE_COLUMNS = ["chemical_id", "source", "region", "period", "price_usd_per_kg",
                 "fetched_at", "hts_code", "grade", "assessment_type"]
CHEMICAL_COLUMNS = ["name", "cas", "pubchem_cid", "iupac_name", "molecular_formula",
                    "molecular_weight_g_per_mol", "hts_codes", "status"]


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open (creating the parent dir if needed) and ensure the schema exists."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables if they do not already exist."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def _insert_rows(conn: sqlite3.Connection, table: str, columns: list[str],
                 rows: list[dict]) -> int:
    """Append `rows` to `table`, pulling values by column name. Raw insert."""
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in columns)
    col_list = ", ".join(columns)
    sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
    conn.executemany(sql, [[r.get(c) for c in columns] for r in rows])
    conn.commit()
    return len(rows)


def write_utility_observations(rows: list[dict], conn: sqlite3.Connection | None = None) -> int:
    """Append normalized EIA utility rows to `utility_observations`. Append-only."""
    own = conn is None
    conn = conn or connect()
    try:
        return _insert_rows(conn, "utility_observations", UTILITY_COLUMNS, rows)
    finally:
        if own:
            conn.close()


def write_price_observations(rows: list[dict], conn: sqlite3.Connection | None = None) -> int:
    """Append normalized price rows to `price_observations`. Append-only.

    No connector produces these yet (USITC/ICIS feed not in the repo); the table
    and writer exist so a future feed plugs in without a migration.
    """
    own = conn is None
    conn = conn or connect()
    try:
        return _insert_rows(conn, "price_observations", PRICE_COLUMNS, rows)
    finally:
        if own:
            conn.close()


def write_chemicals(registry: dict, conn: sqlite3.Connection | None = None) -> int:
    """Upsert PubChem reference identity into `chemicals`.

    `registry` is the {"chemicals": {name: {...}}} dict pubchem.build_registry()
    returns. Reference identity, not a time series -> upsert on name (re-running
    the PubChem pull refreshes in place instead of duplicating).
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
    try:
        placeholders = ", ".join("?" for _ in CHEMICAL_COLUMNS)
        col_list = ", ".join(CHEMICAL_COLUMNS)
        sql = (f"INSERT OR REPLACE INTO chemicals ({col_list}) "
               f"VALUES ({placeholders})")
        conn.executemany(sql, [[r.get(c) for c in CHEMICAL_COLUMNS] for r in rows])
        conn.commit()
        return len(rows)
    finally:
        if own:
            conn.close()


def dump_csv(table: str, out_path: Path | str,
             conn: sqlite3.Connection | None = None) -> int:
    """Export an entire table to CSV (header + all rows). Returns the row count.

    Used by CI to attach a human-readable artifact: the SQLite DB stays the real
    store, this is just a readable projection of one table for the run page.
    """
    import csv

    own = conn is None
    conn = conn or connect()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        columns = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
        with out_path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            writer.writerows([[row[c] for c in columns] for row in rows])
        print(f"Exported {len(rows)} rows from {table} -> {out_path}")
        return len(rows)
    finally:
        if own:
            conn.close()


def verify(conn: sqlite3.Connection | None = None, limit: int = 5) -> None:
    """Print row counts and a small sample from each table for confirmation."""
    own = conn is None
    conn = conn or connect()
    try:
        for table in ("utility_observations", "price_observations", "chemicals"):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"\n== {table}: {count} rows ==")
            sample = conn.execute(f"SELECT * FROM {table} LIMIT {limit}").fetchall()
            for row in sample:
                print("  " + " | ".join(f"{k}={row[k]}" for k in row.keys()))
            if not sample:
                print("  (empty)")
    finally:
        if own:
            conn.close()


def main() -> int:
    conn = connect()
    print(f"Initialized SQLite store at {DB_PATH}")
    verify(conn)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
