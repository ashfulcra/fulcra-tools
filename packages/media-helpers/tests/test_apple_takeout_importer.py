from pathlib import Path
from datetime import datetime, timezone

import pytest

from fulcra_media.importers.apple_takeout import parse_playback_csv
from fulcra_media.importers.base import NormalizedEvent

FIXTURE = Path(__file__).parent / "fixtures" / "apple_takeout_playback_sample.csv"


def test_parse_playback_filters_to_play_events():
    events = list(parse_playback_csv(FIXTURE))
    # 5 rows: 4 PLAY + 1 PAUSE → 4 events
    assert len(events) == 4


def test_parse_playback_movie_event_shape():
    events = list(parse_playback_csv(FIXTURE))
    e = next(e for e in events if e.title == "Dune: Part Two")
    assert e.importer == "apple-takeout"
    assert e.service == "apple-tv"
    assert e.category == "watched"
    assert e.note == "Dune: Part Two"
    # Times treated as UTC by default
    assert e.start_time == datetime(2025, 1, 15, 20, 30, 0, tzinfo=timezone.utc)
    assert e.end_time == datetime(2025, 1, 15, 23, 16, 0, tzinfo=timezone.utc)
    assert e.timestamp_confidence == "high"
    assert e.external_ids["device_type"] == "Apple TV"
    assert e.external_ids["device_model"] == "Apple TV 4K (3rd generation)"
    assert e.external_ids["country"] == "US"
    assert e.external_ids["content_fingerprint"] == "movie:dune-part-two"


def test_parse_playback_episode_event_shape():
    events = list(parse_playback_csv(FIXTURE))
    e = next(e for e in events if e.title == "Severance")
    assert e.note == "Severance S02E01 – The We We Are"
    assert e.external_ids["content_fingerprint"] == "tv:severance:s02e01"


def test_parse_playback_deterministic_id_per_session():
    events = list(parse_playback_csv(FIXTURE))
    ids = [e.deterministic_id for e in events]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("com.fulcra.media.apple-takeout.v1.") for i in ids)
