"""Ingest pipeline — exact wire format assertion."""
from __future__ import annotations

import json

import httpx
import pytest

from fulcra_attention.fulcra import FulcraClient
from fulcra_attention.state import State


@pytest.fixture(autouse=True)
def _force_test_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")


def test_ingest_batch_posts_jsonl_to_record_batch(recording_transport):
    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "POST" and r.url.path == "/ingest/v1/record/batch":
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    transport = recording_transport(responder)
    client = FulcraClient(transport=transport)
    # State is built (but unused locally) to confirm the test event shape
    # below matches the structure ingest_batch expects. Marked _state to
    # signal the deliberate non-use to lint.
    _state = State(
        attention_definition_id="def-att",
        tag_ids={"attention": "tag-a", "web": "tag-w"},
    )
    event = {
        "specversion": 1,
        "data": json.dumps({"note": "Attention: Test", "title": "Test"}, sort_keys=True),
        "metadata": {
            "data_type": "DurationAnnotation",
            "recorded_at": {
                "start_time": "2026-05-18T14:00:00Z",
                "end_time":   "2026-05-18T14:05:00Z",
            },
            "tags": ["tag-a", "tag-w"],
            "source": [
                "com.fulcra.attention.v2.0123456789abcdef",
                "com.fulcradynamics.annotation.def-att",
            ],
            "content_type": "application/json",
        },
    }
    client.ingest_batch([event])
    assert len(transport.requests) == 1
    sent = transport.requests[0]
    assert sent.headers["content-type"] == "application/x-jsonl"
    assert sent.headers["authorization"] == "Bearer test-tok"
    expected_line = json.dumps(event, sort_keys=True).encode()
    assert sent.content == expected_line


def test_ingest_batch_no_op_on_empty(recording_transport):
    transport = recording_transport(lambda r: pytest.fail("unexpected"))
    client = FulcraClient(transport=transport)
    client.ingest_batch([])
    assert transport.requests == []


def test_ingest_batch_two_events_joined_by_newline(recording_transport):
    transport = recording_transport(
        lambda r: httpx.Response(200, json={"ok": True})
    )
    client = FulcraClient(transport=transport)
    a = {"specversion": 1, "data": "a", "metadata": {"x": 1}}
    b = {"specversion": 1, "data": "b", "metadata": {"x": 2}}
    client.ingest_batch([a, b])
    body = transport.requests[0].content
    assert body == json.dumps(a, sort_keys=True).encode() + b"\n" + json.dumps(b, sort_keys=True).encode()
