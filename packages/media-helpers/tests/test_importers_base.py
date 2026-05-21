from datetime import datetime, timezone

from fulcra_media.importers.base import NormalizedEvent


def test_normalized_event_has_required_fields():
    event = NormalizedEvent(
        importer="netflix-slim",
        service="netflix",
        category="watched",
        note="Stranger Things S01E01 – The Vanishing of Will Byers",
        title="Stranger Things",
        start_time=datetime(2024, 8, 14, 21, 0, tzinfo=timezone.utc),
        end_time=datetime(2024, 8, 14, 21, 30, tzinfo=timezone.utc),
        deterministic_id="com.fulcra.media.netflix.abc123def4567890",
        timestamp_confidence="high",
        external_ids={"profile": "default"},
    )
    assert event.importer == "netflix-slim"
    assert event.service == "netflix"
    assert event.category == "watched"
    assert event.timestamp_confidence == "high"
    assert event.external_ids == {"profile": "default"}


def test_normalized_event_external_ids_defaults_to_empty():
    event = NormalizedEvent(
        importer="netflix-slim",
        service="netflix",
        category="watched",
        note="x",
        title="x",
        start_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        deterministic_id="id",
        timestamp_confidence="low",
    )
    assert event.external_ids == {}


def test_normalized_event_rejects_naive_datetimes():
    import pytest
    with pytest.raises(ValueError):
        NormalizedEvent(
            importer="x", service="x", category="watched",
            note="x", title="x",
            start_time=datetime(2024, 1, 1),  # no tzinfo
            end_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            deterministic_id="id", timestamp_confidence="high",
        )
