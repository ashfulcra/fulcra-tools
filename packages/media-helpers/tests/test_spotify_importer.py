from pathlib import Path
from datetime import datetime, timezone

from fulcra_media.importers.spotify import parse_extended_zip

FIXTURE = Path(__file__).parent / "fixtures" / "spotify_extended_sample.zip"


def test_parse_extended_filters_skipped_and_short():
    events = list(parse_extended_zip(FIXTURE))
    # of 5 entries: keep music #1 (210s, !skipped), keep podcast #3 (2400s ms),
    # drop music #2 (skipped), drop podcast #4 (15s), drop #5 (no uri)
    assert len(events) == 2


def test_parse_extended_music_event_shape():
    events = list(parse_extended_zip(FIXTURE))
    e = next(e for e in events if e.title == "Get Lucky")
    assert e.importer == "spotify-extended"
    assert e.service == "spotify"
    assert e.category == "listened"
    assert e.note == "Daft Punk – Get Lucky"
    assert e.timestamp_confidence == "high"
    # ts is stream-end; start = ts - ms_played (210s)
    assert e.end_time == datetime(2026, 5, 10, 20, 30, 0, tzinfo=timezone.utc)
    assert e.start_time == datetime(2026, 5, 10, 20, 26, 30, tzinfo=timezone.utc)
    assert e.external_ids["kind"] == "music"
    assert e.external_ids["content_fingerprint"] == "music:daft-punk:get-lucky"


def test_parse_extended_podcast_event_shape():
    events = list(parse_extended_zip(FIXTURE))
    e = next(e for e in events if "Crime Machine" in e.note)
    assert e.note == "Reply All – The Crime Machine, Part I"
    assert e.title == "Reply All"
    assert e.external_ids["kind"] == "podcast"
    assert e.external_ids["content_fingerprint"].startswith("podcast:reply-all:")


def test_parse_extended_deterministic_id_per_stream():
    events = list(parse_extended_zip(FIXTURE))
    ids = [e.deterministic_id for e in events]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("com.fulcra.media.spotify-extended.v1.") for i in ids)
