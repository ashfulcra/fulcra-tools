from datetime import datetime, timezone

import httpx
import pytest

from fulcra_media.fulcra import FulcraClient
from tests.conftest import json_response


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
