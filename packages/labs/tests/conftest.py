"""Shared fixtures for fulcra-labs tests. Hermetic — never hits the network."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


class RecordingTransport(httpx.MockTransport):
    """MockTransport that records every request it sees."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []

        def wrapper(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(wrapper)


@pytest.fixture
def recording_transport():
    def make(handler: Callable[[httpx.Request], httpx.Response]) -> RecordingTransport:
        return RecordingTransport(handler)
    return make


@pytest.fixture(autouse=True)
def labs_home(monkeypatch, tmp_path):
    """Point the config home at a throwaway dir so tests never touch
    ~/.config/fulcra-labs."""
    home = tmp_path / "fulcra-labs"
    monkeypatch.setenv("FULCRA_LABS_HOME", str(home))
    return home


@pytest.fixture
def load_fixture():
    def _load(name: str) -> dict:
        return json.loads((FIXTURES / name).read_text())
    return _load
