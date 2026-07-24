from __future__ import annotations

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


def _plugin_dist_names() -> set[str]:
    """Distribution names of every workspace package that declares at least
    one ``fulcra_collect.plugins`` entry point."""
    names: set[str] = set()
    for pyproject in _WORKSPACE.glob("packages/*/pyproject.toml"):
        data = tomllib.loads(pyproject.read_text())
        project = data.get("project", {})
        group = project.get("entry-points", {}).get("fulcra_collect.plugins", {})
        if group and project.get("name"):
            names.add(project["name"])
    return names


def test_manifest_matches_entry_points():
    manifest_ids = {pid for pid, _ in BUNDLED_PLUGINS}
    assert manifest_ids == _entry_point_ids()


def test_every_plugin_package_is_in_the_macos_bundle():
    """A plugin that isn't in the frozen macOS bundle's ``requires`` ships
    absent from the app — entry-point discovery finds nothing for it inside
    the Briefcase build. Guard the two lists against drift."""
    menubar = tomllib.loads((_WORKSPACE / "packages/menubar/pyproject.toml").read_text())
    reqs = menubar["tool"]["briefcase"]["app"]["fulcra-menubar"]["requires"]
    bundled = {
        req.split(">")[0].split("<")[0].split("=")[0].split("~")[0].strip()
        for req in reqs
    }
    missing = _plugin_dist_names() - bundled
    assert not missing, f"plugin packages absent from the macOS bundle requires: {missing}"


def test_discover_uses_entry_points_when_present():
    result = registry.discover()
    assert "generic-rss" in result.plugins


def test_discover_falls_back_to_manifest_when_entry_points_empty(monkeypatch):
    monkeypatch.setattr(registry, "entry_points", lambda group: [])
    result = registry.discover()
    manifest_ids = {pid for pid, _ in BUNDLED_PLUGINS}
    assert manifest_ids.issubset(set(result.plugins))
    assert not result.errors
