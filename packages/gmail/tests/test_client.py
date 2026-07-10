"""GmailClient: pagination, get, profile, refresh-on-401, invalid_grant."""
from __future__ import annotations

import httpx
import pytest

from fulcra_gmail.accounts import STATUS_ACTIVE, STATUS_AUTH_FAILED
from fulcra_gmail.client import (
    GMAIL_API_BASE,
    GmailClient,
)

_ACCOUNT_ID = "acct-0000-synthetic"
_REFRESH_TOKEN = "synthetic-refresh-token-AAAA"  # noqa: S105 — fake
_CLIENT_ID = "synthetic-client-id.apps.example.test"
_CLIENT_SECRET = "synthetic-client-secret-BBBB"  # noqa: S105 — fake


def _seed_account(keychain, store, *, status: str = STATUS_ACTIVE) -> None:
    keychain.set("client:client_id", _CLIENT_ID)
    keychain.set("client:client_secret", _CLIENT_SECRET)
    keychain.set(f"account:{_ACCOUNT_ID}:refresh_token", _REFRESH_TOKEN)
    store.write({
        "accounts": [{
            "account_id": _ACCOUNT_ID,
            "email": "synthetic@example.test",
            "display_order": 1,
            "added_at": "2026-01-01T00:00:00+00:00",
            "status": status,
        }],
        "nonces": {},
    })


def _client_for(make_registry, keychain, store, handler):
    _seed_account(keychain, store)
    transport = httpx.MockTransport(handler)
    registry = make_registry(transport)
    return GmailClient(_ACCOUNT_ID, registry=registry, transport=transport), registry


def test_list_message_ids_paginates_to_exhaustion(make_registry, keychain, store):
    pages = {
        None: {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "p2"},
        "p2": {"messages": [{"id": "m3"}], "nextPageToken": "p3"},
        "p3": {"messages": [{"id": "m4"}, {"id": "m5"}]},  # no nextPageToken
    }
    seen_page_tokens = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1"})
        assert str(request.url).startswith(f"{GMAIL_API_BASE}/users/me/messages")
        token = request.url.params.get("pageToken")
        seen_page_tokens.append(token)
        return httpx.Response(200, json=pages[token])

    client, _ = _client_for(make_registry, keychain, store, handler)
    ids = client.list_message_ids("subject:receipt")

    assert ids == ["m1", "m2", "m3", "m4", "m5"]
    # Followed nextPageToken through every page exactly once.
    assert seen_page_tokens == [None, "p2", "p3"]


def test_get_message_full(make_registry, keychain, store):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1"})
        assert request.url.path.endswith("/users/me/messages/m1")
        assert request.url.params.get("format") == "full"
        return httpx.Response(200, json={"id": "m1", "payload": {"headers": []}})

    client, _ = _client_for(make_registry, keychain, store, handler)
    msg = client.get_message("m1")
    assert msg["id"] == "m1"


def test_get_profile(make_registry, keychain, store):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1"})
        assert request.url.path.endswith("/users/me/profile")
        return httpx.Response(200, json={"emailAddress": "synthetic@example.test"})

    client, _ = _client_for(make_registry, keychain, store, handler)
    assert client.get_profile()["emailAddress"] == "synthetic@example.test"


def test_refresh_on_401_success(make_registry, keychain, store):
    """First access token 401s; a forced refresh mints a new one and the
    retry succeeds."""
    tokens_issued = []
    gmail_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            token = f"at-{len(tokens_issued) + 1}"
            tokens_issued.append(token)
            return httpx.Response(200, json={"access_token": token})
        # Gmail: 401 while the stale first token is presented, 200 after.
        auth = request.headers["authorization"]
        gmail_calls.append(auth)
        if auth == "Bearer at-1":
            return httpx.Response(401, json={"error": {"code": 401}})
        return httpx.Response(200, json={"messages": [{"id": "m9"}]})

    client, _ = _client_for(make_registry, keychain, store, handler)
    ids = client.list_message_ids("in:inbox")

    assert ids == ["m9"]
    assert tokens_issued == ["at-1", "at-2"]  # refreshed exactly once
    assert gmail_calls == ["Bearer at-1", "Bearer at-2"]


def test_invalid_grant_marks_auth_failed_and_does_not_raise(
    make_registry, keychain, store
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(400, json={"error": "invalid_grant"})
        pytest.fail("Gmail API must not be called once refresh fails")

    client, registry = _client_for(make_registry, keychain, store, handler)

    # No raise on any read; fail-soft empty/None results.
    assert client.list_message_ids("in:inbox") == []
    assert client.get_message("m1") is None
    assert client.get_profile() is None

    assert registry.get_account(_ACCOUNT_ID).status == STATUS_AUTH_FAILED
    # Token is retained (a re-auth rotates it) — fail-soft, not a purge.
    assert registry.get_refresh_token(_ACCOUNT_ID) == _REFRESH_TOKEN
