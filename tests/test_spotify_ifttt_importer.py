"""Tests for the Spotify IFTTT->GDrive xlsx importer."""
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fulcra_media.importers.spotify_ifttt import (
    parse_ifttt_timestamp,
    parse_ifttt_zip,
)

FIXTURE = Path(__file__).parent / "fixtures" / "spotify_ifttt_small.zip"


def test_parse_ifttt_timestamp_basic():
    ts = parse_ifttt_timestamp("November 4, 2022 at 03:53PM", tz=timezone.utc)
    assert ts == datetime(2022, 11, 4, 15, 53, tzinfo=timezone.utc)


def test_parse_ifttt_timestamp_handles_local_tz():
    """IFTTT renders the user's local time. Importer accepts a tz override."""
    eastern = ZoneInfo("America/New_York")
    ts = parse_ifttt_timestamp("November 4, 2022 at 03:53PM", tz=eastern)
    # 3:53 PM Eastern → 20:53 UTC (DST ended Nov 6, 2022, so Nov 4 is EDT/-04:00)
    assert ts.astimezone(timezone.utc) == datetime(2022, 11, 4, 19, 53, tzinfo=timezone.utc)


def test_parse_ifttt_timestamp_invalid():
    with pytest.raises(ValueError):
        parse_ifttt_timestamp("not a date", tz=timezone.utc)


def test_parse_ifttt_zip_yields_events():
    events = list(parse_ifttt_zip(FIXTURE))
    # Fixture has 9 rows across 4 files. Two exact (ts, track_id) pairs are
    # cross-applet duplicates (Nov 4 03:53PM Reelin', Oct 23 23:45 Hounds),
    # so 7 unique events remain.
    assert len(events) == 7


def test_parse_ifttt_zip_dedupe_keeps_same_track_different_times():
    """A real replay (same track, different timestamps) must produce two events."""
    events = list(parse_ifttt_zip(FIXTURE))
    hounds = [e for e in events if "Hounds of Love" in e.note]
    # Original 04:23PM play + 23:45 replay = 2 distinct events
    assert len(hounds) == 2
    assert hounds[0].deterministic_id != hounds[1].deterministic_id


def test_parse_ifttt_zip_drops_exact_cross_applet_duplicate():
    """The two applets log the same play with the same timestamp — keep only one."""
    events = list(parse_ifttt_zip(FIXTURE))
    reelin = [e for e in events if "Reelin'" in e.note and e.start_time.year == 2022]
    assert len(reelin) == 1


def test_parse_ifttt_zip_normalizes_numerified_track_name():
    """Track '1901' (numeric) is coerced to float by spreadsheet — must round-trip to str."""
    events = list(parse_ifttt_zip(FIXTURE))
    phoenix = [e for e in events if e.external_ids.get("artist") == "Phoenix"
               and e.external_ids.get("track_id") == "1Ug5wxoHthwxctyWTUMGta"]
    assert len(phoenix) == 1
    assert phoenix[0].title == "1901"
    assert "1901" in phoenix[0].note
    assert phoenix[0].external_ids["content_fingerprint"] == "music:phoenix:1901"


def test_parse_ifttt_zip_event_shape():
    events = list(parse_ifttt_zip(FIXTURE))
    e = next(e for e in events if "Reelin'" in e.note and e.start_time.year == 2022)
    assert e.importer == "spotify-ifttt"
    assert e.service == "spotify"
    assert e.category == "listened"
    # Point-in-time + 1s sentinel (Fulcra silently drops zero-duration events)
    assert (e.end_time - e.start_time).total_seconds() == 1
    # No duration data from IFTTT — confidence is medium (IFTTT polls
    # /me/player/recently-played so timestamps are Spotify's, not the poll time)
    assert e.timestamp_confidence == "medium"
    assert e.external_ids["track_id"] == "1I7zHEdDx8Ny5RxzYPqsU2"
    assert e.external_ids["artist"] == "Steely Dan"
    assert e.external_ids["content_fingerprint"] == "music:steely-dan:reelin-in-the-years"


def test_parse_ifttt_zip_deterministic_ids_stable_across_runs():
    a = list(parse_ifttt_zip(FIXTURE))
    b = list(parse_ifttt_zip(FIXTURE))
    assert [e.deterministic_id for e in a] == [e.deterministic_id for e in b]
    assert all(e.deterministic_id.startswith("com.fulcra.media.spotify-ifttt.")
               for e in a)


def test_parse_ifttt_zip_results_sorted_by_time():
    """Deterministic ordering makes downstream verify + dedup deterministic."""
    events = list(parse_ifttt_zip(FIXTURE))
    times = [e.start_time for e in events]
    assert times == sorted(times)


def test_parse_ifttt_zip_accepts_tz_override():
    eastern = ZoneInfo("America/New_York")
    events = list(parse_ifttt_zip(FIXTURE, tz=eastern))
    e = next(e for e in events if "Reelin'" in e.note and e.start_time.year == 2022)
    assert e.start_time.astimezone(timezone.utc) == datetime(
        2022, 11, 4, 19, 53, tzinfo=timezone.utc
    )
