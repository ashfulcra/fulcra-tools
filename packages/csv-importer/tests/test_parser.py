"""Tests for the general CSV → GenericEvent parser."""
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fulcra_csv import ColumnMap, parse_csv, parse_value
from fulcra_csv.events import GenericEvent, slugify, derive_source_id

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_value_strips_and_stringifies():
    assert parse_value("  hello  ") == "hello"
    assert parse_value("") == ""
    assert parse_value(None) == ""


def test_slugify_collapses_punctuation():
    assert slugify("Hey, Mister!") == "hey-mister"
    assert slugify("Pre-existing-Hyphens") == "pre-existing-hyphens"


def test_derive_source_id_is_stable():
    a = derive_source_id("prefix", "ts", "title", None)
    b = derive_source_id("prefix", "ts", "title", None)
    assert a == b
    assert a.startswith("prefix.")
    # Field order matters
    c = derive_source_id("prefix", "title", "ts", None)
    assert a != c


def test_parse_minimal_csv():
    events = list(parse_csv(FIXTURES / "minimal.csv"))
    assert len(events) == 2
    e = events[0]
    assert isinstance(e, GenericEvent)
    assert e.start_time == datetime(2024, 5, 10, 14, 33, tzinfo=timezone.utc)
    # No end_time → 1s sentinel
    assert (e.end_time - e.start_time).total_seconds() == 1
    assert e.title == "First entry"
    assert e.note == "First entry"


def test_parse_csv_with_custom_columns():
    colmap = ColumnMap(
        timestamp="when",
        title="track",
        subtitle="artist",
        source_id="track_id",
        extras=(("url", "spotify_url"),),
    )
    events = list(parse_csv(
        FIXTURES / "spotify_ifttt_sample.csv",
        column_map=colmap,
        default_tag="spotify",
    ))
    assert len(events) == 3
    first = events[0]
    assert first.title == "Reelin' In The Years"
    assert first.note == "Steely Dan – Reelin' In The Years"
    assert first.tag == "spotify"
    assert first.external_ids["spotify_url"].startswith("https://open.spotify.com/track/")
    # source_id is a hash that incorporates the timestamp AND the explicit
    # id column (so same-track-different-time replays stay distinct), so
    # the track id isn't literal in the source_id.
    assert first.source_id.startswith("fulcra-csv.v1.")


def test_parse_csv_handles_numeric_track_name():
    """'1901' parses as text via csv.DictReader (csv always yields strings)."""
    colmap = ColumnMap(timestamp="when", title="track", subtitle="artist")
    events = list(parse_csv(FIXTURES / "spotify_ifttt_sample.csv", column_map=colmap))
    phoenix = [e for e in events if e.note == "Phoenix – 1901"]
    assert len(phoenix) == 1
    assert phoenix[0].title == "1901"


def test_parse_csv_with_duration_column():
    colmap = ColumnMap(
        timestamp="timestamp", title="title", subtitle="subtitle",
        tag="service", duration_seconds="duration_s", source_id="id",
    )
    events = list(parse_csv(FIXTURES / "per_row_tag.csv", column_map=colmap))
    assert len(events) == 3
    song = events[0]
    assert (song.end_time - song.start_time).total_seconds() == 180
    assert song.tag == "spotify"
    movie = events[2]
    # No subtitle, just title — note falls back to title alone
    assert movie.note == "Movie C"


def test_parse_csv_tz_for_naive_timestamps():
    """When the row's timestamp has no tz, the parser stamps the provided tz."""
    csv_path = FIXTURES / "spotify_ifttt_sample.csv"
    eastern = ZoneInfo("America/New_York")
    colmap = ColumnMap(timestamp="when", title="track", subtitle="artist")
    events = list(parse_csv(csv_path, column_map=colmap, tz=eastern))
    e = events[0]
    # Nov 4 2022 → EDT/-04:00
    assert e.start_time.astimezone(timezone.utc) == datetime(2022, 11, 4, 19, 53, tzinfo=timezone.utc)


def test_parse_csv_rejects_missing_timestamp_column(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("foo,bar\n1,2\n")
    with pytest.raises(ValueError, match="timestamp column 'timestamp' not found"):
        list(parse_csv(bad))


def test_parse_csv_skips_blank_timestamp(tmp_path):
    csv = tmp_path / "blank.csv"
    csv.write_text("timestamp,title\n,empty ts row\n2024-01-01,kept row\n")
    events = list(parse_csv(csv))
    assert len(events) == 1
    assert events[0].title == "kept row"


def test_parse_csv_skips_blank_note(tmp_path):
    """A row with neither title nor note has nothing to label — skip it."""
    csv = tmp_path / "blank_note.csv"
    csv.write_text('timestamp,title\n2024-01-01,\n')
    events = list(parse_csv(csv))
    assert events == []


def test_source_id_deterministic_across_runs():
    csv = FIXTURES / "minimal.csv"
    a = [e.source_id for e in parse_csv(csv)]
    b = [e.source_id for e in parse_csv(csv)]
    assert a == b
    assert all(s.startswith("fulcra-csv.v1.") for s in a)


def test_source_id_prefix_override():
    csv = FIXTURES / "minimal.csv"
    events = list(parse_csv(csv, source_id_prefix="com.example.foo"))
    assert all(e.source_id.startswith("com.example.foo.") for e in events)
