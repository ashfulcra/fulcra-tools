"""AccountRegistry B4 lifecycle: nonce mint/consume/reject, getProfile
binding, in-place re-auth, and remove."""
from __future__ import annotations

import httpx

from fulcra_gmail.accounts import (
    NONCE_TTL_SECONDS,
    STATUS_ACTIVE,
    STATUS_AUTH_FAILED,
)

_CLIENT_ID = "synthetic-client.apps.example.test"
_CLIENT_SECRET = "synthetic-secret-XYZ"  # noqa: S105 — fake
_REDIRECT = "http://127.0.0.1:9292/api/oauth/callback"


def _oauth_transport(*, email: str, refresh_token: str, on_exchange=None):
    """MockTransport that serves the code-exchange + getProfile legs.

    ``on_exchange`` (optional) is called each time the token endpoint is hit —
    used to count exchanges (replay/mismatch tests assert it is NOT called).
    """

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
        return httpx.Response(404, json={"error": "unexpected"})  # pragma: no cover

    return httpx.MockTransport(handler)


def _account_token_keys(keychain) -> list[str]:
    return [k for k in keychain.store if k.startswith("account:")]


def test_getprofile_discovered_address_names_the_account(
    make_registry, keychain, store
):
    """The account is named from the granted token's getProfile address,
    even when it differs from the operator's hint (B4)."""
    transport = _oauth_transport(
        email="actual@example.test", refresh_token="rt-1"
    )
    registry = make_registry(transport)
    registry.set_client_credentials(_CLIENT_ID, _CLIENT_SECRET)

    session = registry.begin_add_account(
        _REDIRECT, expected_email="wrong-hint@example.test"
    )
    result = registry.complete_add_account(session.state, "auth-code-1")

    assert result.ok is True
    assert result.is_new is True
    assert result.email == "actual@example.test"  # NOT the hint
    account = registry.get_account(result.account_id)
    assert account.email == "actual@example.test"
    assert account.status == STATUS_ACTIVE
    # Refresh token stored under the discovered account's id.
    assert registry.get_refresh_token(result.account_id) == "rt-1"


def test_reauth_existing_address_rotates_token_in_place(
    make_registry, keychain, store
):
    registry = make_registry(_oauth_transport(email="dup@example.test",
                                               refresh_token="rt-first"))
    registry.set_client_credentials(_CLIENT_ID, _CLIENT_SECRET)
    s1 = registry.begin_add_account(_REDIRECT)
    r1 = registry.complete_add_account(s1.state, "code-1")
    assert r1.is_new is True

    # Simulate a prior auth failure to prove re-auth clears it.
    registry.mark_auth_failed(r1.account_id)
    assert registry.get_account(r1.account_id).status == STATUS_AUTH_FAILED

    # Re-auth the SAME address with a rotated refresh token.
    registry._transport = _oauth_transport(email="dup@example.test",
                                           refresh_token="rt-second")
    s2 = registry.begin_add_account(_REDIRECT)
    r2 = registry.complete_add_account(s2.state, "code-2")

    assert r2.is_new is False
    assert r2.account_id == r1.account_id  # same account, no dup row
    assert len(registry.list_accounts()) == 1
    assert registry.get_account(r1.account_id).status == STATUS_ACTIVE
    assert registry.get_refresh_token(r1.account_id) == "rt-second"  # rotated


def test_nonce_missing_rejected_no_token(make_registry, keychain, store):
    calls = {"n": 0}
    registry = make_registry(
        _oauth_transport(email="x@example.test", refresh_token="rt",
                         on_exchange=lambda: calls.__setitem__("n", calls["n"] + 1))
    )
    registry.set_client_credentials(_CLIENT_ID, _CLIENT_SECRET)

    result = registry.complete_add_account(None, "code-x")

    assert result.ok is False
    assert calls["n"] == 0  # no exchange attempted
    assert registry.list_accounts() == []
    assert _account_token_keys(keychain) == []


def test_nonce_mismatched_rejected_no_token(make_registry, keychain, store):
    calls = {"n": 0}
    registry = make_registry(
        _oauth_transport(email="x@example.test", refresh_token="rt",
                         on_exchange=lambda: calls.__setitem__("n", calls["n"] + 1))
    )
    registry.set_client_credentials(_CLIENT_ID, _CLIENT_SECRET)
    # Mint a real nonce, then present a DIFFERENT (never-minted) state.
    registry.begin_add_account(_REDIRECT)

    result = registry.complete_add_account("never-minted-state", "code-x")

    assert result.ok is False
    assert calls["n"] == 0
    assert registry.list_accounts() == []
    assert _account_token_keys(keychain) == []


def test_nonce_replayed_rejected_no_second_token(make_registry, keychain, store):
    calls = {"n": 0}
    registry = make_registry(
        _oauth_transport(email="once@example.test", refresh_token="rt",
                         on_exchange=lambda: calls.__setitem__("n", calls["n"] + 1))
    )
    registry.set_client_credentials(_CLIENT_ID, _CLIENT_SECRET)
    session = registry.begin_add_account(_REDIRECT)

    first = registry.complete_add_account(session.state, "code-1")
    assert first.ok is True
    assert calls["n"] == 1

    # Replay the SAME state — nonce already consumed, must reject.
    replay = registry.complete_add_account(session.state, "code-1")

    assert replay.ok is False
    assert calls["n"] == 1  # no second exchange
    assert len(registry.list_accounts()) == 1
    assert len(_account_token_keys(keychain)) == 1


def test_nonce_expired_rejected_no_token(make_registry, keychain, store):
    calls = {"n": 0}
    registry = make_registry(
        _oauth_transport(email="x@example.test", refresh_token="rt",
                         on_exchange=lambda: calls.__setitem__("n", calls["n"] + 1))
    )
    registry.set_client_credentials(_CLIENT_ID, _CLIENT_SECRET)
    session = registry.begin_add_account(_REDIRECT)

    # Age the nonce past its TTL directly in the store.
    store.doc["nonces"][session.state]["created_at"] -= NONCE_TTL_SECONDS + 60

    result = registry.complete_add_account(session.state, "code-1")

    assert result.ok is False
    assert calls["n"] == 0
    assert registry.list_accounts() == []
    assert _account_token_keys(keychain) == []


def test_remove_account_drops_token_and_row(make_registry, keychain, store):
    registry = make_registry(_oauth_transport(email="gone@example.test",
                                              refresh_token="rt"))
    registry.set_client_credentials(_CLIENT_ID, _CLIENT_SECRET)
    session = registry.begin_add_account(_REDIRECT)
    result = registry.complete_add_account(session.state, "code-1")
    account_id = result.account_id
    assert registry.get_refresh_token(account_id) == "rt"

    removed = registry.remove_account(account_id)

    assert removed is True
    assert registry.list_accounts() == []
    assert registry.get_refresh_token(account_id) is None
    assert _account_token_keys(keychain) == []
    # Idempotent second remove.
    assert registry.remove_account(account_id) is False


def test_authorize_url_carries_nonce_state_and_pkce(make_registry):
    registry = make_registry(None)
    registry.set_client_credentials(_CLIENT_ID, _CLIENT_SECRET)
    session = registry.begin_add_account(_REDIRECT)

    assert f"state={session.state}" in session.authorize_url
    assert "code_challenge=" in session.authorize_url
    assert "code_challenge_method=S256" in session.authorize_url
    assert "gmail.readonly" in session.authorize_url
