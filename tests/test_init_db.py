import sqlite3

from pipeline.init_db import DB_PATH, init_db


def test_init_db_creates_expected_tables():
    init_db()

    expected_tables = {
        "price_observations",
        "utility_observations",
        "producer_filings",
        "pipeline_runs",
    }

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table';"
        ).fetchall()

    actual_tables = {row[0] for row in rows}

    assert expected_tables.issubset(actual_tables)