from datetime import datetime, timezone

from fulcra_common.ingest import DurationEvent
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


def test_normalized_event_to_duration_event():
    """The pipeline-side factory introduced in refactor #69. NormalizedEvent
    stays the importer-side intermediate; FulcraClient.ingest_batch goes
    through DurationEvent + IngestPipeline."""
    UTC = timezone.utc
    ne = NormalizedEvent(
        importer="trakt", service="trakt", category="watched",
        note="n", title="t",
        start_time=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        end_time=datetime(2026, 5, 22, 12, 30, 0, tzinfo=UTC),
        deterministic_id="com.fulcra.media.trakt.deadbeef",
        timestamp_confidence="high",
        external_ids={"trakt_id": 1},
        extra_source_ids=("com.fulcra.content.watched.v1.fp",),
    )
    ev = ne.to_duration_event(
        definition_id="def-watched",
        tags=("tag-trakt",),
    )
    assert isinstance(ev, DurationEvent)
    assert ev.definition_id == "def-watched"
    assert ev.source_id == "com.fulcra.media.trakt.deadbeef"
    assert ev.tags == ("tag-trakt",)
    assert ev.extra_source_ids == ("com.fulcra.content.watched.v1.fp",)
    assert ev.note == "n" and ev.title == "t"
    assert ev.service == "trakt"
    assert ev.timestamp_confidence == "high"
    assert ev.external_ids == {"trakt_id": 1}
    assert ev.start == ne.start_time and ev.end == ne.end_time


def test_normalized_event_to_duration_event_default_tags_empty():
    UTC = timezone.utc
    ne = NormalizedEvent(
        importer="trakt", service="trakt", category="watched",
        note="n", title="t",
        start_time=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        end_time=datetime(2026, 5, 22, 12, 30, 0, tzinfo=UTC),
        deterministic_id="src",
        timestamp_confidence="high",
    )
    ev = ne.to_duration_event(definition_id="def")
    assert ev.tags == ()
    assert ev.extra_source_ids == ()
