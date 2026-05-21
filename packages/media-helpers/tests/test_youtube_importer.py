"""Tests for the YouTube Takeout importer."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fulcra_media.importers.youtube import (
    normalize_entry,
    parse_takeout_json,
)

FIXTURE = Path(__file__).parent / "fixtures" / "youtube_watch_history_small.json"


def _load() -> list[dict]:
    return json.loads(FIXTURE.read_text())


# ---------- normalize_entry ----------

def test_normalize_entry_strips_watched_prefix():
    raw = _load()[0]  # OK Go - Here It Goes Again
    ev = normalize_entry(raw)
    assert ev is not None
    assert ev.importer == "youtube"
    assert ev.service == "youtube"
    assert ev.category == "watched"
    assert ev.title == "OK Go - Here It Goes Again"
    assert "Watched " not in ev.title


def test_normalize_entry_sets_channel_from_subtitles():
    raw = _load()[0]
    ev = normalize_entry(raw)
    assert ev.external_ids["channel"] == "OK Go"
    assert "channel_url" in ev.external_ids


def test_normalize_entry_carries_video_id_when_url_parseable():
    raw = _load()[0]
    ev = normalize_entry(raw)
    assert ev.external_ids["video_id"] == "dTAAsCNK7RA"
    assert ev.external_ids["url"] == "https://www.youtube.com/watch?v=dTAAsCNK7RA"


def test_normalize_entry_uses_time_field_as_timestamp():
    raw = _load()[0]
    ev = normalize_entry(raw)
    assert ev.start_time == datetime(2024, 5, 16, 22, 40, tzinfo=timezone.utc)
    # 1-second sentinel — Takeout doesn't surface watch duration
    assert (ev.end_time - ev.start_time).total_seconds() == 1


def test_normalize_entry_confidence_is_high():
    raw = _load()[0]
    ev = normalize_entry(raw)
    assert ev.timestamp_confidence == "high"


def test_normalize_entry_skips_non_youtube_header():
    raw = _load()[-1]  # Google Ads entry
    assert raw["header"] == "Google Ads"
    assert normalize_entry(raw) is None


def test_normalize_entry_skips_entries_with_no_time():
    raw = {"header": "YouTube", "title": "Watched x", "products": ["YouTube"]}
    assert normalize_entry(raw) is None


def test_normalize_entry_skips_entries_with_no_title():
    raw = {"header": "YouTube", "time": "2024-05-16T22:40:00.000Z",
           "products": ["YouTube"]}
    assert normalize_entry(raw) is None


def test_normalize_entry_handles_removed_video_no_titleUrl():
    """Privacy-removed entries have title but no titleUrl. Keep them."""
    raw = _load()[2]
    ev = normalize_entry(raw)
    assert ev is not None
    assert ev.title == "a video that has been removed"
    assert "video_id" not in ev.external_ids
    assert "url" not in ev.external_ids


def test_normalize_entry_extracts_v_param_from_short_url():
    """If title starts with 'Watched https://...' that whole URL becomes the title."""
    raw = _load()[3]
    ev = normalize_entry(raw)
    # raw_url_only is the v= param
    assert ev.external_ids["video_id"] == "raw_url_only"


def test_normalize_entry_deterministic_id_stable():
    raw = _load()[0]
    a = normalize_entry(raw)
    b = normalize_entry(raw)
    assert a.deterministic_id == b.deterministic_id
    assert a.deterministic_id.startswith("com.fulcra.media.youtube.v1.")


def test_normalize_entry_content_fingerprint_present():
    raw = _load()[0]
    ev = normalize_entry(raw)
    assert "content_fingerprint" in ev.external_ids


# ---------- parse_takeout_json ----------

def test_parse_takeout_json_returns_3_youtube_events():
    """5 raw entries: 1 ad (skipped), 1 OK Go, 1 3Blue1Brown,
    1 removed (kept), 1 raw-url (kept) → 4 YouTube events."""
    events = list(parse_takeout_json(FIXTURE))
    assert len(events) == 4
    assert all(e.importer == "youtube" for e in events)
    assert all(e.category == "watched" for e in events)


def test_parse_takeout_json_iterates_chronologically_or_as_given():
    """Takeout JSON is generally newest-first. We pass through that order."""
    events = list(parse_takeout_json(FIXTURE))
    times = [e.start_time for e in events]
    # OK Go (22:40) is first in fixture
    assert times[0] == datetime(2024, 5, 16, 22, 40, tzinfo=timezone.utc)


def test_parse_takeout_json_empty_array():
    """Empty Takeout file shouldn't crash."""
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("[]")
        path = Path(f.name)
    assert list(parse_takeout_json(path)) == []


def test_parse_takeout_json_handles_iso_with_milliseconds():
    """Takeout times have .000Z millisecond precision."""
    events = list(parse_takeout_json(FIXTURE))
    assert events[0].start_time.microsecond == 0
