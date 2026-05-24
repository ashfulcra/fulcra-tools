from datetime import datetime, timezone

import httpx
import pytest

from fulcra_media.fulcra import FulcraClient, ImportResult
from fulcra_media.importers.base import NormalizedEvent
from fulcra_media.state import State
from media_test_helpers import json_response


@pytest.fixture(autouse=True)
def fake_token(mocker):
    mocker.patch.dict("os.environ", {"FULCRA_ACCESS_TOKEN": "test-token"})


def _ev(i: int, det_id: str | None = None) -> NormalizedEvent:
    return NormalizedEvent(
        importer="netflix-slim",
        service="netflix",
        category="watched",
        note=f"N{i}",
        title=f"T{i}",
        start_time=datetime(2026, 5, 12, 21, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 12, 22, 0, tzinfo=timezone.utc),
        deterministic_id=det_id or f"com.fulcra.media.netflix.id{i:04d}",
        timestamp_confidence="low",
    )


def test_run_import_dedupes_against_existing(recording_transport):
    """One existing, two new -> ingest 2, skip 1, verify 2."""
    # Real Fulcra records carry source_id pointing at the current def, plus
    # the per-event sources array. fetch_existing_source_ids filters records
    # by source_id to ignore orphans from soft-deleted defs.
    _DEF_SID = "com.fulcradynamics.annotation.def-watched"
    existing_response = [
        {"source_id": _DEF_SID, "sources": ["com.fulcra.media.netflix.id0001", _DEF_SID]},
    ]
    after_response = [
        {"source_id": _DEF_SID, "sources": ["com.fulcra.media.netflix.id0001", _DEF_SID]},
        {"source_id": _DEF_SID, "sources": ["com.fulcra.media.netflix.id0002", _DEF_SID]},
        {"source_id": _DEF_SID, "sources": ["com.fulcra.media.netflix.id0003", _DEF_SID]},
    ]
    call_counter = {"get": 0, "post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            call_counter["get"] += 1
            return json_response(200, existing_response if call_counter["get"] == 1 else after_response)
        if request.method == "POST":
            call_counter["post"] += 1
            return httpx.Response(204)
        pytest.fail(request.url)

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(
        watched_definition_id="def-watched",
        listened_definition_id="def-listened",
        tag_ids={"netflix": "tag-netflix"},
    )
    events = [_ev(1), _ev(2), _ev(3)]
    result = client.run_import(events, state, chunk_size=10)
    assert isinstance(result, ImportResult)
    assert result.skipped_existing == 1
    assert result.posted == 2
    assert result.verified == 2
    assert call_counter["post"] == 1
    assert call_counter["get"] == 2


def test_run_import_no_new_events_does_not_post(recording_transport):
    _DEF_SID = "com.fulcradynamics.annotation.d"
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, [
                {"source_id": _DEF_SID, "sources": ["com.fulcra.media.netflix.id0001", _DEF_SID]},
            ])
        pytest.fail(f"unexpected POST {request.url}")
    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(watched_definition_id="d", listened_definition_id="d2", tag_ids={"netflix": "t"})
    result = client.run_import([_ev(1)], state, chunk_size=10)
    assert result.posted == 0
    assert result.skipped_existing == 1
    assert result.verified == 0


def test_run_import_chunks_large_input(recording_transport):
    post_count = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, [])
        post_count["n"] += 1
        return httpx.Response(204)
    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(watched_definition_id="d", listened_definition_id="d2", tag_ids={"netflix": "t"})
    events = [_ev(i) for i in range(25)]
    # GET response is empty so verification finds 0; the pipeline used to raise
    # but now reports the gap non-fatally (Fulcra has read-after-write lag).
    result = client.run_import(events, state, chunk_size=10)
    assert result.total == 25
    assert result.posted == 25
    assert result.verified == 0
    assert post_count["n"] == 3
