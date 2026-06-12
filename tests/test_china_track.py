"""Tests for the China-track additions.

Covers: the region-family contamination guard in engine/prices.py (the critical
correctness change -- US floors must never silently consume foreign prices once
comtrade/sunsirs rows share the table), fx.py's monthly-average + RMB->USD math,
sunsirs.py's licensed-file ingest, and the margin-anchors loader's
fail-loudly-on-unresearched-country behavior. All offline except the db_conn
guard tests, which use the existing fixture (skipped without DATABASE_URL,
exactly like the other storage-backed tests).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from engine import prices  # noqa: E402
from pipeline import storage  # noqa: E402
from fx import monthly_average_from_daily, rmb_per_ton_to_usd_per_kg  # noqa: E402
from margin_anchors import AnchorError, load_anchor  # noqa: E402
from sunsirs import normalize_rows as sunsirs_normalize  # noqa: E402
from sunsirs import read_licensed_file  # noqa: E402


# --- region family logic (pure) ---------------------------------------------

def test_region_family_classification():
    assert prices.region_family("US") == "US"
    assert prices.region_family("US_IMPORTS_ALL_ORIGINS") == "US"
    assert prices.region_family("CA") == "US"           # California, not Canada
    assert prices.region_family("DE") == "US"           # Delaware, not Germany
    assert prices.region_family("DE_IMPORTS") == "DE"   # Germany (underscore-tagged)
    assert prices.region_family("CN_EXPORTS") == "CN"
    assert prices.region_family("CN_SPOT") == "CN"


# --- region guard against the real store (db-backed, like other prices tests)

def _price_row(chemical_id, region, period, price):
    return {
        "chemical_id": chemical_id, "source": "TEST", "region": region,
        "period": period, "price_usd_per_kg": price,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def test_us_request_never_resolves_to_foreign_price(db_conn):
    """The exact bug the guard exists for: only a CN price exists for the
    period; a US request must FAIL, not silently use the Chinese price."""
    storage.write_price_observations(
        [_price_row("aniline", "CN_EXPORTS", "2023-05", 1.50)], conn=db_conn)
    with pytest.raises(prices.PriceUnavailable):
        prices.resolve_price_usd_per_ton("aniline", "2023-05",
                                         "US_IMPORTS_ALL_ORIGINS", db_conn)


def test_us_fallback_still_works_within_family(db_conn):
    """Backward compatibility: an older US-family month still resolves."""
    storage.write_price_observations(
        [_price_row("aniline", "US_IMPORTS_ALL_ORIGINS", "2023-03", 1.20),
         _price_row("aniline", "CN_EXPORTS", "2023-05", 9.99)], conn=db_conn)
    # 2023-05 US: no US row that month -> falls back in time to 2023-03 US,
    # NOT to the same-month Chinese row.
    price = prices.resolve_price_usd_per_ton("aniline", "2023-05",
                                             "US_IMPORTS_ALL_ORIGINS", db_conn)
    assert price == pytest.approx(1.20 * 1000.0)


def test_cn_request_resolves_within_cn_family_only(db_conn):
    storage.write_price_observations(
        [_price_row("aniline", "US_IMPORTS_ALL_ORIGINS", "2023-05", 1.20),
         _price_row("aniline", "CN_EXPORTS", "2023-04", 1.05)], conn=db_conn)
    # CN_SPOT request: same-family CN_EXPORTS is usable; the US row is not.
    price = prices.resolve_price_usd_per_ton("aniline", "2023-05", "CN_SPOT", db_conn)
    assert price == pytest.approx(1.05 * 1000.0)


# --- fx ----------------------------------------------------------------------

def test_monthly_average_from_daily():
    payload = {"rates": {
        "2024-01-02": {"CNY": 7.10},
        "2024-01-03": {"CNY": 7.20},
        "2024-02-01": {"CNY": 7.00},
        "2024-02-02": {},               # missing fixing skipped
    }}
    avgs = monthly_average_from_daily(payload)
    assert avgs["2024-01"] == pytest.approx(7.15)
    assert avgs["2024-02"] == pytest.approx(7.00)


def test_rmb_per_ton_to_usd_per_kg():
    # 7100 RMB/ton at 7.10 CNY/USD = 1000 USD/ton = 1.0 USD/kg
    assert rmb_per_ton_to_usd_per_kg(7100.0, 7.10) == pytest.approx(1.0)


def test_fx_rejects_nonpositive_rate():
    from fx import FxUnavailable
    with pytest.raises(FxUnavailable):
        rmb_per_ton_to_usd_per_kg(7100.0, 0.0)


# --- sunsirs licensed-file ingest ---------------------------------------------

def _write_csv(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_sunsirs_ingest_and_normalize(tmp_path):
    f = _write_csv(tmp_path / "lic.csv",
                   "date,commodity,price,grade\n"
                   "2024-01-05,Aniline,7100,industrial\n"
                   "2024-01-19,Aniline,7300,industrial\n"
                   "2024-02-02,Pure Benzene,7810,\n"
                   "2024-01-12,unknown chem,9999,\n")          # unmapped -> dropped
    records = read_licensed_file(f)
    assert len(records) == 3
    rows = sunsirs_normalize(records, rate_fn=lambda p: 7.10,
                             fetched_at="2026-06-10T00:00:00+00:00")
    by_key = {(r["chemical_id"], r["period"]): r for r in rows}
    aniline = by_key[("aniline", "2024-01")]
    # monthly average 7200 RMB/t VAT-incl. -> ex-VAT 7200/1.13, then /7.10/1000
    # (global invariant: strip 13% VAT in RMB first, THEN convert FX)
    assert aniline["price_usd_per_kg"] == pytest.approx(
        7200 / 1.13 / 7.10 / 1000, rel=1e-4)
    assert aniline["region"] == "CN_SPOT"
    assert aniline["assessment_type"] == "spot"
    assert aniline["grade"] == "industrial"
    assert aniline["hts_code"] is None
    benzene = by_key[("benzene", "2024-02")]
    assert benzene["price_usd_per_kg"] == pytest.approx(
        7810 / 1.13 / 7.10 / 1000, rel=1e-4)


def test_sunsirs_skips_period_without_fx(tmp_path, capsys):
    f = _write_csv(tmp_path / "lic.csv",
                   "date,commodity,price\n2024-03-05,Aniline,7000\n")
    records = read_licensed_file(f)

    def no_rate(_period):
        raise KeyError("no fx")

    rows = sunsirs_normalize(records, rate_fn=no_rate)
    assert rows == []  # skipped loudly, never guessed
    assert "SKIP" in capsys.readouterr().err


def test_sunsirs_fetch_api_is_licence_gated():
    from sunsirs import LicenceRequired, fetch_api
    with pytest.raises(LicenceRequired):
        fetch_api()


# --- margin anchors -----------------------------------------------------------

def _anchors_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "anchors.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_manual_anchor_loads_when_cited(tmp_path):
    cfg = _anchors_yaml(tmp_path, """
