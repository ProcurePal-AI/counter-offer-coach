"""
routes.py -- route discovery & selection for the should-cost engine.

Until now callers hardcoded a route name (e.g. "benzene_nitration_hydrogenation")
when calling feedstock_cost / utility_cost. This module lets code instead ask the
config what routes exist, which one is the default, and what distinguishes each --
so adding a second route (phenol amination, etc.) needs no edits to the callers.

It is pure config inspection: no prices, no DB. Cost still lives in feedstock.py
(3.1) and utilities.py (3.2); this only answers "which route, and what is it?".
"""

from __future__ import annotations

import sys
from pathlib import Path

# Sibling-module import (this repo runs modules as scripts, no package).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from feedstock import (  # noqa: E402
    CONFIG_DIR,
    _load_yaml,
    feedstock_masses_per_ton,
    final_product,
    load_registry_mw,
    load_route,
)


def _routes_map(process: str, config_path: Path | None = None) -> dict:
    path = config_path or (CONFIG_DIR / "aniline.yaml")
    processes = _load_yaml(path)["processes"]
    if process not in processes:
        raise KeyError(f"process {process!r} not in config (have: {list(processes)})")
    return processes[process]["routes"]


def list_routes(process: str, *, config_path: Path | None = None) -> list[str]:
    """All route names defined for `process`, in config order."""
    return list(_routes_map(process, config_path))


def default_route(process: str, *, config_path: Path | None = None) -> str:
    """The route flagged `is_default: true`.

    Exactly one default is expected. If none is flagged but only one route
    exists, that route is the default. Zero defaults with multiple routes, or
    more than one default, is a config error and raises (better than silently
    benchmarking the wrong process).
    """
    routes = _routes_map(process, config_path)
    if not routes:
        raise ValueError(f"{process}: no routes defined")
    flagged = [name for name, r in routes.items() if r.get("is_default")]
    if len(flagged) == 1:
        return flagged[0]
    if not flagged and len(routes) == 1:
        return next(iter(routes))
    if not flagged:
        raise ValueError(
            f"{process}: no route marked is_default among {list(routes)} "
            f"(set is_default: true on exactly one)"
        )
    raise ValueError(
        f"{process}: {len(flagged)} routes marked is_default ({flagged}); "
        f"exactly one expected"
    )


def route_info(process: str, route_name: str, *,
               config_path: Path | None = None,
               registry_path: Path | None = None) -> dict:
    """A descriptor that distinguishes a route from its siblings.

    Includes the things that actually differ between routes: the final product,
    the ordered step ids, the purchased feedstocks (the real fingerprint --
    e.g. benzene+nitric_acid+hydrogen vs phenol+ammonia), and which chemicals are
    intermediates. Requires the route's chemicals to be in the registry (for MWs).
    """
    route = load_route(process, route_name, config_path)
    mw = load_registry_mw(registry_path)
    ordered = sorted(route["steps"], key=lambda s: s["order"])
    step_outputs = {s["main_output"] for s in route["steps"]}
    consumed = {i["chemical_id"] for s in route["steps"] for i in s["inputs"]}
    return {
        "route": route_name,
        "is_default": bool(route.get("is_default", False)),
        "final_product": final_product(route),
        "n_steps": len(ordered),
        "steps": [s["step_id"] for s in ordered],
        "purchased_feedstocks": sorted(feedstock_masses_per_ton(route, mw)),
        "intermediates": sorted(step_outputs & consumed),
    }


def describe_routes(process: str, *,
                    config_path: Path | None = None,
                    registry_path: Path | None = None) -> dict:
    """{route_name: route_info(...)} for every route -- lets a caller tell them apart."""
    return {
        name: route_info(process, name, config_path=config_path, registry_path=registry_path)
        for name in list_routes(process, config_path=config_path)
    }


def _main() -> int:
    process = "aniline"
    names = list_routes(process)
    default = default_route(process)
    print(f"process '{process}': {len(names)} route(s); default = {default}\n")
    for name, info in describe_routes(process).items():
        star = " (default)" if info["is_default"] else ""
        print(f"  {name}{star}")
        print(f"      product:     {info['final_product']}")
        print(f"      steps:       {' -> '.join(info['steps'])}")
        print(f"      feedstocks:  {', '.join(info['purchased_feedstocks'])}")
        if info["intermediates"]:
            print(f"      intermediates: {', '.join(info['intermediates'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
