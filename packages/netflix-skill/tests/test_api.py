"""Installed canonical loader/console/package seams; transport in test_native."""
import sys
import tomllib
from pathlib import Path


def test_console_delegates_actual_canonical_main(ni, fixtures_dir, monkeypatch, capsys):
    import json
    from fulcra_netflix import loader

    monkeypatch.setattr(sys, "argv", ["fulcra-netflix", str(fixtures_dir / "slim.csv"),
                                    "--check-only", "--json"])
    assert loader.main() == 0
    assert json.loads(capsys.readouterr().out)["would_post"] == 5
    assert loader.SCRIPT_PATH.read_bytes().startswith(b"#!/usr/bin/env python3\n")
    assert b"# /// script" not in loader.SCRIPT_PATH.read_bytes()


def test_loader_source_and_bundled_select_exact_bytes(ni, monkeypatch, tmp_path):
    from fulcra_netflix import loader

    source = loader.SCRIPT_PATH.read_bytes()
    package = tmp_path / "fulcra_netflix"
    package.mkdir()
    copied_loader = package / "loader.py"
    copied_loader.write_bytes(Path(loader.__file__).read_bytes())
    bundled = package / "_bundled" / loader._SCRIPT_REL
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(source)
    import importlib.util
    spec = importlib.util.spec_from_file_location("netflix_bundled_loader", copied_loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.SCRIPT_PATH == bundled
    assert mod.load()._read_events is not None
    assert bundled.read_bytes() == source


def test_distribution_dependency_and_console_contract():
    package = Path(__file__).parents[1]
    meta = tomllib.loads((package / "pyproject.toml").read_text())
    assert set(meta["project"]["dependencies"]) == {
        "fulcra-common", "fulcra-collect", "fulcra-media-helpers"}
    assert meta["project"]["scripts"]["fulcra-netflix"] == "fulcra_netflix.loader:main"
    assert all(meta["tool"]["uv"]["sources"][name] == {"workspace": True}
               for name in meta["project"]["dependencies"])
    include = meta["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert include["skills/fulcra-netflix"] == "fulcra_netflix/_bundled/skills/fulcra-netflix"
