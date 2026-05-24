"""Tests for the Trakt OAuth handler and health check.

Both modules use keyring for credential lookup, so tests use the
_in_memory_keyring fixture to avoid touching the real OS keychain.
httpx calls are mocked to avoid real network access.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# In-memory keyring fixture (prevents touching the real OS keychain)
# ---------------------------------------------------------------------------

@pytest.fixture
def _in_memory_keyring(monkeypatch):
    """Replace keyring backend with a simple dict so tests are hermetic."""
    store: dict[tuple[str, str], str] = {}

    def _set(service, key, value):
        store[(service, key)] = value

    def _get(service, key):
        return store.get((service, key))

    def _delete(service, key):
        import keyring.errors
        if (service, key) not in store:
            raise keyring.errors.PasswordDeleteError("not found")
        del store[(service, key)]

    import fulcra_collect.credentials as _creds_mod
    monkeypatch.setattr(_creds_mod.keyring, "set_password", _set)
    monkeypatch.setattr(_creds_mod.keyring, "get_password", _get)
    monkeypatch.setattr(_creds_mod.keyring, "delete_password", _delete)
    return store


# ---------------------------------------------------------------------------
# trakt_oauth_handler
# ---------------------------------------------------------------------------

def test_trakt_oauth_handler_exchanges_code_for_tokens(mocker, _in_memory_keyring):
    from fulcra_collect import credentials as _creds
    _creds.set_secret("trakt", "client_id", "the-client-id")
    _creds.set_secret("trakt", "client_secret", "the-client-secret")

    mock_response = mocker.Mock()
    mock_response.json.return_value = {
        "access_token": "ACCESS-TOKEN",
        "refresh_token": "REFRESH-TOKEN",
        "expires_in": 7776000,
        "created_at": 1234567890,
    }
    mock_response.raise_for_status = mocker.Mock()
    mock_client = mocker.MagicMock()
    mock_client.__enter__ = mocker.Mock(return_value=mock_client)
    mock_client.__exit__ = mocker.Mock(return_value=False)
    mock_client.post = mocker.Mock(return_value=mock_response)
    mocker.patch("httpx.Client", return_value=mock_client)

    from fulcra_media.trakt_oauth import trakt_oauth_handler
    tokens = trakt_oauth_handler(
        plugin_id="trakt",
        code="AUTH-CODE",
        code_verifier="verifier",
        redirect_uri="http://localhost/cb",
    )
    assert tokens["access_token"] == "ACCESS-TOKEN"
    assert tokens["refresh_token"] == "REFRESH-TOKEN"
    assert tokens["expires_in"] == "7776000"
    assert tokens["created_at"] == "1234567890"

    # Confirm the request body included the expected fields
    call_kwargs = mock_client.post.call_args[1]
    sent = call_kwargs.get("json", {})
    assert sent["code"] == "AUTH-CODE"
    assert sent["code_verifier"] == "verifier"
    assert sent["client_id"] == "the-client-id"
    assert sent["client_secret"] == "the-client-secret"
    assert sent["grant_type"] == "authorization_code"


def test_trakt_oauth_handler_missing_credentials_raises(_in_memory_keyring):
    """When client_id / client_secret are not in the keychain, raise RuntimeError."""
    from fulcra_media.trakt_oauth import trakt_oauth_handler
    with pytest.raises(RuntimeError, match="not configured"):
        trakt_oauth_handler(
            plugin_id="trakt",
            code="X",
            code_verifier="V",
            redirect_uri="http://localhost/cb",
        )


def test_trakt_oauth_handler_uses_trakt_token_endpoint(mocker, _in_memory_keyring):
    """The handler posts to the correct Trakt token endpoint."""
    from fulcra_collect import credentials as _creds
    _creds.set_secret("trakt", "client_id", "cid")
    _creds.set_secret("trakt", "client_secret", "csec")

    mock_response = mocker.Mock()
    mock_response.json.return_value = {
        "access_token": "A", "refresh_token": "R",
        "expires_in": 1, "created_at": 0,
    }
    mock_response.raise_for_status = mocker.Mock()
    mock_client = mocker.MagicMock()
    mock_client.__enter__ = mocker.Mock(return_value=mock_client)
    mock_client.__exit__ = mocker.Mock(return_value=False)
    mock_client.post = mocker.Mock(return_value=mock_response)
    mocker.patch("httpx.Client", return_value=mock_client)

    from fulcra_media.trakt_oauth import trakt_oauth_handler, TRAKT_TOKEN_ENDPOINT
    trakt_oauth_handler(
        plugin_id="trakt", code="c", code_verifier="v",
        redirect_uri="http://localhost/cb",
    )
    posted_url = mock_client.post.call_args[0][0]
    assert posted_url == TRAKT_TOKEN_ENDPOINT


# ---------------------------------------------------------------------------
# trakt_health_check
# ---------------------------------------------------------------------------

class _FakeCtx:
    plugin_id = "trakt"


def test_trakt_health_check_ok(mocker, _in_memory_keyring):
    from fulcra_collect import credentials as _creds
    _creds.set_secret("trakt", "access_token", "tok")
    _creds.set_secret("trakt", "client_id", "cid")

    me_response = mocker.Mock()
    me_response.status_code = 200
    me_response.json.return_value = {"username": "testuser"}
    me_response.raise_for_status = mocker.Mock()

    history_response = mocker.Mock()
    history_response.status_code = 200
    history_response.json.return_value = [
        {
            "type": "movie",
            "movie": {"title": "The Matrix"},
            "watched_at": "2026-05-01T20:00:00.000Z",
        },
        {
            "type": "episode",
            "show": {"title": "Severance"},
            "episode": {"season": 1, "number": 3},
            "watched_at": "2026-04-30T21:00:00.000Z",
        },
    ]
    history_response.raise_for_status = mocker.Mock()

    mock_client = mocker.MagicMock()
    mock_client.__enter__ = mocker.Mock(return_value=mock_client)
    mock_client.__exit__ = mocker.Mock(return_value=False)
    mock_client.get = mocker.Mock(side_effect=[me_response, history_response])
    mocker.patch("httpx.Client", return_value=mock_client)

    from fulcra_media.trakt_health import trakt_health_check
    result = trakt_health_check(_FakeCtx())

    assert result.ok is True
    assert "testuser" in result.summary
    assert len(result.preview) == 2
    assert result.preview[0]["title"] == "The Matrix"
    assert result.preview[1]["title"] == "Severance S1E3"


def test_trakt_health_check_not_signed_in(_in_memory_keyring):
    """When no credentials are stored, returns ok=False with a clear message."""
    from fulcra_media.trakt_health import trakt_health_check
    result = trakt_health_check(_FakeCtx())
    assert result.ok is False
    assert "Not signed in" in result.summary


def test_trakt_health_check_expired_token(mocker, _in_memory_keyring):
    """HTTP 401 from Trakt returns ok=False with an expiry message."""
    from fulcra_collect import credentials as _creds
    _creds.set_secret("trakt", "access_token", "expired-tok")
    _creds.set_secret("trakt", "client_id", "cid")

    me_response = mocker.Mock()
    me_response.status_code = 401

    mock_client = mocker.MagicMock()
    mock_client.__enter__ = mocker.Mock(return_value=mock_client)
    mock_client.__exit__ = mocker.Mock(return_value=False)
    mock_client.get = mocker.Mock(return_value=me_response)
    mocker.patch("httpx.Client", return_value=mock_client)

    from fulcra_media.trakt_health import trakt_health_check
    result = trakt_health_check(_FakeCtx())
    assert result.ok is False
    assert "expired" in result.summary.lower()


def test_trakt_health_check_network_error(mocker, _in_memory_keyring):
    """Network failure returns ok=False with a 'could not reach' message."""
    import httpx as _httpx
    from fulcra_collect import credentials as _creds
    _creds.set_secret("trakt", "access_token", "tok")
    _creds.set_secret("trakt", "client_id", "cid")

    mock_client = mocker.MagicMock()
    mock_client.__enter__ = mocker.Mock(return_value=mock_client)
    mock_client.__exit__ = mocker.Mock(return_value=False)
    mock_client.get = mocker.Mock(
        side_effect=_httpx.ConnectError("connection refused")
    )
    mocker.patch("httpx.Client", return_value=mock_client)

    from fulcra_media.trakt_health import trakt_health_check
    result = trakt_health_check(_FakeCtx())
    assert result.ok is False
    assert "Could not reach Trakt" in result.summary
