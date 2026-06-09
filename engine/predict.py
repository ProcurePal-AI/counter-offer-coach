"""
predict.py -- Cat 3.4 Monte Carlo propagation + Cat 3.5 public predictor.

This module wraps the existing deterministic calculators:
  * feedstock.py  (3.1)
  * utilities.py  (3.2)
  * markup.py     (3.3)

It intentionally does not duplicate their formulas. Cat 2 monthly prices are
treated as fixed observations for the requested month/region; only structured
Cat 1 static uncertainty is sampled. If Cat 1 gives no machine-readable band for
a required parameter, the deterministic value is preserved as low=base=high.
"""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

# Sibling-module import (this repo runs modules as scripts, no package).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from feedstock import feedstock_cost, load_route  # noqa: E402
from markup import FloorCosts, MarkupParams, apply_markup, params_from_config  # noqa: E402
from prices import resolve_price_usd_per_ton, resolve_utility_price_usd_per_unit  # noqa: E402
from routes import default_route  # noqa: E402
from utilities import utility_cost  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROCESS = "aniline"
DEFAULT_DRAWS = 1000
DEFAULT_SEED = 42
CURRENCY = "USD_per_metric_ton"

PriceFn = Callable[[str, str, str], float]
UtilityPriceFn = Callable[[str, str, str], float]


@dataclass(frozen=True)
class SampleSpec:
    """A Cat 1 parameter represented as a triangular distribution."""

    path: str
    low: float
    base: float
    high: float
    has_band: bool

    def draw(self, rng: np.random.Generator) -> float:
        if self.low == self.base == self.high:
            return self.base
        return float(rng.triangular(self.low, self.base, self.high))


def _percentiles(values: list[float]) -> dict[str, float]:
    p10, p50, p90 = np.percentile(np.asarray(values, dtype=float), [10, 50, 90])
    return {"p10": float(p10), "p50": float(p50), "p90": float(p90)}


def _numeric_spec(path: str, value: float) -> SampleSpec:
    v = float(value)
    return SampleSpec(path=path, low=v, base=v, high=v, has_band=False)


def _field_spec(path: str, raw: Any) -> SampleSpec:
    """Return a SampleSpec from a numeric field or a future low/base/high mapping."""
    if isinstance(raw, dict):
        if {"low", "base", "high"} <= set(raw):
            return SampleSpec(
                path=path,
                low=float(raw["low"]),
                base=float(raw["base"]),
                high=float(raw["high"]),
                has_band=True,
            )
        if {"min", "base", "max"} <= set(raw):
            return SampleSpec(
                path=path,
                low=float(raw["min"]),
                base=float(raw["base"]),
                high=float(raw["max"]),
                has_band=True,
            )
        if "value" in raw:
            return _numeric_spec(path, float(raw["value"]))
    return _numeric_spec(path, float(raw))


def _yield_spec(step: dict) -> SampleSpec:
    y = step["yield_pct"]
    path = f"steps.{step['step_id']}.yield_pct"
    if {"low", "base", "high"} <= set(y):
        return SampleSpec(path, float(y["low"]), float(y["base"]), float(y["high"]), True)
    if {"min", "base", "max"} <= set(y):
        return SampleSpec(path, float(y["min"]), float(y["base"]), float(y["max"]), True)

    value = float(y["value"])
    uncertainty = y.get("uncertainty")
    if uncertainty is None:
        return _numeric_spec(path, value)
    band = float(uncertainty)
    return SampleSpec(
        path=path,
        low=max(0.0, value - band),
        base=value,
        high=min(100.0, value + band),
        has_band=band > 0.0,
    )


def inspect_cat1_uncertainty(route: dict) -> dict[str, list[dict[str, Any]]]:
    """Inspect Cat 1 route parameters that the MC wrapper knows how to sample."""
    sampled: list[dict[str, Any]] = []
    deterministic: list[dict[str, Any]] = []

    def add(spec: SampleSpec) -> None:
        row = {
            "path": spec.path,
            "low": spec.low,
            "base": spec.base,
            "high": spec.high,
        }
        (sampled if spec.has_band else deterministic).append(row)

    for step in route["steps"]:
        add(_yield_spec(step))
        energy = step.get("energy", {})
        for key in ("electricity_kwh_per_ton_output", "steam_gj_per_ton_output"):
            if key in energy:
                add(_field_spec(f"steps.{step['step_id']}.energy.{key}", energy[key]))

    markup = route.get("markup", {})
    for key in ("variable_pct", "fixed_pct", "target_margin_pct"):
        if key in markup:
            add(_field_spec(f"markup.{key}", markup[key]))

    return {"sampled": sampled, "deterministic": deterministic}


def _sample_route(route: dict, rng: np.random.Generator) -> tuple[dict, dict]:
    sampled = copy.deepcopy(route)
    sample_values: dict[str, float] = {}

    for step in sampled["steps"]:
        spec = _yield_spec(step)
        value = spec.draw(rng)
        step["yield_pct"]["value"] = value
        sample_values[spec.path] = value

        energy = step.get("energy", {})
        for key in ("electricity_kwh_per_ton_output", "steam_gj_per_ton_output"):
            if key not in energy:
                continue
            spec = _field_spec(f"steps.{step['step_id']}.energy.{key}", energy[key])
            value = spec.draw(rng)
            energy[key] = value
            sample_values[spec.path] = value

    return sampled, sample_values


