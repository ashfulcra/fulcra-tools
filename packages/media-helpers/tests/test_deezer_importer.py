"""Tests for the Deezer importer (fetch + normalize only)."""
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from fulcra_media.importers.deezer import (
    fetch_history,
    normalize_history,
    normalize_track,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------- normalize_track ----------

def test_normalize_track_happy_path_steely_dan():
    page1 = _load("deezer_history_page1.json")
    track = page1["data"][0]  # Reelin' In The Years
    ev = normalize_track(track)
    assert ev is not None
    assert ev.importer == "deezer"
    assert ev.service == "deezer"
    assert ev.category == "listened"
    assert ev.note == "Steely Dan – Reelin' In The Years"
    assert ev.title == "Reelin' In The Years"
    assert ev.start_time == datetime.fromtimestamp(1715900000, tz=timezone.utc)
    # 1-second sentinel — Deezer history doesn't expose per-play duration
    assert (ev.end_time - ev.start_time).total_seconds() == 1
    assert ev.timestamp_confidence == "high"
    assert ev.external_ids["deezer_track_id"] == 1109731
    assert ev.external_ids["artist"] == "Steely Dan"
    assert ev.external_ids["album"] == "Can't Buy a Thrill"
    assert ev.external_ids["content_fingerprint"] == "music:steely-dan:reelin-in-the-years"
    assert ev.deterministic_id.startswith("com.fulcra.media.deezer.v1.")


def test_normalize_track_deterministic_id_stable():
    """Same input → same id across runs."""
    page1 = _load("deezer_history_page1.json")
    track = page1["data"][0]
    a = normalize_track(track)
    b = normalize_track(track)
    assert a.deterministic_id == b.deterministic_id


def test_normalize_track_same_track_different_times_distinct_ids():
    """Same track id at different timestamps → distinct deterministic ids."""
    page1 = _load("deezer_history_page1.json")
    page2 = _load("deezer_history_page2.json")
    pixies1 = page1["data"][3]  # ts 1715897600
    pixies2 = page2["data"][2]  # ts 1715896600 (same track id)
    assert pixies1["id"] == pixies2["id"]
    a = normalize_track(pixies1)
    b = normalize_track(pixies2)
    assert a.deterministic_id != b.deterministic_id


def test_normalize_track_missing_artist_returns_none():
    t = {"id": 1, "title": "x", "timestamp": 1715897000, "album": {"title": "a"}}
    assert normalize_track(t) is None


def test_normalize_track_missing_title_returns_none():
    t = {"id": 1, "timestamp": 1715897000, "artist": {"name": "a"}}
    assert normalize_track(t) is None


def test_normalize_track_missing_timestamp_returns_none():
    t = {"id": 1, "title": "x", "artist": {"name": "a"}}
    assert normalize_track(t) is None


def test_normalize_track_missing_id_returns_none():
    t = {"title": "x", "timestamp": 1715897000, "artist": {"name": "a"}}
    assert normalize_track(t) is None


def test_normalize_track_empty_album_omitted_from_external():
    t = {
        "id": 7,
        "title": "Solo",
        "timestamp": 1715897000,
        "artist": {"name": "Lone"},
    }
    ev = normalize_track(t)
    assert ev is not None
    assert "album" not in ev.external_ids


def test_normalize_history_filters_invalid_and_returns_rest():
    page1 = _load("deezer_history_page1.json")
    # add one malformed at the end
    bad = {"title": "no-artist", "id": 1, "timestamp": 1715890000}
    events = list(normalize_history(page1["data"] + [bad]))
    assert len(events) == 4


def test_normalize_history_handles_empty_list():
    assert list(normalize_history([])) == []


# ---------- fetch_history ----------

def _build_transport(captured_requests: list, pages: list[dict]) -> httpx.MockTransport:
    """Mock transport that serves the given pages in order."""
    page_iter = iter(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append({
            "url": str(request.url),
            "params": dict(request.url.params),
            "host": request.url.host,
            "path": request.url.path,
        })
        try:
            page = next(page_iter)
        except StopIteration:
            return httpx.Response(200, json={"data": [], "total": 0})
        return httpx.Response(200, json=page)

    return httpx.MockTransport(handler)


def test_fetch_history_single_page_no_next():
    """One page with no `next` key → stop after one request."""
    captured: list[dict] = []
    one_page = {
        "data": [
            {"id": 1, "title": "x", "timestamp": 1715897000,
             "artist": {"name": "y"}, "album": {"title": "z"}, "type": "track"},
        ],
        "total": 1,
    }
    tracks = list(fetch_history(
        {"access_token": "tok"},
        transport=_build_transport(captured, [one_page]),
        sleep_between_pages=0.0,
    ))
    assert len(tracks) == 1
    assert len(captured) == 1
    # Access token in query string
    assert captured[0]["params"]["access_token"] == "tok"
    assert captured[0]["host"] == "api.deezer.com"
    assert captured[0]["path"] == "/user/me/history"


def test_fetch_history_paginates_via_next():
    captured: list[dict] = []
    page1 = _load("deezer_history_page1.json")
    page2 = _load("deezer_history_page2.json")
    tracks = list(fetch_history(
        {"access_token": "tok"},
        transport=_build_transport(captured, [page1, page2]),
        sleep_between_pages=0.0,
    ))
    # page1: 4 entries, page2: 3 entries
    assert len(tracks) == 7
    assert len(captured) == 2
    # 2nd request honors the `next` URL's params (index=4)
    assert captured[1]["params"].get("index") == "4"


def test_fetch_history_respects_max_pages():
    """When max_pages=1, stop even if `next` is present."""
    captured: list[dict] = []
    p1 = _load("deezer_history_page1.json")  # has `next`
    p2 = _load("deezer_history_page2.json")
    list(fetch_history(
        {"access_token": "tok"},
        max_pages=1,
        transport=_build_transport(captured, [p1, p2]),
        sleep_between_pages=0.0,
    ))
    assert len(captured) == 1


def test_fetch_history_passes_since_as_timestamp_filter():
    """When since is set, we only yield tracks at or after that timestamp."""
    captured: list[dict] = []
    p1 = _load("deezer_history_page1.json")
    since = datetime.fromtimestamp(1715898000, tz=timezone.utc)
    tracks = list(fetch_history(
        {"access_token": "tok"},
        since=since,
        transport=_build_transport(captured, [p1]),
        sleep_between_pages=0.0,
        max_pages=1,
    ))
    # Drop any with timestamp < 1715898000
    assert all(t["timestamp"] >= 1715898000 for t in tracks)
    assert len(tracks) == 3  # 4 in page, one (Pixies @ 1715897600) below the bar


def test_fetch_history_handles_empty_data():
    captured: list[dict] = []
    empty = {"data": [], "total": 0}
    tracks = list(fetch_history(
        {"access_token": "tok"},
        transport=_build_transport(captured, [empty]),
        sleep_between_pages=0.0,
    ))
    assert tracks == []


def test_fetch_history_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")
    transport = httpx.MockTransport(handler)
    with pytest.raises(httpx.HTTPStatusError):
        list(fetch_history(
            {"access_token": "tok"},
            transport=transport, sleep_between_pages=0.0,
        ))


def test_fetch_history_deezer_error_envelope_raises():
    """Deezer signals errors via a top-level `error` object with HTTP 200."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "error": {"type": "OAuthException", "message": "Invalid OAuth access token.", "code": 300},
        })
    transport = httpx.MockTransport(handler)
    with pytest.raises(RuntimeError, match="Invalid OAuth"):
        list(fetch_history(
            {"access_token": "bad"},
            transport=transport, sleep_between_pages=0.0,
        ))
