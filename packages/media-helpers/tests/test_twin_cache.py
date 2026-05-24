"""Tests for the cross-batch twin cache."""
import json
from datetime import datetime, timezone


from fulcra_csv import find_low_conf_twins
from fulcra_media import twin_cache
from fulcra_media.importers.base import NormalizedEvent


def _evt(*, fp: str, confidence: str, sid: str,
         ts: datetime | None = None) -> NormalizedEvent:
    ts = ts or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return NormalizedEvent(
        importer="test",
        service="test",
        category="watched",
        note="x",
        title="x",
        start_time=ts,
        end_time=ts,
        deterministic_id=sid,
        timestamp_confidence=confidence,
        external_ids={"content_fingerprint": fp},
    )


def test_load_empty_when_missing(tmp_path):
    cache_path = tmp_path / "cache.json"
    assert twin_cache.load(cache_path) == {}


def test_load_empty_on_corrupt_file(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("not json")
    assert twin_cache.load(cache_path) == {}


def test_record_imported_events_writes_high_conf_with_fingerprint(tmp_path):
    cache_path = tmp_path / "cache.json"
    events = [
        _evt(fp="tv:dune:s01e01", confidence="high", sid="src-1"),
        _evt(fp="music:phoenix:1901", confidence="high", sid="src-2"),
        # low-conf — excluded
        _evt(fp="tv:other:s01e01", confidence="low", sid="src-3"),
        # high-conf but no fingerprint — excluded
        NormalizedEvent(
            importer="t", service="s", category="watched",
            note="n", title="n",
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            deterministic_id="src-4", timestamp_confidence="high",
            external_ids={},
        ),
    ]
    added = twin_cache.record_imported_events(events, cache_path)
    assert added == 2
    cache = twin_cache.load(cache_path)
    assert set(cache.keys()) == {"tv:dune:s01e01", "music:phoenix:1901"}
    assert cache["tv:dune:s01e01"]["source_id"] == "src-1"


def test_record_idempotent_overwrites_existing(tmp_path):
    cache_path = tmp_path / "cache.json"
    e_v1 = _evt(fp="tv:x:s01e01", confidence="high", sid="trakt-1")
    e_v2 = _evt(fp="tv:x:s01e01", confidence="high", sid="netflix-1",
                ts=datetime(2026, 4, 1, tzinfo=timezone.utc))
    twin_cache.record_imported_events([e_v1], cache_path)
    twin_cache.record_imported_events([e_v2], cache_path)
    cache = twin_cache.load(cache_path)
    assert len(cache) == 1
    # Last-write-wins; that's intentional for a "best known source"
    assert cache["tv:x:s01e01"]["source_id"] == "netflix-1"


def test_load_for_twin_lookup_returns_event_compatible_objects(tmp_path):
    cache_path = tmp_path / "cache.json"
    twin_cache.record_imported_events(
        [_evt(fp="music:phoenix:1901", confidence="high", sid="src-1")],
        cache_path,
    )
    cached = twin_cache.load_for_twin_lookup(cache_path)
    assert len(cached) == 1
    c = cached[0]
    assert c.external_ids["content_fingerprint"] == "music:phoenix:1901"
    assert c.external_ids["timestamp_confidence"] == "high"
    assert c.source_id == "src-1"


def test_load_for_twin_lookup_feeds_find_low_conf_twins_correctly(tmp_path):
    """Integration: cached high-conf + incoming low-conf → matched pair."""
    cache_path = tmp_path / "cache.json"
    twin_cache.record_imported_events(
        [_evt(fp="tv:dune:s01e01", confidence="high", sid="netflix-1",
              ts=datetime(2026, 4, 1, tzinfo=timezone.utc))],
        cache_path,
    )
    incoming_low_conf = _evt(
        fp="tv:dune:s01e01", confidence="low", sid="trakt-1",
        ts=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    pairs = find_low_conf_twins(
        [incoming_low_conf],
        extra_pool=twin_cache.load_for_twin_lookup(cache_path),
    )
    assert len(pairs) == 1
    low, high = pairs[0]
    # `low` is a NormalizedEvent (deterministic_id); `high` came from the
    # cache (source_id). Use the helper that handles both.
    from fulcra_media.twin_cache import _source_id_of
    assert _source_id_of(low) == "trakt-1"
    assert _source_id_of(high) == "netflix-1"


def test_load_for_twin_lookup_skips_corrupt_timestamps(tmp_path):
    """Garbage timestamps in the cache shouldn't crash the loader."""
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({
        "fp-1": {"source_id": "src-1", "start_time": "garbage"},
        "fp-2": {"source_id": "src-2"},  # missing start_time
        "fp-3": {"source_id": "src-3", "start_time": "2026-01-01T00:00:00Z"},
    }))
    cached = twin_cache.load_for_twin_lookup(cache_path)
    assert len(cached) == 1
    assert cached[0].source_id == "src-3"


def test_clear_removes_the_file(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{}")
    twin_cache.clear(cache_path)
    assert not cache_path.exists()


def test_clear_no_file_is_noop(tmp_path):
    """Calling clear when the file doesn't exist shouldn't crash."""
    cache_path = tmp_path / "cache.json"
    twin_cache.clear(cache_path)
    assert not cache_path.exists()
