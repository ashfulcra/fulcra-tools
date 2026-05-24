"""Tests for the daemon's HTTP server."""
from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from fulcra_collect import config as _config
from fulcra_collect.daemon import Daemon, Config
from fulcra_collect.registry import RegistryResult
from fulcra_collect.web import build_app, _ensure_token, _web_token_path


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _build_test_daemon(collect_home, plugins: dict | None = None):
    registry = RegistryResult(plugins=plugins or {})
    return Daemon(registry=registry, config=Config())


def _client(daemon) -> TestClient:
    token = _ensure_token()
    app = build_app(daemon)
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------------------------------------------------------------------------
# Original tests (preserved)
# ---------------------------------------------------------------------------

def test_status_requires_auth(collect_home):
    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    client = TestClient(app)
    r = client.get("/api/status")
    assert r.status_code == 401


def test_status_with_valid_token(collect_home):
    token = _ensure_token()
    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    client = TestClient(app)
    r = client.get("/api/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert "plugins" in body
    assert isinstance(body["plugins"], list)


def test_status_with_wrong_token(collect_home):
    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    client = TestClient(app)
    r = client.get("/api/status", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


def test_root_returns_placeholder_when_frontend_missing(collect_home):
    # When packages/web-ui/dist/ doesn't exist yet (it will in B6), root
    # returns a clear error JSON instead of 500-ing.
    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    client = TestClient(app)
    r = client.get("/")
    # Either an error JSON (B5) or the actual HTML (B6 onwards) — both OK.
    assert r.status_code == 200


def test_token_file_has_0600_permissions(collect_home, tmp_path, monkeypatch):
    """The web token file must be 0600 so other users can't read it."""
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))
    _ensure_token()
    p = _web_token_path()
    assert p.exists()
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600


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
# Plugin operations — delegation to handle_request
# ---------------------------------------------------------------------------

def test_plugin_run_unknown(collect_home):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.post("/api/plugin/no-such/run")
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_reload_route(collect_home):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.post("/api/reload")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_version_route(collect_home):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/version")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "daemon_version" in body


def test_credentials_unknown_plugin(collect_home):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/plugin/no-such/credentials")
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_credentials_known_plugin(collect_home, _in_memory_keyring):
    from fulcra_collect.plugin import Plugin, Credential
    plugin = Plugin(
        id="svc",
        name="Service",
        kind="manual",
        run=lambda c: None,
        required_credentials=(
            Credential(key="api_key", label="API Key", help=""),
        ),
    )
    daemon = _build_test_daemon(collect_home, plugins={"svc": plugin})
    client = _client(daemon)
    r = client.get("/api/plugin/svc/credentials")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["credentials"]["api_key"] == "missing"


