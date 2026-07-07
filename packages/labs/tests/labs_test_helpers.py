"""Non-fixture helpers shared across fulcra-labs test modules."""
from __future__ import annotations

import json
from collections.abc import Callable
from unittest.mock import MagicMock

import httpx

from fulcra_labs.store import LabsClient


def json_response(status: int, body: dict | list) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )


class RecordingTransport(httpx.MockTransport):
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []

        def wrapper(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(wrapper)


def make_client(handler, *, catalog=None) -> tuple[LabsClient, RecordingTransport]:
    """Build a LabsClient wired to a MockTransport handler, with its fulcra_api
    lib stubbed so catalog reads (definition_exists / adoption) are hermetic.

    ``catalog`` is the list ``annotations_catalog`` returns (default [])."""
    transport = RecordingTransport(handler)
    client = LabsClient(transport=transport)
    fake_lib = MagicMock()
    fake_lib.annotations_catalog.return_value = catalog or []
    client._lib = lambda: fake_lib  # type: ignore[method-assign]
    return client, transport
