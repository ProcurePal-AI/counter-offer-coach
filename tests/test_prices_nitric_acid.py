"""Tests for the nitric_acid -> ammonia derivation in engine/prices.py."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline import storage
from engine import prices


def _conn():
    return storage.connect(Path(tempfile.mktemp(suffix=".db")))


def _write_ammonia(conn, price_usd_per_kg: float, period: str = "2023-12"):
    storage.write_price_observations(
        [{
            "chemical_id": "ammonia",
            "source": "USGS",
            "region": "US",
            "period": period,
            "price_usd_per_kg": price_usd_per_kg,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }],
        conn=conn,
    )


def test_nitric_acid_derives_from_ammonia():
    conn = _conn()
    try:
        # 0.661387 $/kg ammonia == 661.39 $/ton (i.e. 600 $/short ton).
        _write_ammonia(conn, 0.661387)
        nitric = prices.resolve_price_usd_per_ton("nitric_acid", "2023-12", "US", conn)
        ammonia = prices.resolve_price_usd_per_ton("ammonia", "2023-12", "US", conn)
        assert nitric == pytest.approx(ammonia * prices.T_NH3_PER_T_HNO3, rel=1e-9)
        # Feedstock floor sits below the ammonia price per ton (ratio < 1).
        assert 0 < nitric < ammonia
    finally:
        conn.close()


def test_nitric_acid_unavailable_when_ammonia_missing():
    conn = _conn()
    try:
        with pytest.raises(prices.PriceUnavailable) as exc:
            prices.resolve_price_usd_per_ton("nitric_acid", "2023-12", "US", conn)
        # Message points at the real cause (ammonia), not a vague failure.
        assert "ammonia" in str(exc.value).lower()
    finally:
        conn.close()


def test_factor_is_in_sane_stoichiometric_range():
    # Theoretical floor 0.2703 (100% yield) up to ~0.30 at realistic conversion.
    assert 0.270 <= prices.T_NH3_PER_T_HNO3 <= 0.300


def test_nitric_acid_no_longer_pending():
    assert "nitric_acid" not in prices._PENDING_FEEDS
