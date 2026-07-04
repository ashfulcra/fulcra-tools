"""Shared test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Every test gets a throwaway COORD_ENGINE_STATE_DIR so the suite never
    writes nonce state into the real ~/.local/state/coord-engine (a stray
    artifact there can trigger spurious double-acting warnings in real use)."""
    monkeypatch.setenv("COORD_ENGINE_STATE_DIR", str(tmp_path / "state"))
