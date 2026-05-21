"""Tests for fulcra_media.importers.generic_csv (media adapter over fulcra-csv)."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fulcra_csv import ColumnMap

from fulcra_media.importers.generic_csv import parse_media_csv

FIXTURES = Path(__file__).parent / "fixtures"


def test_listened_defaults_to_music_fingerprint():
    events = list(parse_media_csv(
        FIXTURES / "generic_listened.csv",
        service="example-stream",
        category="listened",
    ))
    assert len(events) == 3
    e = events[0]
    assert e.importer == "generic-csv"
    assert e.service == "example-stream"
    assert e.category == "listened"
    assert e.note == "Artist A – First Song"
    assert e.title == "First Song"
    assert e.external_ids["content_fingerprint"] == "music:artist-a:first-song"
    assert e.external_ids["artist"] == "Artist A"
    assert e.deterministic_id.startswith("com.fulcra.media.generic-csv.example-stream.v1.")


def test_listened_repeat_play_keeps_two_events():
    """Same track at different timestamps = real replay (rows 1 and 3)."""
    events = list(parse_media_csv(
        FIXTURES / "generic_listened.csv",
        service="example-stream",
        category="listened",
    ))
    first_song = [e for e in events if e.title == "First Song"]
    assert len(first_song) == 2
    assert first_song[0].deterministic_id != first_song[1].deterministic_id


def test_watched_defaults_to_movie_fingerprint():
    cm = ColumnMap(
        timestamp="timestamp",
        title="title",
        duration_seconds="duration_s",
    )
    events = list(parse_media_csv(
        FIXTURES / "generic_watched.csv",
        service="manual",
        category="watched",
        column_map=cm,
    ))
    assert len(events) == 2
    e = events[0]
    assert e.category == "watched"
    assert e.title == "Dune Part Two"
    assert (e.end_time - e.start_time).total_seconds() == 9525
    assert e.external_ids["content_fingerprint"] == "movie:dune-part-two"


def test_invalid_category_raises():
    with pytest.raises(ValueError, match="category must be one of"):
        list(parse_media_csv(
            FIXTURES / "generic_listened.csv",
            service="foo", category="wishlisted",
        ))


def test_deterministic_id_stable_across_runs():
    a = list(parse_media_csv(
        FIXTURES / "generic_listened.csv",
        service="example-stream", category="listened",
    ))
    b = list(parse_media_csv(
        FIXTURES / "generic_listened.csv",
        service="example-stream", category="listened",
    ))
    assert [e.deterministic_id for e in a] == [e.deterministic_id for e in b]


def test_explicit_fingerprint_kind_none_skips_fingerprint():
    events = list(parse_media_csv(
        FIXTURES / "generic_listened.csv",
        service="example-stream", category="listened",
        fingerprint_kind=None,
    ))
    assert all("content_fingerprint" not in e.external_ids for e in events)
