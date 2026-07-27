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


def _bundle_manifest():
    """Import the release build's manifest module by path (scripts/ isn't a
    package, and the build script consumes it as a CLI)."""
    import importlib.util

    path = _WORKSPACE / "packages/menubar/scripts/bundle_manifest.py"
    spec = importlib.util.spec_from_file_location("bundle_manifest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bundle_manifest_covers_every_plugin_package():
    """Being in Briefcase ``requires`` is not enough: a monorepo package is not
    on PyPI, so the release build must ALSO build it a local wheel and prove it
    landed in the bundle. Both of those derive from bundle_manifest, so assert
    the manifest actually resolves every plugin-owning package.

    PR #455 review: fulcra-purpleair was in ``requires`` while the wheel-build
    loop and presence guard kept their own hand-written lists, so the release
    build still could not install it.
    """
    manifest = _bundle_manifest()
    dists = {d for d, _ in manifest.bundle_workspace_packages()}
    missing = _plugin_dist_names() - dists
    assert not missing, f"plugin packages the release build would not ship: {missing}"


def test_bundle_manifest_import_names_are_real_packages():
    """The presence guard checks directories by IMPORT name, which is not a
    mechanical transform of the dist name (fulcra-media-helpers ships
    fulcra_media; fulcra-csv-importer ships fulcra_csv). A wrong name would
    make the guard fail an otherwise-good build — or pass a bad one."""
    manifest = _bundle_manifest()
    for dist, import_name in manifest.bundle_workspace_packages():
        pkg_dirs = list(_WORKSPACE.glob(f"packages/*/{import_name}/__init__.py"))
        assert pkg_dirs, f"{dist}: import name {import_name!r} is not a real package"


def test_build_script_derives_both_lists_from_the_manifest():
    """Regression guard for the drift class itself: the release script must not
    reintroduce a hand-written package list in either the wheel build or the
    presence guard."""
    script = (_WORKSPACE / "packages/menubar/scripts/build_macos_app.sh").read_text()
    assert script.count("bundle_manifest.py") >= 1, "script no longer uses the manifest"
    assert "--dists" in script, "wheel build no longer derives its package list"
    assert "--imports" in script, "presence guard no longer derives its package list"
    # The old hand-written forms, spelled so this fails if either comes back.
    assert "for pkg in fulcra-common" not in script
    assert "for need in fulcra_collect" not in script


def test_discover_uses_entry_points_when_present():
    result = registry.discover()
    assert "generic-rss" in result.plugins


def test_discover_falls_back_to_manifest_when_entry_points_empty(monkeypatch):
    monkeypatch.setattr(registry, "entry_points", lambda group: [])
    result = registry.discover()
    manifest_ids = {pid for pid, _ in BUNDLED_PLUGINS}
    assert manifest_ids.issubset(set(result.plugins))
    assert not result.errors
