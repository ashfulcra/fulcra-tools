"""Shared test fixtures. The view-layer modules import PyObjC, which is
macOS-only — tests that touch them skip on Linux. Pure-model tests run
everywhere."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config, items):
    if sys.platform != "darwin":
        skip_pyobjc = pytest.mark.skip(reason="PyObjC view layer is macOS-only")
        for item in items:
            if "view_layer" in item.keywords:
                item.add_marker(skip_pyobjc)


@pytest.fixture
def temp_config_home(monkeypatch):
    """Point FULCRA_COLLECT_HOME at a temp dir so tests never touch the
    real `~/.config/fulcra-collect`."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("FULCRA_COLLECT_HOME", tmp)
        yield Path(tmp)
