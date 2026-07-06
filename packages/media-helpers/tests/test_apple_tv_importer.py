"""Apple TV UTS-cache importer tests.

Fixtures are trimmed/anonymized captures of REAL tahoma_watchnow canvas
payloads from a live machine (2026-07-06): show/episode titles and ids are
replaced, but the structure, shelf types, context strings, S/E numbers and
timestamps are the real thing.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fulcra_media.importers.apple_tv import (
    decode_body,
    iter_cache_entries,
    parse_cache,
    parse_canvas_payload,
    parse_item_timestamp,
    scan_cache,
)

FIXTURES = Path(__file__).parent / "fixtures"
UPNEXT_FIXTURE = FIXTURES / "apple_tv_watchnow_upnext.json"
HISTORY_FIXTURE = FIXTURES / "apple_tv_watchnow_recentlywatched.json"

FETCHED_AT = datetime(2026, 7, 6, 18, 57, 45, tzinfo=timezone.utc)

CANVAS_URL = (
    "https://uts-api.itunes.apple.com/uts/v3/canvases/Roots/tahoma_watchnow"
    "?caller=js&locale=en-US"
)


def _upnext_payload() -> dict:
    return json.loads(UPNEXT_FIXTURE.read_text())


def _history_payload() -> dict:
    return json.loads(HISTORY_FIXTURE.read_text())


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

def test_parse_item_timestamp_epoch_ms():
    dt = parse_item_timestamp(1783359806043)
    assert dt == datetime.fromtimestamp(1783359806.043, tz=timezone.utc)


def test_parse_item_timestamp_epoch_seconds():
    dt = parse_item_timestamp(1783359806)
    assert dt == datetime.fromtimestamp(1783359806, tz=timezone.utc)


def test_parse_item_timestamp_iso():
    dt = parse_item_timestamp("2026-07-06T14:23:26Z")
    assert dt == datetime(2026, 7, 6, 14, 23, 26, tzinfo=timezone.utc)


def test_parse_item_timestamp_numeric_string():
    dt = parse_item_timestamp("1783359806043")
    assert dt == datetime.fromtimestamp(1783359806.043, tz=timezone.utc)


def test_parse_item_timestamp_garbage_is_none():
    assert parse_item_timestamp(None) is None
    assert parse_item_timestamp("not a time") is None
    assert parse_item_timestamp({}) is None


# ---------------------------------------------------------------------------
# Up Next shelf semantics (real fixture)
# ---------------------------------------------------------------------------

def test_up_next_continue_items_become_high_conf_events():
    events = list(parse_canvas_payload(_upnext_payload(), FETCHED_AT))
    cont = [e for e in events if e.external_ids["kind"] == "continue"]
    # 5 Continue episodes + 1 Continue movie in the fixture.
    assert len(cont) == 6
    assert all(e.timestamp_confidence == "high" for e in cont)
    assert all(e.category == "watched" for e in cont)


def test_up_next_continue_episode_shape():
    events = list(parse_canvas_payload(_upnext_payload(), FETCHED_AT))
    e = next(e for e in events if e.external_ids.get("show") == "Edgeworld"
             and e.external_ids["kind"] == "continue")
    assert e.note == "Edgeworld S02E15 – Chapter 2.15"
    assert e.title == "Edgeworld"
    # timestamp is the item's own epoch-ms activity time, NOT the fetch time
    assert e.start_time == datetime.fromtimestamp(1783359806.043, tz=timezone.utc)
    assert e.start_time != FETCHED_AT
    assert (e.end_time - e.start_time) == timedelta(seconds=1)
    assert e.deterministic_id.startswith("com.fulcra.media.apple-tv.v1.")
    assert e.external_ids["content_fingerprint"] == "tv:edgeworld:s02e15"
    # High-confidence events carry the cross-source time-bucket fingerprint.
    assert len(e.extra_source_ids) == 1
    assert e.extra_source_ids[0].startswith("com.fulcra.content.watched.v1.")


def test_up_next_continue_movie_shape():
    events = list(parse_canvas_payload(_upnext_payload(), FETCHED_AT))
    e = next(e for e in events if e.note == "End Result")
    assert e.external_ids["kind"] == "continue"
    assert e.timestamp_confidence == "high"
    assert e.external_ids["content_fingerprint"] == "movie:end-result"
    assert len(e.extra_source_ids) == 1


def test_up_next_next_episode_emits_prior_episode():
    events = list(parse_canvas_payload(_upnext_payload(), FETCHED_AT))
    derived = [e for e in events
               if e.external_ids["kind"] == "completed_prior_episode"]
    assert len(derived) == 1
    e = derived[0]
    # The fixture's NextEpisode item is S1E6 → the completed episode is S1E5.
    assert e.external_ids["show"] == "Ultimate Fun Assured"
    assert e.external_ids["season"] == 1
    assert e.external_ids["episode"] == 5
    assert e.note == "Ultimate Fun Assured S01E05"
    assert e.timestamp_confidence == "medium"
    assert e.external_ids["derived_from"] == "next_episode"
    assert e.external_ids["content_fingerprint"] == "tv:ultimate-fun-assured:s01e05"
    # Medium confidence → no cross-source time-bucket fingerprint.
    assert e.extra_source_ids == ()


def test_up_next_next_season_is_skipped():
    """NextSeason means the prior season's finale was completed, but its
    episode number isn't in the payload — we skip rather than guess."""
    events = list(parse_canvas_payload(_upnext_payload(), FETCHED_AT))
    assert not any("Honeytrap" in (e.title or "") for e in events)


