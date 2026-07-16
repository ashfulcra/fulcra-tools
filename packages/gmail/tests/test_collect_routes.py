"""P1-c — the Gmail add-account OAuth routes, driven end-to-end with FAKES.

A FastAPI app wired with a fake-backed AccountRegistry (in-memory store + fake
keychain + httpx.MockTransport for the token/getProfile legs). No real network,
keychain, or OAuth client — only live-credential verification is deferred to T4.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fulcra_gmail import collect_routes
from fulcra_gmail.collect_plugin import ADD_ACCOUNT_START_PATH, OAUTH_CALLBACK_PATH

_CLIENT_ID = "synthetic-client.apps.example.test"
_CLIENT_SECRET = "synthetic-secret-XYZ"  # noqa: S105 — fake


def _oauth_transport(*, email: str, refresh_token: str, on_exchange=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            if on_exchange is not None:
                on_exchange()
            return httpx.Response(200, json={
                "access_token": "at-synthetic",
                "refresh_token": refresh_token,
                "expires_in": 3599,
            })
        if request.url.path.endswith("/users/me/profile"):
            return httpx.Response(200, json={"emailAddress": email})
        return httpx.Response(404, json={"error": "unexpected"})

    return httpx.MockTransport(handler)


def _app(registry):
    app = FastAPI()
    collect_routes.register(app, ctx=None, registry_factory=lambda: registry)
    return TestClient(app, follow_redirects=False)


def _account_token_keys(keychain):
    return [k for k in keychain.store if k.startswith("account:")]


def _state_from_start(client) -> str:
    resp = client.get(ADD_ACCOUNT_START_PATH)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://accounts.google.com/")
    qs = parse_qs(urlparse(location).query)
    return qs["state"][0]


def test_start_endpoint_redirects_with_nonce_state(make_registry, keychain, store):
    registry = make_registry()
    registry.set_client_credentials(_CLIENT_ID, _CLIENT_SECRET)
    client = _app(registry)

    resp = client.get(ADD_ACCOUNT_START_PATH)
    assert resp.status_code == 302
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    state = qs["state"][0]
    # The minted nonce is recorded in the registry (single-use, unconsumed yet).
    assert state in registry._snapshot()["nonces"]
    # PKCE challenge + the readonly scope ride along.
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["scope"] == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_callback_valid_nonce_and_code_binds_account(make_registry, keychain, store):
    registry = make_registry(_oauth_transport(email="new@example.test",
                                              refresh_token="rt-1"))
    registry.set_client_credentials(_CLIENT_ID, _CLIENT_SECRET)
    client = _app(registry)

    state = _state_from_start(client)
    resp = client.get(OAUTH_CALLBACK_PATH, params={"state": state, "code": "auth-code"})
    assert resp.status_code == 200
    account = registry.find_by_email("new@example.test")
    assert account is not None
    assert registry.get_refresh_token(account.account_id) == "rt-1"
    # The success page must carry the operator forward: rule builder first,
    # plus add-another-account and dashboard (Ash feedback, 2026-07-16).
    assert "/api/gmail/rules/ui" in resp.text
    assert "/api/oauth/gmail/add-account/start" in resp.text


def test_callback_missing_nonce_rejects_with_no_token(make_registry, keychain, store):
    exchanges = []
    registry = make_registry(_oauth_transport(
        email="x@example.test", refresh_token="rt", on_exchange=lambda: exchanges.append(1)))
    registry.set_client_credentials(_CLIENT_ID, _CLIENT_SECRET)
    client = _app(registry)

    # A bogus state never minted by start → rejected, no code exchange, no token.
    resp = client.get(OAUTH_CALLBACK_PATH, params={"state": "bogus", "code": "c"})
    assert resp.status_code == 400
    assert exchanges == []  # never exchanged a code
    assert _account_token_keys(keychain) == []


def test_callback_replayed_nonce_rejects_second_time(make_registry, keychain, store):
    exchanges = []
    registry = make_registry(_oauth_transport(
        email="once@example.test", refresh_token="rt", on_exchange=lambda: exchanges.append(1)))
    registry.set_client_credentials(_CLIENT_ID, _CLIENT_SECRET)
    client = _app(registry)

    state = _state_from_start(client)
    first = client.get(OAUTH_CALLBACK_PATH, params={"state": state, "code": "c1"})
    assert first.status_code == 200
    # Replay the same (consumed) nonce → rejected, no second exchange.
    second = client.get(OAUTH_CALLBACK_PATH, params={"state": state, "code": "c2"})
    assert second.status_code == 400
    assert len(exchanges) == 1  # only the first callback exchanged


def test_callback_missing_code_rejects_with_no_token(make_registry, keychain, store):
    exchanges = []
    registry = make_registry(_oauth_transport(
        email="x@example.test", refresh_token="rt", on_exchange=lambda: exchanges.append(1)))
    registry.set_client_credentials(_CLIENT_ID, _CLIENT_SECRET)
    client = _app(registry)

    state = _state_from_start(client)
    resp = client.get(OAUTH_CALLBACK_PATH, params={"state": state})  # no code
    assert resp.status_code == 400
    assert exchanges == []
    assert _account_token_keys(keychain) == []


def test_callback_known_address_rotates_in_place_no_dup(make_registry, keychain, store):
    # First add.
    registry = make_registry(_oauth_transport(email="dup@example.test",
                                              refresh_token="rt-first"))
    registry.set_client_credentials(_CLIENT_ID, _CLIENT_SECRET)
    client = _app(registry)
    state1 = _state_from_start(client)
    r1 = client.get(OAUTH_CALLBACK_PATH, params={"state": state1, "code": "c1"})
    assert r1.status_code == 200
    acct1 = registry.find_by_email("dup@example.test")

    # Re-auth the SAME address with a rotated token — no duplicate row.
    registry._transport = _oauth_transport(email="dup@example.test",
                                           refresh_token="rt-second")
    state2 = _state_from_start(client)
    r2 = client.get(OAUTH_CALLBACK_PATH, params={"state": state2, "code": "c2"})
    assert r2.status_code == 200
    assert len(registry.list_accounts()) == 1  # no dup
    acct2 = registry.find_by_email("dup@example.test")
    assert acct2.account_id == acct1.account_id
    assert registry.get_refresh_token(acct2.account_id) == "rt-second"


def test_callback_google_error_param_rejects(make_registry, keychain, store):
    registry = make_registry()
    registry.set_client_credentials(_CLIENT_ID, _CLIENT_SECRET)
    client = _app(registry)
    resp = client.get(OAUTH_CALLBACK_PATH, params={"error": "access_denied"})
    assert resp.status_code == 400
    assert _account_token_keys(keychain) == []
