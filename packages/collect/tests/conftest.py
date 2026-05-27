"""Shared test fixtures for fulcra-collect."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def collect_home(tmp_path: Path, monkeypatch) -> Path:
    """Point the hub's config directory at a temp dir for the test.

    Closes any cached SQLite connections (Phase 1 of refactor #1) on
    teardown so a subsequent test's ``db.open()`` opens a fresh
    connection against its own tmp_path rather than reusing one that
    points at a now-deleted directory."""
    home = tmp_path / "collect-home"
    home.mkdir()
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(home))
    # Defensive: drop any connection cached from a previous test in the
    # same worker before the test starts touching state. (Pytest fixtures
    # are per-test by default, but the thread-local cache outlives them.)
    from fulcra_collect import db as _db
    _db.close_all()
    yield home
    _db.close_all()