def test_set_and_delete_credential(collect_home, _in_memory_keyring):
    from fulcra_collect.plugin import Plugin, Credential
    plugin = Plugin(
        id="svc",
        name="Service",
        kind="manual",
        run=lambda c: None,
        required_credentials=(
            Credential(key="api_key", label="API Key", help=""),
        ),
    )
    daemon = _build_test_daemon(collect_home, plugins={"svc": plugin})
    client = _client(daemon)

    # Set credential
    r = client.put("/api/plugin/svc/credential/api_key", json={"secret": "s3cr3t"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Now shows as set
    r = client.get("/api/plugin/svc/credentials")
    assert r.json()["credentials"]["api_key"] == "set"

    # Delete
    r = client.delete("/api/plugin/svc/credential/api_key")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Now missing again
    r = client.get("/api/plugin/svc/credentials")
    assert r.json()["credentials"]["api_key"] == "missing"


# ---------------------------------------------------------------------------
# Plugin settings
# ---------------------------------------------------------------------------

def test_get_settings_unknown_plugin(collect_home):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/plugin/no-such/settings")
    assert r.status_code == 404


def test_get_settings_empty_initially(collect_home):
    from fulcra_collect.plugin import Plugin, Setting
    plugin = Plugin(
        id="rss", name="RSS", kind="manual", run=lambda c: None,
        required_settings=(
            Setting(key="feed_url", label="Feed URL", kind="url"),
        ),
    )
    daemon = _build_test_daemon(collect_home, plugins={"rss": plugin})
    client = _client(daemon)
    r = client.get("/api/plugin/rss/settings")
    assert r.status_code == 200
    assert r.json() == {}


def test_plugin_settings_put_validates_against_required_settings(collect_home):
    from fulcra_collect.plugin import Plugin, Setting
    plugin = Plugin(
        id="rss", name="RSS", kind="scheduled",
        run=lambda c: None,
        default_interval=datetime.timedelta(hours=1),
        required_settings=(
            Setting(key="feed_url", label="Feed URL", kind="url"),
            Setting(key="category", label="Category", kind="enum",
                    enum_values=("watched", "listened", "read")),
        ),
    )
    daemon = _build_test_daemon(collect_home, plugins={"rss": plugin})
    client = _client(daemon)

    # Unknown key
    r = client.put("/api/plugin/rss/settings", json={"bogus": "x"})
    assert r.status_code == 400

    # Bad enum value
    r = client.put("/api/plugin/rss/settings", json={"category": "not-a-value"})
    assert r.status_code == 400

    # Valid — both settings at once
    r = client.put("/api/plugin/rss/settings",
                   json={"feed_url": "https://example.com/feed.xml",
                         "category": "watched"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Values are persisted and readable
    r = client.get("/api/plugin/rss/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["feed_url"] == "https://example.com/feed.xml"
    assert body["category"] == "watched"


def test_plugin_settings_put_unknown_plugin(collect_home):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.put("/api/plugin/no-such/settings", json={"key": "val"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Plugin enable / disable
# ---------------------------------------------------------------------------

def test_enable_disable_unknown_plugin(collect_home):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    assert client.post("/api/plugin/no-such/enable").status_code == 404
    assert client.post("/api/plugin/no-such/disable").status_code == 404


def test_enable_then_disable(collect_home):
    from fulcra_collect.plugin import Plugin
    plugin = Plugin(id="x", name="X", kind="manual", run=lambda c: None)
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    client = _client(daemon)

    r = client.post("/api/plugin/x/enable")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # Verify persisted in config.toml
    cfg = _config.load()
    assert "x" in cfg.enabled

    r = client.post("/api/plugin/x/disable")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    cfg = _config.load()
    assert "x" not in cfg.enabled


# ---------------------------------------------------------------------------
# Plugin contract introspection
# ---------------------------------------------------------------------------

def test_plugin_contract_unknown(collect_home):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/plugin/no-such/contract")
    assert r.status_code == 404


def test_plugin_contract_returns_full_shape(collect_home):
    from fulcra_collect.plugin import Plugin, Setting, Credential, SetupStep, Permission
    plugin = Plugin(
        id="example", name="Example", kind="scheduled",
        run=lambda c: None,
        description="Imports example data.",
        category="other",
        default_interval=datetime.timedelta(hours=1),
        required_settings=(
            Setting(key="feed_url", label="Feed URL", kind="url"),
        ),
        required_credentials=(
            Credential(key="api_key", label="API key", help="Your key."),
        ),
        required_permissions=(
            Permission(id="full-disk-access", explanation="Needs file access."),
        ),
        setup_steps=(
            SetupStep(kind="intro", title="What this does", body_md="Import example data."),
        ),
    )
    daemon = _build_test_daemon(collect_home, plugins={"example": plugin})
    client = _client(daemon)
    r = client.get("/api/plugin/example/contract")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "example"
    assert body["name"] == "Example"
    assert body["kind"] == "scheduled"
    assert body["category"] == "other"
    assert body["description"] == "Imports example data."
    assert body["default_interval_s"] == 3600
    assert body["required_settings"][0]["key"] == "feed_url"
    assert body["required_settings"][0]["kind"] == "url"
    assert body["required_credentials"][0]["key"] == "api_key"
    assert body["required_permissions"][0]["id"] == "full-disk-access"
    assert body["setup_steps"][0]["kind"] == "intro"
    assert body["health_check_available"] is False


def test_plugin_contract_no_interval_when_manual(collect_home):
    from fulcra_collect.plugin import Plugin
    plugin = Plugin(id="m", name="M", kind="manual", run=lambda c: None)
    daemon = _build_test_daemon(collect_home, plugins={"m": plugin})
    client = _client(daemon)
    r = client.get("/api/plugin/m/contract")
    assert r.status_code == 200
    assert r.json()["default_interval_s"] is None


def test_plugin_contract_health_check_available(collect_home):
    from fulcra_collect.plugin import Plugin, HealthResult
    plugin = Plugin(
        id="h", name="H", kind="manual", run=lambda c: None,
        health_check=lambda ctx: HealthResult(ok=True, summary="ok"),
    )
    daemon = _build_test_daemon(collect_home, plugins={"h": plugin})
    client = _client(daemon)
    r = client.get("/api/plugin/h/contract")
    assert r.json()["health_check_available"] is True


def test_plugin_contract_enum_setting_values(collect_home):
    from fulcra_collect.plugin import Plugin, Setting
    plugin = Plugin(
        id="e", name="E", kind="manual", run=lambda c: None,
        required_settings=(
            Setting(key="mode", label="Mode", kind="enum",
                    enum_values=("a", "b", "c"), required=False),
        ),
    )
    daemon = _build_test_daemon(collect_home, plugins={"e": plugin})
    client = _client(daemon)
    r = client.get("/api/plugin/e/contract")
    s = r.json()["required_settings"][0]
    assert s["enum_values"] == ["a", "b", "c"]
    assert s["required"] is False


# ---------------------------------------------------------------------------
# Plugin health check
# ---------------------------------------------------------------------------

def test_plugin_health_check_unknown(collect_home):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.post("/api/plugin/no-such/health_check")
    assert r.status_code == 404


def test_plugin_health_check_when_not_declared(collect_home):
    from fulcra_collect.plugin import Plugin
    plugin = Plugin(id="x", name="X", kind="manual", run=lambda c: None)
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    client = _client(daemon)
    r = client.post("/api/plugin/x/health_check")
    assert r.status_code == 200
    assert r.json() == {"available": False}


def test_plugin_health_check_returns_result(collect_home):
    from fulcra_collect.plugin import Plugin, HealthResult

    def check(ctx):
        return HealthResult(ok=True, summary="all good",
                            preview=[{"title": "Song A"}])

    plugin = Plugin(id="x", name="X", kind="manual", run=lambda c: None,
                    health_check=check)
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    client = _client(daemon)
    r = client.post("/api/plugin/x/health_check")
    body = r.json()
    assert body["available"] is True
    assert body["ok"] is True
    assert body["summary"] == "all good"
    assert body["preview"] == [{"title": "Song A"}]


def test_plugin_health_check_catches_exceptions(collect_home):
    from fulcra_collect.plugin import Plugin

    def bad_check(ctx):
        raise RuntimeError("service unreachable")

    plugin = Plugin(id="x", name="X", kind="manual", run=lambda c: None,
                    health_check=bad_check)
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    client = _client(daemon)
    r = client.post("/api/plugin/x/health_check")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["ok"] is False
    assert "RuntimeError" in body["summary"]
    assert "service unreachable" in body["summary"]


# ---------------------------------------------------------------------------
# Fulcra account auth
# ---------------------------------------------------------------------------

def test_fulcra_auth_status_unauthenticated(collect_home, _in_memory_keyring):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/fulcra/auth/status")
    assert r.status_code == 200
    assert r.json()["authenticated"] is False


def _mock_httpx_success(mocker):
    """Return a mock httpx.Client context-manager whose GET returns 200."""
    mock_resp = mocker.Mock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = mocker.Mock()
    mock_client = mocker.MagicMock()
    mock_client.__enter__ = mocker.Mock(return_value=mock_client)
    mock_client.__exit__ = mocker.Mock(return_value=False)
    mock_client.get = mocker.Mock(return_value=mock_resp)
    return mock_client


def test_fulcra_auth_token_set_and_clear(collect_home, _in_memory_keyring, mocker):
    mock_client = _mock_httpx_success(mocker)
    mocker.patch("httpx.Client", return_value=mock_client)

    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)

    r = client.post("/api/fulcra/auth/token", json={"token": "abc"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = client.get("/api/fulcra/auth/status")
    assert r.json()["authenticated"] is True

    r = client.delete("/api/fulcra/auth/token")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = client.get("/api/fulcra/auth/status")
    assert r.json()["authenticated"] is False


def test_fulcra_auth_token_empty_rejected(collect_home, _in_memory_keyring):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.post("/api/fulcra/auth/token", json={"token": "   "})
    assert r.status_code == 400


def test_fulcra_auth_token_strips_whitespace(collect_home, _in_memory_keyring, mocker):
    mock_client = _mock_httpx_success(mocker)
    mocker.patch("httpx.Client", return_value=mock_client)

    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.post("/api/fulcra/auth/token", json={"token": "  mytoken  "})
    assert r.status_code == 200

    # Verify the stored token has no surrounding whitespace
    import fulcra_collect.credentials as _creds
    assert _creds.get_user_secret("bearer-token") == "mytoken"


def test_fulcra_auth_token_validates_and_stores_on_success(collect_home, _in_memory_keyring, mocker):
    """A valid Fulcra token is verified against the API before storage."""
    daemon = _build_test_daemon(collect_home)
    mock_client = _mock_httpx_success(mocker)
    mocker.patch("httpx.Client", return_value=mock_client)
    client = _client(daemon)
    r = client.post("/api/fulcra/auth/token", json={"token": "real-token"})
    assert r.status_code == 200
    from fulcra_collect import credentials as _creds
    assert _creds.get_user_secret("bearer-token") == "real-token"


def test_fulcra_auth_token_rejects_401_from_fulcra(collect_home, _in_memory_keyring, mocker):
    """A token that Fulcra rejects with 401 is NOT stored."""
    daemon = _build_test_daemon(collect_home)
    mock_resp = mocker.Mock()
    mock_resp.status_code = 401
    mock_client = mocker.MagicMock()
    mock_client.__enter__ = mocker.Mock(return_value=mock_client)
    mock_client.__exit__ = mocker.Mock(return_value=False)
    mock_client.get = mocker.Mock(return_value=mock_resp)
    mocker.patch("httpx.Client", return_value=mock_client)
    client = _client(daemon)
    r = client.post("/api/fulcra/auth/token", json={"token": "bad-typo"})
    assert r.status_code == 401
    assert "Fulcra rejected" in r.json()["detail"]
    from fulcra_collect import credentials as _creds
    assert _creds.get_user_secret("bearer-token") is None


def test_fulcra_auth_token_rejects_network_failure(collect_home, _in_memory_keyring, mocker):
    """If Fulcra is unreachable, the token is NOT stored."""
    import httpx
    daemon = _build_test_daemon(collect_home)
    mock_client = mocker.MagicMock()
    mock_client.__enter__ = mocker.Mock(return_value=mock_client)
    mock_client.__exit__ = mocker.Mock(return_value=False)
    mock_client.get = mocker.Mock(side_effect=httpx.ConnectError("DNS failed"))
    mocker.patch("httpx.Client", return_value=mock_client)
    client = _client(daemon)
    r = client.post("/api/fulcra/auth/token", json={"token": "valid-but-fulcra-down"})
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# Auth guard on new routes
# ---------------------------------------------------------------------------

def test_new_routes_require_auth(collect_home):
    """Spot-check that new routes also enforce the Bearer token."""
    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    client = TestClient(app)  # No auth header

    paths = [
        ("GET", "/api/version"),
        ("POST", "/api/reload"),
        ("GET", "/api/fulcra/auth/status"),
    ]
    for method, path in paths:
        r = client.request(method, path)
        assert r.status_code == 401, f"{method} {path} should require auth"


# ---------------------------------------------------------------------------
# OAuth routes
# ---------------------------------------------------------------------------

def test_oauth_start_requires_oauth_handler(collect_home):
    from fulcra_collect.plugin import Plugin
    plugin = Plugin(id="x", name="X", kind="manual", run=lambda c: None)
    # plugin has no oauth_handler
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    client = _client(daemon)
    r = client.post("/api/oauth/x/start")
    assert r.status_code == 404


def test_oauth_start_returns_state_and_challenge(collect_home):
    from fulcra_collect.plugin import Plugin
    plugin = Plugin(id="x", name="X", kind="manual", run=lambda c: None,
                    oauth_handler=lambda **kw: {"access_token": "abc"})
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    # Simulate the URL being available on the daemon (normally set by serve())
    daemon._web_url = "http://127.0.0.1:9999"
    client = _client(daemon)
    r = client.post("/api/oauth/x/start")
    assert r.status_code == 200
    body = r.json()
    assert "state" in body and "code_challenge" in body


def test_oauth_callback_invokes_handler_and_stores_tokens(collect_home, _in_memory_keyring):
    from fulcra_collect.plugin import Plugin
    calls = []

    def fake_handler(*, plugin_id, code, code_verifier, redirect_uri):
        calls.append({"plugin_id": plugin_id, "code": code})
        return {"access_token": "token-A", "refresh_token": "token-R"}

    plugin = Plugin(id="x", name="X", kind="manual", run=lambda c: None,
                    oauth_handler=fake_handler)
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    # Simulate the URL being available on the daemon (normally set by serve())
    daemon._web_url = "http://127.0.0.1:9999"
    client = _client(daemon)
    start = client.post("/api/oauth/x/start").json()
    r = client.get(f"/api/oauth/x/callback?code=AUTH-CODE&state={start['state']}")
    assert r.status_code == 200
    assert calls[0]["code"] == "AUTH-CODE"
    from fulcra_collect import credentials as _creds
    assert _creds.get_secret("x", "access_token") == "token-A"
    assert _creds.get_secret("x", "refresh_token") == "token-R"


def test_oauth_callback_rejects_invalid_state(collect_home):
    from fulcra_collect.plugin import Plugin
    plugin = Plugin(id="x", name="X", kind="manual", run=lambda c: None,
                    oauth_handler=lambda **kw: {"access_token": "abc"})
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    client = _client(daemon)
    r = client.get("/api/oauth/x/callback?code=AUTH&state=never-issued")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Phase D — activity feed
# ---------------------------------------------------------------------------

def test_activity_route_empty(collect_home):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/activity")
    assert r.status_code == 200
    assert r.json() == {"entries": []}


def test_activity_route_returns_newest_first(collect_home):
    daemon = _build_test_daemon(collect_home)
    daemon.activity.add(plugin_id="lastfm", summary="A")
    daemon.activity.add(plugin_id="lastfm", summary="B")
    client = _client(daemon)
    r = client.get("/api/activity?limit=10")
    body = r.json()
    assert body["entries"][0]["summary"] == "B"
    assert body["entries"][1]["summary"] == "A"


def test_activity_route_validates_limit(collect_home):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    assert client.get("/api/activity?limit=0").status_code == 400
    assert client.get("/api/activity?limit=500").status_code == 400


def test_activity_route_entry_shape(collect_home):
    daemon = _build_test_daemon(collect_home)
    daemon.activity.add(plugin_id="trakt", summary="Watched: Breaking Bad", ok=True)
    daemon.activity.add(plugin_id="lastfm", summary="auth failed", ok=False)
    client = _client(daemon)
    r = client.get("/api/activity?limit=2")
    entries = r.json()["entries"]
    assert entries[0]["plugin_id"] == "lastfm"
    assert entries[0]["ok"] is False
    assert entries[1]["plugin_id"] == "trakt"
    assert entries[1]["ok"] is True
    for e in entries:
        assert "timestamp" in e
        assert e["timestamp"].endswith("Z")


def test_activity_route_requires_auth(collect_home):
    from fastapi.testclient import TestClient
    from fulcra_collect.web import build_app
    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    client = TestClient(app)  # no auth header
    r = client.get("/api/activity")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Phase E — definition routes
# ---------------------------------------------------------------------------

def test_definitions_route_requires_fulcra_auth(collect_home, _in_memory_keyring):
    """Without a stored Fulcra bearer-token the route returns 401."""
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/definitions")
    assert r.status_code == 401


def test_definitions_route_returns_list(collect_home, _in_memory_keyring, monkeypatch):
    """With a valid token the route returns definitions from the Fulcra API."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("bearer-token", "valid-token")

    # Monkeypatch httpx.Client to avoid hitting the real Fulcra API

    fake_defs = [
        {"id": "def-1", "name": "Watched", "annotation_type": "duration",
         "deleted_at": None},
        {"id": "def-2", "name": "Listened", "annotation_type": "duration",
         "deleted_at": None},
        {"id": "def-3", "name": "Old", "annotation_type": "duration",
         "deleted_at": "2026-01-01T00:00:00Z"},  # should be filtered
    ]

    class FakeResponse:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return fake_defs

    class FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, path, **kw): return FakeResponse()

    monkeypatch.setattr("fulcra_collect.web.httpx", type("httpx", (), {"Client": FakeClient})())

    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/definitions")
    assert r.status_code == 200
    body = r.json()
    # def-3 is deleted — must be excluded
    assert len(body["definitions"]) == 2
    ids = {d["id"] for d in body["definitions"]}
    assert "def-1" in ids
    assert "def-3" not in ids


def test_definitions_route_filters_by_annotation_type(collect_home, _in_memory_keyring, monkeypatch):
    """?annotation_type=moment returns only moment-type definitions."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("bearer-token", "valid-token")

    fake_defs = [
        {"id": "dur-1", "name": "Watched", "annotation_type": "duration", "deleted_at": None},
        {"id": "mom-1", "name": "Coffee", "annotation_type": "moment", "deleted_at": None},
    ]

    class FakeResponse:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return fake_defs

    class FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, path, **kw): return FakeResponse()

    monkeypatch.setattr("fulcra_collect.web.httpx", type("httpx", (), {"Client": FakeClient})())

    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/definitions?annotation_type=moment")
    assert r.status_code == 200
    defs = r.json()["definitions"]
    assert len(defs) == 1
    assert defs[0]["annotation_type"] == "moment"


def test_definition_recent_requires_fulcra_auth(collect_home, _in_memory_keyring):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/definitions/some-id/recent")
    assert r.status_code == 401


def test_definition_recent_validates_limit(collect_home, _in_memory_keyring):
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("bearer-token", "valid-token")
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    assert client.get("/api/definitions/x/recent?limit=0").status_code == 400
    assert client.get("/api/definitions/x/recent?limit=25").status_code == 400


def test_definition_recent_returns_entries(collect_home, _in_memory_keyring, monkeypatch):
    """Returns matching events for the definition, sorted newest-first."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("bearer-token", "valid-token")

    def_id = "def-abc"
    def_source = f"com.fulcradynamics.annotation.{def_id}"

    fake_records = [
        {"metadata": {"source": [def_source], "recorded_at": {"start_time": "2026-05-20T10:00:00Z", "end_time": "2026-05-20T11:00:00Z"}}},
        {"metadata": {"source": [def_source], "recorded_at": {"start_time": "2026-05-22T10:00:00Z", "end_time": "2026-05-22T11:00:00Z"}}},
        {"metadata": {"source": ["other-source"], "recorded_at": "2026-05-21T09:00:00Z"}},  # not for this def
    ]

    class FakeResponse:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return fake_records

    class FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, path, **kw): return FakeResponse()

    monkeypatch.setattr("fulcra_collect.web.httpx", type("httpx", (), {"Client": FakeClient})())

    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get(f"/api/definitions/{def_id}/recent?limit=5")
    assert r.status_code == 200
    entries = r.json()["entries"]
    # Only 2 records match this definition (the third has other-source)
    assert len(entries) == 2
    # Sorted newest-first by end_time
    end_times = [e["metadata"]["recorded_at"]["end_time"] for e in entries]
    assert end_times[0] > end_times[1]


# ---------------------------------------------------------------------------
# Phase E — bind / clear definition routes
# ---------------------------------------------------------------------------

def test_bind_definition_unknown_plugin(collect_home):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.post("/api/plugin/no-such/definition",
                    json={"definition_id": "abc"})
    assert r.status_code == 404


def test_bind_definition_missing_body_fields(collect_home):
    from fulcra_collect.plugin import Plugin
    plugin = Plugin(id="x", name="X", kind="manual", run=lambda c: None)
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    client = _client(daemon)
    # Body has neither definition_id nor force_new
    r = client.post("/api/plugin/x/definition", json={})
    assert r.status_code == 400


def test_bind_definition_stores_id(collect_home):
    from fulcra_collect.plugin import Plugin
    from fulcra_collect import state as _state_mod
    plugin = Plugin(id="x", name="X", kind="manual", run=lambda c: None)
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    client = _client(daemon)
    r = client.post("/api/plugin/x/definition", json={"definition_id": "def-uuid-123"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    st = _state_mod.load("x")
    assert st.definition_id == "def-uuid-123"


def test_bind_definition_force_new_clears_id(collect_home):
    from fulcra_collect.plugin import Plugin
    from fulcra_collect import state as _state_mod
    plugin = Plugin(id="x", name="X", kind="manual", run=lambda c: None)
    # Pre-load state with an existing definition_id
    st = _state_mod.load("x")
    st.definition_id = "old-def-id"
    _state_mod.save(st)
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    client = _client(daemon)
    r = client.post("/api/plugin/x/definition", json={"force_new": True})
    assert r.status_code == 200
    st2 = _state_mod.load("x")
    assert st2.definition_id is None


def test_clear_definition_unknown_plugin(collect_home):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.delete("/api/plugin/no-such/definition")
    assert r.status_code == 404


def test_clear_definition_removes_id(collect_home):
    from fulcra_collect.plugin import Plugin
    from fulcra_collect import state as _state_mod
    plugin = Plugin(id="x", name="X", kind="manual", run=lambda c: None)
    st = _state_mod.load("x")
    st.definition_id = "some-def"
    _state_mod.save(st)
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    client = _client(daemon)
    r = client.delete("/api/plugin/x/definition")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    st2 = _state_mod.load("x")
    assert st2.definition_id is None


# ---------------------------------------------------------------------------
# Phase G — quick-record HTTP routes
# ---------------------------------------------------------------------------

def _fake_httpx_for_daemon(monkeypatch, *, get_data=None, post_exc=None):
    """Patch httpx inside fulcra_collect.daemon so daemon methods don't
    hit the real network."""
    import fulcra_collect.daemon as daemon_mod

    get_data = get_data or []

    class _FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return self._data
        def __init__(self, data): self._data = data

    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, *a, **kw): return _FakeResp(get_data)
        def post(self, *a, **kw):
            if post_exc:
                raise post_exc
            return _FakeResp({"ok": True})


    class _FakeClientFactory:
        def __init__(self, **kw): pass
        def __enter__(self): return _FakeClient()
        def __exit__(self, *a): pass

    # We need to wrap in a class so both context-manager forms work
    class _WrappedClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, *a, **kw): return _FakeResp(get_data)
        def post(self, *a, **kw):
            if post_exc:
                raise post_exc
            return _FakeResp({"ok": True})

    monkeypatch.setattr(daemon_mod, "httpx",
                        type("httpx", (), {"Client": _WrappedClient})())


def test_quick_record_definitions_requires_auth(collect_home):
    """GET /api/quick-record/definitions requires the web Bearer token."""
    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    from fastapi.testclient import TestClient
    client = TestClient(app)  # no auth header
    r = client.get("/api/quick-record/definitions")
    assert r.status_code == 401


def test_quick_record_definitions_unauthenticated_fulcra(
        collect_home, _in_memory_keyring, monkeypatch):
    """Route returns ok=False with empty list when no Fulcra bearer token."""
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/quick-record/definitions")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["definitions"] == []


def test_quick_record_definitions_returns_moments(
        collect_home, _in_memory_keyring, monkeypatch):
    """Route returns Moment defs from the daemon's quick_record_list handler."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("bearer-token", "valid-token")

    fake_defs = [
        {"id": "m1", "name": "Coffee", "annotation_type": "moment",
         "deleted_at": None, "created_at": "2026-05-10T00:00:00Z"},
        {"id": "dur1", "name": "Work", "annotation_type": "duration",
         "deleted_at": None, "created_at": "2026-05-09T00:00:00Z"},
    ]
    _fake_httpx_for_daemon(monkeypatch, get_data=fake_defs)

    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/quick-record/definitions")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # Only moments are returned; duration filtered out
    assert len(body["definitions"]) == 1
    assert body["definitions"][0]["annotation_type"] == "moment"


def test_record_annotation_requires_auth(collect_home):
    """POST /api/annotations requires the web Bearer token."""
    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.post("/api/annotations", json={"definition_id": "abc"})
    assert r.status_code == 401


def test_record_annotation_unauthenticated_fulcra(
        collect_home, _in_memory_keyring, monkeypatch):
    """Route returns ok=False when no Fulcra bearer token is stored."""
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.post("/api/annotations", json={"definition_id": "def-abc"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_record_annotation_happy_path(
        collect_home, _in_memory_keyring, monkeypatch):
    """Route calls Fulcra POST and returns ok=True. Activity buffer is updated."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("bearer-token", "valid-token")
    _fake_httpx_for_daemon(monkeypatch)

    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.post("/api/annotations",
                    json={"definition_id": "def-abcdef12", "comment": None})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    # Activity buffer has one success entry
    entries = daemon.activity.recent(limit=1)
    assert entries[0].ok is True
    assert entries[0].plugin_id == "quick-record"


def test_record_annotation_api_failure_returns_error(
        collect_home, _in_memory_keyring, monkeypatch):
    """Route returns ok=False and surfaces activity entry on Fulcra API failure."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("bearer-token", "valid-token")
    _fake_httpx_for_daemon(monkeypatch, post_exc=RuntimeError("connection refused"))

    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.post("/api/annotations", json={"definition_id": "def-xyz"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "Fulcra API" in body["error"]
    entries = daemon.activity.recent(limit=1)
    assert entries[0].ok is False


def test_record_annotation_missing_definition_id_rejected(
        collect_home, _in_memory_keyring, monkeypatch):
    """Route returns 422 when definition_id is missing from the body."""
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    # definition_id is a required field; omitting it gives a validation error
    r = client.post("/api/annotations", json={})
    assert r.status_code == 422