def test_up_next_catalog_noise_is_ignored():
    """Recently Added / Now Available items are catalog events, not watches."""
    events = list(parse_canvas_payload(_upnext_payload(), FETCHED_AT))
    titles = {e.title for e in events}
    for noise in ("Sovereign: Age of Beasts", "Fast Swap", "Turbine Red-Eye",
                  "Comet Town", "Deputies", "Hemsted Farm", "The Bradford"):
        assert noise not in titles


def test_up_next_fixture_total_event_count():
    # 6 Continue + 1 derived prior-episode; NextSeason + 6 noise items skipped.
    events = list(parse_canvas_payload(_upnext_payload(), FETCHED_AT))
    assert len(events) == 7


def test_next_episode_e1_is_skipped():
    """NextEpisode on E1 would need the previous season's finale number —
    not derivable, so no event."""
    payload = {"data": {"canvas": {"shelves": [{
        "displayType": "upNextLockup",
        "items": [{
            "type": "Episode", "showTitle": "Some Show", "title": "Pilot II",
            "seasonNumber": 2, "episodeNumber": 1,
            "context": "NextEpisode", "localizedContext": "Next Episode",
            "timestamp": 1783359806043,
        }],
    }]}}}
    assert list(parse_canvas_payload(payload, FETCHED_AT)) == []


def test_up_next_unknown_context_is_skipped():
    payload = {"data": {"canvas": {"shelves": [{
        "displayType": "upNextLockup",
        "items": [{
            "type": "Episode", "showTitle": "S", "title": "T",
            "seasonNumber": 1, "episodeNumber": 2,
            "context": "SomethingNew", "localizedContext": "Something New",
            "timestamp": 1783359806043,
        }],
    }]}}}
    assert list(parse_canvas_payload(payload, FETCHED_AT)) == []


def test_up_next_item_without_timestamp_is_skipped():
    payload = {"data": {"canvas": {"shelves": [{
        "displayType": "upNextLockup",
        "items": [{
            "type": "Episode", "showTitle": "S", "title": "T",
            "seasonNumber": 1, "episodeNumber": 2, "context": "Continue",
        }],
    }]}}}
    assert list(parse_canvas_payload(payload, FETCHED_AT)) == []


# ---------------------------------------------------------------------------
# Recently Watched shelf semantics (real fixture)
# ---------------------------------------------------------------------------

def test_history_emits_one_low_conf_event_per_episode():
    events = list(parse_canvas_payload(_history_payload(), FETCHED_AT))
    assert len(events) == 20
    assert all(e.timestamp_confidence == "low" for e in events)
    assert all(e.external_ids["kind"] == "history" for e in events)
    assert all(e.category == "watched" for e in events)
    # Low-confidence events never get the cross-source time-bucket id.
    assert all(e.extra_source_ids == () for e in events)


def test_history_start_time_is_snapshot_fetch_time_never_release_date():
    """releaseDate is the original AIR date (2008-2009 for most fixture
    items) and must NEVER be used as the watch time."""
    events = list(parse_canvas_payload(_history_payload(), FETCHED_AT))
    for e in events:
        assert e.start_time == FETCHED_AT
        assert (e.end_time - e.start_time) == timedelta(seconds=1)
        assert e.external_ids["time_estimated"] is True
        # The air date is carried as metadata only.
        if "release_date_ms" in e.external_ids:
            air = datetime.fromtimestamp(
                e.external_ids["release_date_ms"] / 1000, tz=timezone.utc)
            assert e.start_time != air


def test_history_det_ids_are_idempotent_across_snapshots():
    """No timestamp component in the history det_id → the same episode in
    two different snapshots produces the same id."""
    first = list(parse_canvas_payload(_history_payload(), FETCHED_AT))
    later = list(parse_canvas_payload(
        _history_payload(), FETCHED_AT + timedelta(days=3)))
    assert {e.deterministic_id for e in first} == {e.deterministic_id for e in later}
    assert len({e.deterministic_id for e in first}) == 20


