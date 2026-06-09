"""Shared pytest fixtures.

DB-backed tests run against PostgreSQL via DATABASE_URL, but inside a throwaway
schema (`pytest_tmp`) that is created fresh per test and dropped afterwards. This
gives full isolation -- including row counts -- without touching the real tables,
and without needing a separate database. Tests skip cleanly when DATABASE_URL is
unset (e.g. local runs with no Neon access).
"""

import os
import sys
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
# Repo root enables `from pipeline import storage` / `from engine import prices`;
# pipeline/ enables bare `import storage` (how the connectors run as scripts).
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

# Load DATABASE_URL from the project-root .env for local runs. load_dotenv does not
# override real environment variables, so a CI-provided secret still wins.
load_dotenv(ROOT / ".env")

# Unique per test process so concurrent CI runs against the same Neon database use
# separate throwaway schemas and never clobber each other.
TEST_SCHEMA = f"pytest_{uuid.uuid4().hex[:12]}"


def pytest_configure(config):
    """In CI, a missing DATABASE_URL must FAIL the run, not silently skip.

    The db_conn fixture skips when DATABASE_URL is unset so a developer without
    Neon access can still run the pure-logic tests locally. But on CI that same
    skip would quietly drop every DB-backed test while the run stayed green -- a
    false pass. CI runners set CI=true, so we hard-fail there instead. (To allow
    skips even in a CI-like shell, unset CI.)
    """
    if os.environ.get("CI") and not os.environ.get("DATABASE_URL"):
        raise pytest.UsageError(
            "DATABASE_URL is not set but CI is detected. DB-backed tests would "
            "skip silently and the run would falsely pass. Configure the "
            "DATABASE_URL repository secret (or unset CI to allow local skips)."
        )


@pytest.fixture
def db_conn():
    """Yield a Postgres connection whose tables live in an isolated temp schema."""
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set; skipping Postgres-backed test")

    import psycopg2
    import storage

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {TEST_SCHEMA}")
        # Session-level search_path: every later statement on this connection
        # (init_db, the storage writers, the test's own queries) hits the temp schema.
        cur.execute(f"SET search_path TO {TEST_SCHEMA}")
    conn.commit()

    storage.init_db(conn)  # create the three tables inside pytest_tmp
    try:
        yield conn
    finally:
        conn.rollback()  # discard the test's uncommitted writes / any error state
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
            # Reset search_path before returning the backend to Neon's pooler, so a
            # later client never inherits a path pointing at this dropped schema.
            cur.execute("SET search_path TO public")
        conn.commit()
        conn.close()
