"""Trakt plugin keychain-path OAuth refresh.

The keychain path (web-UI OAuth wizard) builds static Bearer headers from
ctx.credentials — when the access token expires (~90 days) every run 401s
forever unless the plugin refreshes via the stored refresh_token and
persists the rotated pair back through ctx.set_credential. These tests
cover that reactive-refresh path end to end with mocked httpx (no network),
mirroring the monkeypatch style of test_collect_plugins.py.
"""
from __future__ import annotations

import logging

import httpx
import pytest

from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState

from fulcra_media.plugins.trakt import _run_trakt
from fulcra_media.state import State as MediaState


KEYCHAIN_CREDS = {
    "access_token": "OLD-ACCESS",
    "refresh_token": "OLD-REFRESH",
    "client_id": "the-client-id",
    "client_secret": "the-client-secret",
}

HISTORY_ITEMS = [{"id": 1}, {"id": 2}]


def _http_401() -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://api.trakt.tv/sync/history")
    resp = httpx.Response(401, request=req)
    return httpx.HTTPStatusError("401 Unauthorized", request=req, response=resp)


def _http_500() -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://api.trakt.tv/sync/history")
    resp = httpx.Response(500, request=req)
    return httpx.HTTPStatusError("500 Server Error", request=req, response=resp)


class _FakeResult:
    posted = 2
    skipped_existing = 0


class _FakeClient:
    def ensure_tag(self, name, state):
        pass

    def run_import(self, events, state, check_only=False, claim=None,
                   unclaim=None):
        return _FakeResult()


