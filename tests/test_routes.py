"""Tests for engine/routes.py (route discovery & selection)."""

from __future__ import annotations

import copy
import tempfile
from pathlib import Path

import pytest
import yaml

from engine import routes, feedstock

PROC, ROUTE = "aniline", "benzene_nitration_hydrogenation"


def _write_cfg(cfg: dict) -> Path:
    p = Path(tempfile.mktemp(suffix=".yaml"))
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _base_cfg() -> dict:
    return yaml.safe_load(open("config/aniline.yaml", encoding="utf-8"))


# --- against the real (single-route) config --------------------------------
def test_list_routes_single():
    assert routes.list_routes(PROC) == [ROUTE]


def test_default_route_single():
    assert routes.default_route(PROC) == ROUTE


def test_route_info_distinguishes_by_feedstock():
    info = routes.route_info(PROC, ROUTE)
    assert info["final_product"] == "aniline"
    assert info["steps"] == ["nitration", "hydrogenation"]
    assert info["purchased_feedstocks"] == ["benzene", "hydrogen", "nitric_acid"]
    assert info["intermediates"] == ["nitrobenzene"]
    assert info["is_default"] is True


# --- multi-route behavior via a temp config --------------------------------
def _two_route_cfg(default_flags=(True, False)) -> Path:
    cfg = _base_cfg()
    routes_map = cfg["processes"]["aniline"]["routes"]
    base = routes_map[ROUTE]
    base["is_default"] = default_flags[0]
    alt = copy.deepcopy(base)
    alt["is_default"] = default_flags[1]
    routes_map["alt_route"] = alt
    return _write_cfg(cfg)


def test_list_routes_multi():
    cfg = _two_route_cfg()
    assert set(routes.list_routes(PROC, config_path=cfg)) == {ROUTE, "alt_route"}


def test_default_route_picks_flagged_one():
    cfg = _two_route_cfg(default_flags=(True, False))
    assert routes.default_route(PROC, config_path=cfg) == ROUTE


def test_default_route_raises_on_two_defaults():
    cfg = _two_route_cfg(default_flags=(True, True))
    with pytest.raises(ValueError, match="is_default"):
        routes.default_route(PROC, config_path=cfg)


def test_default_route_raises_on_zero_defaults_multi():
    cfg = _two_route_cfg(default_flags=(False, False))
    with pytest.raises(ValueError, match="is_default"):
        routes.default_route(PROC, config_path=cfg)


def test_describe_routes_covers_all():
    cfg = _two_route_cfg()
    desc = routes.describe_routes(PROC, config_path=cfg)
    assert set(desc) == {ROUTE, "alt_route"}
    assert all("purchased_feedstocks" in v for v in desc.values())


def test_unknown_process_raises():
    with pytest.raises(KeyError):
        routes.list_routes("not_a_process")
