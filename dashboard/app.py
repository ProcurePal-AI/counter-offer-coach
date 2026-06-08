# Data Source Freshness Dashboard
#
# Shows how up-to-date each data source in the collection pipeline is,
# on a single screen. Display-only: no alerts, no warnings.
#
# Run (from the repo root):
#     streamlit run dashboard/app.py
#
# Requirements:
#     pip install streamlit psycopg2-binary python-dotenv
#     (reads the Neon DATABASE_URL from the project-root .env, same as the pipeline)

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

# This file lives in dashboard/, so the repo root is one level up (parents[1]).
ROOT = Path(__file__).resolve().parents[1]

# pipeline/ holds storage.py (the shared Postgres seam); put it on the path so we
# read through the same connection logic the connectors use.
sys.path.insert(0, str(ROOT / "pipeline"))

REGISTRY_PATH = ROOT / "config" / "chemical_registry.yaml"


# ---------------------------------------------------------------------------
# DATA ACCESS SEAM
# ---------------------------------------------------------------------------
# Everything that touches the database is isolated here. It reads real per-source
# status from PostgreSQL (Neon) via pipeline/storage.py. If the DB is unreachable
# (no DATABASE_URL, network down, tables missing) it returns {} and the UI falls
# back to "no data" placeholders -- so the dashboard always renders.
#
# Return shape of get_source_status(): a list of dicts, one per source:
#     source:            str   -- source name, e.g. "EIA"
#     latest_period:     str   -- month of the most recent fetched_at, "YYYY-MM" (None
#                                 if no data). PubChem uses the registry file's month.
#     rows:              int   -- total row count for that source
#     expected_cadence:  str   -- "monthly" | "quarterly" | "annual" | "one-time"
def _live_status() -> dict[str, dict]:
    """Read real per-source status from Postgres. Returns {} on any failure.

    `latest_period` is the month of the most recent `fetched_at` (when the source was
    last pulled), sliced to "YYYY-MM". PubChem identity has no `fetched_at` column, so
    it keeps approximating its month from the registry file's modification time.
    """
    try:
        import storage

        conn = storage.connect()
    except Exception:
        return {}  # no DATABASE_URL / unreachable / driver missing -> graceful fallback

    # Timestamped, source-tagged observation feeds: (source, table, cadence). Table
    # names are fixed literals here (not user input); the source is parameterized.
    feeds = [
        ("EIA", "utility_observations", "monthly"),
        ("USITC", "price_observations", "monthly"),
        ("USGS", "price_observations", "annual"),
    ]
    out: dict[str, dict] = {}
    try:
        with conn.cursor() as cur:
            for source, table, cadence in feeds:
                cur.execute(
                    f"SELECT COUNT(*), MAX(fetched_at) FROM {table} WHERE source = %s",
                    (source,),
                )
                count, latest = cur.fetchone()
                if count:
                    out[source] = {
                        "source": source,
                        "latest_period": latest[:7] if latest else None,  # fetched_at -> YYYY-MM
                        "rows": count,
                        "expected_cadence": cadence,
                    }

            # PubChem -> chemicals (one-time reference identity; no timestamp column,
            # so approximate "last pulled" from the registry file's modification month)
            cur.execute("SELECT COUNT(*) FROM chemicals")
            (count,) = cur.fetchone()
            if count:
                pulled = None
                if REGISTRY_PATH.exists():
                    pulled = datetime.fromtimestamp(REGISTRY_PATH.stat().st_mtime).strftime("%Y-%m")
                out["PubChem"] = {"source": "PubChem", "latest_period": pulled,
                                  "rows": count, "expected_cadence": "one-time"}
    except Exception:
        return {}
    finally:
        conn.close()
    return out


def get_source_status() -> list[dict]:
    # Each entry is the "no data" placeholder for a source. Live values from Postgres
    # override the placeholder when present -- so a missing/empty DB shows "no data",
    # never fake numbers. USGS and EDGAR connectors are not built yet, so they have
    # no live query and always stay "no data".
    no_data = {
        "EIA":     {"source": "EIA",     "latest_period": None, "rows": 0, "expected_cadence": "monthly"},
        "USITC":   {"source": "USITC",   "latest_period": None, "rows": 0, "expected_cadence": "monthly"},
        "USGS":    {"source": "USGS",    "latest_period": None, "rows": 0, "expected_cadence": "annual"},
        "EDGAR":   {"source": "EDGAR",   "latest_period": None, "rows": 0, "expected_cadence": "quarterly"},
        "PubChem": {"source": "PubChem", "latest_period": None, "rows": 0, "expected_cadence": "one-time"},
    }
    live = _live_status()
    return [
        live.get("EIA", no_data["EIA"]),
        live.get("USITC", no_data["USITC"]),
        live.get("USGS", no_data["USGS"]),
        no_data["EDGAR"],
        live.get("PubChem", no_data["PubChem"]),
    ]


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def days_since_period(period: str | None) -> int | None:
    """Days between the first day of `period` (YYYY-MM) and today.

    Periods are months, so we anchor to the first day of the month. Returns
    None when the source has no data yet.
    """
    if not period:
        return None
    year, month = (int(p) for p in period.split("-"))
    return (date.today() - date(year, month, 1)).days


# Human-readable badge for each cadence value.
CADENCE_LABEL = {
    "monthly": "Monthly",
    "quarterly": "Quarterly",
    "annual": "Annual",
    "one-time": "One-time",
}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
import streamlit as st  # noqa: E402  (imported here so the seam above stays DB/UI-agnostic)

st.set_page_config(page_title="Data Source Freshness", page_icon="📊", layout="wide")

st.title("📊 Data Source Freshness Dashboard")
st.caption(f"Last refreshed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

sources = get_source_status()

# One card per source, laid out side by side so everything fits on one screen.
columns = st.columns(len(sources))

for col, item in zip(columns, sources):
    with col:
        with st.container(border=True):
            has_data = item["latest_period"] is not None
            is_one_time = item["expected_cadence"] == "one-time"
            cadence = CADENCE_LABEL.get(item["expected_cadence"], item["expected_cadence"])

            if is_one_time:
                # One-time reference pull (e.g. PubChem chemical identity). Not a
                # time series, so we show the snapshot size and pull month and skip
                # the "days old" metric -- freshness/staleness does not apply here.
                st.markdown(f"#### {item['source']}")
                st.metric(label="Last pulled", value=item["latest_period"] or "—")
                st.markdown(f"Rows — **{item['rows']:,}**")
                st.markdown(f"Cadence — **{cadence}**")
                st.caption("Reference identity (one-time pull)")
            elif not has_data:
                # No data yet -> render the whole card muted (gray).
                st.markdown(f"#### :gray[{item['source']}]")
                st.markdown(":gray[**데이터 없음**]")
                st.markdown(":gray[Latest period — —]")
                st.markdown(":gray[Age — —]")
                st.markdown(f":gray[Rows — {item['rows']:,}]")
                st.markdown(f":gray[Cadence — {cadence}]")
            else:
                age_days = days_since_period(item["latest_period"])
                st.markdown(f"#### {item['source']}")
                st.metric(label="Latest period", value=item["latest_period"])
                st.markdown(f"**{age_days:,} days ago**")
                st.markdown(f"Rows — **{item['rows']:,}**")
                st.markdown(f"Cadence — **{cadence}**")
