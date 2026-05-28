"""Unit tests for the unified ingest pipeline (refactor #69)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from fulcra_common.ingest import (
    DurationEvent,
    IngestPipeline,
    MomentEvent,
)

UTC = timezone.utc


def test_moment_event_holds_required_fields():
    ev = MomentEvent(
        definition_id="def-1",
        source_id="src-1",
        ts=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )
    assert ev.definition_id == "def-1"
    assert ev.source_id == "src-1"
    assert ev.ts == datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    assert ev.extra_source_ids == ()
    assert ev.tags == ()
    assert ev.external_ids == {}
    assert ev.note is None and ev.title is None and ev.service is None


def test_duration_event_computes_duration_seconds():
    ev = DurationEvent(
        definition_id="def-1",
        source_id="src-1",
        start=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        end=datetime(2026, 5, 22, 12, 5, 0, tzinfo=UTC),
    )
    assert ev.duration_seconds == 300


def test_duration_event_clamps_negative_duration_to_zero():
    # Defensive — a misordered start/end shouldn't propagate a negative
    # into the wire payload (matches the max(0, …) clamp the three legacy
    # sites used).
    ev = DurationEvent(
        definition_id="def-1",
        source_id="src-1",
        start=datetime(2026, 5, 22, 12, 5, 0, tzinfo=UTC),
        end=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )
    assert ev.duration_seconds == 0


def test_pipeline_build_record_moment_matches_wire():
    pipe = IngestPipeline(client=None)  # build_record needs no client
    ev = MomentEvent(
        definition_id="def-1",
        source_id="src-1",
        ts=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        tags=("tag-a", "tag-b"),
        note="hello",
    )
    rec = pipe.build_record(ev)
    assert rec["specversion"] == 1
    assert rec["metadata"]["data_type"] == "MomentAnnotation"
    assert rec["metadata"]["recorded_at"] == "2026-05-22T12:00:00Z"
    assert rec["metadata"]["tags"] == ["tag-a", "tag-b"]
    assert rec["metadata"]["source"] == [
        "src-1", "com.fulcradynamics.annotation.def-1",
    ]
    payload = json.loads(rec["data"])
    # Moment events do NOT get duration_seconds.
    assert "duration_seconds" not in payload
    assert payload.get("note") == "hello"


def test_pipeline_build_record_duration_injects_duration_seconds():
    pipe = IngestPipeline(client=None)
    ev = DurationEvent(
        definition_id="def-1",
        source_id="src-1",
        start=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        end=datetime(2026, 5, 22, 12, 5, 0, tzinfo=UTC),
        tags=("tag-a",),
        service="web",
        note="hi",
        title="t",
        external_ids={"foo": "bar"},
    )
    rec = pipe.build_record(ev)
    assert rec["metadata"]["data_type"] == "DurationAnnotation"
    assert rec["metadata"]["recorded_at"] == {
        "start_time": "2026-05-22T12:00:00Z",
        "end_time":   "2026-05-22T12:05:00Z",
    }
    payload = json.loads(rec["data"])
    # The #30 defensive field — must be injected by the pipeline for
    # every DurationEvent, since that's the whole point of consolidating.
    assert payload["duration_seconds"] == 300
    assert payload["service"] == "web"
    assert payload["note"] == "hi"
    assert payload["title"] == "t"
    assert payload["external_ids"] == {"foo": "bar"}


def test_pipeline_build_record_appends_extra_source_ids():
    pipe = IngestPipeline(client=None)
    ev = DurationEvent(
        definition_id="def-1",
        source_id="src-1",
        start=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        end=datetime(2026, 5, 22, 12, 5, 0, tzinfo=UTC),
        extra_source_ids=("com.fulcra.content.listened.v1.abc",),
    )
    rec = pipe.build_record(ev)
    assert rec["metadata"]["source"] == [
        "src-1",
        "com.fulcra.content.listened.v1.abc",
        "com.fulcradynamics.annotation.def-1",
    ]


def test_pipeline_build_record_emits_comment_at_top_level_data():
    """Quick-record's `comment` is distinct from `note` and lives at the
    top of the data payload (not nested under external_ids)."""
    pipe = IngestPipeline(client=None)
    ev = MomentEvent(
        definition_id="def-1", source_id="s",
        ts=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        comment="hi",
    )
    rec = pipe.build_record(ev)
    payload = json.loads(rec["data"])
    assert payload["comment"] == "hi"
    assert "note" not in payload  # comment ≠ note


def test_pipeline_build_record_without_definition_id():
    """The daemon tombstone path writes events with no annotation
    definition — the source array stops at source_id."""
    pipe = IngestPipeline(client=None)
    ev = MomentEvent(
        definition_id=None, source_id="s",
        ts=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    )
    rec = pipe.build_record(ev)
    assert rec["metadata"]["source"] == ["s"]  # no def-source appended


# ---- I/O path tests via fake httpx transport ----


class _FakeTransport(httpx.BaseTransport):
    def __init__(self):
        self.calls: list[tuple[str, bytes, dict]] = []

    def handle_request(self, request):
        self.calls.append((str(request.url), request.read(),
                           dict(request.headers)))
        return httpx.Response(204)


def test_pipeline_ingest_batch_posts_jsonl():
    from fulcra_common import BaseFulcraClient

    class _Client(BaseFulcraClient):
        def get_token(self):  # bypass the fulcra CLI shell-out
            return "tok"

    transport = _FakeTransport()
    client = _Client(base_url="https://api.test", transport=transport)
    pipe = IngestPipeline(client=client)
    pipe.ingest_batch([
        MomentEvent(
            definition_id="def-1", source_id="s",
            ts=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        ),
    ])
    assert len(transport.calls) == 1
    url, body, headers = transport.calls[0]
    assert url == "https://api.test/ingest/v1/record/batch"
    assert headers["content-type"] == "application/x-jsonl"
    assert headers["authorization"] == "Bearer tok"
    assert b"MomentAnnotation" in body


def test_pipeline_ingest_batch_empty_is_a_noop():
    from fulcra_common import BaseFulcraClient

    class _Client(BaseFulcraClient):
        def get_token(self): return "tok"

    transport = _FakeTransport()
    client = _Client(base_url="https://api.test", transport=transport)
    IngestPipeline(client=client).ingest_batch([])
    assert transport.calls == []


def test_pipeline_ingest_batch_raises_without_client():
    pipe = IngestPipeline(client=None)
    try:
        pipe.ingest_batch([
            MomentEvent(
                definition_id="d", source_id="s",
                ts=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
            ),
        ])
    except RuntimeError as exc:
        assert "BaseFulcraClient" in str(exc)
    else:
        raise AssertionError("ingest_batch without client must raise")
