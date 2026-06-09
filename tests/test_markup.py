"""Tests for engine/markup.py (Cat 3.3).

The single most important assertion in this file: revenue-share margins are
DIVIDED (price = cost / (1 - margin)), never multiplied. The methodology note
calls cost-plus-as-if-revenue-margin "a common, silent error" -- this test makes
it loud.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

from markup import (  # noqa: E402
    FloorCosts,
    MarkupError,
    MarkupParams,
    apply_markup,
    fair_price_usd_per_ton,
    params_from_config,
    params_from_edgar_ratios,
)

FLOOR = FloorCosts(feedstock_usd_per_ton=700.0, utility_usd_per_ton=250.0,
                   catalyst_usd_per_ton=50.0)  # floor = 1000


def test_config_mode_hand_computed():
    """Exact hand calculation at the current aniline.yaml placeholder values."""
    params = MarkupParams(variable_pct=0.02, fixed_pct=0.08,
                          revenue_shares={"target_margin": 0.05})
    result = apply_markup(FLOOR, params)
    # cost base = 1000 + 0.02*700 + 0.08*1000 = 1094; price = 1094 / 0.95
    assert result["cost_side"]["cost_base_usd_per_ton"] == pytest.approx(1094.0)
    assert result["fair_price_usd_per_ton"] == pytest.approx(1094.0 / 0.95)


def test_margin_is_divided_not_multiplied():
    """price = cost/(1-m) strictly exceeds cost*(1+m) for any m in (0,1)."""
    params = MarkupParams(variable_pct=0.0, fixed_pct=0.0,
                          revenue_shares={"target_margin": 0.15})
    price = fair_price_usd_per_ton(FLOOR, params)
    assert price == pytest.approx(1000.0 / 0.85)
    assert price > 1000.0 * 1.15  # the silent cost-plus error would land here


def test_variable_pct_applies_to_feedstock_only():
    params = MarkupParams(variable_pct=0.10, fixed_pct=0.0, revenue_shares={})
    result = apply_markup(FLOOR, params)
    # 10% of feedstock (700), NOT of the whole floor (1000)
    assert result["cost_side"]["variable_allowance_usd_per_ton"] == pytest.approx(70.0)


def test_fixed_pct_applies_to_full_floor_including_catalyst():
    params = MarkupParams(variable_pct=0.0, fixed_pct=0.10, revenue_shares={})
    result = apply_markup(FLOOR, params)
    assert result["cost_side"]["fixed_overhead_usd_per_ton"] == pytest.approx(100.0)


def test_breakdown_shares_sum_to_one():
    params = MarkupParams(variable_pct=0.02, fixed_pct=0.08,
                          revenue_shares={"target_margin": 0.05})
    b = apply_markup(FLOOR, params)["cost_breakdown"]
    assert (b["feedstock_pct"] + b["utility_pct"] + b["catalyst_pct"]
            + b["markup_pct"]) == pytest.approx(1.0)
    assert all(0.0 <= b[k] <= 1.0 for k in b)


def test_revenue_components_decompose_exactly():
    params = params_from_edgar_ratios(sga_pct=0.11, da_pct=0.07, ebit_margin_pct=0.08,
                                      variable_pct=0.02, fixed_pct_ex_da=0.04)
    result = apply_markup(FLOOR, params)
    price = result["fair_price_usd_per_ton"]
    lines = result["revenue_side"]["by_component_usd_per_ton"]
    # cost base + sum of revenue-side dollars reconstructs the price exactly
    assert result["cost_side"]["cost_base_usd_per_ton"] + sum(lines.values()) \
        == pytest.approx(price)
    assert lines["sga"] == pytest.approx(0.11 * price)


def test_params_from_config_maps_target_margin_to_revenue_side():
    block = {"variable_pct": 0.02, "fixed_pct": 0.08, "target_margin_pct": 0.05,
             "source": "test"}
    params = params_from_config(block)
    assert params.revenue_shares == {"target_margin": 0.05}
    assert params.source == "test"


def test_real_config_block_loads():
    """The actual aniline.yaml markup block must construct without error."""
    from feedstock import load_route
    route = load_route("aniline", "benzene_nitration_hydrogenation")
    params = params_from_config(route["markup"])
    price = fair_price_usd_per_ton(FLOOR, params)
    assert price > FLOOR.floor_usd_per_ton  # fair price always above the floor


def test_wedge_guard_blocks_explosive_margins():
    with pytest.raises(MarkupError):
        MarkupParams(variable_pct=0.0, fixed_pct=0.0,
                     revenue_shares={"sga": 0.3, "da": 0.2, "ebit": 0.2})  # 0.70 >= 0.60


def test_negative_inputs_rejected():
    with pytest.raises(MarkupError):
        FloorCosts(feedstock_usd_per_ton=-1.0, utility_usd_per_ton=0.0)
    with pytest.raises(MarkupError):
        MarkupParams(variable_pct=-0.01, fixed_pct=0.0, revenue_shares={})
    with pytest.raises(MarkupError):
        MarkupParams(variable_pct=0.0, fixed_pct=0.0, revenue_shares={"m": -0.05})


def test_zero_markup_returns_floor():
    params = MarkupParams(variable_pct=0.0, fixed_pct=0.0, revenue_shares={})
    assert fair_price_usd_per_ton(FLOOR, params) == pytest.approx(1000.0)
