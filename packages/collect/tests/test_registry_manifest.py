from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from fulcra_collect import registry
from fulcra_collect._bundled_plugins import BUNDLED_PLUGINS

_WORKSPACE = Path(__file__).resolve().parents[3]


def _entry_point_ids() -> set[str]:
    """Every plugin id declared across the workspace pyprojects'
    [project.entry-points."fulcra_collect.plugins"] tables."""
    ids: set[str] = set()
    pyprojects = list(_WORKSPACE.glob("packages/*/pyproject.toml"))
    for pyproject in pyprojects:
        if not pyproject.is_file():
            continue
        data = tomllib.loads(pyproject.read_text())
        group = (
            data.get("project", {})
            .get("entry-points", {})
            .get("fulcra_collect.plugins", {})
        )
        ids.update(group.keys())
    return ids


def test_manifest_matches_entry_points():
    manifest_ids = {pid for pid, _ in BUNDLED_PLUGINS}
    assert manifest_ids == _entry_point_ids()


def test_discover_uses_entry_points_when_present():
    result = registry.discover()
    assert "generic-rss" in result.plugins


def test_discover_falls_back_to_manifest_when_entry_points_empty(monkeypatch):
    monkeypatch.setattr(registry, "entry_points", lambda group: [])
    result = registry.discover()
    manifest_ids = {pid for pid, _ in BUNDLED_PLUGINS}
    assert manifest_ids.issubset(set(result.plugins))
    assert not result.errors
