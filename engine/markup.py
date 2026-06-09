"""
markup.py -- Cat 3.3 markup layer.

Takes the physical floor computed by 3.1 (feedstock) + 3.2 (utilities + catalyst)
and produces the pre-premium "fair price" by applying the route markup. It is the
LAST deterministic stage before the Monte Carlo wrapper (3.4): floor in, fair
price + itemized wedge out. Pure -- no DB, no config I/O in the math functions;
everything is injected, mirroring how 3.1/3.2 take a price_fn.

The two markup semantics (and why they must not be mixed)
---------------------------------------------------------
The methodology note ("Markup, Margin & Conversion Costs") fixes two rules this
module encodes so they cannot be silently violated:

  1. COST-SIDE percentages (Towler-style overhead heuristics) are fractions of a
     COST base and are applied multiplicatively:
        variable_pct  -- residual allowance for minor unmodeled items, applied to
                         the FEEDSTOCK cost only (per config/aniline.yaml's own
                         comment: "residual fraction of feedstock").
        fixed_pct     -- coarse capital/labor conversion-gap placeholder, applied
                         to the FULL PHYSICAL FLOOR (feedstock + utilities +
                         catalyst): fixed overhead scales with total plant cash
                         cost, not with one input line.

  2. REVENUE-SHARE percentages (anything sourced from financial statements --
     target margin, SG&A/revenue, D&A/revenue, EBIT margin) are fractions of the
     PRICE, so they go in the DENOMINATOR:
        fair_price = cost_base / (1 - sum(revenue_shares))
     NOT cost_base * (1 + sum(...)). The two diverge fast at double-digit
     margins; cost-plus-as-if-revenue-margin is the silent error the note warns
     about, so apply_markup() owns the division and nothing else in the engine
     ever applies a margin.

Two parameter constructors
--------------------------
  * params_from_config(route["markup"])   -- today's aniline.yaml block:
      revenue side = {target_margin: target_margin_pct} only.
  * params_from_edgar_ratios(...)         -- the EDGAR-anchored mode the
      methodology note asks for (pipeline/edgar_financials.py produces the
      ratios): revenue side = {sga, da, ebit}, all auditable revenue shares from
      10-K XBRL. In this mode fixed_pct must be the EX-D&A labor/maintenance gap
      only, because D&A is already counted as a revenue share -- the constructor
      docstring spells out the no-double-count algebra.

The premium discipline (do not break it): everything here is set a priori and
FROZEN. If the fair price starts matching observed market prices closely, that
is markup absorbing the premium -- a failure, not a success.

CLI:  python engine/markup.py   # fair price from the live store if available,
                                 # else a worked example with illustrative costs.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

# Sibling-module import (this repo runs modules as scripts, no package).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from feedstock import ROOT, load_route  # noqa: E402

# A revenue-share wedge at/above this is treated as a data/config error, not a
# market reality (1 - wedge -> 0 sends the price to infinity). Commodity-chemical
# SG&A + D&A + EBIT sums run roughly 0.15-0.35 of revenue; 0.60 is far outside
# anything a sane anchor produces.
MAX_REVENUE_WEDGE = 0.60


class MarkupError(ValueError):
    """A markup parameter is outside its valid domain."""


@dataclass(frozen=True)
class FloorCosts:
    """The physical floor, $/ton of final product (outputs of 3.1 + 3.2).

    catalyst is carried as its own line (3.2 reports it separately because it is
    not a metered energy stream), but it IS part of the physical floor.
    """

    feedstock_usd_per_ton: float
    utility_usd_per_ton: float
    catalyst_usd_per_ton: float = 0.0

    def __post_init__(self) -> None:
        for name in ("feedstock_usd_per_ton", "utility_usd_per_ton", "catalyst_usd_per_ton"):
            if getattr(self, name) < 0:
                raise MarkupError(f"{name} must be >= 0, got {getattr(self, name)}")

    @property
    def floor_usd_per_ton(self) -> float:
        return self.feedstock_usd_per_ton + self.utility_usd_per_ton + self.catalyst_usd_per_ton


@dataclass(frozen=True)
class MarkupParams:
    """Frozen markup parameters: cost-side fractions + revenue-share components.

    revenue_shares is a named dict (e.g. {"target_margin": 0.05} or
    {"sga": 0.11, "da": 0.07, "ebit": 0.08}) so the wedge stays itemized in the
    output -- Cat 4's error decomposition and the Phase-2 calibration both need
    to see WHICH share contributed what, not one anonymous number.
    """

    variable_pct: float                       # cost side, fraction of FEEDSTOCK cost
    fixed_pct: float                          # cost side, fraction of PHYSICAL FLOOR
    revenue_shares: dict[str, float] = field(default_factory=dict)
    source: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.variable_pct <= 1.0:
            raise MarkupError(f"variable_pct must be in [0, 1], got {self.variable_pct}")
        if not 0.0 <= self.fixed_pct <= 1.0:
            raise MarkupError(f"fixed_pct must be in [0, 1], got {self.fixed_pct}")
        for name, share in self.revenue_shares.items():
            if not 0.0 <= share < 1.0:
                raise MarkupError(f"revenue share {name!r} must be in [0, 1), got {share}")
        wedge = self.revenue_wedge
        if wedge >= MAX_REVENUE_WEDGE:
            raise MarkupError(
                f"total revenue wedge {wedge:.3f} >= {MAX_REVENUE_WEDGE} "
                f"({self.revenue_shares}); this is a config/data error -- "
                f"1/(1-wedge) would explode the price"
            )

    @property
    def revenue_wedge(self) -> float:
        return sum(self.revenue_shares.values())


def params_from_config(route_markup: dict) -> MarkupParams:
    """MarkupParams from today's config block (aniline.yaml `markup:`).

    Maps {variable_pct, fixed_pct, target_margin_pct, source} as-is:
    target_margin_pct is a revenue share (an EBITDA-margin-like number), so it
    lands on the revenue side and will be DIVIDED, never multiplied.
    """
    return MarkupParams(
        variable_pct=float(route_markup["variable_pct"]),
        fixed_pct=float(route_markup["fixed_pct"]),
        revenue_shares={"target_margin": float(route_markup["target_margin_pct"])},
        source=str(route_markup.get("source", "")),
    )


def params_from_edgar_ratios(sga_pct: float, da_pct: float, ebit_margin_pct: float,
                             *, variable_pct: float, fixed_pct_ex_da: float,
                             source: str = "SEC EDGAR XBRL companyfacts") -> MarkupParams:
    """MarkupParams anchored to reported financials (the methodology note's ask).

    The no-double-count algebra (all shares are fractions of revenue):
        revenue = COGS + SG&A + EBIT          (other-opex folded into EBIT here)
        COGS    = physical floor + conversion gap (labor, maintenance, plant
                  overhead, COGS-embedded D&A)
    Pulling TOTAL D&A out as its own revenue share means the remaining cost-side
    gap must be EX-D&A -- hence the parameter is named fixed_pct_ex_da and must
    NOT be the same 0.08 used in config mode (which lumps capital recovery in).
    Then:
        fair_price = (floor + variable + fixed_ex_da_gap) / (1 - sga - da - ebit)

    sga_pct / da_pct / ebit_margin_pct come from pipeline/edgar_financials.py
    (company-wide ratios -- aniline-allocation error is real; band them wide in
    the Monte Carlo, per the methodology note).
    """
    return MarkupParams(
        variable_pct=variable_pct,
        fixed_pct=fixed_pct_ex_da,
        revenue_shares={"sga": float(sga_pct), "da": float(da_pct),
                        "ebit": float(ebit_margin_pct)},
        source=source,
    )


def apply_markup(floor: FloorCosts, params: MarkupParams) -> dict:
    """Fair price ($/ton) = (floor + cost-side allowances) / (1 - revenue wedge).

    Returns the itemized build-up plus the predictions-schema breakdown shares
    (feedstock_pct / utility_pct / markup_pct as fractions of the fair price;
    catalyst_pct is included as an extra, schema-permitted key so the floor's
    catalyst line is not silently lumped into utilities).
    """
    variable_allowance = params.variable_pct * floor.feedstock_usd_per_ton
    fixed_overhead = params.fixed_pct * floor.floor_usd_per_ton
    cost_base = floor.floor_usd_per_ton + variable_allowance + fixed_overhead

    wedge = params.revenue_wedge  # validated < MAX_REVENUE_WEDGE at construction
    fair_price = cost_base / (1.0 - wedge)

    # Each revenue share's dollar value is share x PRICE (they are revenue
    # fractions) -- itemized so the wedge decomposes exactly.
    revenue_lines = {name: share * fair_price for name, share in params.revenue_shares.items()}

    markup_usd = fair_price - floor.floor_usd_per_ton  # everything above the floor
    return {
        "floor": {
            "feedstock_usd_per_ton": floor.feedstock_usd_per_ton,
            "utility_usd_per_ton": floor.utility_usd_per_ton,
            "catalyst_usd_per_ton": floor.catalyst_usd_per_ton,
            "floor_usd_per_ton": floor.floor_usd_per_ton,
        },
        "cost_side": {
            "variable_allowance_usd_per_ton": variable_allowance,
            "fixed_overhead_usd_per_ton": fixed_overhead,
            "cost_base_usd_per_ton": cost_base,
        },
        "revenue_side": {
            "wedge_pct_of_price": wedge,
            "by_component_usd_per_ton": revenue_lines,
        },
        "fair_price_usd_per_ton": fair_price,
        "markup_usd_per_ton": markup_usd,
        # predictions.schema.yaml cost_breakdown shares (fractions of fair price).
        "cost_breakdown": {
            "feedstock_pct": floor.feedstock_usd_per_ton / fair_price,
            "utility_pct": floor.utility_usd_per_ton / fair_price,
            "catalyst_pct": floor.catalyst_usd_per_ton / fair_price,  # extra key, allowed
            "markup_pct": markup_usd / fair_price,
        },
        "params": {
            "variable_pct": params.variable_pct,
            "fixed_pct": params.fixed_pct,
            "revenue_shares": dict(params.revenue_shares),
            "source": params.source,
        },
    }


def fair_price_usd_per_ton(floor: FloorCosts, params: MarkupParams) -> float:
    """Convenience scalar for the Monte Carlo wrapper (3.4)."""
    return apply_markup(floor, params)["fair_price_usd_per_ton"]


def _print_result(label: str, result: dict) -> None:
    f, c, r = result["floor"], result["cost_side"], result["revenue_side"]
    print(f"\n{label}")
    print(f"  floor: feedstock ${f['feedstock_usd_per_ton']:.2f} + "
          f"utilities ${f['utility_usd_per_ton']:.2f} + "
          f"catalyst ${f['catalyst_usd_per_ton']:.2f} = ${f['floor_usd_per_ton']:.2f}/t")
    print(f"  + variable allowance ${c['variable_allowance_usd_per_ton']:.2f}"
          f"  + fixed overhead ${c['fixed_overhead_usd_per_ton']:.2f}"
          f"  -> cost base ${c['cost_base_usd_per_ton']:.2f}/t")
    shares = ", ".join(f"{k}={v:.1%}" for k, v in result["params"]["revenue_shares"].items())
    print(f"  / (1 - {r['wedge_pct_of_price']:.1%} [{shares}])")
    print(f"  = FAIR PRICE ${result['fair_price_usd_per_ton']:.2f}/t "
          f"(${result['fair_price_usd_per_ton'] / 1000:.3f}/kg)")
    b = result["cost_breakdown"]
    print(f"  breakdown of price: feedstock {b['feedstock_pct']:.1%}, "
          f"utility {b['utility_pct']:.1%}, catalyst {b['catalyst_pct']:.1%}, "
          f"markup {b['markup_pct']:.1%}")


def _main() -> int:
    process, route_name = "aniline", "benzene_nitration_hydrogenation"
    route = load_route(process, route_name)
    params = params_from_config(route["markup"])

    # Try the live store via 3.1 + 3.2; fall back to a worked example.
    sys.path.insert(0, str(ROOT / "pipeline"))
    try:
        import storage
        import prices
        from feedstock import feedstock_cost
        from utilities import utility_cost

        conn = storage.connect()
        try:
            period, region = "2025-01", "US"
            fs = feedstock_cost(process, route_name, period, region,
                                lambda c, p, r: prices.resolve_price_usd_per_ton(c, p, r, conn))
            ut = utility_cost(process, route_name, period, region,
                              lambda k, p, r: prices.resolve_utility_price_usd_per_unit(k, p, r, conn))
        finally:
            conn.close()
        floor = FloorCosts(
            feedstock_usd_per_ton=fs["feedstock_cost_usd_per_ton"],
            utility_usd_per_ton=ut["utility_cost_usd_per_ton"],
            catalyst_usd_per_ton=ut.get("catalyst_cost_usd_per_ton", 0.0),
        )
        _print_result(f"Fair price, {period} {region} (config-mode markup):",
                      apply_markup(floor, params))
    except Exception as exc:  # missing DB / missing prices -> worked example
        print(f"(store not ready -- {exc})\nUsing ILLUSTRATIVE floor numbers:")
        floor = FloorCosts(feedstock_usd_per_ton=700.0, utility_usd_per_ton=250.0,
                           catalyst_usd_per_ton=50.0)
        _print_result("Worked example (config-mode markup):", apply_markup(floor, params))

    # If the EDGAR ratio summary exists, also show the anchored mode.
    summary = ROOT / "data" / "edgar_financials_summary.csv"
    if summary.exists():
        sys.path.insert(0, str(ROOT / "pipeline"))
        from edgar_financials import load_peer_median_ratios  # noqa: E402

        ratios = load_peer_median_ratios(summary)
        anchored = params_from_edgar_ratios(
            sga_pct=ratios["sga_pct"], da_pct=ratios["da_pct"],
            ebit_margin_pct=ratios["ebit_margin_pct"],
            variable_pct=params.variable_pct,
            # ex-D&A conversion gap: a deliberately smaller cost-side residual,
            # since capital recovery now lives on the revenue side. Soft.
            fixed_pct_ex_da=0.04,
        )
        _print_result("EDGAR-anchored markup (peer-median ratios):",
                      apply_markup(floor, anchored))
    else:
        print("\n(no data/edgar_financials_summary.csv yet -- run "
              "`python pipeline/edgar_financials.py` to enable the EDGAR-anchored mode)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
