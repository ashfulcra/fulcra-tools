"""Shared pytest fixtures."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest


@pytest.fixture
def recording_transport() -> Callable[..., httpx.MockTransport]:
    """Factory for a MockTransport that records every outgoing request.

    Usage:
        def test_x(recording_transport):
            transport = recording_transport(lambda r: httpx.Response(200, json={"id": "x"}))
            # transport.requests is the list of httpx.Request objects observed
    """
    def _factory(
        responder: Callable[[httpx.Request], httpx.Response]
    ) -> httpx.MockTransport:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return responder(request)

        t = httpx.MockTransport(handler)
        t.requests = requests  # type: ignore[attr-defined]
        return t

    return _factory
