"""
feedstock.py -- Cat 3.1 feedstock cost calculator.

Computes the feedstock (purchased raw-material) cost of one metric ton of a
route's final product, from the process config + chemical registry + an injected
price resolver. It implements the cost-resolution contract from the Cat 3 note:

  * An input that is the main_output of an earlier step is an INTERMEDIATE: it is
    never priced from the market. Instead its own upstream feedstocks are traced
    through and counted once. This is what prevents double-counting nitrobenzene.
  * An input produced by no step is a PURCHASED FEEDSTOCK: it is priced via the
    injected resolver (prices.resolve_price_usd_per_ton).

This module does cost *assembly* only -- stoichiometry x (MW_in / MW_out) / yield
-- and reads MWs from the chemical registry. All price *derivation* lives in
prices.py and is injected as `price_fn`, so feedstock costing is pure and
testable with no database.

CLI:  python engine/feedstock.py   # prints per-ton feedstock masses; tries a
                                    # cost if the SQLite store has the prices.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

import yaml

# Sibling-module import (this repo runs modules as scripts, no package).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prices import PriceUnavailable  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"

# A resolver: (chemical_id, period, region) -> price in USD per metric ton.
PriceFn = Callable[[str, str, str], float]


def _load_yaml(path: Path) -> dict:
    # encoding pinned: configs hold en-dashes; a GBK/CJK default locale would crash.
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def load_registry_mw(registry_path: Path | None = None) -> dict[str, float]:
    """{chemical_id: molecular_weight_g_per_mol} from the chemical registry."""
    path = registry_path or (CONFIG_DIR / "chemical_registry.yaml")
    chem = _load_yaml(path)["chemicals"]
    return {name: float(rec["molecular_weight_g_per_mol"]) for name, rec in chem.items()}


def load_route(process: str, route: str, config_path: Path | None = None) -> dict:
    path = config_path or (CONFIG_DIR / "aniline.yaml")
    return _load_yaml(path)["processes"][process]["routes"][route]


def final_product(route: dict) -> str:
    """The product a route makes: produced by a step, consumed by none."""
    outputs = {s["main_output"] for s in route["steps"]}
    consumed = {i["chemical_id"] for s in route["steps"] for i in s["inputs"]}
    terminal = outputs - consumed
    if len(terminal) == 1:
        return next(iter(terminal))
    # Fallback for odd graphs: the highest-order step's output.
    return max(route["steps"], key=lambda s: s["order"])["main_output"]


def feedstock_masses_per_ton(route: dict, mw: dict[str, float]) -> dict[str, float]:
    """Metric tons of each PURCHASED feedstock per ton of the final product.

    Intermediates are traced through (recursed), never accumulated -- so the
    same atoms are counted exactly once and no intermediate is "purchased".
    """
    by_output = {s["main_output"]: s for s in route["steps"]}
    masses: dict[str, float] = {}

    def need(chemical_id: str, qty_tons: float) -> None:
        step = by_output.get(chemical_id)
        if step is None:  # leaf == purchased feedstock
            masses[chemical_id] = masses.get(chemical_id, 0.0) + qty_tons
            return
        yield_frac = float(step["yield_pct"]["value"]) / 100.0
        mw_out = mw[step["main_output"]]
        for inp in step["inputs"]:
            cid = inp["chemical_id"]
            # tons of input per `qty_tons` of this step's output:
            #   stoich (mol/mol) x MW ratio (mass/mass) / yield (loss gross-up)
            mass_in = qty_tons * float(inp["stoichiometric_ratio"]) * (mw[cid] / mw_out) / yield_frac
            need(cid, mass_in)

    need(final_product(route), 1.0)
    return masses


def feedstock_cost(process: str, route_name: str, period: str, region: str,
                   price_fn: PriceFn, *, config_path: Path | None = None,
                   registry_path: Path | None = None) -> dict:
    """Feedstock cost ($/ton of final product) plus a per-feedstock breakdown.

    Raises PriceUnavailable (listing every missing feed) if any purchased
    feedstock has no price source yet -- a feedstock cost with a guessed input
    would be worse than no number.
    """
    route = load_route(process, route_name, config_path)
    mw = load_registry_mw(registry_path)
    masses = feedstock_masses_per_ton(route, mw)

    lines: dict[str, dict] = {}
    total = 0.0
    missing: list[str] = []
    for cid, mass_t in sorted(masses.items()):
        try:
            price_t = price_fn(cid, period, region)
        except PriceUnavailable as exc:
            missing.append(str(exc))
            continue
        cost = price_t * mass_t
        lines[cid] = {
            "mass_t_per_t": mass_t,
            "price_usd_per_ton": price_t,
            "cost_usd_per_ton": cost,
        }
        total += cost

    if missing:
        raise PriceUnavailable(
            "cannot compute feedstock cost; missing prices:\n  - "
            + "\n  - ".join(missing)
        )

    return {
        "product": final_product(route),
        "period": period,
        "region": region,
        "feedstock_cost_usd_per_ton": total,
        "by_feedstock": lines,
    }


def _main() -> int:
    process, route_name = "aniline", "benzene_nitration_hydrogenation"
    route = load_route(process, route_name)
    mw = load_registry_mw()

    print(f"Feedstock masses per ton of {final_product(route)} "
          f"({process}/{route_name}):")
    for cid, mass in sorted(feedstock_masses_per_ton(route, mw).items()):
        print(f"  {cid:12s} {mass:.4f} t/t   ({mass * 1000:.1f} kg/t)")

    # Try a real cost if the store has the prices; report honestly if it doesn't.
    sys.path.insert(0, str(ROOT / "pipeline"))
    try:
        import storage
        import prices

        conn = storage.connect()
        try:
            result = feedstock_cost(
                process, route_name, "2025-01", "US",
                lambda c, p, r: prices.resolve_price_usd_per_ton(c, p, r, conn),
            )
        finally:
            conn.close()
        print(f"\nFeedstock cost: ${result['feedstock_cost_usd_per_ton']:.2f}/ton")
        for cid, ln in result["by_feedstock"].items():
            print(f"  {cid:12s} {ln['mass_t_per_t']:.4f} t/t x "
                  f"${ln['price_usd_per_ton']:.2f}/t = ${ln['cost_usd_per_ton']:.2f}/t")
    except PriceUnavailable as exc:
        print(f"\n(no full cost yet -- {exc})")
    except Exception as exc:  # missing DB, etc.
        print(f"\n(no full cost yet -- store not ready: {exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
