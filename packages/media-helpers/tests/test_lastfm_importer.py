"""Tests for the Last.fm importer (no CLI yet — fetch + normalize only)."""
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from fulcra_media.importers.lastfm import (
    fetch_recent_tracks,
    normalize_history,
    normalize_track,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------- normalize_track ----------

def test_normalize_track_skips_nowplaying_via_attr():
    """The currently-playing track has @attr.nowplaying=true and no date."""
    page1 = _load("lastfm_recent_tracks_page1.json")
    nowplaying = page1["recenttracks"]["track"][0]
    assert normalize_track(nowplaying) is None


def test_normalize_track_skips_when_date_missing_even_without_attr():
    """Defensive: any track without a date field is incomplete (not a real scrobble)."""
    incomplete = {"name": "x", "artist": {"#text": "y"}}
    assert normalize_track(incomplete) is None


def test_normalize_track_builds_normalizedevent_from_steely_dan():
    page1 = _load("lastfm_recent_tracks_page1.json")
    track = page1["recenttracks"]["track"][1]  # Reelin' In The Years
    ev = normalize_track(track)
    assert ev is not None
    assert ev.importer == "lastfm"
    assert ev.service == "lastfm"
    assert ev.category == "listened"
    assert ev.note == "Steely Dan – Reelin' In The Years"
    assert ev.title == "Reelin' In The Years"
    assert ev.start_time == datetime.fromtimestamp(1715900000, tz=timezone.utc)
    # 1-second sentinel
    assert (ev.end_time - ev.start_time).total_seconds() == 1
    assert ev.timestamp_confidence == "high"
    assert ev.external_ids["artist"] == "Steely Dan"
    assert ev.external_ids["track"] == "Reelin' In The Years"
    assert ev.external_ids["album"] == "Can't Buy a Thrill"
    assert ev.external_ids["mbid"] == "be9e92d1-acd2-3a37-95c1-bef27faf16ee"
    assert ev.external_ids["content_fingerprint"] == "music:steely-dan:reelin-in-the-years"


def test_normalize_track_omits_empty_album_and_mbid():
    """Tracks where album/mbid are empty strings shouldn't pollute external_ids."""
    page1 = _load("lastfm_recent_tracks_page1.json")
    pixies = page1["recenttracks"]["track"][4]  # Wave of Mutilation
    ev = normalize_track(pixies)
    # Album present but no MBID
    assert ev.external_ids.get("album") == "Doolittle"
    assert "mbid" not in ev.external_ids or not ev.external_ids["mbid"]


def test_normalize_track_deterministic_id_stable():
    """Same input → same source-id across runs."""
    page1 = _load("lastfm_recent_tracks_page1.json")
    track = page1["recenttracks"]["track"][1]
    a = normalize_track(track)
    b = normalize_track(track)
    assert a.deterministic_id == b.deterministic_id
    assert a.deterministic_id.startswith("com.fulcra.media.lastfm.v1.")


def test_normalize_track_handles_artist_as_string_text():
    """Last.fm sometimes returns artist as a bare string instead of an object."""
    track = {
        "artist": "Solo Artist",
        "name": "A Song",
        "date": {"uts": "1715897000", "#text": "..."},
    }
    ev = normalize_track(track)
    assert ev is not None
    assert ev.external_ids["artist"] == "Solo Artist"
    assert ev.note == "Solo Artist – A Song"


def test_normalize_history_filters_nowplaying_and_returns_4_from_5():
    page1 = _load("lastfm_recent_tracks_page1.json")
    events = list(normalize_history(page1["recenttracks"]["track"]))
    assert len(events) == 4  # 5 input, drop the 1 nowplaying


def test_normalize_history_handles_empty_list():
    assert list(normalize_history([])) == []


def test_normalize_track_same_track_different_times_distinct_ids():
    """Phoenix '1901' appears in both pages at different timestamps → distinct ids.

    Originally this test loaded page1 + page2 to compare two normalisations
    of the same track at different ts, but page1's '1901' entry is a
    `nowplaying` row that normalize_track correctly returns None for —
    leaving only one normalisation to assert against. Test now does what
    the name says: builds a synthetic second row with the same track at a
    different timestamp and asserts the deterministic ids differ.
    """
    page2 = _load("lastfm_recent_tracks_page2.json")
    p1901 = next(t for t in page2["recenttracks"]["track"] if t["name"] == "1901")
    ev = normalize_track(p1901)
    assert ev is not None
    assert ev.start_time == datetime.fromtimestamp(1715896800, tz=timezone.utc)

    # Synthetic second row, same track, 1 hour later → different id.
    p1901_later = dict(p1901)
    p1901_later["date"] = dict(p1901["date"])
    p1901_later["date"]["uts"] = str(int(p1901["date"]["uts"]) + 3600)
    ev_later = normalize_track(p1901_later)
    assert ev_later is not None
    assert ev_later.deterministic_id != ev.deterministic_id


# ---------- fetch_recent_tracks ----------

def _build_transport(captured_requests: list, pages: list[dict]) -> httpx.MockTransport:
    """Mock transport that serves the given pages in order, recording each request."""
    page_iter = iter(pages)
    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(dict(request.url.params))
        try:
            page = next(page_iter)
        except StopIteration:
            return httpx.Response(200, json={"recenttracks": {"track": [], "@attr": {"totalPages": "0"}}})
        return httpx.Response(200, json=page)
    return httpx.MockTransport(handler)


def test_fetch_recent_tracks_yields_one_page_when_total_pages_1():
    captured: list[dict] = []
    one_page = {
        "recenttracks": {
            "@attr": {"totalPages": "1", "page": "1"},
            "track": [{"name": "x", "artist": {"#text": "y"},
                       "date": {"uts": "1715897000"}}],
        }
    }
    creds = {"username": "u", "api_key": "k"}
    tracks = list(fetch_recent_tracks(
        creds, transport=_build_transport(captured, [one_page]),
    ))
    assert len(tracks) == 1
    assert len(captured) == 1
    assert captured[0]["user"] == "u"
    assert captured[0]["api_key"] == "k"
    assert captured[0]["page"] == "1"


def test_fetch_recent_tracks_paginates_through_totalpages():
    captured: list[dict] = []
    p1 = _load("lastfm_recent_tracks_page1.json")
    p2 = _load("lastfm_recent_tracks_page2.json")
    creds = {"username": "u", "api_key": "k"}
    tracks = list(fetch_recent_tracks(
        creds, transport=_build_transport(captured, [p1, p2]),
        sleep_between_pages=0.0,
    ))
    # Page 1 has 5 tracks (1 nowplaying), page 2 has 3 — total 8 raw
    assert len(tracks) == 8
    assert [c["page"] for c in captured] == ["1", "2"]


def test_fetch_recent_tracks_passes_from_when_since_set():
    captured: list[dict] = []
    one_page = {"recenttracks": {"@attr": {"totalPages": "1"}, "track": []}}
    creds = {"username": "u", "api_key": "k"}
    since = datetime(2024, 5, 16, 22, 0, tzinfo=timezone.utc)
    list(fetch_recent_tracks(
        creds, since=since,
        transport=_build_transport(captured, [one_page]),
    ))
    assert "from" in captured[0]
    assert int(captured[0]["from"]) == int(since.timestamp())


def test_fetch_recent_tracks_respects_max_pages():
    """When max_pages is set, stops early even if more pages exist."""
    captured: list[dict] = []
    page_with_more = {
        "recenttracks": {"@attr": {"totalPages": "10", "page": "1"},
                         "track": [{"name": "x"}]}
    }
    # Provide the same page twice — caller should only request once
    creds = {"username": "u", "api_key": "k"}
    list(fetch_recent_tracks(
        creds, max_pages=1,
        transport=_build_transport(captured, [page_with_more, page_with_more]),
    ))
    assert len(captured) == 1


def test_fetch_recent_tracks_handles_empty_track_list_gracefully():
    """An empty page (no scrobbles in the time window) should not crash."""
    captured: list[dict] = []
    creds = {"username": "u", "api_key": "k"}
    empty = {"recenttracks": {"@attr": {"totalPages": "1"}, "track": []}}
    tracks = list(fetch_recent_tracks(
        creds, transport=_build_transport(captured, [empty]),
    ))
    assert tracks == []


def test_fetch_recent_tracks_handles_single_track_as_dict():
    """Last.fm returns a bare dict (not a list) when there's only one scrobble."""
    captured: list[dict] = []
    creds = {"username": "u", "api_key": "k"}
    single = {
        "recenttracks": {
            "@attr": {"totalPages": "1"},
            "track": {"name": "Solo", "artist": {"#text": "Lone"},
                      "date": {"uts": "1715897000"}},
        }
    }
    tracks = list(fetch_recent_tracks(
        creds, transport=_build_transport(captured, [single]),
    ))
    assert len(tracks) == 1
    assert tracks[0]["name"] == "Solo"


def test_fetch_recent_tracks_raises_on_lastfm_error_envelope():
    """Last.fm returns {'error': 6, 'message': 'User not found'} for bad inputs."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": 6, "message": "User not found"})
    transport = httpx.MockTransport(handler)
    creds = {"username": "nope", "api_key": "k"}
    with pytest.raises(RuntimeError, match="User not found"):
        list(fetch_recent_tracks(creds, transport=transport))


def test_fetch_recent_tracks_raises_on_rate_limit():
    """Code 29 is rate limit — should surface as a typed error."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": 29, "message": "Rate limit exceeded"})
    transport = httpx.MockTransport(handler)
    creds = {"username": "u", "api_key": "k"}
    with pytest.raises(RuntimeError, match="Rate limit"):
        list(fetch_recent_tracks(creds, transport=transport))


def test_fetch_recent_tracks_propagates_http_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")
    transport = httpx.MockTransport(handler)
    creds = {"username": "u", "api_key": "k"}
    with pytest.raises(httpx.HTTPStatusError):
        list(fetch_recent_tracks(creds, transport=transport))
