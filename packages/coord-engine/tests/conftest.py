"""Shared test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Every test gets a throwaway COORD_ENGINE_STATE_DIR so the suite never
    writes nonce state into the real ~/.local/state/coord-engine (a stray
    artifact there can trigger spurious double-acting warnings in real use)."""
    monkeypatch.setenv("COORD_ENGINE_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def _no_host_wake_provisioning(monkeypatch):
    """The suite must never inherit a developer's host wake provisioning: with
    COORD_WAKE_ADAPTER_DIR set, the default host-adapter invoker would run that
    host's real adapter script (and post a real notification) from a test. Tests
    that exercise the seam set it explicitly to a throwaway stub dir."""
    monkeypatch.delenv("COORD_WAKE_ADAPTER_DIR", raising=False)
    monkeypatch.delenv("COORD_WAKE_ADAPTER_TIMEOUT", raising=False)
