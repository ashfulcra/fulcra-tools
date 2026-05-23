"""Non-fixture test helpers shared across media-helpers test modules."""
from __future__ import annotations

import json

import httpx


def json_response(status: int, body: dict | list) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
