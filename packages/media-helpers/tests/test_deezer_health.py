"""Tests for the Deezer health check used by the wizard's
test_connection step.

Mirrors test_apple_podcasts_health.py — proves the three branches the
wizard cares about: no-creds-no-network, rejected-creds, and the happy
path with a preview list. httpx.MockTransport stands in for the real
Deezer API so the test stays hermetic.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from fulcra_media import deezer_health


FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class _Ctx:
    """Minimal RunContext stand-in — the health check reads only
    ctx.credentials. config/plugin_id are accepted for shape parity."""
    credentials: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    plugin_id: str = "deezer"


def _install_transport(monkeypatch, handler):
    """Install a MockTransport on every httpx.Client constructed by the
    health module — same trick used elsewhere in the test suite for
    hermetic API tests."""
    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(deezer_health.httpx, "Client", _factory)


def test_no_credentials_returns_friendly_error():
    """No access token → ok=False, no network call, message names the
    field the user must fill in."""
    ctx = _Ctx(credentials={})  # nothing configured

    result = deezer_health.deezer_health_check(ctx)

    assert result.ok is False
    assert "access token" in result.summary.lower()


def test_happy_path_returns_preview(monkeypatch):
    """Valid token + populated history → ok=True with a preview list
    populated from the response."""
    body = json.loads((FIXTURES / "deezer_history_page1.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        # Confirm the access token was forwarded as a query param.
        assert "access_token=t0k" in str(request.url)
        return httpx.Response(200, json=body)

    _install_transport(monkeypatch, handler)
    ctx = _Ctx(credentials={"access-token": "t0k"})

    result = deezer_health.deezer_health_check(ctx)

    assert result.ok is True
    assert "Signed in to Deezer" in result.summary
    assert len(result.preview) >= 1
    # title carries "Artist — Track"
    assert " — " in result.preview[0]["title"]
    # watched_at is an ISO timestamp derived from the Unix epoch in the body
    assert result.preview[0]["watched_at"].startswith("20")


def test_rejected_token_inline_error(monkeypatch):
    """Deezer's 200-OK-with-inline-error path → ok=False with a clear
    rotate-token message."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {
            "type": "OAuthException", "code": 300, "message": "Invalid token"
        }})

    _install_transport(monkeypatch, handler)
    ctx = _Ctx(credentials={"access-token": "bad"})

    result = deezer_health.deezer_health_check(ctx)

    assert result.ok is False
    assert "token" in result.summary.lower()


def test_rejected_token_http_401(monkeypatch):
    """HTTP 401 (gateway-level rejection) → ok=False with same actionable
    rotate-token wording."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    _install_transport(monkeypatch, handler)
    ctx = _Ctx(credentials={"access-token": "rejected"})

    result = deezer_health.deezer_health_check(ctx)

    assert result.ok is False
    assert "token" in result.summary.lower()
