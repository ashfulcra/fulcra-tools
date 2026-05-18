"""Cross-component contract: Chrome-extension-shaped POST -> Python relay -> Fulcra ingest.

This test constructs a payload that is byte-identical to what
chrome/src/background.ts buildPayload() produces, POSTs it through a live relay,
and asserts that every field survives the relay → ingest → Fulcra pipeline intact.

It is the canonical "TS and Python agree" assertion. If either side drifts — new
fields added, field names renamed, null semantics changed — this test breaks loudly.

Wire format as of chrome/src/background.ts @ buildPayload() (non-categorized branch):
  {
    url:             string,          // scrubbed by TS before storage
    title:           string | null,
    og_description:  string | null,
    favicon_url:     string | null,
    category:        null,            // non-categorized path
    chrome_identity: string | null,   // getChromeIdentity() result
    og_type:         string | null,
    lang:            string | null,
    start_time:      "YYYY-MM-DDTHH:MM:SSZ",  // toIsoSecondZ output
    end_time:        "YYYY-MM-DDTHH:MM:SSZ",
    client:          "fulcra-attention-chrome/0.1.0",  // CLIENT constant
  }

Fields are set in the exact order defined in types.ts AttentionEvent interface.
"""
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


# ---------- helpers replicated from background.ts ----------

def _to_iso_second_z(dt: datetime) -> str:
    """Python equivalent of TS toIsoSecondZ(ms).

    TS:  new Date(Math.floor(ms / 1000) * 1000).toISOString().replace(".000", "")
    Result: "2026-05-17T10:30:00Z" — whole-second UTC, trailing Z, no fractional part.
    """
    truncated = dt.replace(microsecond=0, tzinfo=timezone.utc)
    iso = truncated.isoformat()                    # e.g. "2026-05-17T10:30:00+00:00"
    return iso.replace("+00:00", "Z")              # -> "2026-05-17T10:30:00Z"


# ---------- fixtures ----------

@pytest.fixture
def state() -> State:
    return State(
        attention_definition_id="def-att",
        tag_ids={"attention": "tag-a", "web": "tag-w"},
    )


@pytest.fixture
def capturing_client(recording_transport):
    """FulcraClient that always returns 200 and records every request body."""
    transport = recording_transport(
        lambda r: httpx.Response(200, json={"ok": True})
    )
    return FulcraClient(transport=transport)


@pytest.fixture
def live_relay(state, capturing_client, monkeypatch, tmp_path):
    """Live relay on an OS-assigned port. Yields (port, ctx, transport)."""
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    # Redirect watermark writes to a temp dir so we don't pollute ~/.config.
    monkeypatch.setattr("fulcra_attention.state.DEFAULT_PATH", tmp_path / "state.json")
    ctx = ReceiverContext(
        client=capturing_client,
        state=state,
        bearer_token="chrome-relay-secret",
    )
    server = make_server(host="127.0.0.1", port=0, context=ctx)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield port, ctx, capturing_client._transport
    finally:
        server.shutdown()
        server.server_close()


# ---------- the contract test ----------

