"""
comtrade.py -- UN Comtrade multi-country trade benchmark connector.

Part 2 (DataFetcher) companion to usitc.py. Where usitc.py gives a single
US-import series per chemical, this pulls the SAME chemicals from UN Comtrade
across MANY reporter countries -- because US aniline imports are sparse (~3-6
clean months/year), while EU/China/Japan/Korea trade is far denser. The foreign
series are VALIDATION / CALIBRATION evidence (densifying the observed-price panel
and cross-checking USITC levels), NOT drop-in inputs to the US cost floor: a
German import price is Germany's market, not a US transaction.

Design: identical seam to usitc.py -- fetch raw -> normalize to the SAME
price_observations schema -> hand rows to write_price_observations(). No schema
change. Country/flow lives in the existing `region` column via a fixed naming
convention (e.g. "DE_IMPORTS", "CN_EXPORTS"); `source` is "COMTRADE"; `hts_code`
carries the HS6. Raw responses are preserved under data/raw/comtrade/.

HS codes (6-digit, the Comtrade granularity): aniline 292141, benzene 290220,
ammonia (anhydrous) 281410. Comtrade reports per period a trade value (USD) and a
net weight (kg); unit value = value / netWeight, in USD/kg -- same definition as
USITC. Rows with missing/zero weight are retained with price_usd_per_kg=None;
downstream owns outlier/holdout logic (mirrors usitc.py).

THE FALLBACK HAZARD (read engine/prices.py before relying on this): once foreign
rows share price_observations with US rows, the resolver's "any region" fallback
could silently feed a Chinese export price into a US floor calc. The companion
guard in prices.py (region allowlist / strict-pin) MUST land with this connector.

API: UN Comtrade Comtrade+ ("preview"/free tier). A free subscription key raises
rate limits and is read from COMTRADE_API_KEY when present; without it the public
endpoint still serves limited requests. Endpoint shape:
  https://comtradeapi.un.org/data/v1/get/C/A/HS?reporterCode=...&period=...&cmdCode=...&flowCode=M
We request ANNUAL (freq "A") by default for the long validation window; monthly
("M") is available via --freq M where a reporter supports it.

Run examples:
  python pipeline/comtrade.py --dry-run
  COMTRADE_API_KEY=... python pipeline/comtrade.py --reporters DE CN JP KR --years 10 --write
  python pipeline/comtrade.py --mock-response data/raw/comtrade/<file>.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    from pipeline.storage import write_price_observations
except ModuleNotFoundError:  # Allows `python pipeline/comtrade.py` from the repo root.
    from storage import write_price_observations

COMTRADE_BASE = "https://comtradeapi.un.org/data/v1/get/C"
SOURCE = "COMTRADE"
DEFAULT_YEARS = 10

# Our chemical ids -> 6-digit HS codes (Comtrade's reporting granularity).
# Note: USITC uses 8/10-digit HTS; Comtrade is HS6. 292141 is the aniline+salts
# subheading; 281410 is anhydrous ammonia. We store the HS6 in hts_code.
CHEMICALS = {
    "benzene": {"hs_code": "290220"},
    "aniline": {"hs_code": "292141"},
    "ammonia": {"hs_code": "281410"},
}

# Reporter ISO-alpha2 -> Comtrade M49 numeric reporter code. Comtrade keys
# countries by M49, not ISO; this is the lookup for the candidate reporter set.
# (Extend as needed; these cover the dense-aniline-trade candidates.)
REPORTER_M49 = {
    "US": "842", "DE": "276", "CN": "156", "JP": "392", "KR": "410",
    "IN": "699", "BE": "056", "NL": "528", "FR": "251", "IT": "380",
    "GB": "826", "ES": "724", "BR": "076",
}

# Comtrade flow codes -> the region suffix we store. Imports and exports are kept
# as DISTINCT region tags so they are never blended.
FLOW_SUFFIX = {"M": "IMPORTS", "X": "EXPORTS"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def region_tag(reporter_iso: str, flow_code: str) -> str:
    """Region string stored in price_observations, e.g. ('DE','M') -> 'DE_IMPORTS'."""
    suffix = FLOW_SUFFIX.get(flow_code.upper())
    if suffix is None:
        raise ValueError(f"unknown flow code {flow_code!r}; expected 'M' or 'X'")
    return f"{reporter_iso.upper()}_{suffix}"


def _period_list(years: int, freq: str) -> str:
    """Comtrade `period` param: comma-separated years (A) or YYYYMM months (M)."""
    now = datetime.now(timezone.utc)
    if freq == "A":
        first = now.year - years
        return ",".join(str(y) for y in range(first, now.year + 1))
    # Monthly: build YYYYMM back `years*12` months from the current month.
    end_total = now.year * 12 + (now.month - 1)
    months = []
    for offset in range(years * 12 + 1):
        y, m = divmod(end_total - offset, 12)
        months.append(f"{y:04d}{m + 1:02d}")
    return ",".join(reversed(months))


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_key:
        # Comtrade+ subscription key header.
        headers["Ocp-Apim-Subscription-Key"] = api_key
    return headers


def build_params(reporter_m49: str, hs_code: str, flow_code: str,
                 years: int, freq: str) -> dict[str, str]:
    """Query params for one reporter/commodity/flow over the period window."""
    return {
        "reporterCode": reporter_m49,
        "period": _period_list(years, freq),
        "cmdCode": hs_code,
        "flowCode": flow_code.upper(),
        "partnerCode": "0",        # 0 = World (all partners aggregated)
        "partner2Code": "0",
        "customsCode": "C00",
        "motCode": "0",
        "includeDesc": "false",
    }


def _raw_path(chemical_id: str, reporter_iso: str, flow_code: str,
              raw_dir: Path | None = None) -> Path:
    raw_dir = raw_dir or _repo_root() / "data" / "raw" / "comtrade"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return raw_dir / f"{timestamp}_{chemical_id}_{reporter_iso}_{flow_code}.json"


def preserve_raw_response(response_json: dict[str, Any], chemical_id: str,
                          reporter_iso: str, flow_code: str,
                          raw_dir: Path | None = None) -> Path:
    """Write raw Comtrade JSON without overwriting (mirrors usitc.py)."""
    path = _raw_path(chemical_id, reporter_iso, flow_code, raw_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = path
    for suffix in range(1, 1000):
        try:
            with candidate.open("x") as f:
                json.dump(response_json, f, indent=2, sort_keys=True)
                f.write("\n")
            return candidate
        except FileExistsError:
            candidate = path.with_name(f"{path.stem}_{suffix}{path.suffix}")
    raise FileExistsError(f"Could not find a free raw response path for {path}")


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    if cleaned in {"", "-", "."}:
        return None
    return float(cleaned)


def _period(raw_period: Any, freq: str) -> str | None:
    """Comtrade period -> 'YYYY-MM'. Annual rows store at 'YYYY-12' (year slot,
    same convention usgs_minerals.py uses for annual data)."""
    if raw_period is None:
        return None
    text = str(raw_period).strip()
    if freq == "A" and re.match(r"^\d{4}$", text):
        return f"{text}-12"
    if re.match(r"^\d{6}$", text):           # YYYYMM
        return f"{text[:4]}-{text[4:]}"
    if re.match(r"^\d{4}-\d{2}$", text):
        return text
    return None


def _unit_value(value_usd: float | None, net_weight_kg: float | None) -> float | None:
    if value_usd is None or net_weight_kg is None or net_weight_kg <= 0:
        return None
    return round(value_usd / net_weight_kg, 6)


def normalize_rows(chemical_id: str, hs_code: str, reporter_iso: str,
                   flow_code: str, response_json: dict[str, Any], freq: str,
                   fetched_at: str | None = None) -> list[dict[str, Any]]:
    """Normalize Comtrade records into the price_observations schema.

    Comtrade returns {"data": [ {period, primaryValue, netWgt, ...}, ... ]}.
    primaryValue is trade value in USD; netWgt is net weight in kg. We keep one
    row per period; rows lacking a usable weight get price_usd_per_kg=None.
    """
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    records = response_json.get("data") or []
    region = region_tag(reporter_iso, flow_code)
    hs_norm = re.sub(r"[^0-9]", "", hs_code)

    by_period: dict[str, dict[str, Any]] = {}
    for rec in records:
        period = _period(rec.get("period") or rec.get("refYear") or rec.get("refMonth"), freq)
        if period is None:
            continue
        value_usd = _number(rec.get("primaryValue") or rec.get("PrimaryValue"))
        net_weight = _number(rec.get("netWgt") or rec.get("NetWgt") or rec.get("netWeight"))
        # If a period appears more than once (e.g. partner splits leaked in),
        # accumulate value and weight so the unit value stays a true average.
        slot = by_period.setdefault(period, {"value_usd": 0.0, "net_weight": 0.0,
                                             "has_value": False, "has_weight": False})
        if value_usd is not None:
            slot["value_usd"] += value_usd
            slot["has_value"] = True
        if net_weight is not None:
            slot["net_weight"] += net_weight
            slot["has_weight"] = True

    rows = []
    for period in sorted(by_period):
        slot = by_period[period]
        value_usd = slot["value_usd"] if slot["has_value"] else None
        net_weight = slot["net_weight"] if slot["has_weight"] else None
        rows.append({
            "chemical_id": chemical_id,
            "source": SOURCE,
            "region": region,
            "period": period,
            "price_usd_per_kg": _unit_value(value_usd, net_weight),
            "fetched_at": fetched_at,
            "hts_code": hs_norm,        # HS6 here (vs USITC's 8/10-digit HTS)
            "grade": None,
            "assessment_type": None,
        })
    return rows


def fetch_one(api_key: str | None, chemical_id: str, hs_code: str,
              reporter_iso: str, flow_code: str, years: int, freq: str,
              raw_dir: Path | None = None) -> list[dict[str, Any]]:
    reporter_m49 = REPORTER_M49.get(reporter_iso.upper())
    if reporter_m49 is None:
        raise ValueError(f"no M49 code for reporter {reporter_iso!r}; add it to REPORTER_M49")
    url = f"{COMTRADE_BASE}/{freq}/HS"
    response = requests.get(url, headers=_headers(api_key),
                            params=build_params(reporter_m49, hs_code, flow_code, years, freq),
                            timeout=60)
    response.raise_for_status()
    response_json = response.json()
    raw_path = preserve_raw_response(response_json, chemical_id, reporter_iso,
                                     flow_code, raw_dir)
    print(f"      raw saved -> {raw_path}")
    return normalize_rows(chemical_id, hs_code, reporter_iso, flow_code,
                          response_json, freq)


def load_mock_response(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return json.load(f)


def pull_all(api_key: str | None, reporters: list[str], flows: list[str],
             years: int = DEFAULT_YEARS, freq: str = "A",
             chemicals: list[str] | None = None, raw_dir: Path | None = None,
             mock_response: Path | None = None) -> list[dict[str, Any]]:
    chemicals = chemicals or list(CHEMICALS)
    rows: list[dict[str, Any]] = []
    for chemical_id in chemicals:
        hs_code = CHEMICALS[chemical_id]["hs_code"]
        for reporter_iso in reporters:
            for flow_code in flows:
                print(f"  {chemical_id} (HS {hs_code}) | {reporter_iso} {flow_code} ...")
                if mock_response:
                    response_json = load_mock_response(mock_response)
                    preserve_raw_response(response_json, chemical_id, reporter_iso,
                                          flow_code, raw_dir)
                    fetched = normalize_rows(chemical_id, hs_code, reporter_iso,
                                             flow_code, response_json, freq)
                else:
                    fetched = fetch_one(api_key, chemical_id, hs_code, reporter_iso,
                                        flow_code, years, freq, raw_dir)
                clean = sum(r["price_usd_per_kg"] is not None for r in fetched)
                print(f"      {len(fetched)} rows ({clean} clean)")
                rows.extend(fetched)
    return rows


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pull UN Comtrade trade benchmarks by country.")
    parser.add_argument("--reporters", nargs="+", default=["DE", "CN", "JP", "KR"],
                        help="Reporter ISO-alpha2 codes (default: DE CN JP KR).")
    parser.add_argument("--flows", nargs="+", default=["M"], choices=["M", "X"],
                        help="Trade flows: M=imports, X=exports (default: M).")
    parser.add_argument("--chemicals", nargs="+", default=None,
                        choices=list(CHEMICALS),
                        help="Subset of chemicals (default: all).")
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS)
    parser.add_argument("--freq", choices=["A", "M"], default="A",
                        help="A=annual (default, long window), M=monthly.")
    parser.add_argument("--raw-dir", type=Path,
                        default=_repo_root() / "data" / "raw" / "comtrade")
    parser.add_argument("--mock-response", type=Path,
                        help="Local raw Comtrade JSON for validation without the API.")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--write", action="store_false", dest="dry_run",
                        help="Write normalized observations to PostgreSQL.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    api_key = os.environ.get("COMTRADE_API_KEY")  # optional; raises rate limits

    print("Pulling chemical trade price signals from UN Comtrade...")
    try:
        rows = pull_all(api_key=api_key, reporters=args.reporters, flows=args.flows,
                        years=args.years, freq=args.freq, chemicals=args.chemicals,
                        raw_dir=args.raw_dir, mock_response=args.mock_response)
    except requests.HTTPError as exc:
        print(f"ERROR: Comtrade request failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not rows:
        print("WARNING: no normalized rows returned; nothing written.", file=sys.stderr)
        return 1

    write_price_observations(rows, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
