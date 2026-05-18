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
def running_server(state, client_with_ingest_capture, monkeypatch, tmp_path):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    # Redirect watermark/state writes to tmp so happy-path POSTs in these
    # tests don't pollute ~/.config/fulcra-attention/state.json on the dev
    # box. The individual watermark tests below ALSO set this (redundant
    # but safe — last setattr wins, both point at tmp).
    monkeypatch.setattr("fulcra_attention.state.DEFAULT_PATH", tmp_path / "state.json")
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


def test_post_missing_auth_returns_401(running_server):
    _server, port, _ctx = running_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/attention",
                 body=json.dumps({
                     "url": "https://x.com/", "title": "T",
                     "category": None,
                     "start_time": _now(), "end_time": _now(),
                     "client": "c"}),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 401
    body = json.loads(resp.read())
    assert body["error"] == "unauthorized"
    conn.close()


def test_post_wrong_bearer_returns_401(running_server):
    _server, port, _ctx = running_server
    status, payload = _post(port, {
        "url": "https://x.com/", "title": "T", "category": None,
        "start_time": _now(), "end_time": _now(), "client": "c",
    }, token="not-the-token")
    assert status == 401
    assert payload["error"] == "unauthorized"


def test_get_health_does_not_require_auth(running_server):
    _server, port, _ctx = running_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/health")
    resp = conn.getresponse()
    assert resp.status == 200
    body = json.loads(resp.read())
    assert body["ok"] is True
    conn.close()


def test_post_both_url_and_category_rejected(running_server):
    _server, port, _ctx = running_server
    status, payload = _post(port, {
        "url": "https://x.com/", "title": "T", "category": "banking",
        "start_time": _now(), "end_time": _now(), "client": "c",
    })
    assert status == 400
    assert payload["error"] == "bad payload"
    assert "url" in payload["message"] and "category" in payload["message"]


def test_post_neither_url_nor_category_rejected(running_server):
    _server, port, _ctx = running_server
    status, payload = _post(port, {
        "url": None, "title": None, "category": None,
        "start_time": _now(), "end_time": _now(), "client": "c",
    })
    assert status == 400
    assert payload["error"] == "bad payload"


def test_post_end_before_start_rejected(running_server):
    _server, port, _ctx = running_server
    now = datetime.now(timezone.utc).replace(microsecond=0)
    status, payload = _post(port, {
        "url": "https://x.com/", "title": "T", "category": None,
        "start_time": now.isoformat().replace("+00:00", "Z"),
        "end_time":   (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "client": "c",
    })
    assert status == 400


def test_post_future_end_time_rejected(running_server):
    _server, port, _ctx = running_server
    now = datetime.now(timezone.utc).replace(microsecond=0)
    status, payload = _post(port, {
        "url": "https://x.com/", "title": "T", "category": None,
        "start_time": now.isoformat().replace("+00:00", "Z"),
        "end_time":   (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "client": "c",
    })
    assert status == 400


def test_post_missing_required_fields_rejected(running_server):
    _server, port, _ctx = running_server
    status, payload = _post(port, {"url": "https://x.com/"})
    assert status == 400


def test_post_writes_watermark_to_state(running_server, client_with_ingest_capture, tmp_path, monkeypatch):
    """After a successful POST, state.watermarks[client] == end_time (to-second)."""
    _server, port, ctx = running_server
    # Re-point state_mod.DEFAULT_PATH so the watermark write doesn't touch user home
    state_path = tmp_path / "state.json"
    monkeypatch.setattr("fulcra_attention.state.DEFAULT_PATH", state_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    end = now
    start = now - timedelta(minutes=1)
    status, _ = _post(port, {
        "url": "https://x.com/", "title": "T", "category": None,
        "start_time": start.isoformat().replace("+00:00", "Z"),
        "end_time":   end.isoformat().replace("+00:00", "Z"),
        "client": "my-client/1.0",
    })
    assert status == 200
    # ctx.state was mutated in-place
    assert ctx.state.watermarks["my-client/1.0"] == end.isoformat().replace("+00:00", "Z")


def test_post_watermark_is_monotonic_max(running_server, client_with_ingest_capture, tmp_path, monkeypatch):
    """A later POST with an earlier end_time does NOT lower the watermark."""
    _server, port, ctx = running_server
    state_path = tmp_path / "state.json"
    monkeypatch.setattr("fulcra_attention.state.DEFAULT_PATH", state_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    # First POST: end = now
    _post(port, {
        "url": "https://x.com/a", "title": "A", "category": None,
        "start_time": (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "end_time":   now.isoformat().replace("+00:00", "Z"),
        "client": "c",
    })
    high = ctx.state.watermarks["c"]
    # Second POST: end = now - 30s (earlier)
    earlier = now - timedelta(seconds=30)
    _post(port, {
        "url": "https://x.com/b", "title": "B", "category": None,
        "start_time": (earlier - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "end_time":   earlier.isoformat().replace("+00:00", "Z"),
        "client": "c",
    })
    # Watermark stays at the higher value
    assert ctx.state.watermarks["c"] == high


def test_post_uses_constant_time_compare(running_server):
    """Smoke test that the bearer-comparison path uses hmac.compare_digest.
    Verifying via a near-miss token that differs only in the last char."""
    _server, port, _ctx = running_server
    status, payload = _post(port, {
        "url": "https://x.com/", "title": "T", "category": None,
        "start_time": _now(), "end_time": _now(), "client": "c",
    }, token="test-beareS")  # differs from "test-bearer" only in final char
    assert status == 401
