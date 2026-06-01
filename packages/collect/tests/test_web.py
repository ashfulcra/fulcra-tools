"""Tests for the daemon's HTTP server."""
from __future__ import annotations

import datetime

import pytest
from collect_test_helpers import install_fake_httpx
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


def test_root_sets_cookie_and_is_uncacheable(collect_home):
    # The `/` response carries the Set-Cookie that bootstraps the SPA's
    # bearer auth. It must be no-store so a reload always re-hits the
    # daemon and re-issues the cookie — otherwise a tab with a stale
    # fulcra_token cookie can be served `/` from cache and stay stuck on
    # "auth required" even after reloading (the onboarding bug this fixes).
    token = _ensure_token()
    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    # dist/index.html ships in the repo, so this is the HTML+cookie path.
    assert r.cookies.get("fulcra_token") == token
    assert r.headers.get("cache-control") == "no-store"


def test_token_file_has_0600_permissions(collect_home, tmp_path, monkeypatch):
    """The web token file must be 0600 so other users can't read it."""
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))
    _ensure_token()
    p = _web_token_path()
    assert p.exists()
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600


# ---------------------------------------------------------------------------
# In-memory keyring fixture — moved to conftest.py so daemon-method tests
# (e.g. test_daemon_delete_definition.py) can share it without duplication.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Plugin operations — delegation to handle_request
# ---------------------------------------------------------------------------

def test_plugin_run_unknown(collect_home):
    """Unknown plugin id on /run returns 404 — matches the contract /enable
    /disable routes' behaviour. Routes that delegate to handle_request used
    to return 200 with {ok: False} (task #83); now they raise HTTP 404."""
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.post("/api/plugin/no-such/run")
    assert r.status_code == 404
    assert "no-such" in r.json()["detail"]


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
    """Unknown plugin id on /credentials returns 404 — task #83."""
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/plugin/no-such/credentials")
    assert r.status_code == 404
    assert "no-such" in r.json()["detail"]


