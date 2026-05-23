import json
from datetime import datetime, timezone

import httpx
import pytest

from fulcra_media.fulcra import FulcraClient
from fulcra_media.importers.base import NormalizedEvent
from fulcra_media.state import State
from media_test_helpers import json_response


@pytest.fixture(autouse=True)
def fake_token(mocker):
    mocker.patch.dict("os.environ", {"FULCRA_ACCESS_TOKEN": "test-token"})


def _ev(idx: int) -> NormalizedEvent:
    return NormalizedEvent(
        importer="netflix-slim",
        service="netflix",
        category="watched",
        note=f"Note {idx}",
        title=f"Title {idx}",
        start_time=datetime(2026, 5, 12, 21, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 12, 22, 0, tzinfo=timezone.utc),
        deterministic_id=f"com.fulcra.media.netflix.id{idx:04d}",
        timestamp_confidence="low",
        external_ids={"time_estimated": True, "occurrence_index": 0},
    )


def test_ingest_batch_posts_jsonl_with_correct_shape(recording_transport):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ingest/v1/record/batch"
        assert request.headers["content-type"].startswith("application/x-jsonl")
        seen["lines"] = request.content.splitlines()
        return httpx.Response(204)

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(
        watched_definition_id="def-watched",
        listened_definition_id="def-listened",
        tag_ids={"netflix": "tag-netflix"},
    )

    events = [_ev(1), _ev(2)]
    client.ingest_batch(events, state)

    assert len(seen["lines"]) == 2
    first = json.loads(seen["lines"][0])
    assert first["specversion"] == 1
    md = first["metadata"]
    assert md["data_type"] == "DurationAnnotation"
    assert md["recorded_at"] == {
        "start_time": "2026-05-12T21:00:00Z",
        "end_time":   "2026-05-12T22:00:00Z",
    }
    assert md["content_type"] == "application/json"
    assert md["tags"] == ["tag-netflix"]
    assert "com.fulcra.media.netflix.id0001" in md["source"]
    assert "com.fulcradynamics.annotation.def-watched" in md["source"]

    data_inner = json.loads(first["data"])
    assert data_inner["note"] == "Note 1"
    assert data_inner["title"] == "Title 1"
    assert data_inner["service"] == "netflix"
    assert data_inner["timestamp_confidence"] == "low"
    assert data_inner["external_ids"]["time_estimated"] is True


def test_ingest_batch_routes_listened_events_to_listened_definition(recording_transport):
    captured = []
    def handler(request: httpx.Request) -> httpx.Response:
        captured.extend(request.content.splitlines())
        return httpx.Response(204)
    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(
        watched_definition_id="def-watched",
        listened_definition_id="def-listened",
        tag_ids={"spotify": "tag-spotify"},
    )
    ev = _ev(1)
    ev.category = "listened"
    ev.service = "spotify"
    client.ingest_batch([ev], state)
    md = json.loads(captured[0])["metadata"]
    assert "com.fulcradynamics.annotation.def-listened" in md["source"]
    assert md["tags"] == ["tag-spotify"]


def test_ingest_batch_empty_input_does_not_post(recording_transport):
    transport = recording_transport(lambda r: pytest.fail(f"unexpected {r.url}"))
    client = FulcraClient(transport=transport)
    state = State(watched_definition_id="x", listened_definition_id="y")
    client.ingest_batch([], state)  # no-op
