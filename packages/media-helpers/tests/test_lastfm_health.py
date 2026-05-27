"""Tests for the Last.fm health check used by the wizard's test_connection
step. Companion to the runtime-side regression in test_collect_plugins.py
(`_run_lastfm` username plumbing) — this side proves we surface a clear
error inside the wizard *before* the user reaches the run loop.

These tests deliberately stick to plain `monkeypatch` (no pytest-mock /
respx) so they're runnable in any minimal install of the workspace.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest

from fulcra_media.lastfm_health import lastfm_health_check


@dataclass
class _Ctx:
    """Minimal RunContext stand-in — health_check only reads credentials
    and config off of ctx. State + plugin_id are accepted but unused."""
    credentials: dict
    config: dict
    plugin_id: str = "lastfm"


class _FakeResp:
    def __init__(self, *, status_code: int, json_body: object):
        self.status_code = status_code
        self._json = json_body
    def json(self):
        return self._json


class _FakeClient:
    """httpx.Client stub. The instance is captured by the monkeypatch
    factory so tests can assert which URL+params we hit."""
    def __init__(self, response: _FakeResp):
        self._response = response
        self.calls: list[tuple[str, dict]] = []
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def get(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        return self._response


def _patch_httpx(monkeypatch, response):
    fake = _FakeClient(response)
    monkeypatch.setattr(
        "fulcra_media.lastfm_health.httpx",
        type("httpx", (), {
            "Client": lambda *a, **kw: fake,
            # Re-export error classes used in the except chain so isinstance
            # checks against the real httpx hierarchy still work.
            "TimeoutException": httpx.TimeoutException,
            "HTTPError": httpx.HTTPError,
        })(),
    )
    return fake


def test_missing_both_creds_returns_clear_message():
    result = lastfm_health_check(_Ctx(credentials={}, config={}))
    assert result.ok is False
    assert "API key" in result.summary and "username" in result.summary


def test_missing_username_alone_calls_it_out():
    """Username is a Setting; the wizard could have written api-key but
    left username unset. Tell the user precisely which field is missing
    so they know which step to Back into."""
    result = lastfm_health_check(_Ctx(credentials={"api-key": "k"}, config={}))
    assert result.ok is False
    assert "username" in result.summary
    assert "API key" not in result.summary


def test_missing_api_key_alone_calls_it_out():
    result = lastfm_health_check(
        _Ctx(credentials={}, config={"username": "alice"})
    )
    assert result.ok is False
    assert "API key" in result.summary
    assert "username" not in result.summary


def test_happy_path_returns_summary_and_preview(monkeypatch):
    fake = _patch_httpx(monkeypatch, _FakeResp(
        status_code=200,
        json_body={
            "recenttracks": {
                "track": [
                    {"artist": {"#text": "Radiohead"}, "name": "Lucky",
                     "date": {"uts": "1700000000"}},
                    {"artist": {"#text": "Aphex Twin"}, "name": "Xtal",
                     "date": {"uts": "1699999000"}},
                ],
            },
        },
    ))
    result = lastfm_health_check(_Ctx(
        credentials={"api-key": "good-key"},
        config={"username": "alice"},
    ))
    assert result.ok is True
    assert "alice" in result.summary
    assert "2 recent" in result.summary
    assert result.preview[0]["title"] == "Radiohead — Lucky"
    # watched_at must be ISO 8601 so the wizard's test_connection
    # Lit component (`new Date(entry.watched_at).toLocaleString(...)`)
    # can format it. Storing the raw UTS string ("1700000000") makes
    # the JS Date constructor return "Invalid Date" — the QA finding
    # this fix addresses. 1700000000 → 2023-11-14T22:13:20+00:00.
    assert result.preview[0]["watched_at"] == "2023-11-14T22:13:20+00:00"
    assert result.preview[1]["watched_at"] == "2023-11-14T21:56:40+00:00"
    # We send the credentials we got — no leakage of unrelated fields.
    url, params = fake.calls[0]
    assert url == "https://ws.audioscrobbler.com/2.0/"
    assert params["method"] == "user.getRecentTracks"
    assert params["user"] == "alice"
    assert params["api_key"] == "good-key"
    assert params["format"] == "json"


def test_lastfm_returns_inline_unknown_user_error(monkeypatch):
    """Last.fm uses HTTP 200 with `{error: 6, message: ...}` for unknown
    usernames (and most validation errors). We translate that into a
    user-pointing message rather than letting "Unexpected" leak out."""
    _patch_httpx(monkeypatch, _FakeResp(
        status_code=200,
        json_body={"error": 6, "message": "User not found"},
    ))
    result = lastfm_health_check(_Ctx(
        credentials={"api-key": "k"},
        config={"username": "nobody"},
    ))
    assert result.ok is False
    assert "nobody" in result.summary


@pytest.mark.parametrize("code", [10, 26])
def test_lastfm_returns_inline_bad_api_key_error(monkeypatch, code):
    """Codes 10 (Invalid API key) and 26 (Suspended API key) both mean
    'go re-check the key' — same actionable message."""
    _patch_httpx(monkeypatch, _FakeResp(
        status_code=200,
        json_body={"error": code, "message": "Whatever Last.fm says"},
    ))
    result = lastfm_health_check(_Ctx(
        credentials={"api-key": "rejected"},
        config={"username": "alice"},
    ))
    assert result.ok is False
    assert "API key" in result.summary


def test_http_403_translates_to_bad_key(monkeypatch):
    """Some Last.fm endpoints return a bare 403 rather than the inline
    body. Treat it the same as an api-key rejection."""
    _patch_httpx(monkeypatch, _FakeResp(status_code=403, json_body={}))
    result = lastfm_health_check(_Ctx(
        credentials={"api-key": "bad"},
        config={"username": "alice"},
    ))
    assert result.ok is False
    assert "API key" in result.summary


def test_http_5xx_treated_as_temporary(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResp(status_code=503, json_body={}))
    result = lastfm_health_check(_Ctx(
        credentials={"api-key": "k"},
        config={"username": "alice"},
    ))
    assert result.ok is False
    assert "temporary" in result.summary
