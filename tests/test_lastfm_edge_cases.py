"""Aggressive edge-case sweep for the Last.fm importer."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from fulcra_media.importers.lastfm import (
    fetch_recent_tracks,
    normalize_history,
    normalize_track,
)


# ---------- malformed data ----------

def test_normalize_track_artist_only_mbid_no_text():
    """Some Last.fm responses have artist={mbid: '...', #text: ''}."""
    track = {
        "artist": {"mbid": "abc-123", "#text": ""},
        "name": "Mystery",
        "date": {"uts": "1715897000"},
    }
    # No artist text → can't build a note → skip
    assert normalize_track(track) is None


def test_normalize_track_with_extra_unknown_fields():
    """Forward-compat: ignore unrecognized keys."""
    track = {
        "artist": {"#text": "X"},
        "album": {"#text": "A"},
        "name": "Song",
        "date": {"uts": "1715897000"},
        "loved": "1",  # extended=1 field
        "image": [{"size": "small", "#text": "..."}],
        "future_field": {"nested": "junk"},
    }
    ev = normalize_track(track)
    assert ev is not None
    assert ev.note == "X – Song"


def test_normalize_track_unicode_in_name_and_artist():
    track = {
        "artist": {"#text": "Sigur Rós"},
        "name": "Sæglópur",
        "date": {"uts": "1715897000"},
    }
    ev = normalize_track(track)
    assert ev.external_ids["content_fingerprint"] == "music:sigur-rs:sglpur"


def test_normalize_track_with_whitespace_only_artist_skipped():
    track = {
        "artist": {"#text": "   "},
        "name": "Song",
        "date": {"uts": "1715897000"},
    }
    assert normalize_track(track) is None


def test_normalize_track_negative_uts_is_skipped():
    """Pre-epoch timestamps are data quality artifacts, not real scrobbles."""
    track = {
        "artist": {"#text": "X"},
        "name": "Y",
        "date": {"uts": "-100"},
    }
    assert normalize_track(track) is None


def test_normalize_track_epoch_sentinel_uts_is_skipped():
    """Last.fm can return UTS 1/2/3-style placeholders; skip them."""
    track = {
        "artist": {"#text": "Frank Black"},
        "name": "(I Want to Live on an) Abstract Plain",
        "date": {"uts": "1"},
    }
    assert normalize_track(track) is None


def test_normalize_track_uts_as_int_not_string():
    """Defensive: even if Last.fm sends uts as int."""
    track = {
        "artist": {"#text": "X"},
        "name": "Y",
        "date": {"uts": 1715897000},
    }
    ev = normalize_track(track)
    assert ev is not None
    assert ev.start_time == datetime.fromtimestamp(1715897000, tz=timezone.utc)


def test_normalize_history_iterator_input_not_list():
    """normalize_history should accept any iterable, not just list."""
    def gen():
        yield {"artist": {"#text": "X"}, "name": "Y", "date": {"uts": "1715897000"}}
    events = list(normalize_history(gen()))
    assert len(events) == 1


# ---------- fetch error paths ----------

def test_fetch_handles_missing_recenttracks_envelope():
    """If Last.fm returns malformed JSON without recenttracks, don't crash."""
    def handler(request):
        return httpx.Response(200, json={"something_else": {}})
    creds = {"username": "u", "api_key": "k"}
    tracks = list(fetch_recent_tracks(
        creds, transport=httpx.MockTransport(handler),
    ))
    assert tracks == []


def test_fetch_handles_track_key_missing_entirely():
    """Empty recenttracks with no track key."""
    def handler(request):
        return httpx.Response(200, json={"recenttracks": {"@attr": {"totalPages": "1"}}})
    creds = {"username": "u", "api_key": "k"}
    tracks = list(fetch_recent_tracks(
        creds, transport=httpx.MockTransport(handler),
    ))
    assert tracks == []


def test_fetch_handles_missing_total_pages():
    """Without @attr.totalPages, default to 1 so we don't loop forever."""
    pages_served = []
    def handler(request):
        pages_served.append(dict(request.url.params))
        return httpx.Response(200, json={
            "recenttracks": {
                "track": [{"name": "x", "artist": {"#text": "y"},
                           "date": {"uts": "1715897000"}}],
                # No @attr at all
            }
        })
    creds = {"username": "u", "api_key": "k"}
    tracks = list(fetch_recent_tracks(
        creds, transport=httpx.MockTransport(handler),
    ))
    assert len(tracks) == 1
    assert len(pages_served) == 1


def test_fetch_sleep_param_is_respected_in_tests():
    """Setting sleep=0 must skip the actual sleep call."""
    pages = [
        {"recenttracks": {"@attr": {"totalPages": "3", "page": "1"}, "track": []}},
        {"recenttracks": {"@attr": {"totalPages": "3", "page": "2"}, "track": []}},
        {"recenttracks": {"@attr": {"totalPages": "3", "page": "3"}, "track": []}},
    ]
    page_iter = iter(pages)
    def handler(request):
        return httpx.Response(200, json=next(page_iter))
    creds = {"username": "u", "api_key": "k"}
    import time
    t0 = time.perf_counter()
    list(fetch_recent_tracks(
        creds, transport=httpx.MockTransport(handler), sleep_between_pages=0.0,
    ))
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5  # would be ~0.5s if sleep ran twice


def test_fetch_since_and_until_both_sent():
    captured = []
    def handler(request):
        captured.append(dict(request.url.params))
        return httpx.Response(200, json={"recenttracks": {"@attr": {"totalPages": "1"}, "track": []}})
    creds = {"username": "u", "api_key": "k"}
    list(fetch_recent_tracks(
        creds,
        since=datetime(2024, 5, 16, 22, 0, tzinfo=timezone.utc),
        until=datetime(2024, 5, 17, 0, 0, tzinfo=timezone.utc),
        transport=httpx.MockTransport(handler),
    ))
    assert "from" in captured[0]
    assert "to" in captured[0]
    assert int(captured[0]["from"]) < int(captured[0]["to"])


def test_fetch_clamps_limit_to_200_max():
    """Last.fm hard-caps limit at 200; importer should clamp silently."""
    captured = []
    def handler(request):
        captured.append(dict(request.url.params))
        return httpx.Response(200, json={"recenttracks": {"@attr": {"totalPages": "1"}, "track": []}})
    creds = {"username": "u", "api_key": "k"}
    list(fetch_recent_tracks(
        creds, limit=1000,  # ridiculous
        transport=httpx.MockTransport(handler),
    ))
    assert int(captured[0]["limit"]) == 200
