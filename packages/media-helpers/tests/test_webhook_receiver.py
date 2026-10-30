"""Integration tests for the Plex/Jellyfin webhook receiver.

Each protocol test uses a real loopback HTTP server and an explicit fake
writer capturing normalized events. Actual configured native delivery is
covered separately in test_webhook_v1.py and test_webhook_cli.py.

Goals exercised:
- Plex media.scrobble (TV) → 1 normalized event, correct fields
- Plex media.scrobble (movie) → 1 ingest, movie fingerprint
- Plex media.play / media.pause → 204, no ingest
- Plex malformed payload → 400 + structured error JSON
- Jellyfin PlaybackStop (TV) → 1 ingest
- Jellyfin PlaybackStop (movie) → 1 ingest
- Jellyfin PlaybackStart → 204, no ingest
- Wrong Authorization → 401
- GET /webhook → 405
- GET /health → 200 + counters
- Loopback-only mode rejects non-127.0.0.1 (simulated via fake address)
- Bearer mode requires bearer
- Same scrobble within a minute → stable deterministic identity
  (native dedup delivery is covered by test_webhook_v1.py)
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest

from fulcra_common import ImportResult
from fulcra_media.state import State
from fulcra_media import webhook_receiver as wr


# Thin shim so the test reads like a `requests`-style flow even though we
# only have httpx in deps. POST with raw bytes uses `content=`; GETs are
# trivial.
class _RequestsShim:
    @staticmethod
    def post(url, data=None, json=None, headers=None):
        if json is not None:
            return httpx.post(url, json=json, headers=headers, timeout=10.0)
        return httpx.post(url, content=data, headers=headers, timeout=10.0)

    @staticmethod
    def get(url, headers=None):
        return httpx.get(url, headers=headers, timeout=10.0)


requests = _RequestsShim()


# ----------------------------------------------------------------------
# Test infrastructure: spin up a real server with a protocol-only fake
# ----------------------------------------------------------------------

class IngestCapture:
    """Protocol-only fake: capture normalized events, without encoding records."""
    record_destination_ids = None
    record_preview_mode = False

    def __init__(self):
        self.events = []

    def ingest_batch(self, events, state):
        self.events.extend(vars(event) for event in events)
        return ImportResult(len(events), 0, len(events), len(events))


@pytest.fixture
def capture() -> IngestCapture:
    return IngestCapture()


@pytest.fixture
def state() -> State:
    return State(
        watched_definition_id="def-watched-uuid",
        listened_definition_id="def-listened-uuid",
        tag_ids={"plex": "tag-plex", "jellyfin": "tag-jellyfin"},
    )


@pytest.fixture
def fulcra_client(capture: IngestCapture) -> IngestCapture:
    return capture


class ServerHandle:
    def __init__(self, server, thread, url: str) -> None:
        self.server = server
        self.thread = thread
        self.url = url

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)


@pytest.fixture
def make_running_server(state, fulcra_client):
    """Factory: start a server on an ephemeral port, return ServerHandle."""
    handles: list[ServerHandle] = []

    def _make(bearer_token: str | None = None, host: str = "127.0.0.1"):
        server = wr.make_server(
            host=host, port=0, state=state, client=fulcra_client,
            bearer_token=bearer_token,
        )
        port = server.server_address[1]
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.02),
            name="webhook-test", daemon=True,
        )
        thread.start()
        url = f"http://{host}:{port}"
        # Tiny wait so serve_forever's loop is actually entered before
        # the test's first request races it. ThreadingHTTPServer binds
        # synchronously, but serve_forever sets up internal state.
        time.sleep(0.02)
        handle = ServerHandle(server, thread, url)
        handles.append(handle)
        return handle

    yield _make

    for h in handles:
        h.stop()


# ----------------------------------------------------------------------
# Plex fixtures (multipart/form-data with a payload field)
# ----------------------------------------------------------------------

def _multipart_body(payload: dict) -> tuple[bytes, str]:
    """Build a multipart/form-data body with a single `payload` JSON field.

    Returns (body_bytes, content_type_header).
    """
    boundary = "----fulcra-test-boundary"
    json_bytes = json.dumps(payload).encode("utf-8")
    body = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"payload\"\r\n"
        f"Content-Type: application/json\r\n\r\n"
    ).encode("utf-8") + json_bytes + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def plex_payload(event: str = "media.scrobble",
                 *, media_type: str = "episode") -> dict:
    md: dict[str, Any] = {
        "ratingKey": "12345",
        "key": "/library/metadata/12345",
        "guid": "plex://episode/abcdef",
        "duration": 60_000,
        "viewOffset": 55_000,
        "title": "The Pilot",
    }
    if media_type == "episode":
        md.update({
            "librarySectionType": "show",
            "type": "episode",
            "grandparentTitle": "Demo Show",
            "parentTitle": "Season 1",
            "parentIndex": 1,
            "index": 5,
            "year": 2023,
        })
    else:
        md.update({
            "librarySectionType": "movie",
            "type": "movie",
            "title": "Demo Movie",
            "year": 2022,
            "duration": 120_000,
        })
    return {
        "event": event,
        "user": True,
        "owner": True,
        "Account": {"id": 42, "thumb": "x", "title": "tester"},
        "Server": {"title": "Home", "uuid": "server-uuid-abc"},
        "Player": {"local": True, "publicAddress": "192.0.2.4",
                   "title": "Living Room Apple TV", "uuid": "pl-uuid"},
        "Metadata": md,
    }


# ----------------------------------------------------------------------
# Jellyfin fixtures (application/json body, single event)
# ----------------------------------------------------------------------

def jellyfin_payload(event: str = "PlaybackStop",
                     *, item_type: str = "Episode",
                     position_fraction: float = 0.95) -> dict:
    runtime_ticks = 60 * 60 * 10_000_000  # 60-minute episode (in 100ns ticks)
    position_ticks = int(runtime_ticks * position_fraction)
    item: dict[str, Any] = {
        "Id": "item-uuid-abc",
        "Name": "The Pilot",
        "Type": item_type,
        "ProductionYear": 2023,
        "RunTimeTicks": runtime_ticks,
        "PremiereDate": "2023-01-15T00:00:00Z",
    }
    if item_type == "Episode":
        item.update({
            "SeriesName": "Demo Show",
            "SeasonName": "Season 1",
            "ParentIndexNumber": 1,
            "IndexNumber": 5,
        })
    else:
        item["Name"] = "Demo Movie"
        item["ProductionYear"] = 2022
    return {
        "Event": event,
        "Item": item,
        "User": {"Id": "user-uuid", "Name": "tester"},
        "Server": {"Id": "server-uuid-jf", "Name": "Home"},
        "Session": {"DeviceId": "dev-id", "DeviceName": "Firefox"},
        "PlaybackPositionTicks": position_ticks,
        "Date": "2026-05-17T12:00:00Z",
    }


# ======================================================================
# Plex tests
# ======================================================================

def test_plex_media_scrobble_tv_posts_one_event(make_running_server, capture):
    h = make_running_server()
    body, ctype = _multipart_body(plex_payload("media.scrobble", media_type="episode"))
    r = requests.post(f"{h.url}/webhook", data=body, headers={"Content-Type": ctype})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload == {"posted": 1, "skipped": 0}
    assert len(capture.events) == 1
    data = capture.events[0]
    assert data["service"] == "plex"
    assert "Demo Show S01E05" in data["note"]
    assert data["title"] == "The Pilot"
    assert data["external_ids"]["content_fingerprint"] == "tv:demo-show:s01e05"
    assert data["deterministic_id"].startswith("com.fulcra.media.plex.v1.")


def test_plex_media_scrobble_movie_posts_with_movie_fingerprint(make_running_server, capture):
    h = make_running_server()
    body, ctype = _multipart_body(plex_payload("media.scrobble", media_type="movie"))
    r = requests.post(f"{h.url}/webhook", data=body, headers={"Content-Type": ctype})
    assert r.status_code == 200
    assert len(capture.events) == 1
    data = capture.events[0]
    assert data["external_ids"]["content_fingerprint"] == "movie:demo-movie:y2022"
    assert data["title"] == "Demo Movie"


def test_plex_media_play_returns_204_no_ingest(make_running_server, capture):
    h = make_running_server()
    body, ctype = _multipart_body(plex_payload("media.play"))
    r = requests.post(f"{h.url}/webhook", data=body, headers={"Content-Type": ctype})
    assert r.status_code == 204
    assert len(capture.events) == 0


def test_plex_media_pause_returns_204_no_ingest(make_running_server, capture):
    h = make_running_server()
    body, ctype = _multipart_body(plex_payload("media.pause"))
    r = requests.post(f"{h.url}/webhook", data=body, headers={"Content-Type": ctype})
    assert r.status_code == 204
    assert len(capture.events) == 0


def test_plex_malformed_payload_returns_400_envelope(make_running_server, capture):
    h = make_running_server()
    boundary = "----fulcra-test-boundary"
    bad_payload = "this is { not json at all"
    body = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"payload\"\r\n"
        f"Content-Type: application/json\r\n\r\n"
        f"{bad_payload}\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    r = requests.post(
        f"{h.url}/webhook", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert r.status_code == 400, r.text
    payload = r.json()
    assert payload["ok"] is False
    assert "message" in payload
    assert len(capture.events) == 0


def test_plex_multipart_without_payload_field_returns_400(make_running_server, capture):
    h = make_running_server()
    boundary = "----b"
    body = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"other\"\r\n\r\n"
        f"hello\r\n--{boundary}--\r\n"
    ).encode()
    r = requests.post(
        f"{h.url}/webhook", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert r.status_code == 400
    assert r.json()["ok"] is False


# ======================================================================
# Jellyfin tests
# ======================================================================

def test_jellyfin_playback_stop_tv_posts_one_event(make_running_server, capture):
    h = make_running_server()
    payload = jellyfin_payload("PlaybackStop", item_type="Episode")
    r = requests.post(f"{h.url}/webhook", json=payload)
    assert r.status_code == 200, r.text
    assert r.json() == {"posted": 1, "skipped": 0}
    assert len(capture.events) == 1
    data = capture.events[0]
    assert data["service"] == "jellyfin"
    assert "Demo Show S01E05" in data["note"]
    assert data["external_ids"]["content_fingerprint"] == "tv:demo-show:s01e05"


def test_jellyfin_playback_stop_movie_posts_with_movie_fingerprint(make_running_server, capture):
    h = make_running_server()
    payload = jellyfin_payload("PlaybackStop", item_type="Movie")
    r = requests.post(f"{h.url}/webhook", json=payload)
    assert r.status_code == 200, r.text
    data = capture.events[0]
    assert data["external_ids"]["content_fingerprint"] == "movie:demo-movie:y2022"
    assert data["title"] == "Demo Movie"


def test_jellyfin_playback_start_returns_204_no_ingest(make_running_server, capture):
    h = make_running_server()
    payload = jellyfin_payload("PlaybackStart")
    r = requests.post(f"{h.url}/webhook", json=payload)
    assert r.status_code == 204
    assert len(capture.events) == 0


def test_jellyfin_below_threshold_does_not_post(make_running_server, capture):
    """User stops at 40% — that's a bailout, not a real watch."""
    h = make_running_server()
    payload = jellyfin_payload("PlaybackStop", position_fraction=0.40)
    r = requests.post(f"{h.url}/webhook", json=payload)
    assert r.status_code == 204
    assert len(capture.events) == 0


