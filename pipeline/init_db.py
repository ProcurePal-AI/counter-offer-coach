import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "data" / "counter_offer_coach.db"

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS price_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chemical_id TEXT NOT NULL,
    source TEXT NOT NULL,
    hts_code TEXT,
    region TEXT NOT NULL,
    period TEXT NOT NULL,
    price_usd_per_kg REAL NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS utility_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    utility TEXT NOT NULL,
    source TEXT NOT NULL,
    region TEXT NOT NULL,
    period TEXT NOT NULL,
    price_usd_per_unit REAL NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS producer_filings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    ticker TEXT,
    source TEXT NOT NULL,
    filing_type TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    period_end_date TEXT,
    source_url TEXT NOT NULL,
    local_file_path TEXT,
    fetched_at TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_name TEXT NOT NULL,
    source TEXT NOT NULL,
    run_started_at TEXT NOT NULL,
    run_finished_at TEXT,
    status TEXT NOT NULL,
    records_inserted INTEGER DEFAULT 0,
    records_updated INTEGER DEFAULT 0,
    latest_data_period TEXT,
    freshness_days INTEGER,
    is_stale INTEGER DEFAULT 0,
    error_message TEXT,
    notes TEXT
);
"""

def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(CREATE_TABLES_SQL)
        conn.commit()

    print(f"Database initialized at {DB_PATH}")

if __name__ == "__main__":
    init_db()