@pytest.fixture
def _pipeline(monkeypatch):
    """Stub the post-fetch pipeline (normalize → policies → import) so the
    tests exercise only the fetch/refresh behaviour. Returns the shared
    event log that fetch/refresh stubs also append to, for ordering
    assertions."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.normalize_history",
        lambda items, cluster_threshold: ["ev-trakt"],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.apply_cluster_policy",
        lambda events, policy: events,
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.find_low_conf_twins",
        lambda events, extra_pool=None: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.twin_cache.load_for_twin_lookup",
        lambda: [],
    )
    monkeypatch.setattr("fulcra_media.plugins.trakt.FulcraClient",
                        lambda: _FakeClient())
    monkeypatch.setattr("fulcra_media.plugins.trakt.newest_event_iso",
                        lambda events: "2026-06-09T00:00:00Z")
    # Pre-bootstrapped media state: cached def id, no resolver round trip.
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt._state_load",
        lambda path: MediaState(watched_definition_id="def-watched-123"),
    )
    monkeypatch.setattr("fulcra_media.plugins.trakt._state_save",
                        lambda state: None)
    return calls


def _make_ctx(calls: list[tuple]) -> tuple[RunContext, PluginState]:
    st = PluginState("trakt")
    ctx = RunContext(
        plugin_id="trakt",
        config={},
        credentials=dict(KEYCHAIN_CREDS),
        state=st,
        log=logging.getLogger("test.trakt"),
        _emit=lambda e: None,
        _set_credential=lambda key, value: calls.append(
            ("set_credential", key, value)),
    )
    return ctx, st


def test_happy_path_no_refresh_attempted(monkeypatch, _pipeline):
    """A 200 fetch never touches the refresh endpoint or the keychain."""
    calls = _pipeline

    def fake_fetch(headers, per_page=1000):
        calls.append(("fetch", headers["Authorization"]))
        yield from HISTORY_ITEMS

    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history_with_headers",
        fake_fetch,
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.httpx.post",
        lambda *a, **kw: pytest.fail("refresh POST must not happen on 200"),
    )

    ctx, st = _make_ctx(calls)
    _run_trakt(ctx)

    assert calls == [("fetch", "Bearer OLD-ACCESS")]
    assert st.watermark == "2026-06-09T00:00:00Z"


def test_401_refreshes_persists_both_tokens_then_retries(monkeypatch, _pipeline):
    """The live bug: 401 on the keychain path. Expected recovery — POST the
    refresh grant with the stored creds, persist BOTH rotated tokens via
    ctx.set_credential BEFORE the retry fetch (Trakt refresh tokens are
    single-use; losing the rotated one forces a full re-login), retry once
    with the new Bearer token, import normally."""
    calls = _pipeline
    fetch_count = {"n": 0}

    def fake_fetch(headers, per_page=1000):
        fetch_count["n"] += 1
        calls.append(("fetch", headers["Authorization"]))
        if fetch_count["n"] == 1:
            raise _http_401()
        yield from HISTORY_ITEMS

    posted: list[dict] = []

    def fake_post(url, *, json, timeout):
        posted.append({"url": url, "json": json})
        calls.append(("refresh_post", url))
        return httpx.Response(
            200,
            json={"access_token": "NEW-ACCESS", "refresh_token": "NEW-REFRESH",
                  "expires_in": 7776000, "created_at": 1234567890},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history_with_headers",
        fake_fetch,
    )
    monkeypatch.setattr("fulcra_media.plugins.trakt.httpx.post", fake_post)

    ctx, st = _make_ctx(calls)
    _run_trakt(ctx)

    # Refresh POST carried the stored refresh creds in TraktAuth._refresh's shape.
    assert posted == [{
        "url": "https://api.trakt.tv/oauth/token",
        "json": {
            "refresh_token": "OLD-REFRESH",
            "client_id": "the-client-id",
            "client_secret": "the-client-secret",
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            "grant_type": "refresh_token",
        },
    }]
    # Full ordering: failed fetch → refresh → persist BOTH tokens → retry.
    assert calls == [
        ("fetch", "Bearer OLD-ACCESS"),
        ("refresh_post", "https://api.trakt.tv/oauth/token"),
        ("set_credential", "access_token", "NEW-ACCESS"),
        ("set_credential", "refresh_token", "NEW-REFRESH"),
        ("fetch", "Bearer NEW-ACCESS"),
    ]
    # The run completed: watermark advanced off the imported events.
    assert st.watermark == "2026-06-09T00:00:00Z"


def test_401_then_refresh_failure_raises_actionable_error(monkeypatch, _pipeline):
    """When the refresh grant itself is rejected (refresh token expired or
    revoked), the plugin must raise a RuntimeError pointing at the web UI
    wizard — and must NOT write partial junk to the keychain."""
    calls = _pipeline

    def fake_fetch(headers, per_page=1000):
        raise _http_401()
        yield  # pragma: no cover — make it a generator like the real one

    def fake_post(url, *, json, timeout):
        return httpx.Response(401, json={"error": "invalid_grant"},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history_with_headers",
        fake_fetch,
    )
    monkeypatch.setattr("fulcra_media.plugins.trakt.httpx.post", fake_post)

    ctx, _ = _make_ctx(calls)
    with pytest.raises(RuntimeError, match=(
            r"access token expired and refresh failed — re-connect Trakt "
            r"in the Fulcra Collect web UI wizard")):
        _run_trakt(ctx)
    assert not [c for c in calls if c[0] == "set_credential"], \
        "no credentials may be persisted when the refresh failed"


def test_401_without_refresh_creds_raises_actionable_error(monkeypatch, _pipeline):
    """A 401 with no stored refresh_token/client_secret can't be recovered
    in-process — fail with the re-connect instruction, not a bare 401."""
    calls = _pipeline

    def fake_fetch(headers, per_page=1000):
        raise _http_401()
        yield  # pragma: no cover

    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history_with_headers",
        fake_fetch,
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.httpx.post",
        lambda *a, **kw: pytest.fail("must not POST refresh without creds"),
    )

    ctx, _ = _make_ctx(calls)
    ctx.credentials.pop("refresh_token")
    ctx.credentials.pop("client_secret")
    with pytest.raises(RuntimeError, match="re-connect Trakt"):
        _run_trakt(ctx)
    assert not [c for c in calls if c[0] == "set_credential"]


def test_non_401_error_propagates_unchanged(monkeypatch, _pipeline):
    """Only a 401 triggers the refresh path. A 500 (or any other HTTP
    error) propagates as-is so the runner reports the real failure."""
    calls = _pipeline

    def fake_fetch(headers, per_page=1000):
        raise _http_500()
        yield  # pragma: no cover

    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history_with_headers",
        fake_fetch,
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.httpx.post",
        lambda *a, **kw: pytest.fail("refresh POST must not happen on 500"),
    )

    ctx, _ = _make_ctx(calls)
    with pytest.raises(httpx.HTTPStatusError, match="500"):
        _run_trakt(ctx)
    assert not [c for c in calls if c[0] == "set_credential"]
