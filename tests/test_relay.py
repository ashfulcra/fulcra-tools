"""Relay HTTP endpoint — exercised via stdlib http.client against a live server."""
from __future__ import annotations

import http.client
import json
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from fulcra_attention.fulcra import FulcraClient
from fulcra_attention.relay import ReceiverContext, make_server
from fulcra_attention.state import State


@pytest.fixture
def state() -> State:
    return State(
        attention_definition_id="def-att",
        tag_ids={"attention": "tag-a", "web": "tag-w"},
    )


@pytest.fixture
def client_with_ingest_capture(recording_transport):
    """FulcraClient whose /ingest/v1/record/batch always 200s."""
    transport = recording_transport(
        lambda r: httpx.Response(200, json={"ok": True})
    )
    return FulcraClient(transport=transport)


@pytest.fixture
def running_server(state, client_with_ingest_capture, monkeypatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    ctx = ReceiverContext(
        client=client_with_ingest_capture,
        state=state,
        bearer_token="test-bearer",
    )
    server = make_server(host="127.0.0.1", port=0, context=ctx)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield server, port, ctx
    finally:
        server.shutdown()
        server.server_close()


def _post(port: int, body: dict, *, token: str = "test-bearer") -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "POST", "/attention",
        body=json.dumps(body),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    resp = conn.getresponse()
    status = resp.status
    payload = json.loads(resp.read())
    conn.close()
    return status, payload


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def test_post_attention_happy_path_url(running_server, client_with_ingest_capture):
    _server, port, ctx = running_server
    now = datetime.now(timezone.utc).replace(microsecond=0)
    end = now
    start = now - timedelta(minutes=5)
    status, payload = _post(port, {
        "url": "https://example.com/article",
        "title": "Test",
        "category": None,
        "chrome_identity": "ash@fulcradynamics.com",
        "og_type": "article",
        "lang": "en",
        "start_time": start.isoformat().replace("+00:00", "Z"),
        "end_time":   end.isoformat().replace("+00:00", "Z"),
        "client": "curl/0.1",
    })
    assert status == 200, payload
    assert payload["posted"] == 1
    # FulcraClient saw exactly one ingest POST
    transport = client_with_ingest_capture._transport
    assert len(transport.requests) == 1
    sent_body = transport.requests[0].content
    line = json.loads(sent_body)
    assert line["metadata"]["data_type"] == "DurationAnnotation"
    inner = json.loads(line["data"])
    assert inner["title"] == "Test"
    assert inner["url"] == "https://example.com/article"
    # Context flowed through to external_ids
    assert inner["external_ids"]["chrome_identity"] == "ash@fulcradynamics.com"
    assert inner["external_ids"]["og_type"] == "article"
    assert inner["external_ids"]["lang"] == "en"


def test_post_attention_happy_path_category(running_server, client_with_ingest_capture):
    _server, port, ctx = running_server
    now = datetime.now(timezone.utc).replace(microsecond=0)
    end = now
    start = now - timedelta(minutes=2)
    status, payload = _post(port, {
        "url": None,
        "title": None,
        "category": "banking",
        "start_time": start.isoformat().replace("+00:00", "Z"),
        "end_time":   end.isoformat().replace("+00:00", "Z"),
        "client": "curl/0.1",
    })
    assert status == 200
    inner = json.loads(json.loads(client_with_ingest_capture._transport.requests[0].content)["data"])
    assert inner["category"] == "banking"
    assert inner["url"] is None


def test_post_unknown_path_404s(running_server):
    _server, port, _ctx = running_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/garbage", body="{}",
                 headers={"Authorization": "Bearer test-bearer",
                          "Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 404
    conn.close()
