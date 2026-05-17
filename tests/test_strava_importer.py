"""Tests for the Strava importer (fetch + normalize)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from fulcra_media.importers.strava import (
    fetch_activities,
    normalize_activity,
    normalize_activities,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())


# ---------- normalize_activity ----------

def test_normalize_activity_run_happy_path():
    page1 = _load("strava_activities_page1.json")
    run = page1[0]
    ev = normalize_activity(run)
    assert ev is not None
    assert ev.importer == "strava"
    assert ev.service == "strava"
    assert ev.category == "activity"
    assert ev.title == "Morning Run"
    assert ev.note == "Run – Morning Run"
    assert ev.start_time == datetime(2026, 5, 17, 13, 0, 0, tzinfo=timezone.utc)
    # elapsed_time 1830s → end - start = 1830s
    assert (ev.end_time - ev.start_time).total_seconds() == 1830
    assert ev.timestamp_confidence == "high"
    # deterministic id format
    assert ev.deterministic_id == "com.fulcra.media.strava.v1.13371001"
    # external_ids contents
    ext = ev.external_ids
    assert ext["strava_activity_id"] == 13371001
    assert ext["athlete_id"] == 99001
    assert ext["sport_type"] == "Run"
    assert ext["distance_meters"] == 5012.4
    assert ext["moving_time_seconds"] == 1750
    assert ext["elevation_gain_meters"] == 42.0
    assert ext["average_heartrate"] == 152.3
    assert "content_fingerprint" in ext
    assert ext["content_fingerprint"].startswith("workout:99001:run:")


def test_normalize_activity_uses_sport_type_for_note_when_available():
    """sport_type is more specific than type — TrailRun vs Run."""
    page2 = _load("strava_activities_page2.json")
    trail = page2[1]  # TrailRun
    ev = normalize_activity(trail)
    assert ev.note == "TrailRun – Trail Run"
    assert ev.external_ids["sport_type"] == "TrailRun"


def test_normalize_activity_missing_optional_fields_omitted():
    """No heartrate → external_ids has no 'average_heartrate'."""
    page1 = _load("strava_activities_page1.json")
    swim = page1[2]  # no average_heartrate
    ev = normalize_activity(swim)
    assert ev is not None
    assert "average_heartrate" not in ev.external_ids
    # elevation 0 still recorded (zero is a real value)
    assert ev.external_ids["elevation_gain_meters"] == 0.0


def test_normalize_activity_missing_elevation_omitted():
    raw = {
        "id": 7,
        "name": "Indoor Run",
        "type": "Run",
        "sport_type": "Run",
        "start_date": "2026-05-17T10:00:00Z",
        "start_date_local": "2026-05-17T03:00:00Z",
        "elapsed_time": 1800,
        "moving_time": 1800,
        "distance": 5000.0,
        "athlete": {"id": 99001},
    }
    ev = normalize_activity(raw)
    assert ev is not None
    assert "elevation_gain_meters" not in ev.external_ids
    assert "average_heartrate" not in ev.external_ids


def test_normalize_activity_skips_when_required_missing():
    """No id, no athlete, no start_date → drop."""
    assert normalize_activity({"name": "x"}) is None
    assert normalize_activity({"id": 1, "name": "x"}) is None
    assert normalize_activity({"id": 1, "athlete": {"id": 5}, "name": "x"}) is None


def test_normalize_activity_z_suffix_parsed_as_utc():
    raw = {
        "id": 42,
        "name": "x",
        "type": "Run",
        "sport_type": "Run",
        "start_date": "2026-05-17T10:00:00Z",
        "elapsed_time": 60,
        "moving_time": 60,
        "distance": 100.0,
        "athlete": {"id": 1},
    }
    ev = normalize_activity(raw)
    assert ev.start_time.tzinfo is not None
    assert ev.start_time.utcoffset() == timedelta(0)


def test_normalize_activities_filters_malformed_and_yields_rest():
    page1 = _load("strava_activities_page1.json")
    items = page1 + [{"name": "incomplete"}]
    events = list(normalize_activities(items))
    assert len(events) == 3  # only the 3 valid ones


# ---------- fetch_activities ----------

def _build_transport(captured_requests: list, pages: list[list[dict]]) -> httpx.MockTransport:
    """Mock transport that serves the given pages in order, recording each request."""
    page_iter = iter(pages)
    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append({
            "url": str(request.url),
            "params": dict(request.url.params),
            "headers": dict(request.headers),
        })
        try:
            page = next(page_iter)
        except StopIteration:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=page)
    return httpx.MockTransport(handler)


def test_fetch_activities_single_page_returns_all():
    captured: list[dict] = []
    page1 = _load("strava_activities_page1.json")
    creds = {"access_token": "tok"}
    out = list(fetch_activities(
        creds, transport=_build_transport(captured, [page1]),
        per_page=200, sleep_between_pages=0.0,
    ))
    assert len(out) == 3
    # Only one request when page is shorter than per_page
    assert len(captured) == 1
    # Auth in header, not URL
    assert captured[0]["headers"]["authorization"] == "Bearer tok"
    # access_token not in URL params (security)
    assert "access_token" not in captured[0]["params"]
    assert captured[0]["params"]["page"] == "1"
    assert captured[0]["params"]["per_page"] == "200"


def test_fetch_activities_paginates_when_full_page():
    """When per_page items come back, request next page."""
    captured: list[dict] = []
    p1 = _load("strava_activities_page1.json")  # 3 items
    p2 = _load("strava_activities_page2.json")  # 2 items
    creds = {"access_token": "tok"}
    # per_page=3 → first page returns 3 (full), request page 2 (2 items, partial → stop)
    out = list(fetch_activities(
        creds, transport=_build_transport(captured, [p1, p2]),
        per_page=3, sleep_between_pages=0.0,
    ))
    assert len(out) == 5
    assert len(captured) == 2
    assert [c["params"]["page"] for c in captured] == ["1", "2"]


def test_fetch_activities_respects_max_pages():
    captured: list[dict] = []
    p1 = _load("strava_activities_page1.json")
    p2 = _load("strava_activities_page2.json")
    creds = {"access_token": "tok"}
    out = list(fetch_activities(
        creds,
        transport=_build_transport(captured, [p1, p2]),
        per_page=3, max_pages=1, sleep_between_pages=0.0,
    ))
    # max_pages=1 → only the first page (3 items)
    assert len(out) == 3
    assert len(captured) == 1


def test_fetch_activities_passes_after_unix_timestamp_when_since_set():
    captured: list[dict] = []
    creds = {"access_token": "tok"}
    since = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    list(fetch_activities(
        creds, since=since,
        transport=_build_transport(captured, [[]]),
        sleep_between_pages=0.0,
    ))
    assert "after" in captured[0]["params"]
    assert int(captured[0]["params"]["after"]) == int(since.timestamp())


def test_fetch_activities_no_after_when_since_none():
    captured: list[dict] = []
    creds = {"access_token": "tok"}
    list(fetch_activities(
        creds, transport=_build_transport(captured, [[]]),
        sleep_between_pages=0.0,
    ))
    assert "after" not in captured[0]["params"]


def test_fetch_activities_stops_when_empty_page():
    captured: list[dict] = []
    creds = {"access_token": "tok"}
    out = list(fetch_activities(
        creds, transport=_build_transport(captured, [[], _load("strava_activities_page1.json")]),
        per_page=200, sleep_between_pages=0.0,
    ))
    # Empty first page → stop immediately
    assert out == []
    assert len(captured) == 1


def test_fetch_activities_propagates_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Authorization Error", "errors": []})
    transport = httpx.MockTransport(handler)
    creds = {"access_token": "tok"}
    with pytest.raises(httpx.HTTPStatusError):
        list(fetch_activities(creds, transport=transport, sleep_between_pages=0.0))


def test_fetch_activities_propagates_http_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")
    transport = httpx.MockTransport(handler)
    creds = {"access_token": "tok"}
    with pytest.raises(httpx.HTTPStatusError):
        list(fetch_activities(creds, transport=transport, sleep_between_pages=0.0))