def test_chrome_extension_payload_accepted_end_to_end(live_relay):
    """A POST shaped exactly like buildPayload() → 200, ingest called once, fields intact."""
    port, ctx, transport = live_relay

    # Timestamps: mimic what TS produces for a 3-minute visit ending now.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(minutes=3)

    # Build the payload in the SAME field order as types.ts AttentionEvent interface.
    # This order matters for the "byte-identical" requirement: json.dumps with no
    # sort_keys will serialise in insertion order (Python 3.7+ dict guarantee), matching
    # JS object literal order in buildPayload().
    chrome_payload: dict = {
        "url":            "https://example.com/article?id=42&utm_source=twitter",
        "title":          "Example Article",
        "og_description": "A compelling description.",
        "favicon_url":    "https://example.com/favicon.ico",
        "category":       None,
        "chrome_identity": "ash@fulcradynamics.com",
        "og_type":        "article",
        "lang":           "en",
        "start_time":     _to_iso_second_z(start),
        "end_time":       _to_iso_second_z(now),
        "client":         "fulcra-attention-chrome/0.1.0",
    }

    # POST via stdlib http.client (same as test_relay.py pattern).
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "POST", "/attention",
        body=json.dumps(chrome_payload),
        headers={
            "Authorization": "Bearer chrome-relay-secret",
            "Content-Type": "application/json",
        },
    )
    resp = conn.getresponse()
    status = resp.status
    resp_body = json.loads(resp.read())
    conn.close()

    # 1. Relay accepted the request.
    assert status == 200, f"Relay returned {status}: {resp_body}"
    assert resp_body == {"posted": 1, "dropped": 0}

    # 2. Exactly one /ingest/v1/record/batch POST was fired.
    assert len(transport.requests) == 1, (
        f"Expected 1 ingest request, got {len(transport.requests)}"
    )

    # 3. Decode the ingest body.
    ingest_body_bytes = transport.requests[0].content
    event = json.loads(ingest_body_bytes)
    assert event["metadata"]["data_type"] == "DurationAnnotation"
    data = json.loads(event["data"])

    # 4. external_ids carry all four Chrome-specific enrichment fields.
    ext = data["external_ids"]
    assert ext["chrome_identity"] == "ash@fulcradynamics.com", (
        "chrome_identity must survive relay → ingest"
    )
    assert ext["og_type"] == "article", (
        "og_type must survive relay → ingest"
    )
    assert ext["lang"] == "en", (
        "lang must survive relay → ingest"
    )
    assert ext["host"] == "example.com", (
        "host must be extracted from the (post-scrub) URL"
    )
    assert ext["client"] == "fulcra-attention-chrome/0.1.0", (
        "client constant must match types.ts CLIENT"
    )

    # 5. Defense-in-depth scrub: utm_source stripped server-side even though
    #    the TS extension should already have done it.
    assert data["url"] == "https://example.com/article?id=42", (
        "relay must strip tracking params even if extension sent them"
    )
    assert "utm_source" not in data["url"]

    # 6. Timestamps preserved at second precision with Z suffix.
    md = event["metadata"]["recorded_at"]
    assert md["start_time"] == _to_iso_second_z(start), (
        "start_time must round-trip through relay unchanged (to-second, Z suffix)"
    )
    assert md["end_time"] == _to_iso_second_z(now), (
        "end_time must round-trip through relay unchanged"
    )

    # 7. og_description and favicon_url land in data (optional enrichment fields).
    assert data["og_description"] == "A compelling description."
    assert data["favicon_url"] == "https://example.com/favicon.ico"

    # 8. Watermark updated for this client after successful ingest.
    assert ctx.state.watermarks.get("fulcra-attention-chrome/0.1.0") == _to_iso_second_z(now), (
        "update_watermark must record the max end_time seen for this client"
    )


def test_chrome_extension_categorized_payload_accepted(live_relay):
    """The categorized branch of buildPayload(): url=null, category set, enrichment nulled."""
    port, _ctx, transport = live_relay

    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(minutes=1)

    # Categorized branch: url/title/og_*/favicon_url/og_type/lang all null.
    chrome_payload = {
        "url":             None,
        "title":           None,
        "og_description":  None,
        "favicon_url":     None,
        "category":        "banking",
        "chrome_identity": "ash@fulcradynamics.com",
        "og_type":         None,
        "lang":            None,
        "start_time":      _to_iso_second_z(start),
        "end_time":        _to_iso_second_z(now),
        "client":          "fulcra-attention-chrome/0.1.0",
    }

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "POST", "/attention",
        body=json.dumps(chrome_payload),
        headers={
            "Authorization": "Bearer chrome-relay-secret",
            "Content-Type": "application/json",
        },
    )
    resp = conn.getresponse()
    status = resp.status
    resp_body = json.loads(resp.read())
    conn.close()

    assert status == 200, f"Relay returned {status}: {resp_body}"

    ingest_body_bytes = transport.requests[0].content
    event = json.loads(ingest_body_bytes)
    data = json.loads(event["data"])

    assert data["category"] == "banking"
    assert data["url"] is None
    assert data["title"] is None
    assert data["external_ids"]["host"] is None
    assert data["external_ids"]["chrome_identity"] == "ash@fulcradynamics.com"
    assert data["note"] == "Attention: banking"
