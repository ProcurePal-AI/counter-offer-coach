"""Tests for engine/predict.py (Cat 3.4 MC + Cat 3.5 interface)."""

from __future__ import annotations

import copy

import pytest

from engine import feedstock, predict

PROC = "aniline"
ROUTE = "benzene_nitration_hydrogenation"
MONTH = "2025-01"
REGION = "US"


def _feedstock_prices(chemical_id: str, period: str, region: str) -> float:
    return {"benzene": 800.0, "nitric_acid": 350.0, "hydrogen": 1500.0}[chemical_id]


def _utility_prices(kind: str, period: str, region: str) -> float:
    return {"electricity": 0.075, "steam": 3.0}[kind]


def _route() -> dict:
    return copy.deepcopy(feedstock.load_route(PROC, ROUTE))


def _patch_route(monkeypatch: pytest.MonkeyPatch, route: dict) -> None:
    monkeypatch.setattr(predict, "load_route", lambda process, route_name: route)


def _run(monkeypatch: pytest.MonkeyPatch, route: dict | None = None, *, n_draws=200, seed=42):
    route = route or _route()
    _patch_route(monkeypatch, route)
    mc = predict.monte_carlo_fair_price(
        MONTH,
        REGION,
        process=PROC,
        route_name=ROUTE,
        n_draws=n_draws,
        seed=seed,
        feedstock_price_fn=_feedstock_prices,
        utility_price_fn=_utility_prices,
    )
    return predict._summarize_mc(MONTH, REGION, mc), mc


def _zero_uncertainty(route: dict) -> dict:
    route = copy.deepcopy(route)
    for step in route["steps"]:
        step["yield_pct"]["uncertainty"] = 0.0
    return route


def _set_yield(route: dict, step_id: str, value: float) -> dict:
    route = _zero_uncertainty(route)
    for step in route["steps"]:
        if step["step_id"] == step_id:
            step["yield_pct"]["value"] = value
    return route


def _set_energy(route: dict, step_id: str, key: str, value: float) -> dict:
    route = _zero_uncertainty(route)
    for step in route["steps"]:
        if step["step_id"] == step_id:
            step["energy"][key] = value
    return route


def _deterministic(monkeypatch: pytest.MonkeyPatch, route: dict) -> dict:
    _patch_route(monkeypatch, route)
    return predict.deterministic_fair_price(
        MONTH,
        REGION,
        process=PROC,
        route_name=ROUTE,
        feedstock_price_fn=_feedstock_prices,
        utility_price_fn=_utility_prices,
    )


def test_output_ordering(monkeypatch):
    result, _ = _run(monkeypatch)

    assert result["p10"] <= result["p50"] <= result["p90"]
    for layer in ("feedstock", "utility", "markup"):
        band = result["breakdown"][layer]
        assert band["p10"] <= band["p50"] <= band["p90"]


def test_reproducibility(monkeypatch):
    result_1, _ = _run(monkeypatch, seed=42)
    result_2, _ = _run(monkeypatch, seed=42)

    assert result_1 == result_2


def test_zero_uncertainty_matches_deterministic_model(monkeypatch):
    route = _zero_uncertainty(_route())
    result, _ = _run(monkeypatch, route, n_draws=50)
    deterministic = _deterministic(monkeypatch, route)

    assert result["p10"] == pytest.approx(result["p50"])
    assert result["p50"] == pytest.approx(result["p90"])
    assert result["p50"] == pytest.approx(deterministic["total"])
    for layer in ("feedstock", "utility", "markup"):
        assert result["breakdown"][layer]["p10"] == pytest.approx(result["breakdown"][layer]["p50"])
        assert result["breakdown"][layer]["p50"] == pytest.approx(result["breakdown"][layer]["p90"])
        assert result["breakdown"][layer]["p50"] == pytest.approx(deterministic[layer])


def test_lower_nitration_yield_increases_fair_price(monkeypatch):
    base = _deterministic(monkeypatch, _set_yield(_route(), "nitration", 98.0))
    lower = _deterministic(monkeypatch, _set_yield(_route(), "nitration", 95.0))

    assert lower["total"] > base["total"]


def test_lower_hydrogenation_yield_increases_fair_price(monkeypatch):
    base = _deterministic(monkeypatch, _set_yield(_route(), "hydrogenation", 99.0))
    lower = _deterministic(monkeypatch, _set_yield(_route(), "hydrogenation", 96.0))

    assert lower["total"] > base["total"]


def test_higher_electricity_intensity_increases_utility_cost(monkeypatch):
    base = _deterministic(
        monkeypatch, _set_energy(_route(), "hydrogenation", "electricity_kwh_per_ton_output", 180.0)
    )
    higher = _deterministic(
        monkeypatch, _set_energy(_route(), "hydrogenation", "electricity_kwh_per_ton_output", 240.0)
    )

    assert higher["utility"] > base["utility"]


def test_higher_steam_intensity_increases_utility_cost(monkeypatch):
    base = _deterministic(
        monkeypatch, _set_energy(_route(), "hydrogenation", "steam_gj_per_ton_output", 2.5)
    )
    higher = _deterministic(
        monkeypatch, _set_energy(_route(), "hydrogenation", "steam_gj_per_ton_output", 4.0)
    )

    assert higher["utility"] > base["utility"]


def test_draw_level_breakdown_reconciles(monkeypatch):
    _, mc = _run(monkeypatch)

    for draw in mc["draws"]:
        assert draw["total"] == pytest.approx(draw["feedstock"] + draw["utility"] + draw["markup"])


def test_inspection_reports_current_cat1_sampled_and_deterministic_params():
    info = predict.inspect_cat1_uncertainty(_route())

    sampled = {row["path"] for row in info["sampled"]}
    deterministic = {row["path"] for row in info["deterministic"]}
    assert sampled == {
        "steps.nitration.yield_pct",
        "steps.hydrogenation.yield_pct",
    }
    assert "steps.nitration.energy.electricity_kwh_per_ton_output" in deterministic
    assert "steps.hydrogenation.energy.steam_gj_per_ton_output" in deterministic
    assert "markup.variable_pct" in deterministic
    assert "markup.fixed_pct" in deterministic
    assert "markup.target_margin_pct" in deterministic


def test_rejects_empty_draw_count(monkeypatch):
    with pytest.raises(ValueError):
        _run(monkeypatch, n_draws=0)
