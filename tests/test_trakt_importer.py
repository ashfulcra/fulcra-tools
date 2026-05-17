import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fulcra_media.importers.trakt import normalize_history, detect_clusters
from fulcra_media.importers.base import NormalizedEvent

FIXTURE = Path(__file__).parent / "fixtures" / "trakt_history_sample.json"


def _items():
    return json.loads(FIXTURE.read_text())


def test_detect_clusters_threshold_3():
    """3 items share watched_at=2026-05-15T19:41 — detect with threshold 3."""
    clusters = detect_clusters(_items(), threshold=3)
    assert "2026-05-15T19:41:00.000Z" in clusters
    assert clusters["2026-05-15T19:41:00.000Z"] == 3


def test_normalize_history_emits_one_event_per_item():
    events = list(normalize_history(_items(), cluster_threshold=3))
    assert len(events) == 6


def test_normalize_history_episode_event_shape():
    events = list(normalize_history(_items(), cluster_threshold=3))
    e = next(e for e in events if e.external_ids.get("trakt_history_id") == 100001)
    assert e.importer == "trakt"
    assert e.service == "trakt"
    assert e.category == "watched"
    assert e.note == "Severance S02E01 – The We We Are"
    assert e.title == "Severance"
    assert e.start_time == datetime(2026, 5, 12, 20, 30, 0, tzinfo=timezone.utc)
    # runtime 51 -> end = start + 51min
    assert (e.end_time - e.start_time).total_seconds() == 51 * 60
    assert e.timestamp_confidence == "high"   # action=scrobble
    assert e.external_ids["trakt_action"] == "scrobble"
    assert e.external_ids["content_fingerprint"] == "tv:severance:s02e01"
    assert e.external_ids["imdb"] == "tt5790298"


def test_normalize_history_movie_event_shape():
    events = list(normalize_history(_items(), cluster_threshold=3))
    e = next(e for e in events if e.external_ids.get("trakt_history_id") == 100002)
    assert e.note == "Dune: Part Two (2024)"
    assert e.title == "Dune: Part Two"
    assert e.external_ids["content_fingerprint"] == "movie:dune-part-two:y2024"
    # action=watch -> medium
    assert e.timestamp_confidence == "medium"


def test_normalize_history_cluster_items_flagged_low():
    events = list(normalize_history(_items(), cluster_threshold=3))
    cluster_evs = [e for e in events if e.external_ids.get("trakt_history_id") in (100003, 100004, 100005)]
    assert len(cluster_evs) == 3
    for e in cluster_evs:
        assert e.timestamp_confidence == "low"
        assert e.external_ids["timestamp_cluster_size"] == 3


def test_normalize_history_checkin_is_high_confidence():
    events = list(normalize_history(_items(), cluster_threshold=3))
    e = next(e for e in events if e.external_ids.get("trakt_history_id") == 100006)
    assert e.timestamp_confidence == "high"  # action=checkin


def test_normalize_history_deterministic_id_uses_history_id():
    events = list(normalize_history(_items(), cluster_threshold=3))
    e = events[0]
    assert e.deterministic_id.startswith("com.fulcra.media.trakt.v1.history.")
    history_id_str = e.deterministic_id.rsplit(".", 1)[-1]
    assert history_id_str.isdigit()
