"""Unit tests for the unified ingest pipeline (refactor #69)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

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


class _StatusTransport(httpx.BaseTransport):
    """Answer each call with the next queued status; record calls."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls: list[tuple[str, bytes, dict]] = []

    def handle_request(self, request):
        self.calls.append((str(request.url), request.read(),
                           dict(request.headers)))
        return httpx.Response(self.statuses.pop(0))


def _client_with(transport):
    from fulcra_common import BaseFulcraClient

    class _Client(BaseFulcraClient):
        def get_token(self): return "tok"

    return _Client(base_url="https://api.test", transport=transport)


def test_ingest_one_posts_single_record_json():
    """ingest_one uses the spec's single-record endpoint: one DataRecordV1
    as application/json to /ingest/v1/record (not a one-line JSONL batch) —
    per-record error bodies instead of opaque batch failures."""
    import json as _json

    transport = _FakeTransport()
    pipe = IngestPipeline(client=_client_with(transport))
    pipe.ingest_one(MomentEvent(
        definition_id="def-1", source_id="s",
        ts=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    ))
    assert len(transport.calls) == 1
    url, body, headers = transport.calls[0]
    assert url == "https://api.test/ingest/v1/record"
    assert headers["content-type"] == "application/json"
    record = _json.loads(body)
    assert record["specversion"] == 1
    assert set(record) >= {"specversion", "data", "metadata"}


def test_ingest_one_falls_back_to_batch_on_missing_endpoint():
    """A deploy without the single-record endpoint (404/405) must not break
    quick-record/tombstone writes — fall back to the proven batch path."""
    transport = _StatusTransport([404, 204])
    pipe = IngestPipeline(client=_client_with(transport))
    pipe.ingest_one(MomentEvent(
        definition_id="def-1", source_id="s",
        ts=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    ))
    assert len(transport.calls) == 2
    assert transport.calls[0][0].endswith("/ingest/v1/record")
    assert transport.calls[1][0].endswith("/ingest/v1/record/batch")


def test_ingest_one_does_not_swallow_real_errors():
    """A 422 (validation) or 500 from the single-record endpoint is a REAL
    error about this record — raise it, don't retry it into the batch."""
    import pytest

    transport = _StatusTransport([422])
    pipe = IngestPipeline(client=_client_with(transport))
    with pytest.raises(httpx.HTTPStatusError):
        pipe.ingest_one(MomentEvent(
            definition_id="def-1", source_id="s",
            ts=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        ))
    assert len(transport.calls) == 1


# ---- typed ingest (unwrapped records, single JSON / JSONL batch) ----


def test_ingest_typed_single_posts_json():
    transport = _FakeTransport()
    pipe = IngestPipeline(client=_client_with(transport))
    pipe.ingest_typed("MomentAnnotation",
                      [{"recorded_at": "2026-07-08T21:00:00Z", "sources": ["s"]}])
    url, body, headers = transport.calls[0]
    assert url == "https://api.test/ingest/v1/record/MomentAnnotation"
    assert headers["content-type"] == "application/json"
    assert json.loads(body)["sources"] == ["s"]


def test_ingest_typed_batch_posts_jsonl():
    """Content type must be application/x-jsonl exactly — the server 415s
    'application/x-jsonlines' (live-verified 2026-07-08)."""
    transport = _FakeTransport()
    pipe = IngestPipeline(client=_client_with(transport))
    pipe.ingest_typed("MomentAnnotation", [
        {"recorded_at": "2026-07-08T21:00:00Z", "sources": ["a"]},
        {"recorded_at": "2026-07-08T21:01:00Z", "sources": ["b"]},
    ])
    url, body, headers = transport.calls[0]
    assert url.endswith("/ingest/v1/record/MomentAnnotation")
    assert headers["content-type"] == "application/x-jsonl"
    lines = [json.loads(ln) for ln in body.decode().split("\n") if ln]
    assert [ln["sources"] for ln in lines] == [["a"], ["b"]]


def test_ingest_typed_empty_is_noop_and_errors_raise():
    transport = _FakeTransport()
    pipe = IngestPipeline(client=_client_with(transport))
    pipe.ingest_typed("MomentAnnotation", [])
    assert transport.calls == []
    bad = _StatusTransport([422])
    pipe2 = IngestPipeline(client=_client_with(bad))
    with pytest.raises(httpx.HTTPStatusError):
        pipe2.ingest_typed("MomentAnnotation",
                           [{"recorded_at": "x", "sources": ["s"]}])