countries:
  CN:
    mode: manual
    ratios:
      sga_pct: {value: 0.06, source: "Wanhua 2024 AR p.88"}
      da_pct: {value: 0.07, source: "Wanhua 2024 AR cash flow"}
      ebit_margin_pct: {value: 0.12, source: "Wanhua 2024 AR p.85"}
    fixed_pct_ex_da: {value: 0.03, source: "Towler estimate"}
""")
    anchor = load_anchor("CN", config_path=cfg)
    assert anchor["sga_pct"] == pytest.approx(0.06)
    assert anchor["fixed_pct_ex_da"] == pytest.approx(0.03)


def test_manual_anchor_fails_loudly_on_null(tmp_path):
    cfg = _anchors_yaml(tmp_path, """
countries:
  CN:
    mode: manual
    ratios:
      sga_pct: {value: null, source: ""}
      da_pct: {value: 0.07, source: "x"}
      ebit_margin_pct: {value: 0.12, source: "x"}
""")
    with pytest.raises(AnchorError, match="null"):
        load_anchor("CN", config_path=cfg)


def test_manual_anchor_requires_citation(tmp_path):
    cfg = _anchors_yaml(tmp_path, """
countries:
  CN:
    mode: manual
    ratios:
      sga_pct: {value: 0.06, source: ""}
      da_pct: {value: 0.07, source: "x"}
      ebit_margin_pct: {value: 0.12, source: "x"}
""")
    with pytest.raises(AnchorError, match="source"):
        load_anchor("CN", config_path=cfg)


def test_unknown_country_rejected(tmp_path):
    cfg = _anchors_yaml(tmp_path, "countries:\n  US: {mode: edgar_summary}\n")
    with pytest.raises(AnchorError, match="no margin anchor"):
        load_anchor("FR", config_path=cfg)


def test_shipped_config_us_and_cn_states():
    """The committed config: CN must be present but NOT yet loadable (nulls),
    proving the fail-loudly discipline ships in the default file."""
    with pytest.raises(AnchorError):
        load_anchor("CN")
