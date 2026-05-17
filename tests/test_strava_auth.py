"""Tests for StravaAuth (load/save creds + 6-hour token refresh)."""
import json
import time
from pathlib import Path

import httpx
import pytest

from fulcra_media.importers.strava import (
    StravaAuth, load_creds, save_creds,
)


def _write_creds(p: Path, **overrides) -> None:
    data = {
        "client_id": "cid",
        "client_secret": "csec",
        "access_token": "at",
        "refresh_token": "rt",
        "expires_at": int(time.time()) + 10_000,
    }
    data.update(overrides)
    p.write_text(json.dumps(data))


def test_load_creds_returns_dict(tmp_path: Path, mocker):
    p = tmp_path / "strava.json"
    _write_creds(p)
    mocker.patch("fulcra_media.importers.strava.CREDS_PATH", p)
    c = load_creds()
    assert c["client_id"] == "cid"
    assert c["access_token"] == "at"


def test_save_creds_round_trip_with_chmod(tmp_path: Path, mocker):
    p = tmp_path / "strava.json"
    mocker.patch("fulcra_media.importers.strava.CREDS_PATH", p)
    save_creds({
        "client_id": "cid", "client_secret": "csec",
        "access_token": "x", "refresh_token": "y",
        "expires_at": 1234567890,
    })
    assert json.loads(p.read_text())["access_token"] == "x"
    # 0600 perms
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600


def test_auth_header_returns_bearer_when_not_expired(tmp_path: Path, mocker):
    p = tmp_path / "strava.json"
    _write_creds(p, expires_at=int(time.time()) + 10_000, access_token="tok123")
    mocker.patch("fulcra_media.importers.strava.CREDS_PATH", p)
    a = StravaAuth()
    h = a.auth_header()
    assert h == {"Authorization": "Bearer tok123"}


def test_refresh_if_needed_noop_when_token_future(tmp_path: Path, mocker):
    p = tmp_path / "strava.json"
    _write_creds(p, expires_at=int(time.time()) + 10_000, access_token="stillgood")
    mocker.patch("fulcra_media.importers.strava.CREDS_PATH", p)

    posted: list = []
    def fake_post(*args, **kw):
        posted.append((args, kw))
        return httpx.Response(200, json={})
    mocker.patch("fulcra_media.importers.strava.httpx.post", side_effect=fake_post)

    a = StravaAuth()
    a.refresh_if_needed()
    assert posted == []  # no refresh call
    assert a.creds["access_token"] == "stillgood"


def test_refresh_if_needed_refreshes_when_expired(tmp_path: Path, mocker):
    """expires_at in the past → POST /oauth/token + persist new token."""
    p = tmp_path / "strava.json"
    _write_creds(p, expires_at=int(time.time()) - 100, access_token="old")
    mocker.patch("fulcra_media.importers.strava.CREDS_PATH", p)

    new_expires_at = int(time.time()) + 21600  # 6 hours
    captured: dict = {}
    def fake_post(url, data=None, timeout=None, **kw):
        captured["url"] = url
        captured["data"] = data
        return httpx.Response(200, json={
            "access_token": "new_token",
            "refresh_token": "new_rt",
            "expires_at": new_expires_at,
            "expires_in": 21600,
            "token_type": "Bearer",
        })
    mocker.patch("fulcra_media.importers.strava.httpx.post", side_effect=fake_post)

    a = StravaAuth()
    a.refresh_if_needed()

    # Endpoint, form payload
    assert "oauth/token" in captured["url"]
    assert captured["data"]["grant_type"] == "refresh_token"
    assert captured["data"]["refresh_token"] == "rt"
    assert captured["data"]["client_id"] == "cid"
    assert captured["data"]["client_secret"] == "csec"

    # Creds rotated + persisted
    assert a.creds["access_token"] == "new_token"
    assert a.creds["refresh_token"] == "new_rt"
    assert a.creds["expires_at"] == new_expires_at
    persisted = json.loads(p.read_text())
    assert persisted["access_token"] == "new_token"
    assert persisted["refresh_token"] == "new_rt"


def test_refresh_uses_expires_in_when_expires_at_absent(tmp_path: Path, mocker):
    """Some token endpoints only return expires_in — fall back to now + delta."""
    p = tmp_path / "strava.json"
    _write_creds(p, expires_at=0, access_token="old")
    mocker.patch("fulcra_media.importers.strava.CREDS_PATH", p)

    before = int(time.time())
    def fake_post(*args, **kw):
        return httpx.Response(200, json={
            "access_token": "new",
            "refresh_token": "new_rt",
            "expires_in": 21600,
        })
    mocker.patch("fulcra_media.importers.strava.httpx.post", side_effect=fake_post)

    a = StravaAuth()
    a.refresh_if_needed()
    assert a.creds["access_token"] == "new"
    assert a.creds["expires_at"] >= before + 21600 - 5


def test_refresh_raises_on_4xx(tmp_path: Path, mocker):
    p = tmp_path / "strava.json"
    _write_creds(p, expires_at=int(time.time()) - 100)
    mocker.patch("fulcra_media.importers.strava.CREDS_PATH", p)

    def fake_post(*args, **kw):
        return httpx.Response(400, json={"message": "Bad Request"},
                              request=httpx.Request("POST", "https://x"))
    mocker.patch("fulcra_media.importers.strava.httpx.post", side_effect=fake_post)

    a = StravaAuth()
    with pytest.raises(httpx.HTTPStatusError):
        a.refresh_if_needed()


def test_auth_header_triggers_refresh_when_expired(tmp_path: Path, mocker):
    p = tmp_path / "strava.json"
    _write_creds(p, expires_at=int(time.time()) - 100, access_token="old")
    mocker.patch("fulcra_media.importers.strava.CREDS_PATH", p)

    new_at = int(time.time()) + 21600
    def fake_post(*args, **kw):
        return httpx.Response(200, json={
            "access_token": "fresh", "refresh_token": "rt2",
            "expires_at": new_at, "expires_in": 21600,
        })
    mocker.patch("fulcra_media.importers.strava.httpx.post", side_effect=fake_post)

    a = StravaAuth()
    h = a.auth_header()
    assert h == {"Authorization": "Bearer fresh"}


def test_is_expired_uses_slack(tmp_path: Path, mocker):
    """If token expires in 30s but slack is 60s, treat as expired."""
    p = tmp_path / "strava.json"
    _write_creds(p, expires_at=int(time.time()) + 30, access_token="x")
    mocker.patch("fulcra_media.importers.strava.CREDS_PATH", p)
    a = StravaAuth()
    assert a._is_expired(slack_seconds=60) is True
    assert a._is_expired(slack_seconds=10) is False
