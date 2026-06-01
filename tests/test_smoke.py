"""Smoke test so CI has something to pass on day one."""


def test_repo_is_alive():
    assert True


def test_registry_valid_if_present():
    """Once the PubChem pull has run, the registry should be present and valid."""
    from pathlib import Path

    registry = Path(__file__).resolve().parents[1] / "config" / "chemical_registry.yaml"
    if not registry.exists():
        return  # not yet pulled; skip silently
    import yaml

    data = yaml.safe_load(registry.read_text())
    assert "aniline" in data["chemicals"]
    assert data["chemicals"]["aniline"]["molecular_weight_g_per_mol"] > 0
