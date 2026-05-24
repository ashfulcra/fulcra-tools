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
    import os
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))
    token = _ensure_token()
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


def test_fulcra_auth_token_set_and_clear(collect_home, _in_memory_keyring):
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


def test_fulcra_auth_token_strips_whitespace(collect_home, _in_memory_keyring):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.post("/api/fulcra/auth/token", json={"token": "  mytoken  "})
    assert r.status_code == 200

    # Verify the stored token has no surrounding whitespace
    import fulcra_collect.credentials as _creds
    assert _creds.get_user_secret("bearer-token") == "mytoken"


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
