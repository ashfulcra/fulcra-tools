from __future__ import annotations

import sys
from pathlib import Path

from fulcra_collect import _resources


def test_is_frozen_false_in_dev(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert _resources.is_frozen() is False


def test_is_frozen_true_when_sys_frozen_set(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert _resources.is_frozen() is True


def test_dev_paths_point_at_workspace(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    fe = _resources.frontend_dir()
    assert fe.parts[-3:] == ("packages", "web-ui", "dist")
    assert _resources.docs_dir().name == "docs"


def test_frozen_paths_live_under_resources(monkeypatch, tmp_path):
    macos = tmp_path / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (tmp_path / "Contents" / "Resources" / "web-ui" / "dist").mkdir(parents=True)
    (tmp_path / "Contents" / "Resources" / "docs").mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(macos / "Fulcra Collect"))
    assert _resources.frontend_dir().parts[-2:] == ("web-ui", "dist")
    assert _resources.frontend_dir().is_dir()
    assert _resources.docs_dir().name == "docs"
    assert _resources.docs_dir().is_dir()