def test_history_preserves_watch_order_rank():
    events = list(parse_canvas_payload(_history_payload(), FETCHED_AT))
    assert events[0].external_ids["watch_order_rank"] == 0
    assert events[-1].external_ids["watch_order_rank"] == 19


def test_history_content_fingerprints():
    events = list(parse_canvas_payload(_history_payload(), FETCHED_AT))
    e = next(e for e in events if e.external_ids.get("show") == "Orphan Cove")
    assert e.external_ids["content_fingerprint"] == "tv:orphan-cove:s01e10"
    assert e.note == "Orphan Cove S01E10 – Chapter 1.10"


def test_empty_and_foreign_shelves_are_skipped():
    """The history fixture keeps the real Watchlist shelf and the real
    empty/None shelf from the live capture — neither produces events."""
    payload = _history_payload()
    display_types = [s.get("displayType")
                     for s in payload["data"]["canvas"]["shelves"]]
    assert "lockup" in display_types and None in display_types  # fixture sanity
    events = list(parse_canvas_payload(payload, FETCHED_AT))
    assert all(e.external_ids["kind"] == "history" for e in events)


def test_malformed_payloads_yield_nothing():
    for payload in ({}, {"data": {}}, {"data": {"canvas": {}}},
                    {"data": {"canvas": {"shelves": ["not-a-dict", 42]}}},
                    {"data": {"canvas": {"shelves": [{"displayType": "upNextLockup"}]}}}):
        assert list(parse_canvas_payload(payload, FETCHED_AT)) == []


# ---------------------------------------------------------------------------
# Body decoding
# ---------------------------------------------------------------------------

def test_decode_body_gzip():
    assert decode_body(gzip.compress(b'{"a":1}')) == b'{"a":1}'


def test_decode_body_zlib():
    assert decode_body(zlib.compress(b'{"a":1}')) == b'{"a":1}'


def test_decode_body_raw_passthrough():
    assert decode_body(b'{"a":1}') == b'{"a":1}'


# ---------------------------------------------------------------------------
# CFURL cache reading (synthetic Cache.db)
# ---------------------------------------------------------------------------

