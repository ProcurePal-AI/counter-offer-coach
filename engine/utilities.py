"""
utilities.py -- Cat 3.2 utility (energy) cost calculator.

Computes the metered-utility cost of one metric ton of a route's final product:
electricity and steam, read per step as `electricity_kwh_per_ton_output` and
`steam_gj_per_ton_output`, scaled to a per-ton-final-product basis, and priced
through an injected utility resolver. It also reports the per-step catalyst cost
(a direct $/ton-output figure in the config), since the should-cost floor needs
it and it shares the same step-mass scaling -- but catalyst is kept as a SEPARATE
line, not folded into "utilities," because it isn't a metered energy stream.

Where 3.2 sits
--------------
This is the energy sibling of feedstock.py (3.1). Both turn process config into a
per-ton cost; both stay pure (no DB) by taking an injected price function. It is
independent of 2.4 (ammonia -> nitric acid is a FEEDSTOCK price) and 2.5
(producer margins are the MARKUP layer, applied after this build-up). 3.2 needs
only utility prices: electricity (EIA, direct) and steam (derived from gas).

Scaling note
------------
The config gives energy per ton of each STEP's output. To express it per ton of
final product, each step's figure is multiplied by the mass of that step's output
consumed per ton of final product (e.g. nitrobenzene per ton aniline), traced
with the same recursion feedstock.py uses. The final step's output mass is 1.0.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

# Sibling-module import (this repo runs modules as scripts, no package).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prices import PriceUnavailable  # noqa: E402
from feedstock import (  # noqa: E402  -- reuse loaders + product detection
    CONFIG_DIR,
    ROOT,
    final_product,
    load_registry_mw,
    load_route,
)

# A utility resolver: (utility_kind, period, region) -> price in the config unit
# ($/kWh for electricity, $/GJ for steam).
UtilityPriceFn = Callable[[str, str, str], float]


def step_output_masses_per_ton(route: dict, mw: dict[str, float]) -> dict[str, float]:
    """Metric tons of each STEP's main_output per ton of the final product.

    Same recursion as feedstock_masses_per_ton, but it records the quantity at
    which each step's output is demanded (the final step is 1.0; an intermediate
    step is the mass of its output consumed downstream per ton of final product).
    """
    by_output = {s["main_output"]: s for s in route["steps"]}
    outputs: dict[str, float] = {}

    def need(chemical_id: str, qty_tons: float) -> None:
        step = by_output.get(chemical_id)
        if step is None:  # purchased feedstock -- not a step output
            return
        outputs[step["step_id"]] = outputs.get(step["step_id"], 0.0) + qty_tons
        yield_frac = float(step["yield_pct"]["value"]) / 100.0
        mw_out = mw[step["main_output"]]
        for inp in step["inputs"]:
            cid = inp["chemical_id"]
            mass_in = qty_tons * float(inp["stoichiometric_ratio"]) * (mw[cid] / mw_out) / yield_frac
            need(cid, mass_in)

    need(final_product(route), 1.0)
    return outputs


# Maps a config energy field -> (utility_kind passed to the resolver, qty unit).
_ENERGY_FIELDS = {
    "electricity_kwh_per_ton_output": ("electricity", "kWh"),
    "steam_gj_per_ton_output": ("steam", "GJ"),
}


def utility_cost(process: str, route_name: str, period: str, region: str,
                 utility_price_fn: UtilityPriceFn, *,
                 include_catalyst: bool = True,
                 config_path: Path | None = None,
                 registry_path: Path | None = None) -> dict:
    """Utility (electricity + steam) cost per ton of final product, with breakdown.

    Raises PriceUnavailable (listing every missing utility) rather than guess.
    `catalyst_cost_usd_per_ton` is reported separately when include_catalyst.
    """
    route = load_route(process, route_name, config_path)
    mw = load_registry_mw(registry_path)
    step_mass = step_output_masses_per_ton(route, mw)

    by_utility: dict[str, dict] = {}
    by_step: dict[str, dict] = {}
    qty_totals: dict[str, float] = {}      # utility_kind -> total qty per ton product
    catalyst_total = 0.0
    missing: list[str] = []

    for step in route["steps"]:
        sid = step["step_id"]
        out_mass = step_mass.get(sid, 0.0)
        step_line: dict[str, float] = {"output_t_per_t": out_mass}

        energy = step.get("energy", {})
        for field, (kind, unit) in _ENERGY_FIELDS.items():
            if field not in energy:
                continue
            qty_per_ton = float(energy[field]) * out_mass  # scale to final product
            qty_totals[kind] = qty_totals.get(kind, 0.0) + qty_per_ton
            step_line[kind] = qty_per_ton

        if include_catalyst:
            cat = step.get("catalyst", {})
            cat_cost = float(cat.get("cost_usd_per_ton_output", 0.0)) * out_mass
            if cat_cost:
                step_line["catalyst_usd"] = cat_cost
                catalyst_total += cat_cost

        by_step[sid] = step_line

    total = 0.0
    for kind, qty in qty_totals.items():
        try:
            price = utility_price_fn(kind, period, region)
        except PriceUnavailable as exc:
            missing.append(str(exc))
            continue
        cost = price * qty
        by_utility[kind] = {
            "qty_per_ton": qty,
            "unit": _kind_unit(kind),
            "price_usd_per_unit": price,
            "cost_usd_per_ton": cost,
        }
        total += cost

    if missing:
        raise PriceUnavailable(
            "cannot compute utility cost; missing utility prices:\n  - "
            + "\n  - ".join(missing)
        )

    result = {
        "product": final_product(route),
        "period": period,
        "region": region,
        "utility_cost_usd_per_ton": total,
        "by_utility": by_utility,
        "by_step": by_step,
    }
    if include_catalyst:
        result["catalyst_cost_usd_per_ton"] = catalyst_total
    return result


def _kind_unit(kind: str) -> str:
    return {"electricity": "kWh", "steam": "GJ", "natural_gas": "MMBtu"}.get(kind, "")


def _main() -> int:
    process, route_name = "aniline", "benzene_nitration_hydrogenation"
    route = load_route(process, route_name)
    mw = load_registry_mw()

    print(f"Step-output masses per ton of {final_product(route)} "
          f"({process}/{route_name}):")
    for sid, mass in step_output_masses_per_ton(route, mw).items():
        print(f"  {sid:14s} {mass:.4f} t output / t product")

    sys.path.insert(0, str(ROOT / "pipeline"))
    try:
        import storage
        import prices

        conn = storage.connect()
        try:
            result = utility_cost(
                process, route_name, "2025-01", "US",
                lambda k, p, r: prices.resolve_utility_price_usd_per_unit(k, p, r, conn),
            )
        finally:
            conn.close()
        print(f"\nUtility cost: ${result['utility_cost_usd_per_ton']:.2f}/ton "
              f"(+ catalyst ${result.get('catalyst_cost_usd_per_ton', 0):.2f}/ton)")
        for kind, ln in result["by_utility"].items():
            print(f"  {kind:12s} {ln['qty_per_ton']:.2f} {ln['unit']}/t x "
                  f"${ln['price_usd_per_unit']:.4f} = ${ln['cost_usd_per_ton']:.2f}/t")
    except PriceUnavailable as exc:
        print(f"\n(no utility cost yet -- {exc})")
    except Exception as exc:  # missing DB, etc.
        print(f"\n(no utility cost yet -- store not ready: {exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
