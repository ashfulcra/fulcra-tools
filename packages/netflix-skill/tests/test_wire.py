"""Native codec vectors; actual main transport/readback is in test_native."""
from fulcra_common.v1 import record_id_for
from fulcra_netflix.native import build_netflix_record

DEST = "Event/11111111-2222-4333-8444-555555555555"


def test_native_slim_fields_and_canonical_identity(ni, fixtures_dir):
    event = list(ni.parse_slim(fixtures_dir / "slim.csv"))[2]
    row = build_netflix_record(event, DEST, "slim")
    assert row["id"] == record_id_for(DEST, event.det_id)
    assert row["start_time"] == "2024-03-01T12:00:00Z"
    assert row["end_time"] == "2024-03-01T12:00:01Z"
    assert row["sources"] == [event.det_id] and row["tags"] == []
    assert row["title"] == event.title and row["note"] == event.note
    assert row["external_ids"] == {**event.external,
        "content_fingerprint": event.fingerprint, "timestamp_confidence": "low"}
    assert row["duration_seconds"] == 1 and type(row["duration_seconds"]) is int
    assert row["service"] == "netflix" and row["importer"] == "netflix-slim"
    assert row["category"] == "watched" and row["timestamp_confidence"] == "low"
    assert "data" not in row and "metadata" not in row


def test_native_rich_preserves_colon_movie_and_provenance(ni, fixtures_dir):
    event = next(e for e in ni.parse_rich(fixtures_dir / "gdpr.csv") if "Dune" in e.note)
    row = build_netflix_record(event, DEST, "rich")
    assert row["id"] == record_id_for(DEST, event.det_id)
    assert row["title"] == row["note"] == "Dune: Part Two"
    assert row["external_ids"]["profile"] == "User"
    assert row["external_ids"]["device_type"] == "TV"
    assert row["external_ids"]["timestamp_confidence"] == "high"
    assert row["duration_seconds"] == 9312 and type(row["duration_seconds"]) is int
    assert row["start_time"] < row["end_time"]


def test_legacy_positive_helpers_are_retired(ni):
    for name in ("build_record", "encode_batch", "ensure_watched_def", "get_token",
                 "make_api_client", "post_batch", "readback_verify", "PartialPostError",
                 "_classify_http_error"):
        assert not hasattr(ni, name)
