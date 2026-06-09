"""Tests for pipeline/usitc_history.py.

The core thing under test: the recommended start is the LATER of (a) the aligned
existence floor -- the latest of each chemical's earliest clean period, an
INTERSECTION not a union -- and (b) the comparability cap. NULL-priced months
never count as history. Ragged benzene-vs-aniline fixtures prove the alignment.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from usitc_history import (  # noqa: E402
    clean_periods_by_chemical,
    common_clean_floor,
    per_year_coverage,
    resolve_start_period,
    _months_between,
)


def _row(chem, period, price):
    return {"chemical_id": chem, "period": period, "price_usd_per_kg": price,
            "region": "US_IMPORTS_ALL_ORIGINS", "source": "USITC"}


def test_months_between_inclusive():
    assert _months_between("2025-01", "2025-01") == 1
    assert _months_between("2024-12", "2025-01") == 2
    assert _months_between("2020-06", "2025-06") == 61


def test_null_prices_are_not_clean_history():
    rows = {"aniline": [_row("aniline", "2023-01", None),
                        _row("aniline", "2023-02", 1.5),
                        _row("aniline", "2023-03", None)]}
    clean = clean_periods_by_chemical(rows)
    assert clean["aniline"] == ["2023-02"]  # the two NULL months drop out


def test_common_floor_is_intersection_not_union():
    """Benzene reaches back to 2018; aniline only to 2021. Aligned floor = 2021."""
    clean = {
        "benzene": ["2018-01", "2019-01", "2021-01", "2024-01"],
        "aniline": ["2021-01", "2022-01", "2024-01"],
    }
    assert common_clean_floor(clean) == "2021-01"  # latest of the earliests


def test_common_floor_none_when_a_chemical_has_no_clean_data():
    clean = {"benzene": ["2020-01"], "aniline": []}
    assert common_clean_floor(clean) is None


def test_existence_binds_when_data_starts_after_cap():
    """Clean data only goes back ~4 years; a 10-year cap does NOT pull it earlier."""
    rows = {
        "benzene": [_row("benzene", "2022-01", 1.0), _row("benzene", "2025-01", 1.1)],
        "aniline": [_row("aniline", "2022-01", 1.5), _row("aniline", "2025-01", 1.6)],
    }
    verdict = resolve_start_period(rows, comparability_cap_years=10)
    assert verdict["ok"]
    assert verdict["existence_floor"] == "2022-01"
    assert verdict["recommended_start"] == "2022-01"
    assert verdict["binding_constraint"] == "data_existence"


def test_cap_binds_when_data_predates_it():
    """Clean data exists back to 2005, but a 10-year cap refuses to reach it."""
    rows = {
        "benzene": [_row("benzene", "2005-01", 1.0), _row("benzene", "2024-01", 1.1)],
        "aniline": [_row("aniline", "2005-01", 1.5), _row("aniline", "2024-01", 1.6)],
    }
    verdict = resolve_start_period(rows, comparability_cap_years=10)
    assert verdict["ok"]
    assert verdict["existence_floor"] == "2005-01"
    assert verdict["binding_constraint"] == "comparability_cap"
    # recommended start is the cap floor (~10 years back), strictly after 2005
    assert verdict["recommended_start"] > "2005-01"


def test_no_cap_takes_all_aligned_history():
    rows = {
        "benzene": [_row("benzene", "2005-01", 1.0)],
        "aniline": [_row("aniline", "2008-01", 1.5)],
    }
    verdict = resolve_start_period(rows, comparability_cap_years=None)
    assert verdict["recommended_start"] == "2008-01"  # intersection, no cap
    assert verdict["binding_constraint"] == "data_existence"


def test_verdict_not_ok_when_no_common_floor():
    rows = {
        "benzene": [_row("benzene", "2020-01", 1.0)],
        "aniline": [_row("aniline", "2020-01", None)],  # never clean
    }
    verdict = resolve_start_period(rows, comparability_cap_years=10)
    assert not verdict["ok"]
    assert verdict["recommended_start"] is None


def test_recommended_months_covers_the_start():
    rows = {
        "benzene": [_row("benzene", "2020-06", 1.0)],
        "aniline": [_row("aniline", "2020-06", 1.5)],
    }
    verdict = resolve_start_period(rows, comparability_cap_years=None)
    # months must be at least the inclusive span from start to now (June 2025
    # is the floor used here; the real "now" is later, so the real value is >=).
    assert verdict["recommended_months"] >= _months_between("2020-06", "2025-06")


def test_per_year_coverage_counts():
    clean = {"benzene": ["2023-01", "2023-02", "2024-01"],
             "aniline": ["2023-01"]}
    cov = per_year_coverage(clean)
    assert cov[2023]["benzene"] == 2
    assert cov[2023]["aniline"] == 1
    assert cov[2024]["benzene"] == 1
    assert "aniline" not in cov[2024]
