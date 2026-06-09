"""
usitc_history.py -- discover the ideal USITC pull window for the demo chain.

Why this exists
---------------
usitc.py pulls a TRAILING window of DEFAULT_MONTHS ending today. The right window
length is not a number we can guess: it is gated by two things only the data can
tell us, which this tool separates explicitly:

  1. EXISTENCE floor (auto-detected). An 8/10-digit HTS line does not exist for
     all history -- the tariff schedule renumbers codes over time. So each
     chemical has an earliest period for which DataWeb returns a CLEAN
     (non-NULL) price. The benzene line and the aniline line almost certainly
     differ here, and the calibration needs them ALIGNED, so the usable floor is
     the *latest* of the per-chemical earliest-clean periods -- the first period
     from which EVERY target chemical is populated. We intersect, never union.

  2. COMPARABILITY cap (human-set, explicit). Even where old data exists, a
     decades-old blended import unit value reflects a differently-shaped trade
     (origin mix, reporting conventions) than today, which biases calibration
     rather than just widening it. So an optional `comparability_cap_years`
     refuses to reach further back than N years regardless of what exists. This
     is a deliberate judgment the team owns -- the function never makes it
     silently.

The result: take the most clean, ALIGNED history that exists, up to an optional
comparability ceiling. Whichever limit binds first is the ideal start.

This module does NOT run on every cron pull -- re-probing 25 years monthly is
slow and re-fetches overlapping months (the store is append-only, no dedup). Run
it ONCE as a backfill/discovery step, read the start period it prints, then set
usitc.py's window from it (e.g. bake the discovered DEFAULT_MONTHS, or pass
--months). Re-run only to re-validate when the schedule changes.

Run:
  USITC_API_TOKEN=... python pipeline/usitc_history.py
  USITC_API_TOKEN=... python pipeline/usitc_history.py --probe-months 300 --cap-years 10
  python pipeline/usitc_history.py --mock-response data/raw/usitc/<file>.json   # offline
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sibling import (run as a script, no package), mirroring usitc.py's own style.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from usitc import (  # noqa: E402
    CHEMICALS,
    fetch_hts,
    load_mock_response,
    normalize_rows,
)

# A deliberately over-long probe: 25 years. We are not claiming 25 years of data
# exists -- this is the wide net whose returned non-NULL periods reveal the real
# floor. DataWeb serves HTS imports back to ~1989, so the platform is not the limit.
DEFAULT_PROBE_MONTHS = 300

# Default comparability ceiling. ~10 years spans 2-3 petrochemical price cycles
# (enough cycle variation to identify the premium) without reaching into a
# differently-shaped older trade. Set to None to take all existing clean history.
DEFAULT_COMPARABILITY_CAP_YEARS = 10


def _months_between(start_period: str, end_period: str) -> int:
    """Inclusive month count from 'YYYY-MM' to 'YYYY-MM' (>=1)."""
    sy, sm = (int(x) for x in start_period.split("-"))
    ey, em = (int(x) for x in end_period.split("-"))
    return (ey * 12 + em) - (sy * 12 + sm) + 1


def _current_period() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def _cap_period(cap_years: int | None) -> str | None:
    """Earliest period allowed by the comparability cap, as 'YYYY-MM' (or None)."""
    if cap_years is None:
        return None
    now = datetime.now(timezone.utc)
    total = now.year * 12 + (now.month - 1) - cap_years * 12
    year, month = divmod(total, 12)
    return f"{year:04d}-{month + 1:02d}"


def clean_periods_by_chemical(rows_by_chemical: dict[str, list[dict[str, Any]]]
                              ) -> dict[str, list[str]]:
    """{chemical_id: sorted list of periods with a non-NULL price}.

    A period only counts when price_usd_per_kg is present -- USITC keeps
    incomplete months as NULL, and a NULL is not usable history.
    """
    out: dict[str, list[str]] = {}
    for chem, rows in rows_by_chemical.items():
        periods = sorted(
            r["period"] for r in rows
            if r.get("price_usd_per_kg") is not None and r.get("period")
        )
        out[chem] = periods
    return out


def common_clean_floor(clean_by_chemical: dict[str, list[str]]) -> str | None:
    """The first period from which EVERY chemical has clean data (the intersection).

    Each chemical's earliest-clean period differs; the aligned floor is the
    LATEST of those earliest periods. Returns None if any chemical has zero
    clean periods (no aligned history is possible).
    """
    earliest_per_chem: list[str] = []
    for chem, periods in clean_by_chemical.items():
        if not periods:
            return None  # this chemical never resolves -> no common floor
        earliest_per_chem.append(periods[0])
    return max(earliest_per_chem)  # latest of the earliest = aligned start


def per_year_coverage(clean_by_chemical: dict[str, list[str]]) -> dict[int, dict[str, int]]:
    """{year: {chemical_id: count of clean months}} -- the table to eyeball."""
    coverage: dict[int, dict[str, int]] = {}
    for chem, periods in clean_by_chemical.items():
        for period in periods:
            year = int(period.split("-")[0])
            coverage.setdefault(year, {})[chem] = coverage.setdefault(year, {}).get(chem, 0) + 1
    return coverage


def resolve_start_period(rows_by_chemical: dict[str, list[dict[str, Any]]],
                         *, comparability_cap_years: int | None
                         = DEFAULT_COMPARABILITY_CAP_YEARS) -> dict[str, Any]:
    """Decide the ideal start period from probe results.

    Returns a verdict dict with the existence floor, the cap floor, the binding
    constraint, and the recommended start + months-from-now. The recommended
    start is the LATER (more recent) of the existence floor and the cap floor:
    take the most aligned clean history that exists, but never past the cap.
    """
    clean = clean_periods_by_chemical(rows_by_chemical)
    existence_floor = common_clean_floor(clean)
    cap_floor = _cap_period(comparability_cap_years)
    now_period = _current_period()

    if existence_floor is None:
        return {
            "ok": False,
            "reason": "at least one chemical returned zero clean periods; "
                      "no aligned history is possible",
            "clean_periods": clean,
            "existence_floor": None,
            "cap_floor": cap_floor,
            "recommended_start": None,
            "recommended_months": None,
            "binding_constraint": None,
        }

    # Take the later (more recent) of the two floors.
    if cap_floor is not None and cap_floor > existence_floor:
        start, binding = cap_floor, "comparability_cap"
    else:
        start, binding = existence_floor, "data_existence"

    return {
        "ok": True,
        "reason": "",
        "clean_periods": clean,
        "existence_floor": existence_floor,
        "cap_floor": cap_floor,
        "recommended_start": start,
        # +1 buffer month so a trailing-window pull comfortably reaches `start`.
        "recommended_months": _months_between(start, now_period) + 1,
        "binding_constraint": binding,
    }


def _probe_rows(token: str | None, probe_months: int,
                mock_response: Path | None) -> dict[str, list[dict[str, Any]]]:
    """Pull (or mock) a wide window for every target chemical."""
    rows_by_chemical: dict[str, list[dict[str, Any]]] = {}
    for chemical_id, meta in CHEMICALS.items():
        hts_code = meta["hts_code"]
        print(f"  probing {chemical_id} ({meta['display_hts_code']} -> {hts_code}) "
              f"over {probe_months} months...")
        if mock_response is not None:
            response_json = load_mock_response(mock_response)
            rows = normalize_rows(chemical_id, hts_code, response_json)
        else:
            if token is None:
                raise ValueError("USITC_API_TOKEN required unless --mock-response is used.")
            rows = fetch_hts(token, chemical_id, hts_code, probe_months)
        clean = sum(1 for r in rows if r.get("price_usd_per_kg") is not None)
        print(f"    {len(rows)} rows ({clean} clean)")
        rows_by_chemical[chemical_id] = rows
    return rows_by_chemical


def _print_verdict(verdict: dict[str, Any], cap_years: int | None) -> None:
    print("\nPer-year clean-month coverage:")
    coverage = per_year_coverage(verdict["clean_periods"])
    chems = list(verdict["clean_periods"])
    header = "  year   " + "  ".join(f"{c:>10s}" for c in chems)
    print(header)
    for year in sorted(coverage):
        cells = "  ".join(f"{coverage[year].get(c, 0):>10d}" for c in chems)
        print(f"  {year}   {cells}")

    print("\nFloors:")
    for chem, periods in verdict["clean_periods"].items():
        first = periods[0] if periods else "(none clean)"
        print(f"  {chem:>10s} earliest clean: {first}")
    print(f"  aligned existence floor (intersection): {verdict['existence_floor']}")
    cap_label = verdict["cap_floor"] or f"(no cap; cap_years={cap_years})"
    print(f"  comparability cap floor:                {cap_label}")

    if not verdict["ok"]:
        print(f"\nNO USABLE WINDOW: {verdict['reason']}")
        return

    print(f"\n>>> RECOMMENDED START: {verdict['recommended_start']}  "
          f"(binding constraint: {verdict['binding_constraint']})")
    print(f">>> Set usitc.py window to --months {verdict['recommended_months']}  "
          f"(or bake DEFAULT_MONTHS = {verdict['recommended_months']})")
    if verdict["binding_constraint"] == "data_existence":
        print("    Note: data existence binds before the comparability cap -- "
              "you are taking ALL aligned clean history. Good.")
    else:
        print("    Note: the comparability cap binds -- clean data exists earlier "
              f"({verdict['existence_floor']}) but is deliberately excluded. "
              "Raise --cap-years (or pass none) to include it.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Discover the ideal USITC pull window (common clean floor + cap).")
    parser.add_argument("--probe-months", type=int, default=DEFAULT_PROBE_MONTHS,
                        help="How far back to probe (default 300 = 25 years).")
    parser.add_argument("--cap-years", type=int, default=DEFAULT_COMPARABILITY_CAP_YEARS,
                        help="Comparability ceiling in years. Use --no-cap to disable.")
    parser.add_argument("--no-cap", action="store_true",
                        help="Take all aligned clean history with no comparability cap.")
    parser.add_argument("--mock-response", type=Path,
                        help="Local raw DataWeb JSON for offline validation.")
    args = parser.parse_args(argv or sys.argv[1:])

    cap_years = None if args.no_cap else args.cap_years
    token = os.environ.get("USITC_API_TOKEN")
    if not token and not args.mock_response:
        print("ERROR: set USITC_API_TOKEN, or pass --mock-response for offline validation.",
              file=sys.stderr)
        return 1

    print(f"Probing USITC history (cap_years={cap_years})...")
    try:
        rows_by_chemical = _probe_rows(token, args.probe_months, args.mock_response)
    except Exception as exc:  # network / token / validation
        print(f"ERROR: probe failed: {exc}", file=sys.stderr)
        return 1

    verdict = resolve_start_period(rows_by_chemical, comparability_cap_years=cap_years)
    _print_verdict(verdict, cap_years)
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