def _sample_markup_params(route_markup: dict, rng: np.random.Generator) -> MarkupParams:
    variable = _field_spec("markup.variable_pct", route_markup["variable_pct"]).draw(rng)
    fixed = _field_spec("markup.fixed_pct", route_markup["fixed_pct"]).draw(rng)
    margin = _field_spec("markup.target_margin_pct", route_markup["target_margin_pct"]).draw(rng)
    return MarkupParams(
        variable_pct=variable,
        fixed_pct=fixed,
        revenue_shares={"target_margin": margin},
        source=str(route_markup.get("source", "")),
    )


def _fixed_price_fn(conn: Any) -> PriceFn:
    return lambda chemical_id, period, region: resolve_price_usd_per_ton(
        chemical_id, period, region, conn
    )


def _fixed_utility_price_fn(conn: Any) -> UtilityPriceFn:
    return lambda kind, period, region: resolve_utility_price_usd_per_unit(
        kind, period, region, conn
    )


def monte_carlo_fair_price(
    month: str,
    region: str,
    *,
    process: str = DEFAULT_PROCESS,
    route_name: str | None = None,
    n_draws: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED,
    feedstock_price_fn: PriceFn,
    utility_price_fn: UtilityPriceFn,
) -> dict:
    """Run MC with injected deterministic Cat 2 price resolvers."""
    if n_draws <= 0:
        raise ValueError(f"n_draws must be positive, got {n_draws}")

    route_name = route_name or default_route(process)
    base_route = load_route(process, route_name)
    rng = np.random.default_rng(seed)
    draws: list[dict[str, float]] = []

    for _ in range(n_draws):
        sampled_route, _ = _sample_route(base_route, rng)
        params = _sample_markup_params(sampled_route["markup"], rng)

        fs = feedstock_cost(
            process,
            route_name,
            month,
            region,
            feedstock_price_fn,
            route_override=sampled_route,
        )
        ut = utility_cost(
            process,
            route_name,
            month,
            region,
            utility_price_fn,
            route_override=sampled_route,
        )
        marked = apply_markup(
            FloorCosts(
                feedstock_usd_per_ton=fs["feedstock_cost_usd_per_ton"],
                utility_usd_per_ton=ut["utility_cost_usd_per_ton"],
                catalyst_usd_per_ton=ut.get("catalyst_cost_usd_per_ton", 0.0),
            ),
            params,
        )

        feedstock = fs["feedstock_cost_usd_per_ton"]
        utility = ut["utility_cost_usd_per_ton"]
        total = marked["fair_price_usd_per_ton"]
        draws.append(
            {
                "total": total,
                "feedstock": feedstock,
                "utility": utility,
                # Public API has three layers; catalyst is therefore included in
                # this residual non-feedstock/non-utility layer.
                "markup": total - feedstock - utility,
            }
        )

    return {
        "draws": draws,
        "uncertainty": inspect_cat1_uncertainty(base_route),
        "route": route_name,
    }


def _summarize_mc(month: str, region: str, mc: dict) -> dict:
    draws = mc["draws"]
    result = {
        "month": month,
        "region": region,
        "currency": CURRENCY,
        **_percentiles([d["total"] for d in draws]),
        "breakdown": {
            "feedstock": _percentiles([d["feedstock"] for d in draws]),
            "utility": _percentiles([d["utility"] for d in draws]),
            "markup": _percentiles([d["markup"] for d in draws]),
        },
        "route": mc["route"],
        "n_draws": len(draws),
        "uncertainty": mc["uncertainty"],
    }
    return result


def predict_fair_price(
    month: str,
    region: str,
    *,
    n_draws: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED,
) -> dict:
    """Return a JSON-serializable P10/P50/P90 fair-price prediction."""
    sys.path.insert(0, str(ROOT / "pipeline"))
    import storage  # noqa: WPS433

    conn = storage.connect()
    try:
        mc = monte_carlo_fair_price(
            month,
            region,
            n_draws=n_draws,
            seed=seed,
            feedstock_price_fn=_fixed_price_fn(conn),
            utility_price_fn=_fixed_utility_price_fn(conn),
        )
        return _summarize_mc(month, region, mc)
    finally:
        conn.close()


def deterministic_fair_price(
    month: str,
    region: str,
    *,
    feedstock_price_fn: PriceFn,
    utility_price_fn: UtilityPriceFn,
    process: str = DEFAULT_PROCESS,
    route_name: str | None = None,
) -> dict:
    """One deterministic pass, used by tests to compare zero-uncertainty MC."""
    route_name = route_name or default_route(process)
    route = load_route(process, route_name)
    fs = feedstock_cost(
        process, route_name, month, region, feedstock_price_fn, route_override=route
    )
    ut = utility_cost(process, route_name, month, region, utility_price_fn, route_override=route)
    marked = apply_markup(
        FloorCosts(
            feedstock_usd_per_ton=fs["feedstock_cost_usd_per_ton"],
            utility_usd_per_ton=ut["utility_cost_usd_per_ton"],
            catalyst_usd_per_ton=ut.get("catalyst_cost_usd_per_ton", 0.0),
        ),
        params_from_config(route["markup"]),
    )
    total = marked["fair_price_usd_per_ton"]
    feedstock = fs["feedstock_cost_usd_per_ton"]
    utility = ut["utility_cost_usd_per_ton"]
    return {
        "total": total,
        "feedstock": feedstock,
        "utility": utility,
        "markup": total - feedstock - utility,
    }


def _main() -> int:
    result = predict_fair_price("2025-01", "US")
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
