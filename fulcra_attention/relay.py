"""Loopback HTTP relay — accepts browse pings from the Chrome extension.

Stdlib-only (http.server.ThreadingHTTPServer). Mirrors the shape of
fulcra-media's webhook_receiver but with a different endpoint and
payload schema. Bearer-token authentication required.
"""
from __future__ import annotations

import hmac
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .fulcra import FulcraClient, build_tag_name
from .ingest import build_attention_event
from .state import State


_STRING_FIELDS = (
    "url", "category", "title", "og_description", "favicon_url",
    "chrome_identity", "og_type", "lang", "client",
    "start_time", "end_time",
)


def _validate(payload: dict) -> None:
    """Raise ValueError with a human-readable message on schema violation."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    required = ("start_time", "end_time", "client")
    missing = [k for k in required if k not in payload]
    if missing:
        raise ValueError(f"missing fields: {missing}")
    # Type-check every string field — relay refuses ints, lists, dicts in
    # places we expect strings. Closes off a class of accidental
    # crash-via-malformed-payload paths and prevents weird types from
    # flowing into sanitize_tag_value / build_tag_name downstream.
    for k in _STRING_FIELDS:
        v = payload.get(k)
        if v is not None and not isinstance(v, str):
            raise ValueError(f"field {k!r} must be string or null, got {type(v).__name__}")
    url = payload.get("url")
    cat = payload.get("category")
    if (url is None) == (cat is None):
        raise ValueError("exactly one of {url, category} must be non-null")
    try:
        from .ingest import _parse_iso
        st = _parse_iso(payload["start_time"])
        en = _parse_iso(payload["end_time"])
    except ValueError as exc:
        raise ValueError(f"unparseable timestamp: {exc}") from exc
    if st > en:
        raise ValueError("start_time > end_time")
    now = datetime.now(timezone.utc)
    if en > now + timedelta(minutes=5):
        raise ValueError("end_time more than 5 minutes in the future")


class ReceiverContext:
    """Thread-safe shared state for the relay."""

    def __init__(
        self,
        *,
        client: FulcraClient,
        state: State,
        bearer_token: str,
    ) -> None:
        self.client = client
        self.state = state
        self.bearer_token = bearer_token
        self._lock = threading.Lock()
        self.received = 0
        self.posted = 0
        self.dropped = 0

    def bump(self, *, posted: int = 0, dropped: int = 0) -> None:
        with self._lock:
            self.received += 1
            self.posted += posted
            self.dropped += dropped

    def update_watermark(self, client: str, end_time_iso: str) -> None:
        """Thread-safely update the high-water mark for a client and persist state."""
        with self._lock:
            cur = self.state.watermarks.get(client)
            if cur is None or end_time_iso > cur:
                self.state.watermarks[client] = end_time_iso
                from . import state as state_mod
                state_mod.save(self.state, state_mod.DEFAULT_PATH)

    def health(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "definition_id": self.state.attention_definition_id,
                "received": self.received,
                "posted": self.posted,
                "dropped": self.dropped,
            }


class AttentionHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # quiet by default
        return

    def _context(self) -> ReceiverContext:
        return self.server.context  # type: ignore[attr-defined]

    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorize(self) -> bool:
        ctx = self._context()
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(token, ctx.bearer_token):
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return False
        return True

    def _check_host_header(self) -> bool:
        """Reject DNS-rebinding attempts. A browser fetch from an
        attacker page that resolves their hostname to 127.0.0.1 will
        send Host: attacker.example. Loopback binding alone doesn't
        protect against this — only the bearer-token check does, but
        rejecting non-loopback Host values is cheap defense in depth.
        """
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        if host in ("127.0.0.1", "localhost", "::1", ""):
            return True
        self._send_json(400, {"ok": False, "error": "bad host"})
        return False

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(200, self._context().health())
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_host_header():
            return
        path = urlsplit(self.path).path
        if path != "/attention":
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorize():
            return
        ctx = self._context()

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send_json(400, {"ok": False, "error": "bad content-length"})
            return
        body = self.rfile.read(length) if length > 0 else b""

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            self._send_json(400, {"ok": False, "error": "bad json", "message": str(exc)})
            return

        try:
            _validate(payload)
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": "bad payload", "message": str(exc)})
            return

        # Lazy-create identity:<chrome_identity> tag. Done in the relay
        # (not in build_attention_event) because tag creation is a network
        # call — keep ingest.py pure / side-effect-free.
        identity = payload.get("chrome_identity")
        if identity:
            try:
                tag_key = build_tag_name("identity", identity)
            except ValueError:
                tag_key = None
            if tag_key and tag_key not in ctx.state.tag_ids:
                try:
                    ctx.client.ensure_tag(tag_key, ctx.state)
                    from . import state as state_mod
                    state_mod.save(ctx.state, state_mod.DEFAULT_PATH)
                except Exception as exc:
                    # Don't block ingest on identity-tag failure; the event
                    # will just lack the identity tag this round. Log to
                    # stderr so the launchd err log captures it.
                    import sys as _sys
                    print(
                        f"warning: lazy identity-tag create failed: {exc!r}",
                        file=_sys.stderr, flush=True,
                    )

        try:
            event = build_attention_event(payload, state=ctx.state)
        except (KeyError, ValueError) as exc:
            self._send_json(400, {"ok": False, "error": "bad payload", "message": str(exc)})
            return

        try:
            ctx.client.ingest_batch([event])
        except Exception as exc:
            self._send_json(502, {"ok": False, "error": "ingest_failed", "message": str(exc)})
            return

        # Persist watermark for the payload's client (max end_time seen)
        from .ingest import _to_second_iso
        ctx.update_watermark(payload["client"], _to_second_iso(payload["end_time"]))
        ctx.bump(posted=1)
        self._send_json(200, {"posted": 1, "dropped": 0})


def make_server(*, host: str, port: int, context: ReceiverContext) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), AttentionHandler)
    server.context = context  # type: ignore[attr-defined]
    return server
