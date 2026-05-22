"""Shared test fixtures for fulcra-collect."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def collect_home(tmp_path: Path, monkeypatch) -> Path:
    """Point the hub's config directory at a temp dir for the test."""
    home = tmp_path / "collect-home"
    home.mkdir()
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(home))
    return home
