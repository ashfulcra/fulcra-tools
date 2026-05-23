"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest


class RecordingTransport(httpx.MockTransport):
    """MockTransport that records every request it sees."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []

        def wrapper(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            self.bodies.append(request.content)
            return handler(request)

        super().__init__(wrapper)


@pytest.fixture
def recording_transport():
    def make(handler: Callable[[httpx.Request], httpx.Response]) -> RecordingTransport:
        return RecordingTransport(handler)
    return make