def test_credentials_known_plugin(collect_home, _in_memory_keyring):
    from fulcra_collect.plugin import Plugin, Credential
    plugin = Plugin(
        id="svc",
        name="Service",
        kind="manual",
        collect_mode="historical",
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
        collect_mode="historical",
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
        id="rss", name="RSS", kind="manual", collect_mode="historical", run=lambda c: None,
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
        collect_mode="live_polled",
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


def test_settings_and_credentials_round_trip_for_reconfigure(collect_home, _in_memory_keyring):
    """Regression for #48 — wizard pre-fill on re-configure.

    The wizard fetches GET /settings and GET /credentials when it mounts so
    that a returning user sees their existing settings pre-filled and gets a
    "currently set — leave blank to keep" affordance for credentials.  This
    test verifies the routes return the correct shapes that the frontend
    destructuring relies on:

      GET /settings  → flat {key: value} dict (same shape accepted by PUT)
      GET /credentials → {ok: true, credentials: {key: "set"|"missing"}}
    """
    from fulcra_collect.plugin import Plugin, Setting, Credential
    plugin = Plugin(
        id="rss",
        name="RSS",
        kind="manual",
        collect_mode="historical",
        run=lambda c: None,
        required_settings=(
            Setting(key="feed_url", label="Feed URL", kind="url"),
        ),
        required_credentials=(
            Credential(key="api_key", label="API Key", help=""),
        ),
    )
    daemon = _build_test_daemon(collect_home, plugins={"rss": plugin})
    client = _client(daemon)

    # --- settings round-trip ---
    # Before any PUT: GET returns an empty dict (plugin not yet configured).
    r = client.get("/api/plugin/rss/settings")
    assert r.status_code == 200
    assert r.json() == {}

    # PUT a setting value.
    r = client.put("/api/plugin/rss/settings", json={"feed_url": "https://example.com/feed.xml"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # GET returns the same flat dict the wizard will consume to pre-fill inputValues.
    r = client.get("/api/plugin/rss/settings")
    assert r.status_code == 200
    assert r.json() == {"feed_url": "https://example.com/feed.xml"}

    # --- credentials round-trip ---
    # Before set: credential shows as "missing".
    r = client.get("/api/plugin/rss/credentials")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["credentials"]["api_key"] == "missing"

    # After set: credential shows as "set" — wizard marks _credPresent[key]=true.
    r = client.put("/api/plugin/rss/credential/api_key", json={"secret": "tok3n"})
    assert r.status_code == 200
    r = client.get("/api/plugin/rss/credentials")
    body = r.json()
    assert body["ok"] is True
    assert body["credentials"]["api_key"] == "set"


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
    plugin = Plugin(id="x", name="X", kind="manual", collect_mode="historical", run=lambda c: None)
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
        collect_mode="live_polled",
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
    plugin = Plugin(id="m", name="M", kind="manual", collect_mode="historical", run=lambda c: None)
    daemon = _build_test_daemon(collect_home, plugins={"m": plugin})
    client = _client(daemon)
    r = client.get("/api/plugin/m/contract")
    assert r.status_code == 200
    assert r.json()["default_interval_s"] is None


def test_plugin_contract_health_check_available(collect_home):
    from fulcra_collect.plugin import Plugin, HealthResult
    plugin = Plugin(
        id="h", name="H", kind="manual", collect_mode="historical", run=lambda c: None,
        health_check=lambda ctx: HealthResult(ok=True, summary="ok"),
    )
    daemon = _build_test_daemon(collect_home, plugins={"h": plugin})
    client = _client(daemon)
    r = client.get("/api/plugin/h/contract")
    assert r.json()["health_check_available"] is True


def test_plugin_contract_enum_setting_values(collect_home):
    from fulcra_collect.plugin import Plugin, Setting
    plugin = Plugin(
        id="e", name="E", kind="manual", collect_mode="historical", run=lambda c: None,
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
    # When enum_labels is omitted, the contract still ships the key as
    # null so the frontend can branch deterministically.
    assert s["enum_labels"] is None
    assert s["required"] is False


def test_plugin_contract_enum_labels_round_trip(collect_home):
    """Optional enum_labels survive the contract serializer in declaration
    order — that's how the frontend pairs label[i] with value[i] for each
    <option>. Regression for the 2026-05-25 Day One mode-picker fix that
    replaced raw `live_app` / `export_file` tokens with human labels.
    """
    from fulcra_collect.plugin import Plugin, Setting
    plugin = Plugin(
        id="e2", name="E2", kind="manual", collect_mode="historical", run=lambda c: None,
        required_settings=(
            Setting(
                key="mode", label="Mode", kind="enum",
                enum_values=("live_app", "export_file"),
                enum_labels=("Live app (continuous)", "Export file (one-shot)"),
            ),
        ),
    )
    daemon = _build_test_daemon(collect_home, plugins={"e2": plugin})
    client = _client(daemon)
    r = client.get("/api/plugin/e2/contract")
    s = r.json()["required_settings"][0]
    assert s["enum_values"] == ["live_app", "export_file"]
    assert s["enum_labels"] == ["Live app (continuous)", "Export file (one-shot)"]


def test_plugin_contract_setup_step_condition_round_trip(collect_home):
    """SetupStep.condition survives the contract serializer as a JSON object
    with list values (tuples become arrays in JSON). None-condition steps
    serialize as null so the frontend can branch deterministically."""
    from fulcra_collect.plugin import Plugin, SetupStep
    plugin = Plugin(
        id="cond", name="Cond", kind="manual", collect_mode="historical", run=lambda c: None,
        setup_steps=(
            SetupStep(kind="intro", title="Intro"),
            SetupStep(kind="file_upload", title="Upload",
                      condition={"mode": ("export_file",)}),
        ),
    )
    daemon = _build_test_daemon(collect_home, plugins={"cond": plugin})
    client = _client(daemon)
    r = client.get("/api/plugin/cond/contract")
    assert r.status_code == 200
    steps = r.json()["setup_steps"]
    # Unconditional step → null
    assert steps[0]["condition"] is None
    # Conditional step → dict with list values
    assert steps[1]["condition"] == {"mode": ["export_file"]}


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
    plugin = Plugin(id="x", name="X", kind="manual", collect_mode="historical", run=lambda c: None)
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

    plugin = Plugin(id="x", name="X", kind="manual", collect_mode="historical", run=lambda c: None,
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

    plugin = Plugin(id="x", name="X", kind="manual", collect_mode="historical", run=lambda c: None,
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
    plugin = Plugin(id="x", name="X", kind="manual", collect_mode="historical", run=lambda c: None)
    # plugin has no oauth_handler
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    client = _client(daemon)
    r = client.post("/api/oauth/x/start")
    assert r.status_code == 404


def test_oauth_start_returns_state_and_challenge(collect_home):
    from fulcra_collect.plugin import Plugin
    plugin = Plugin(id="x", name="X", kind="manual", collect_mode="historical", run=lambda c: None,
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

    plugin = Plugin(id="x", name="X", kind="manual", collect_mode="historical", run=lambda c: None,
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
    plugin = Plugin(id="x", name="X", kind="manual", collect_mode="historical", run=lambda c: None,
                    oauth_handler=lambda **kw: {"access_token": "abc"})
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    client = _client(daemon)
    r = client.get("/api/oauth/x/callback?code=AUTH&state=never-issued")
    assert r.status_code == 400


def test_oauth_callback_handles_handler_exception_gracefully(collect_home, _in_memory_keyring):
    """When the plugin's oauth_handler raises (e.g. Trakt returns 400
    for the code exchange), the callback returns an HTML failure page
    with status 500 — not a stack trace."""
    from fulcra_collect.plugin import Plugin

    def boom(*, plugin_id, code, code_verifier, redirect_uri):
        raise RuntimeError("Trakt rejected the code: invalid_grant")

    plugin = Plugin(id="x", name="X", kind="manual", collect_mode="historical", run=lambda c: None,
                    oauth_handler=boom)
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    daemon._web_url = "http://127.0.0.1:9999"
    client = _client(daemon)
    start = client.post("/api/oauth/x/start").json()
    r = client.get(f"/api/oauth/x/callback?code=BAD&state={start['state']}")
    assert r.status_code == 500
    # The HTML response should contain the failure copy, not the raw stack trace
    body = r.text
    assert "token exchange failed" in body
    # Specifically NOT the Python exception class name or traceback
    assert "RuntimeError" not in body or "Traceback" not in body


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


def test_definitions_route_ignores_annotation_type_param(collect_home, _in_memory_keyring, monkeypatch):
    """?annotation_type=moment no longer filters — all non-deleted defs are returned.

    Server-side filtering was removed so the frontend can group compatible vs.
    other-type annotations itself. The query param is accepted for backwards
    compatibility but has no effect.
    """
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

    # With annotation_type param — both defs must come back (no server filter).
    r = client.get("/api/definitions?annotation_type=moment")
    assert r.status_code == 200
    defs = r.json()["definitions"]
    assert len(defs) == 2, "annotation_type param must not filter server-side"
    ids = {d["id"] for d in defs}
    assert ids == {"dur-1", "mom-1"}

    # Without the param — same result.
    r2 = client.get("/api/definitions")
    assert r2.status_code == 200
    assert len(r2.json()["definitions"]) == 2


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


def test_definition_recent_handles_null_metadata(collect_home, _in_memory_keyring, monkeypatch):
    """Records with metadata=None must not raise AttributeError (bug #45).

    The Fulcra API sometimes returns records where the ``metadata`` key is
    present but its value is ``None`` rather than absent.  The old filter used
    ``rec.get("metadata", {}).get("source")``, which evaluates to
    ``None.get(...)`` when metadata is explicitly null — crashing with
    AttributeError.  The fix is ``(rec.get("metadata") or {}).get("source")``.
    """
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("bearer-token", "valid-token")

    def_id = "def-null-meta"
    def_source = f"com.fulcradynamics.annotation.{def_id}"

    fake_records = [
        # metadata key present but value is None — triggered the bug
        {"metadata": None, "source_id": "something-else"},
        # normal match via source_id
        {"metadata": None, "source_id": def_source},
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
    # Must not return 502 from an AttributeError crash
    assert r.status_code == 200
    entries = r.json()["entries"]
    # The record matched by source_id should be returned
    assert len(entries) == 1
    assert entries[0]["source_id"] == def_source


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
    plugin = Plugin(id="x", name="X", kind="manual", collect_mode="historical", run=lambda c: None)
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    client = _client(daemon)
    # Body has neither definition_id nor force_new
    r = client.post("/api/plugin/x/definition", json={})
    assert r.status_code == 400


def test_bind_definition_stores_id(collect_home):
    from fulcra_collect.plugin import Plugin
    from fulcra_collect import state as _state_mod
    plugin = Plugin(id="x", name="X", kind="manual", collect_mode="historical", run=lambda c: None)
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
    plugin = Plugin(id="x", name="X", kind="manual", collect_mode="historical", run=lambda c: None)
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
    # No custom name supplied → override stays unset (resolver falls back
    # to canonical_name + machine suffix).
    assert st2.override_definition_name is None


def test_bind_definition_force_new_with_custom_name_persists_override(collect_home):
    """Task #47 regression: when the user types a custom name in the
    "Create new" input, the daemon persists it on plugin state so the
    next run's resolver uses it verbatim instead of canonical_name."""
    from fulcra_collect.plugin import Plugin
    from fulcra_collect import state as _state_mod
    plugin = Plugin(id="x", name="X", kind="manual", collect_mode="historical", run=lambda c: None)
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    client = _client(daemon)
    r = client.post(
        "/api/plugin/x/definition",
        json={"force_new": True, "new_name": "My Custom Listened"},
    )
    assert r.status_code == 200
    st = _state_mod.load("x")
    assert st.definition_id is None
    assert st.override_definition_name == "My Custom Listened"


def test_bind_definition_force_new_blank_name_ignored(collect_home):
    """A whitespace-only new_name must not pin a stupid override on state."""
    from fulcra_collect.plugin import Plugin
    from fulcra_collect import state as _state_mod
    plugin = Plugin(id="x", name="X", kind="manual", collect_mode="historical", run=lambda c: None)
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    client = _client(daemon)
    r = client.post(
        "/api/plugin/x/definition",
        json={"force_new": True, "new_name": "   "},
    )
    assert r.status_code == 200
    st = _state_mod.load("x")
    assert st.override_definition_name is None


def test_bind_definition_pick_existing_clears_pending_override(collect_home):
    """If the user previously typed a custom name but then picks an
    existing def instead, the override must be cleared so the next run
    doesn't ignore the picked def in favor of a find-or-create by name."""
    from fulcra_collect.plugin import Plugin
    from fulcra_collect import state as _state_mod
    plugin = Plugin(id="x", name="X", kind="manual", collect_mode="historical", run=lambda c: None)
    st = _state_mod.load("x")
    st.override_definition_name = "stale-typed-name"
    _state_mod.save(st)
    daemon = _build_test_daemon(collect_home, plugins={"x": plugin})
    client = _client(daemon)
    r = client.post("/api/plugin/x/definition", json={"definition_id": "picked-def"})
    assert r.status_code == 200
    st2 = _state_mod.load("x")
    assert st2.definition_id == "picked-def"
    assert st2.override_definition_name is None


def test_clear_definition_unknown_plugin(collect_home):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.delete("/api/plugin/no-such/definition")
    assert r.status_code == 404


def test_clear_definition_removes_id(collect_home):
    from fulcra_collect.plugin import Plugin
    from fulcra_collect import state as _state_mod
    plugin = Plugin(id="x", name="X", kind="manual", collect_mode="historical", run=lambda c: None)
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
# Task #42 — DELETE /api/definitions/{def_id} (soft-delete a Fulcra def
# and clear any plugin state that was caching that def_id, so the next
# run resolves a fresh one).
# ---------------------------------------------------------------------------

def _patch_fulcra_delete(monkeypatch, *, status_code: int):
    """Patch fulcra_collect.web.httpx so DELETE returns the given status.

    Mirrors the stub style used by test_definitions_route_returns_list so
    the routes under test don't reach the real Fulcra API.
    """
    class _FakeResponse:
        def __init__(self, code):
            self.status_code = code
        def raise_for_status(self):
            if self.status_code >= 400:
                import httpx as _h
                req = _h.Request("DELETE", "http://test")
                raise _h.HTTPStatusError(
                    f"{self.status_code}",
                    request=req,
                    response=_h.Response(self.status_code, request=req),
                )

    class _FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def delete(self, path, **kw):  # noqa: ARG002
            return _FakeResponse(status_code)

    monkeypatch.setattr(
        "fulcra_collect.web.httpx",
        type("httpx", (), {"Client": _FakeClient})(),
    )


def test_delete_definition_requires_fulcra_auth(collect_home, _in_memory_keyring):
    """Without a stored Fulcra bearer-token the route returns 401."""
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.delete("/api/definitions/def-x")
    assert r.status_code == 401


def test_delete_definition_happy_path_clears_bound_plugin_state(
    collect_home, _in_memory_keyring, monkeypatch,
):
    """A 204 from Fulcra returns ok and clears the cached def on any
    plugin that was bound to it."""
    import fulcra_collect.credentials as _creds_mod
    from fulcra_collect.plugin import Plugin
    from fulcra_collect import state as _state_mod

    _creds_mod.set_user_secret("bearer-token", "valid-token")
    _patch_fulcra_delete(monkeypatch, status_code=204)

    # Plugin bound to the def we're about to delete.
    plugin = Plugin(id="bound-plugin", name="Bound", kind="manual",
                    collect_mode="historical",
                    run=lambda c: None)
    st = _state_mod.load("bound-plugin")
    st.definition_id = "def-to-delete"
    _state_mod.save(st)

    daemon = _build_test_daemon(collect_home, plugins={"bound-plugin": plugin})
    client = _client(daemon)
    r = client.delete("/api/definitions/def-to-delete")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "bound-plugin" in body["cleared_plugins"]

    # State must be cleared so the plugin's next run resolves a fresh def
    # instead of trying to write to the now-tombstoned id.
    st2 = _state_mod.load("bound-plugin")
    assert st2.definition_id is None


def test_delete_definition_returns_404_when_already_deleted(
    collect_home, _in_memory_keyring, monkeypatch,
):
    """Fulcra responding 404 (already deleted / unknown id) must surface as 404."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("bearer-token", "valid-token")
    _patch_fulcra_delete(monkeypatch, status_code=404)

    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.delete("/api/definitions/def-gone")
    assert r.status_code == 404


def test_delete_definition_leaves_other_plugins_state_alone(
    collect_home, _in_memory_keyring, monkeypatch,
):
    """Only plugins bound to the deleted def are cleared — others are untouched."""
    import fulcra_collect.credentials as _creds_mod
    from fulcra_collect.plugin import Plugin
    from fulcra_collect import state as _state_mod

    _creds_mod.set_user_secret("bearer-token", "valid-token")
    _patch_fulcra_delete(monkeypatch, status_code=204)

    p1 = Plugin(id="p-keeps", name="Keeps", kind="manual", collect_mode="historical", run=lambda c: None)
    p2 = Plugin(id="p-loses", name="Loses", kind="manual", collect_mode="historical", run=lambda c: None)
    st1 = _state_mod.load("p-keeps")
    st1.definition_id = "def-Y"
    _state_mod.save(st1)
    st2 = _state_mod.load("p-loses")
    st2.definition_id = "def-X"
    _state_mod.save(st2)

    daemon = _build_test_daemon(
        collect_home, plugins={"p-keeps": p1, "p-loses": p2},
    )
    client = _client(daemon)
    r = client.delete("/api/definitions/def-X")
    assert r.status_code == 200
    body = r.json()
    assert body["cleared_plugins"] == ["p-loses"]

    # p-loses's bound def is gone, p-keeps's untouched.
    assert _state_mod.load("p-loses").definition_id is None
    assert _state_mod.load("p-keeps").definition_id == "def-Y"


# ---------------------------------------------------------------------------
# Phase G — quick-record HTTP routes
#
# The fake-httpx seam (install_fake_httpx, with its get_data / post_exc
# convenience) is shared with test_daemon.py and lives in conftest.py.
# ---------------------------------------------------------------------------


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


def test_quick_record_definitions_returns_all_types(
        collect_home, _in_memory_keyring, monkeypatch):
    """Route returns ALL non-deleted defs (Sprint B widened this from
    Moment-only); menubar groups by annotation_type client-side."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("bearer-token", "valid-token")

    fake_defs = [
        {"id": "m1", "name": "Coffee", "annotation_type": "moment",
         "deleted_at": None, "created_at": "2026-05-10T00:00:00Z"},
        {"id": "dur1", "name": "Work", "annotation_type": "duration",
         "deleted_at": None, "created_at": "2026-05-09T00:00:00Z"},
    ]
    install_fake_httpx(monkeypatch, get_data=fake_defs)

    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/quick-record/definitions")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # Both annotation types come back — duration is no longer filtered.
    types = {d["annotation_type"] for d in body["definitions"]}
    assert types == {"moment", "duration"}


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
    """Route calls Fulcra POST and returns ok=True. Activity buffer is updated.

    The daemon's `_record_annotation` warms the quick-record cache via the
    same GET /user/v1alpha1/annotation it uses for the popover list — so the
    fake httpx layer needs to return a matching def for the lookup to
    succeed. Without this, every record_annotation call short-circuits
    with 'unknown definition id ...'.
    """
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("bearer-token", "valid-token")
    install_fake_httpx(monkeypatch, get_data=[
        {"id": "def-abcdef12", "name": "Test Moment",
         "annotation_type": "moment", "tags": ["t-1"],
         "created_at": "2026-05-25T00:00:00Z", "deleted_at": None},
    ])

    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.post("/api/annotations",
                    json={"definition_id": "def-abcdef12", "comment": None})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # Sprint B: source_id and name come back so the menubar can stash
    # them in its "Recently recorded" list for undo.
    assert "source_id" in body
    assert body["name"] == "Test Moment"
    # Activity buffer has one success entry
    entries = daemon.activity.recent(limit=1)
    assert entries[0].ok is True
    assert entries[0].plugin_id == "quick-record"


def test_record_annotation_api_failure_returns_error(
        collect_home, _in_memory_keyring, monkeypatch):
    """Route returns ok=False and surfaces activity entry on Fulcra API failure."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("bearer-token", "valid-token")
    # Cache-warm GET succeeds (returns a matching def) so the test reaches
    # the POST path; the POST raises and we assert on that error surface.
    install_fake_httpx(monkeypatch,
                            get_data=[{"id": "def-xyz", "name": "X",
                                       "annotation_type": "moment",
                                       "tags": [], "deleted_at": None,
                                       "created_at": "2026-05-25T00:00:00Z"}],
                            post_exc=RuntimeError("connection refused"))

    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.post("/api/annotations", json={"definition_id": "def-xyz"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "Fulcra" in body["error"]
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


def test_record_annotation_duration_round_trips(
        collect_home, _in_memory_keyring, monkeypatch):
    """Sprint B: POST /api/annotations with start_time + end_time writes
    a Duration record through the daemon."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("bearer-token", "valid-token")
    install_fake_httpx(monkeypatch, get_data=[
        {"id": "def-d1", "name": "Movie", "annotation_type": "duration",
         "tags": [], "deleted_at": None,
         "created_at": "2026-05-25T00:00:00Z"},
    ])

    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.post("/api/annotations", json={
        "definition_id": "def-d1",
        "start_time": "2026-05-26T20:00:00Z",
        "end_time": "2026-05-26T22:14:00Z",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "source_id" in body


def test_delete_annotation_requires_auth(collect_home):
    """DELETE /api/annotations/{source_id} requires the web Bearer token."""
    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.delete("/api/annotations/src-x")
    assert r.status_code == 401


def test_delete_annotation_writes_tombstone(
        collect_home, _in_memory_keyring, monkeypatch):
    """DELETE /api/annotations/{source_id} posts a sentinel annotation
    so the user has a paper trail. Soft-delete only — see daemon
    docstring."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("bearer-token", "valid-token")
    install_fake_httpx(monkeypatch, get_data=[])

    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.delete(
        "/api/annotations/"
        "com.fulcradynamics.fulcra-collect.quick-record.abc123"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["tombstone_source_id"].startswith(
        "com.fulcradynamics.fulcra-collect.quick-record.undo."
    )


# ---------------------------------------------------------------------------
# Phase 2 — POST /api/extension/attention (browser-extension ingest)
# ---------------------------------------------------------------------------

def _valid_attention_payload() -> dict:
    """A minimal attention payload that passes the relay's _validate()
    schema check. Used across the extension-route tests below."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "url": "https://example.com/article",
        "title": "Test",
        "category": None,
        "chrome_identity": None,
        "og_type": None,
        "lang": None,
        "start_time": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "end_time":   now.isoformat().replace("+00:00", "Z"),
        "client":     "fulcra-attention-chrome/0.1.0",
    }


def test_extension_attention_requires_extension_token_configured(
        collect_home, _in_memory_keyring):
    """No extension-token in keychain → 401, never 500."""
    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.post(
        "/api/extension/attention",
        json=_valid_attention_payload(),
        headers={"Authorization": "Bearer some-token"},
    )
    assert r.status_code == 401


def test_extension_attention_rejects_wrong_token(collect_home, _in_memory_keyring):
    """A wrong bearer token returns 401."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("extension-token", "the-right-one")

    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.post(
        "/api/extension/attention",
        json=_valid_attention_payload(),
        headers={"Authorization": "Bearer the-wrong-one"},
    )
    assert r.status_code == 401


def test_extension_attention_rejects_malformed_json(collect_home, _in_memory_keyring):
    """A request body that isn't valid JSON returns 400, not 500."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("extension-token", "the-right-one")

    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.post(
        "/api/extension/attention",
        content=b"not-json-{{",
        headers={
            "Authorization": "Bearer the-right-one",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 400


def test_extension_attention_rejects_bad_payload_schema(collect_home, _in_memory_keyring):
    """A valid-JSON-but-schema-violating body returns 400, not 500."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("extension-token", "the-right-one")

    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.post(
        "/api/extension/attention",
        json={"definitely": "not the right shape"},
        headers={"Authorization": "Bearer the-right-one"},
    )
    assert r.status_code == 400


def test_extension_attention_happy_path_calls_ingest(
        collect_home, _in_memory_keyring, monkeypatch, tmp_path):
    """A well-formed payload + valid token + bound definition → 200 and
    the attention FulcraClient.ingest_batch gets called once."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("extension-token", "the-right-one")

    # Patch the attention state loader to return a fully-configured State
    # so we don't read the real on-disk attention state file. Save is
    # also stubbed so a watermark write doesn't touch ~/.config either.
    from fulcra_attention.state import State
    fake_state = State(
        attention_definition_id="def-att",
        tag_ids={"attention": "a", "web": "w"},
    )
    monkeypatch.setattr(
        "fulcra_attention.state.load", lambda *a, **kw: fake_state,
    )
    monkeypatch.setattr(
        "fulcra_attention.state.save", lambda *a, **kw: None,
    )

    # Capture ingest calls without hitting the real Fulcra API. Refactor
    # #69 moved attention's POST through IngestPipeline, so we patch the
    # pipeline that the extension route imports. Each `ingest_one` call
    # appends the typed event (so tests can still assert "one event was
    # posted" without depending on the wire-format dict).
    ingest_calls: list[list] = []

    class _FakeIngestPipeline:
        def __init__(self, client=None): pass
        def ingest_one(self, event):
            ingest_calls.append([event])
        def ingest_batch(self, events):
            ingest_calls.append(list(events))

    monkeypatch.setattr(
        "fulcra_collect.routes.extension.IngestPipeline", _FakeIngestPipeline,
    )

    class _FakeFulcraClient:
        def __init__(self, *a, **kw): pass
        def ensure_tag(self, name, state): return "tag-stub"
        def definition_exists(self, def_id): return True

    monkeypatch.setattr(
        "fulcra_attention.fulcra.FulcraClient", _FakeFulcraClient,
    )

    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.post(
        "/api/extension/attention",
        json=_valid_attention_payload(),
        headers={"Authorization": "Bearer the-right-one"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"posted": 1, "dropped": 0}
    assert len(ingest_calls) == 1
    assert len(ingest_calls[0]) == 1


def test_extension_attention_throttles_activity_feed_entries(
        collect_home, _in_memory_keyring, monkeypatch, tmp_path):
    """Extension POSTs are coalesced into the dashboard activity feed at
    most once per ATTENTION_ACTIVITY_INTERVAL_S — otherwise an active
    user's per-tab focus events would saturate the 50-entry ring within
    minutes and hide every other plugin's history.

    Until the throttle window elapses we collect counts + client names
    but emit zero entries. On the next POST past the window we flush one
    entry summarising what accumulated.
    """
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("extension-token", "the-right-one")

    from fulcra_attention.state import State
    fake_state = State(
        attention_definition_id="def-att",
        tag_ids={"attention": "a", "web": "w"},
    )
    monkeypatch.setattr("fulcra_attention.state.load",
                        lambda *a, **kw: fake_state)
    monkeypatch.setattr("fulcra_attention.state.save",
                        lambda *a, **kw: None)

    class _FakeFulcraClient:
        def __init__(self, *a, **kw): pass
        def ensure_tag(self, name, state): return "tag-stub"
        def definition_exists(self, def_id): return True
    class _NoopPipeline:
        def __init__(self, client=None): pass
        def ingest_one(self, event): pass
        def ingest_batch(self, events): pass
    monkeypatch.setattr("fulcra_attention.fulcra.FulcraClient",
                        _FakeFulcraClient)
    monkeypatch.setattr(
        "fulcra_collect.routes.extension.IngestPipeline", _NoopPipeline,
    )

    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    from fastapi.testclient import TestClient
    c = TestClient(app)

    # Inject a fake monotonic clock on the daemon. Use a per-POST
    # snapshot helper so the test stays robust if the route's internals
    # ever call monotonic more than once per request.
    pending: list[float] = [0.0, 1.0, 2.0, 70.0]
    state = {"current": pending.pop(0)}
    def _advance_clock() -> float:
        # Called by note_attention_event AND the stale-def validation
        # check. Both want the same "logical moment" reading per POST.
        return state["current"]
    daemon._monotonic = _advance_clock

    # Each POST below sets state["current"] to the next pending value.

    for _ in range(2):
        r = c.post("/api/extension/attention",
                   json=_valid_attention_payload(),
                   headers={"Authorization": "Bearer the-right-one"})
        assert r.status_code == 200, r.text
        state["current"] = pending.pop(0)

    # After 3 POSTs inside the 60s window, exactly one activity entry
    # should be present (the first POST at t=0 vs last_at=-inf → fires).
    r = c.post("/api/extension/attention",
               json=_valid_attention_payload(),
               headers={"Authorization": "Bearer the-right-one"})
    assert r.status_code == 200, r.text
    entries = daemon.activity.recent()
    attention_entries = [e for e in entries if e.plugin_id == "attention-relay"]
    assert len(attention_entries) == 1
    assert attention_entries[0].ok is True
    assert "Attention:" in attention_entries[0].summary
    assert "fulcra-attention-chrome/0.1.0" in attention_entries[0].summary

    # 4th POST at t=70s is past the 60s throttle → flush a second entry
    # whose count reflects events accumulated since the last flush (2
    # suppressed at t=1 and t=2, plus this 4th POST itself).
    state["current"] = pending.pop(0)
    r = c.post("/api/extension/attention",
               json=_valid_attention_payload(),
               headers={"Authorization": "Bearer the-right-one"})
    assert r.status_code == 200, r.text
    entries = daemon.activity.recent()
    attention_entries = [e for e in entries if e.plugin_id == "attention-relay"]
    assert len(attention_entries) == 2
    # Summary should mention the count "3 events" — 2 suppressed + 1 flush.
    assert "3 event" in attention_entries[0].summary


def test_extension_attention_recovers_from_orphan_definition(
        collect_home, _in_memory_keyring, monkeypatch, tmp_path):
    """When the cached attention_definition_id no longer exists on the
    current Fulcra account (the daemon got re-authed to a different
    account, or the def was deleted), the route detects the orphan,
    clears the stale state, re-resolves a fresh def, persists, and
    surfaces a one-line activity entry explaining what happened.

    Regression for the 2026-05-25 silent-orphan bug: ingest returned 200
    but events were invisible in the timeline because they pointed at a
    def that didn't exist on the new account.
    """
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("extension-token", "the-right-one")

    from fulcra_attention.state import State
    fake_state = State(
        attention_definition_id="orphan-from-account-A",
        tag_ids={"attention": "tag-from-A", "web": "tag-from-A-2"},
    )
    saved_states: list[State] = []
    monkeypatch.setattr("fulcra_attention.state.load",
                        lambda *a, **kw: fake_state)
    monkeypatch.setattr("fulcra_attention.state.save",
                        lambda s, *a, **kw: saved_states.append(s))

    ingest_calls: list[list] = []

    class _FakeFulcraClient:
        def __init__(self, *a, **kw): pass
        def ensure_tag(self, name, state):
            state.tag_ids.setdefault(name, f"new-tag-{name}")
            return state.tag_ids[name]
        def definition_exists(self, def_id):
            # Simulate the orphan case: stored def isn't on this account.
            return def_id != "orphan-from-account-A"
        def ensure_definitions(self, state):
            # Adopt-or-create resolves to a fresh local def id, plus seeds
            # the canonical tags.
            state.attention_definition_id = "fresh-def-on-account-B"
            state.tag_ids.setdefault("attention", "fresh-attention-tag")
            state.tag_ids.setdefault("web", "fresh-web-tag")

    class _CapturingPipeline:
        def __init__(self, client=None): pass
        def ingest_one(self, event):
            ingest_calls.append([event])
        def ingest_batch(self, events):
            ingest_calls.append(list(events))
    monkeypatch.setattr("fulcra_attention.fulcra.FulcraClient",
                        _FakeFulcraClient)
    monkeypatch.setattr(
        "fulcra_collect.routes.extension.IngestPipeline", _CapturingPipeline,
    )

    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.post("/api/extension/attention",
               json=_valid_attention_payload(),
               headers={"Authorization": "Bearer the-right-one"})
    assert r.status_code == 200, r.text

    # State was rebound to the fresh def.
    assert fake_state.attention_definition_id == "fresh-def-on-account-B"
    # Stale tag cache was cleared during recovery and re-seeded.
    assert fake_state.tag_ids.get("attention") == "fresh-attention-tag"
    # State got persisted exactly once during recovery (the watermark save
    # happens too, but that's a separate save() call after).
    assert any(s.attention_definition_id == "fresh-def-on-account-B"
               for s in saved_states)
    # The ingest call was still made — the user's event isn't dropped just
    # because we had to re-resolve first.
    assert len(ingest_calls) == 1
    # The dashboard activity feed surfaces an entry explaining the recovery
    # (so the user can tell from the UI why their data suddenly has a new
    # def association).
    entries = daemon.activity.recent()
    recovery = [e for e in entries
                if "re-resolved" in e.summary
                and e.plugin_id == "attention-relay"]
    assert len(recovery) == 1
    assert "orphan-" in recovery[0].summary
    assert "fresh-de" in recovery[0].summary


def test_extension_attention_validates_def_only_once_per_window(
        collect_home, _in_memory_keyring, monkeypatch, tmp_path):
    """`definition_exists` is the only thing that gates ingest; calling
    it on every POST would double the HTTP round-trip cost per attention
    event. The route caches the validation result for
    _attention_validation_interval_s and re-validates only when the
    interval has elapsed (or the cached def_id changes).
    """
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("extension-token", "the-right-one")

    from fulcra_attention.state import State
    fake_state = State(
        attention_definition_id="def-att",
        tag_ids={"attention": "a", "web": "w"},
    )
    monkeypatch.setattr("fulcra_attention.state.load",
                        lambda *a, **kw: fake_state)
    monkeypatch.setattr("fulcra_attention.state.save",
                        lambda *a, **kw: None)

    exists_calls: list[str] = []
    class _FakeFulcraClient:
        def __init__(self, *a, **kw): pass
        def ensure_tag(self, name, state): return "tag"
        def definition_exists(self, def_id):
            exists_calls.append(def_id)
            return True
    class _NoopPipeline:
        def __init__(self, client=None): pass
        def ingest_one(self, event): pass
        def ingest_batch(self, events): pass
    monkeypatch.setattr("fulcra_attention.fulcra.FulcraClient",
                        _FakeFulcraClient)
    monkeypatch.setattr(
        "fulcra_collect.routes.extension.IngestPipeline", _NoopPipeline,
    )

    daemon = _build_test_daemon(collect_home)
    # Inject a per-POST clock — see the throttle test for the same pattern.
    pending: list[float] = [0.0, 1.0, 2.0, 1000.0]
    clock_state = {"current": pending.pop(0)}
    daemon._monotonic = lambda: clock_state["current"]

    app = build_app(daemon)
    from fastapi.testclient import TestClient
    c = TestClient(app)

    # 3 POSTs inside the 300s window → 1 validation only (the first).
    for _ in range(2):
        r = c.post("/api/extension/attention",
                   json=_valid_attention_payload(),
                   headers={"Authorization": "Bearer the-right-one"})
        assert r.status_code == 200, r.text
        clock_state["current"] = pending.pop(0)
    r = c.post("/api/extension/attention",
               json=_valid_attention_payload(),
               headers={"Authorization": "Bearer the-right-one"})
    assert r.status_code == 200, r.text
    assert exists_calls == ["def-att"]

    # 4th POST past the 300s window → second validation.
    clock_state["current"] = pending.pop(0)
    r = c.post("/api/extension/attention",
               json=_valid_attention_payload(),
               headers={"Authorization": "Bearer the-right-one"})
    assert r.status_code == 200, r.text
    assert exists_calls == ["def-att", "def-att"]


def test_extension_attention_falls_back_to_per_plugin_state(
        collect_home, _in_memory_keyring, monkeypatch, tmp_path):
    """Regression for task #29. When the user picks a def in the wizard's
    definition_picker step the daemon writes it to per-plugin state
    (state/attention-relay.json). The attention extension reads from a
    different file (~/.config/fulcra-attention/state.json) so the picker
    write isn't visible without a fallback. This test asserts the
    fallback works: extension POST sees None in per-package state,
    consults per-plugin state, and lazy-migrates."""
    import fulcra_collect.credentials as _creds_mod
    import fulcra_collect.state as _collect_state_mod
    _creds_mod.set_user_secret("extension-token", "the-right-one")

    # Per-plugin state has the def id (as if the wizard wrote it).
    relay_state = _collect_state_mod.PluginState(
        plugin_id="attention-relay", definition_id="def-from-wizard",
    )
    _collect_state_mod.save(relay_state)

    # Per-package state is fresh / empty (as if the user just paired).
    from fulcra_attention.state import State
    empty = State()
    saved_states: list[State] = []
    monkeypatch.setattr("fulcra_attention.state.load",
                        lambda *a, **kw: empty)
    monkeypatch.setattr("fulcra_attention.state.save",
                        lambda s, *a, **kw: saved_states.append(s))

    class _FakeFulcraClient:
        def __init__(self, *a, **kw): pass
        def ensure_tag(self, name, state):
            state.tag_ids.setdefault(name, f"tag-{name}")
            return state.tag_ids[name]
        def ensure_definitions(self, state):
            # Seed the base tags build_attention_event needs.
            state.tag_ids.setdefault("attention", "tag-attention")
            state.tag_ids.setdefault("web", "tag-web")
        def definition_exists(self, def_id): return True
    class _NoopPipeline:
        def __init__(self, client=None): pass
        def ingest_one(self, event): pass
        def ingest_batch(self, events): pass
    monkeypatch.setattr("fulcra_attention.fulcra.FulcraClient",
                        _FakeFulcraClient)
    monkeypatch.setattr(
        "fulcra_collect.routes.extension.IngestPipeline", _NoopPipeline,
    )

    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.post("/api/extension/attention",
               json=_valid_attention_payload(),
               headers={"Authorization": "Bearer the-right-one"})
    assert r.status_code == 200, r.text
    assert r.json() == {"posted": 1, "dropped": 0}
    # Lazy-migrate wrote the fallback id AND seeded the base tags.
    assert any(s.attention_definition_id == "def-from-wizard"
               for s in saved_states), (
        "extension route must lazy-migrate the per-plugin fallback "
        "into per-package state on first ingest"
    )
    assert empty.tag_ids.get("attention") == "tag-attention", (
        "ensure_definitions must seed the base tags during lazy-migrate "
        "so build_attention_event doesn't KeyError"
    )


def test_extension_attention_missing_definition_returns_412(
        collect_home, _in_memory_keyring, monkeypatch, tmp_path):
    """Token valid, payload valid, but no Attention definition bound →
    412 so the extension can show a setup-needed error rather than
    pretend it worked."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("extension-token", "the-right-one")

    # Patch the attention state loader directly so we don't depend on
    # the on-disk attention state file (which on the dev box may hold a
    # real definition). state.load() takes an optional path keyword that
    # defaults to DEFAULT_PATH at function-def time, so monkeypatching
    # DEFAULT_PATH alone is not enough — patch the loader.
    from fulcra_attention.state import State
    monkeypatch.setattr(
        "fulcra_attention.state.load", lambda *a, **kw: State(),
    )

    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.post(
        "/api/extension/attention",
        json=_valid_attention_payload(),
        headers={"Authorization": "Bearer the-right-one"},
    )
    assert r.status_code == 412


# ---------------------------------------------------------------------------
# POST /api/plugin/attention-relay/pair — one-click extension pairing
# ---------------------------------------------------------------------------

def test_attention_pair_requires_auth(collect_home):
    """Unauthenticated callers can't generate a pair token."""
    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    c = TestClient(app)
    r = c.post("/api/plugin/attention-relay/pair")
    assert r.status_code == 401


def test_attention_pair_returns_token_and_url(collect_home, _in_memory_keyring):
    """Happy path: returns {token, daemon_url} and stashes the token in
    the user-level keychain under 'extension-token'."""
    daemon = _build_test_daemon(collect_home)
    c = _client(daemon)
    r = c.post("/api/plugin/attention-relay/pair")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.get("token"), str) and len(body["token"]) >= 32
    assert isinstance(body.get("daemon_url"), str)
    assert body["daemon_url"].startswith("http://127.0.0.1:")

    # The token must be stored in the user-level keychain so the
    # extension's POSTs to /api/extension/attention authenticate against
    # the same value.
    import fulcra_collect.credentials as _creds_mod
    assert _creds_mod.get_user_secret("extension-token") == body["token"]


def test_attention_pair_overwrites_previous_token(
        collect_home, _in_memory_keyring):
    """Re-pair: the second call replaces the first token cleanly so the
    user can re-pair after re-installing the extension."""
    import fulcra_collect.credentials as _creds_mod
    daemon = _build_test_daemon(collect_home)
    c = _client(daemon)
    r1 = c.post("/api/plugin/attention-relay/pair")
    r2 = c.post("/api/plugin/attention-relay/pair")
    assert r1.status_code == 200 and r2.status_code == 200
    t1, t2 = r1.json()["token"], r2.json()["token"]
    assert t1 != t2
    # Only the second token survives in the keychain.
    assert _creds_mod.get_user_secret("extension-token") == t2


# ---------------------------------------------------------------------------
# POST /api/plugin/{id}/upload — multipart file upload for the wizard's
# file_upload step. The previous wizard implementation base64-encoded files
# in the browser and dropped the blob into the setting; this route replaces
# that dance with a real streaming upload that writes the bytes to disk and
# persists the absolute path (which is what plugins' run() actually expect).
# ---------------------------------------------------------------------------

def _upload_plugin(setting_kind: str = "path"):
    """Build a plugin with one Setting of the given kind. Default 'path'
    matches the wizard's file_upload step contract; other kinds let us
    exercise the rejection branch."""
    from fulcra_collect.plugin import Plugin, Setting
    return Plugin(
        id="netflix-test",
        name="Netflix Test",
        kind="manual",
        collect_mode="historical",
        run=lambda c: None,
        required_settings=(
            Setting(key="path", label="Takeout path", kind=setting_kind),
        ),
    )


def test_upload_unknown_plugin_returns_404(collect_home):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.post(
        "/api/plugin/no-such/upload?key=path",
        files={"file": ("x.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 404
    assert "no-such" in r.json()["detail"]


def test_upload_unknown_setting_key_returns_400(collect_home):
    plugin = _upload_plugin()
    daemon = _build_test_daemon(collect_home, plugins={plugin.id: plugin})
    client = _client(daemon)
    r = client.post(
        f"/api/plugin/{plugin.id}/upload?key=not_a_declared_key",
        files={"file": ("x.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 400
    assert "not_a_declared_key" in r.json()["detail"]


def test_upload_setting_with_wrong_kind_returns_400(collect_home):
    """Uploading to a non-path setting (e.g. a free-text URL field) almost
    always indicates a frontend wiring bug — fail loudly rather than
    silently stuffing a path into a URL field."""
    plugin = _upload_plugin(setting_kind="url")
    daemon = _build_test_daemon(collect_home, plugins={plugin.id: plugin})
    client = _client(daemon)
    r = client.post(
        f"/api/plugin/{plugin.id}/upload?key=path",
        files={"file": ("x.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 400
    assert "kind" in r.json()["detail"].lower()


def test_upload_rejects_path_traversal_filename(collect_home):
    """Filenames with .. or path separators must be rejected so a hostile
    client can't escape the per-plugin uploads directory."""
    plugin = _upload_plugin()
    daemon = _build_test_daemon(collect_home, plugins={plugin.id: plugin})
    client = _client(daemon)
    for bad_name in ("../escape.txt", "..", "."):
        r = client.post(
            f"/api/plugin/{plugin.id}/upload?key=path",
            files={"file": (bad_name, b"hello", "text/plain")},
        )
        assert r.status_code == 400, f"name {bad_name!r} should be rejected"
        assert "invalid filename" in r.json()["detail"]


def test_upload_happy_path_writes_file_and_updates_setting(collect_home):
    """End-to-end: POST a small file, verify it lands at the expected
    path with 0600 perms, and verify the plugin's setting now holds the
    absolute path."""
    plugin = _upload_plugin()
    daemon = _build_test_daemon(collect_home, plugins={plugin.id: plugin})
    client = _client(daemon)
    body = b"hello world\n"
    r = client.post(
        f"/api/plugin/{plugin.id}/upload?key=path",
        files={"file": ("netflix-views.csv", body, "text/csv")},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["ok"] is True
    assert payload["size"] == len(body)
    target = collect_home / "uploads" / plugin.id / "netflix-views.csv"
    assert payload["path"] == str(target.resolve())
    assert target.exists()
    assert target.read_bytes() == body
    # 0600 — owner read/write only.
    assert (target.stat().st_mode & 0o777) == 0o600
    # The path is persisted in the plugin's settings.
    cfg = _config.load()
    assert cfg.plugin_settings[plugin.id]["path"] == str(target.resolve())
    # And it's readable through the public GET /settings route.
    r2 = client.get(f"/api/plugin/{plugin.id}/settings")
    assert r2.status_code == 200
    assert r2.json()["path"] == str(target.resolve())


# ---------------------------------------------------------------------------
# Task #51 — in-app docs viewer (GET /api/docs/{name})
# ---------------------------------------------------------------------------

def test_docs_route_returns_markdown_for_existing_doc(collect_home):
    """The "Data sources" link in the dashboard header hits this route to
    fetch docs/how-do-i-get-my-data.md and render it client-side via
    marked. The doc lives in the repo (docs/), so a happy-path test only
    needs to confirm the route locates it and returns its bytes as
    text/markdown."""
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/docs/how-do-i-get-my-data")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/markdown")
    # Spot-check the rendered content is the real doc (not an error page
    # or a stub). The first heading should be present.
    assert "# How do I get my data into Fulcra?" in r.text


def test_docs_route_rejects_unknown_doc(collect_home):
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/docs/no-such-doc-exists")
    assert r.status_code == 404


def test_docs_route_rejects_path_traversal(collect_home):
    """Defence-in-depth: the route's regex limits names to [A-Za-z0-9_-]+,
    so any input containing ../ or a leading slash is a 400 before we
    even resolve the path. FastAPI matches /api/docs/{name} against a
    single path segment, so the literal "../etc/passwd" would 404 at
    the routing layer — these tests pick inputs that DO hit the handler
    so the regex check is what gates them."""
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    # Single-segment input that looks safe to FastAPI's router but our
    # regex should reject.
    r = client.get("/api/docs/foo.md")           # dot disallowed
    assert r.status_code == 400
    r = client.get("/api/docs/foo bar")          # space disallowed
    assert r.status_code == 400
    r = client.get("/api/docs/")                 # empty after trim
    # FastAPI may 404 the empty-name route entirely or 400 it through
    # the handler — accept either as long as it's not a 200.
    assert r.status_code in (400, 404, 405)


def test_docs_route_requires_auth(collect_home):
    """Same auth shape as /api/activity etc. — Bearer token required."""
    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    # New client without the Authorization header
    client = TestClient(app)
    r = client.get("/api/docs/how-do-i-get-my-data")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Task #64 — quick-record favorites HTTP routes
# ---------------------------------------------------------------------------

def test_quick_record_favorites_requires_auth(collect_home):
    """Both GET and PUT on /api/quick-record/favorites require the web
    Bearer token — same shape as every other /api/* route."""
    daemon = _build_test_daemon(collect_home)
    app = build_app(daemon)
    c = TestClient(app)
    assert c.get("/api/quick-record/favorites").status_code == 401
    assert c.put(
        "/api/quick-record/favorites", json={"favorites": []},
    ).status_code == 401


def test_quick_record_favorites_get_returns_empty_initially(collect_home):
    """A fresh install has no favorites file → the GET succeeds with []."""
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.get("/api/quick-record/favorites")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "favorites": []}


def test_quick_record_favorites_put_then_get_round_trips(collect_home):
    """PUT persists the list; the next GET reflects it."""
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.put(
        "/api/quick-record/favorites",
        json={"favorites": ["def-z", "def-a"]},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    r = client.get("/api/quick-record/favorites")
    # Daemon returns favorites sorted for stability.
    assert r.json() == {"ok": True, "favorites": ["def-a", "def-z"]}


def test_quick_record_favorites_put_persists_to_file(collect_home):
    """The PUT actually hits disk under the test's FULCRA_COLLECT_HOME —
    proves the daemon isn't keeping the list in memory only."""
    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    client.put(
        "/api/quick-record/favorites",
        json={"favorites": ["def-1"]},
    )
    from fulcra_collect import quick_record_favorites as _favs
    assert _favs.load() == {"def-1"}


def test_delete_definition_prunes_from_favorites(
        collect_home, _in_memory_keyring, monkeypatch):
    """Soft-deleting a def must also drop it from favorites — otherwise
    the favorites file would accumulate orphan UUIDs the menubar would
    keep trying to surface but Fulcra would no longer return."""
    import fulcra_collect.credentials as _creds_mod
    _creds_mod.set_user_secret("bearer-token", "valid-token")

    # Patch httpx INSIDE web.py (not daemon.py) — the delete route uses
    # the web module's own httpx client factory, not the daemon's.
    import fulcra_collect.web as web_mod

    class _Resp204:
        status_code = 204
        def raise_for_status(self): pass
        def json(self): return {}

    class _WebClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def delete(self, *a, **kw): return _Resp204()

    monkeypatch.setattr(web_mod, "httpx",
                        type("httpx", (), {"Client": _WebClient,
                                            "HTTPStatusError": Exception,
                                            "ConnectError": Exception,
                                            "ConnectTimeout": Exception,
                                            "TimeoutException": Exception,
                                            "HTTPError": Exception})())

    from fulcra_collect import quick_record_favorites as _favs
    _favs.save({"def-pinned", "def-other"})

    daemon = _build_test_daemon(collect_home)
    client = _client(daemon)
    r = client.delete("/api/definitions/def-pinned")
    assert r.status_code == 200

    # def-pinned was removed from favorites; def-other is untouched.
    assert _favs.load() == {"def-other"}
