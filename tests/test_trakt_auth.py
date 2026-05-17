import json
import time
from pathlib import Path

import httpx
import pytest

from fulcra_media.importers.trakt import (
    TraktAuth, load_creds, save_creds,
)


def test_load_creds_returns_dict(tmp_path: Path, mocker):
    p = tmp_path / "trakt.json"
    p.write_text(json.dumps({"client_id": "cid", "client_secret": "csec", "access_token": "at", "refresh_token": "rt", "expires_in": 86400, "created_at": 9999}))
    mocker.patch("fulcra_media.importers.trakt.CREDS_PATH", p)
    c = load_creds()
    assert c["client_id"] == "cid"
    assert c["access_token"] == "at"


def test_save_creds_round_trip(tmp_path: Path, mocker):
    p = tmp_path / "trakt.json"
    mocker.patch("fulcra_media.importers.trakt.CREDS_PATH", p)
    save_creds({"client_id": "cid", "client_secret": "csec", "access_token": "x", "refresh_token": "y", "expires_in": 86400, "created_at": 1})
    assert json.loads(p.read_text())["access_token"] == "x"


def test_auth_headers(tmp_path: Path, mocker):
    # token not expired (created_at far in the future)
    p = tmp_path / "trakt.json"
    p.write_text(json.dumps({"client_id": "cid", "client_secret": "csec", "access_token": "tok", "refresh_token": "rt", "expires_in": 86400, "created_at": int(time.time()) + 10000}))
    mocker.patch("fulcra_media.importers.trakt.CREDS_PATH", p)
    a = TraktAuth()
    h = a.headers()
    assert h["Authorization"] == "Bearer tok"
    assert h["trakt-api-version"] == "2"
    assert h["trakt-api-key"] == "cid"


def test_refresh_when_expired(tmp_path: Path, mocker):
    """If created_at + expires_in < now, perform a refresh and update creds."""
    p = tmp_path / "trakt.json"
    # token expired 100s ago
    p.write_text(json.dumps({"client_id": "cid", "client_secret": "csec", "access_token": "old", "refresh_token": "rt", "expires_in": 100, "created_at": int(time.time()) - 200}))
    mocker.patch("fulcra_media.importers.trakt.CREDS_PATH", p)

    def fake_post(url, json=None, timeout=None):
        assert "/oauth/token" in url
        assert json["refresh_token"] == "rt"
        return httpx.Response(200, json={
            "access_token": "new",
            "refresh_token": "rt2",
            "expires_in": 86400,
            "created_at": int(time.time()),
        })
    mocker.patch("httpx.post", side_effect=fake_post)

    a = TraktAuth()
    a.headers()  # triggers refresh internally
    c = json.loads(p.read_text())
    assert c["access_token"] == "new"
    assert c["refresh_token"] == "rt2"
