"""Tests for the daemon's HTTP server."""
from __future__ import annotations

from fastapi.testclient import TestClient

from fulcra_collect import config as _config
from fulcra_collect.daemon import Daemon, Config
from fulcra_collect.registry import RegistryResult
from fulcra_collect.web import build_app, _ensure_token, _web_token_path


def _build_test_daemon(collect_home):
    return Daemon(registry=RegistryResult(), config=Config())


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
