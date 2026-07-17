"""Shared test fixtures for fulcra-common."""
from __future__ import annotations

from collections.abc import Callable

import fulcra_common.annotations as annotations
import httpx
import pytest


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
def block_unstubbed_annotation_http(monkeypatch):
    """Fail closed if an annotations test forgets to stub urllib.

    The annotations writer deliberately uses stdlib ``urllib`` rather than the
    httpx transports used by the rest of fulcra-common's tests.  Individual
    writer tests may replace this guard with their local router, but an
    unstubbed emit must never reach the real Fulcra API.
    """

    def blocked_urlopen(*_args, **_kwargs):
        raise AssertionError(
            "annotations HTTP is blocked in tests; explicitly stub urlopen"
        )

    monkeypatch.setattr(annotations.urllib.request, "urlopen", blocked_urlopen)
