"""
margin_anchors.py -- load country-parameterized markup anchors (config + EDGAR).

Bridges config/margin_anchors.yaml to engine/markup.py so the markup layer's
revenue-share ratios come from a per-country source declared in config, never
hardcoded. Two modes:

  * edgar_summary (US): delegates to pipeline/edgar_financials.py's
    load_peer_median_ratios() on the generated summary CSV.
  * manual (e.g. CN/Wanhua): reads hand-extracted ratios, REQUIRING a source
    citation per ratio and refusing null values -- an unanchored markup must
    fail loudly, not run silently on a placeholder.

Output shape matches markup.params_from_edgar_ratios inputs:
    {"sga_pct": ..., "da_pct": ..., "ebit_margin_pct": ..., "fixed_pct_ex_da": ...}

Usage:
    from margin_anchors import load_anchor
    a = load_anchor("US")
    params = params_from_edgar_ratios(a["sga_pct"], a["da_pct"],
                                      a["ebit_margin_pct"],
                                      variable_pct=0.02,
                                      fixed_pct_ex_da=a["fixed_pct_ex_da"])
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "margin_anchors.yaml"
RATIO_KEYS = ("sga_pct", "da_pct", "ebit_margin_pct")
MAX_RATIO = 0.6  # mirrors markup.MAX_REVENUE_WEDGE: a single share past this is a data error


class AnchorError(ValueError):
    """The margin-anchor config for a country is missing or invalid."""


def _load_config(config_path: Path | None = None) -> dict:
    path = config_path or DEFAULT_CONFIG
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    countries = cfg.get("countries")
    if not isinstance(countries, dict) or not countries:
        raise AnchorError(f"{path}: no `countries` mapping found")
    return countries


def _load_edgar_mode(entry: dict, country: str) -> dict[str, float]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from edgar_financials import load_peer_median_ratios  # noqa: E402

    summary = ROOT / entry.get("summary_csv", "data/edgar_financials_summary.csv")
    if not summary.exists():
        raise AnchorError(
            f"{country}: mode edgar_summary but {summary} does not exist -- "
            f"run `python pipeline/edgar_financials.py` first"
        )
    ratios = load_peer_median_ratios(summary)
    return {k: float(ratios[k]) for k in RATIO_KEYS}


def _require_cited_value(block: dict, name: str, country: str) -> float:
    if not isinstance(block, dict):
        raise AnchorError(f"{country}: ratio {name!r} must be a mapping with value+source")
    value, source = block.get("value"), str(block.get("source") or "").strip()
    if value is None:
        raise AnchorError(
            f"{country}: ratio {name!r} is null -- research it (see config notes) "
            f"before anchoring a markup on this country"
        )
    if not source:
        raise AnchorError(f"{country}: ratio {name!r} has a value but no source citation")
    value = float(value)
    if not 0.0 <= value < MAX_RATIO:
        raise AnchorError(f"{country}: ratio {name!r}={value} outside [0, {MAX_RATIO})")
    return value


def _load_manual_mode(entry: dict, country: str) -> dict[str, float]:
    ratios_block = entry.get("ratios") or {}
    out = {name: _require_cited_value(ratios_block.get(name), name, country)
           for name in RATIO_KEYS}
    return out


def load_anchor(country: str, config_path: Path | None = None) -> dict[str, float]:
    """Markup anchor ratios for `country`, validated; raises AnchorError if unusable."""
    countries = _load_config(config_path)
    entry = countries.get(country)
    if entry is None:
        raise AnchorError(
            f"no margin anchor for {country!r}; configured: {sorted(countries)}")

    mode = entry.get("mode")
    if mode == "edgar_summary":
        ratios = _load_edgar_mode(entry, country)
    elif mode == "manual":
        ratios = _load_manual_mode(entry, country)
    else:
        raise AnchorError(f"{country}: unknown mode {mode!r}")

    fixed = entry.get("fixed_pct_ex_da") or {}
    if isinstance(fixed, dict) and fixed.get("value") is not None:
        ratios["fixed_pct_ex_da"] = float(fixed["value"])
    else:
        ratios["fixed_pct_ex_da"] = 0.04  # documented default, mirrors markup CLI
    return ratios


def main() -> int:
    countries = _load_config()
    for country in sorted(countries):
        try:
            anchor = load_anchor(country)
            pretty = ", ".join(f"{k}={v:.3f}" for k, v in anchor.items())
            print(f"{country}: OK  ({pretty})")
        except AnchorError as exc:
            print(f"{country}: NOT READY -- {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
