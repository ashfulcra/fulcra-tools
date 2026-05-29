from __future__ import annotations

import sys
from pathlib import Path

from fulcra_collect import _resources


def test_is_frozen_false_in_dev(monkeypatch):
    # No sys.frozen and no _bundled/ dir → dev checkout.
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(_resources, "_BUNDLED", Path("/no/such/_bundled"))
    assert _resources.is_frozen() is False


def test_is_frozen_true_when_sys_frozen_set(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(_resources, "_BUNDLED", Path("/no/such/_bundled"))
    assert _resources.is_frozen() is True


def test_is_frozen_true_when_bundled_dir_present(monkeypatch, tmp_path):
    # Briefcase doesn't set sys.frozen, but the force-included _bundled/
    # dir IS present in the installed package — that alone means frozen.
    monkeypatch.delattr(sys, "frozen", raising=False)
    bundled = tmp_path / "_bundled"
    bundled.mkdir()
    monkeypatch.setattr(_resources, "_BUNDLED", bundled)
    assert _resources.is_frozen() is True


def test_dev_paths_point_at_workspace(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(_resources, "_BUNDLED", Path("/no/such/_bundled"))
    fe = _resources.frontend_dir()
    assert fe.parts[-3:] == ("packages", "web-ui", "dist")
    docs = _resources.docs_dir()
    assert docs.name == "docs"
    # workspace-root/docs — sibling of packages/, not inside the package
    assert docs.parent == _resources._WORKSPACE_ROOT


def test_frozen_paths_point_at_bundled_package_data(monkeypatch, tmp_path):
    bundled = tmp_path / "_bundled"
    (bundled / "web-ui" / "dist").mkdir(parents=True)
    (bundled / "docs").mkdir(parents=True)
    monkeypatch.setattr(_resources, "_BUNDLED", bundled)  # presence ⇒ frozen
    assert _resources.frontend_dir() == bundled / "web-ui" / "dist"
    assert _resources.docs_dir() == bundled / "docs"
