from datetime import datetime, timezone

import httpx
import pytest

from fulcra_media.fulcra import FulcraClient
from media_test_helpers import json_response


@pytest.fixture(autouse=True)
def fake_token(mocker):
    mocker.patch.dict("os.environ", {"FULCRA_ACCESS_TOKEN": "test-token"})


def test_fetch_existing_source_ids_collects_from_records(recording_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/data/v1alpha1/event/DurationAnnotation"
        # Both params present
        params = dict(request.url.params)
        assert "start_time" in params and "end_time" in params
        return json_response(200, [
            {"metadata": {"source": ["com.fulcra.media.netflix.aaa", "com.fulcradynamics.annotation.x"]}},
            {"metadata": {"source": ["com.fulcra.media.netflix.bbb", "com.fulcradynamics.annotation.x"]}},
            {"metadata": {"source": ["unrelated.source"]}},
        ])

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    got = client.fetch_existing_source_ids(
        start=datetime(2026, 5, 12, 20, 50, tzinfo=timezone.utc),
        end=datetime(2026, 5, 12, 23, 10, tzinfo=timezone.utc),
    )
    assert got == {
        "com.fulcra.media.netflix.aaa",
        "com.fulcra.media.netflix.bbb",
        "com.fulcradynamics.annotation.x",
        "unrelated.source",
    }


def test_fetch_existing_source_ids_empty_when_no_records(recording_transport):
    transport = recording_transport(lambda r: json_response(200, []))
    client = FulcraClient(transport=transport)
    got = client.fetch_existing_source_ids(
        start=datetime(2026, 5, 12, tzinfo=timezone.utc),
        end=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )
    assert got == set()


def test_fetch_existing_source_ids_reads_top_level_sources_array(recording_transport):
    """Fulcra returns source IDs under top-level 'sources' (plural), not metadata.source.

    Verified against real api.fulcradynamics.com — the production response shape is:
        {"id": ..., "sources": [...], "metadata": {<definition metadata>}}
    where metadata is the annotation-definition info, NOT a CloudEvents-style envelope.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, [
            {
                "id": "evt-1",
                "sources": ["com.fulcra.media.netflix.aaa", "com.fulcradynamics.annotation.x"],
                "metadata": {"name": "Watched", "annotation_type": "duration"},
            },
            {
                "id": "evt-2",
                "sources": ["com.fulcra.media.netflix.bbb"],
                "metadata": {},
            },
        ])
    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    got = client.fetch_existing_source_ids(
        start=datetime(2026, 5, 12, tzinfo=timezone.utc),
        end=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )
    assert got == {
        "com.fulcra.media.netflix.aaa",
        "com.fulcra.media.netflix.bbb",
        "com.fulcradynamics.annotation.x",
    }