def _make_cache_db(cache_dir: Path, rows: list[tuple]) -> None:
    """Build a minimal-but-real CFURL cache: rows are
    (url, time_stamp_str, is_on_fs, receiver_data_bytes_or_fs_name)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cache_dir / "Cache.db")
    conn.executescript("""
        CREATE TABLE cfurl_cache_response(
            entry_ID INTEGER PRIMARY KEY AUTOINCREMENT UNIQUE,
            version INTEGER, hash_value INTEGER, storage_policy INTEGER,
            request_key TEXT UNIQUE,
            time_stamp NOT NULL DEFAULT CURRENT_TIMESTAMP, partition TEXT);
        CREATE TABLE cfurl_cache_receiver_data(
            entry_ID INTEGER PRIMARY KEY, isDataOnFS INTEGER,
            receiver_data BLOB);
    """)
    for i, (url, ts, on_fs, data) in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO cfurl_cache_response "
            "(entry_ID, request_key, time_stamp) VALUES (?, ?, ?)",
            (i, url, ts))
        conn.execute(
            "INSERT INTO cfurl_cache_receiver_data VALUES (?, ?, ?)",
            (i, on_fs, data))
    conn.commit()
    conn.close()


def _fs_file(cache_dir: Path, name: str, body: bytes) -> None:
    fs_dir = cache_dir / "fsCachedData"
    fs_dir.mkdir(exist_ok=True)
    (fs_dir / name).write_bytes(body)


def test_iter_cache_entries_inline_gzip_body(tmp_path):
    body = gzip.compress(json.dumps(_upnext_payload()).encode())
    _make_cache_db(tmp_path, [(CANVAS_URL, "2026-07-06 18:57:45", 0, body)])
    entries = list(iter_cache_entries(tmp_path))
    assert len(entries) == 1
    assert entries[0].fetched_at == FETCHED_AT
    assert json.loads(entries[0].body)["data"]["canvas"]


def test_iter_cache_entries_fs_body_read_from_original_dir(tmp_path):
    """isDataOnFS bodies live as files under <cache dir>/fsCachedData/,
    named by the receiver_data string."""
    _make_cache_db(tmp_path, [
        (CANVAS_URL, "2026-07-06 18:57:45", 1, b"AAAA-1111"),
    ])
    _fs_file(tmp_path, "AAAA-1111", json.dumps(_history_payload()).encode())
    entries = list(iter_cache_entries(tmp_path))
    assert len(entries) == 1
    assert json.loads(entries[0].body)["data"]["canvas"]


def test_iter_cache_entries_missing_fs_file_is_tolerated(tmp_path):
    """The app prunes fsCachedData independently of the db — a dangling
    row must be skipped, not crash the scan."""
    inline = gzip.compress(json.dumps(_upnext_payload()).encode())
    _make_cache_db(tmp_path, [
        (CANVAS_URL + "&nextToken=10", "2026-07-06 10:00:00", 1, b"GONE-0000"),
        (CANVAS_URL, "2026-07-06 18:57:45", 0, inline),
    ])
    entries = list(iter_cache_entries(tmp_path))
    assert len(entries) == 1  # only the inline row survives


def test_iter_cache_entries_ignores_unrelated_urls(tmp_path):
    other = gzip.compress(b'{"data":{}}')
    _make_cache_db(tmp_path, [
        ("https://uts-api.itunes.apple.com/uts/v3/clock-scores?x=1",
         "2026-07-06 12:00:00", 0, other),
        ("https://uts-api.itunes.apple.com/uts/v3/configurations?v=94",
         "2026-07-06 12:00:00Z", 0, other),
    ])
    assert list(iter_cache_entries(tmp_path)) == []


def test_iter_cache_entries_matches_playhistory_shelf_urls(tmp_path):
    body = json.dumps(_history_payload()).encode()
    _make_cache_db(tmp_path, [
        ("https://uts-api.itunes.apple.com/uts/v3/shelves/uts.col.PlayHistory?caller=js",
         "2026-07-06 12:00:00", 0, body),
    ])
    assert len(list(iter_cache_entries(tmp_path))) == 1


def test_parse_cache_missing_db_raises_clear_error(tmp_path):
    with pytest.raises(RuntimeError, match="open the TV app once"):
        parse_cache(tmp_path / "nowhere")


def test_scan_cache_malformed_json_body_is_skipped(tmp_path):
    good = gzip.compress(json.dumps(_upnext_payload()).encode())
    _make_cache_db(tmp_path, [
        (CANVAS_URL + "&nextToken=10", "2026-07-06 10:00:00", 0, b"\x00not json"),
        (CANVAS_URL, "2026-07-06 18:57:45", 0, good),
    ])
    scan = scan_cache(tmp_path)
    assert scan.snapshot_count == 2  # both matched the URL filter
    assert len(scan.events) == 7    # but only the good body parsed


# ---------------------------------------------------------------------------
# Cross-snapshot determinism / idempotency
# ---------------------------------------------------------------------------

def test_overlapping_history_snapshots_no_duplicates_earliest_time_wins(tmp_path):
    body = json.dumps(_history_payload()).encode()
    _make_cache_db(tmp_path, [
        (CANVAS_URL + "&nextToken=10", "2026-07-05 08:00:00", 0, gzip.compress(body)),
        (CANVAS_URL + "&nextToken=11", "2026-07-06 18:57:45", 0, gzip.compress(body)),
    ])
    events = parse_cache(tmp_path)
    assert len(events) == 20
    ids = [e.deterministic_id for e in events]
    assert len(ids) == len(set(ids))
    # first-occurrence-wins: the EARLIEST snapshot is the tightest upper
    # bound of the true watch time.
    expected = datetime(2026, 7, 5, 8, 0, 0, tzinfo=timezone.utc)
    assert all(e.start_time == expected for e in events)


def test_overlapping_up_next_snapshots_dedupe_same_day_activity(tmp_path):
    body = json.dumps(_upnext_payload()).encode()
    # request_key is UNIQUE in the real schema — distinct snapshots differ
    # in their utscf/query params, simulated here with a &snap= suffix.
    _make_cache_db(tmp_path, [
        (CANVAS_URL + "&snap=1", "2026-07-06 10:00:00", 0, gzip.compress(body)),
        (CANVAS_URL + "&snap=2", "2026-07-06 18:57:45", 0, gzip.compress(body)),
    ])
    events = parse_cache(tmp_path)
    # Same activity seen in two snapshots collapses to one event each.
    assert len(events) == 7
    ids = [e.deterministic_id for e in events]
    assert len(ids) == len(set(ids))


def test_mixed_snapshots_combine_up_next_and_history(tmp_path):
    _make_cache_db(tmp_path, [
        (CANVAS_URL, "2026-07-06 10:00:00", 0,
         gzip.compress(json.dumps(_upnext_payload()).encode())),
        (CANVAS_URL + "&nextToken=10", "2026-07-06 10:00:05", 0,
         gzip.compress(json.dumps(_history_payload()).encode())),
    ])
    events = parse_cache(tmp_path)
    kinds = {e.external_ids["kind"] for e in events}
    assert kinds == {"continue", "completed_prior_episode", "history"}
    assert len(events) == 27  # 7 up-next + 20 history