def test_jellyfin_invalid_json_returns_400(make_running_server, capture):
    h = make_running_server()
    r = requests.post(
        f"{h.url}/webhook", data=b"{ not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["ok"] is False


# ======================================================================
# Auth + routing tests
# ======================================================================

def test_health_endpoint_returns_ok_and_counters(make_running_server, capture):
    h = make_running_server()
    # Hit the webhook once so received goes to 1.
    body, ctype = _multipart_body(plex_payload("media.scrobble"))
    requests.post(f"{h.url}/webhook", data=body, headers={"Content-Type": ctype})
    r = requests.get(f"{h.url}/health")
    assert r.status_code == 200
    health = r.json()
    assert health["ok"] is True
    assert health["definition_id"] == "def-watched-uuid"
    assert health["received"] == 1
    assert health["posted"] == 1


@pytest.mark.parametrize("mode", ["new", "duplicate", "preview", "unverified", "malformed", "wrong_total", "none", "bool", "negative"])
def test_v1_webhook_reports_verified_duplicate_and_preview_outcomes(make_running_server, fulcra_client, mode):
    from fulcra_common import ImportResult
    destination = "Event/11111111-2222-4333-8444-555555555555"
    result = ImportResult(total=1, skipped_existing=int(mode == "duplicate"),
        posted=int(mode != "duplicate"), verified=int(mode in {"new", "wrong_total"}))
    if mode == "wrong_total":
        result.total = 2
    if mode == "malformed":
        result = object()
    if mode == "none":
        result = None
    if mode == "bool":
        result.verified = True
    if mode == "negative":
        result.skipped_existing = -1
    fulcra_client.record_preview_mode = mode == "preview"
    fulcra_client.record_destination_ids = {"watched": destination}
    fulcra_client.ingest_batch = lambda *args, **kwargs: result
    h = make_running_server()
    response = requests.post(f"{h.url}/webhook", json=jellyfin_payload())
    if mode in {"unverified", "malformed", "wrong_total", "none", "bool", "negative"}:
        assert response.status_code == 502
        assert response.json()["error"] == "ingest_failed"
        assert requests.get(f"{h.url}/health").json()["posted"] == 0
        return
    assert response.status_code == 200
    expected = {"posted": result.verified, "skipped": result.skipped_existing}
    if mode == "preview":
        expected.update(preview=True, would_post=1)
    assert response.json() == expected
    health = requests.get(f"{h.url}/health").json()
    assert health["posted"] == result.verified and health["skipped"] == result.skipped_existing
    assert health["data_type_id"] == destination and "definition_id" not in health


def test_get_webhook_returns_405(make_running_server, capture):
    h = make_running_server()
    r = requests.get(f"{h.url}/webhook")
    assert r.status_code == 405
    assert r.headers.get("Allow") == "POST"


def test_bearer_token_required_when_configured(make_running_server, capture):
    h = make_running_server(bearer_token="secret-xyz")
    body, ctype = _multipart_body(plex_payload("media.scrobble"))
    # No header → 401
    r = requests.post(f"{h.url}/webhook", data=body, headers={"Content-Type": ctype})
    assert r.status_code == 401
    # Wrong header → 401
    r = requests.post(f"{h.url}/webhook", data=body,
                      headers={"Content-Type": ctype, "Authorization": "Bearer wrong"})
    assert r.status_code == 401
    # Correct header → 200
    r = requests.post(f"{h.url}/webhook", data=body,
                      headers={"Content-Type": ctype, "Authorization": "Bearer secret-xyz"})
    assert r.status_code == 200
    assert len(capture.events) == 1


def test_bearer_token_accepts_query_string_fallback(make_running_server, capture):
    """Plex's webhook URL has no header support — token via ?token= is the
    documented workaround we need to honor."""
    h = make_running_server(bearer_token="secret-xyz")
    body, ctype = _multipart_body(plex_payload("media.scrobble"))
    qs = urlencode({"token": "secret-xyz"})
    r = requests.post(
        f"{h.url}/webhook?{qs}", data=body,
        headers={"Content-Type": ctype},
    )
    assert r.status_code == 200, r.text


def test_loopback_only_mode_accepts_local_connections(make_running_server, capture):
    """No bearer + loopback bind: 127.0.0.1 clients always pass."""
    h = make_running_server()
    r = requests.get(f"{h.url}/health")
    assert r.status_code == 200


def test_loopback_only_rejects_simulated_remote_client():
    """When bearer_token is None and the source IP is non-loopback, return 403.

    We don't bind on 0.0.0.0 in the test (that would expose the test box on
    a real network); instead we exercise the handler logic directly with a
    fake client_address. This mirrors how the auth check works at runtime.
    """
    from fulcra_media.webhook_receiver import WebhookHandler, ReceiverContext

    class FakeServer:
        def __init__(self, ctx):
            self.context = ctx

    state = State(watched_definition_id="def")
    client = IngestCapture()
    ctx = ReceiverContext(client=client, state=state,
                          bearer_token=None, host="0.0.0.0")

    class StubHandler(WebhookHandler):
        # Override __init__ to bypass the network setup.
        def __init__(self):
            self.client_address = ("192.0.2.8", 12345)  # TEST-NET-1 (RFC 5737)
            self.headers = {}
            self.path = "/webhook"
            self.server = FakeServer(ctx)
            self._responses: list[tuple[int, dict]] = []

        def _send_json(self, status, body, *, extra_headers=None):
            self._responses.append((status, body))

    h = StubHandler()
    ok = h._authorize(ctx)
    assert ok is False
    assert h._responses[0][0] == 403


# ======================================================================
# Determinism tests
# ======================================================================

def test_plex_same_scrobble_within_minute_produces_same_source_id(
    make_running_server, capture,
):
    """Two webhook hits for the same content within a minute → same det id.

    The receiver passes repeated events to the writer; the deterministic_id
    must stay stable so dedup works. Truncating now to the minute makes it stable within a
    minute and distinct across rewatches.
    """
    h = make_running_server()
    body, ctype = _multipart_body(plex_payload("media.scrobble", media_type="episode"))
    r1 = requests.post(f"{h.url}/webhook", data=body, headers={"Content-Type": ctype})
    r2 = requests.post(f"{h.url}/webhook", data=body, headers={"Content-Type": ctype})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert len(capture.events) == 2
    sources = [event["deterministic_id"] for event in capture.events]
    assert len(sources) == 2
    assert sources[0] == sources[1]


# ======================================================================
# Pure-function normalizer tests (no HTTP)
# ======================================================================

def test_normalize_plex_drops_unknown_event():
    assert wr.normalize_plex(plex_payload("media.play")) is None
    assert wr.normalize_plex(plex_payload("media.pause")) is None
    assert wr.normalize_plex(plex_payload("media.resume")) is None


def test_normalize_jellyfin_drops_unknown_event():
    assert wr.normalize_jellyfin(jellyfin_payload("PlaybackStart")) is None
    assert wr.normalize_jellyfin(jellyfin_payload("PlaybackProgress")) is None


def test_normalize_plex_tv_fields():
    ev = wr.normalize_plex(plex_payload("media.scrobble", media_type="episode"))
    assert ev is not None
    assert ev.importer == "plex"
    assert ev.category == "watched"
    assert ev.title == "The Pilot"
    assert ev.note.startswith("Demo Show S01E05 ")
    assert ev.external_ids["content_fingerprint"] == "tv:demo-show:s01e05"
    assert ev.external_ids["rating_key"] == "12345"


def test_normalize_jellyfin_tv_fields():
    ev = wr.normalize_jellyfin(jellyfin_payload("PlaybackStop"))
    assert ev is not None
    assert ev.importer == "jellyfin"
    assert ev.external_ids["item_id"] == "item-uuid-abc"
    assert ev.external_ids["content_fingerprint"] == "tv:demo-show:s01e05"
