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

from .fulcra import FulcraClient
from .ingest import build_attention_event
from .state import State


def _validate(payload: dict) -> None:
    """Raise ValueError with a human-readable message on schema violation."""
    required = ("start_time", "end_time", "client")
    missing = [k for k in required if k not in payload]
    if missing:
        raise ValueError(f"missing fields: {missing}")
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

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(200, self._context().health())
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
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
            tag_key = f"identity:{identity}"
            if tag_key not in ctx.state.tag_ids:
                try:
                    ctx.client.ensure_tag(tag_key, ctx.state)
                    from . import state as state_mod
                    state_mod.save(ctx.state, state_mod.DEFAULT_PATH)
                except Exception:
                    # Don't block ingest on identity-tag failure; the event
                    # will just lack the identity tag this round.
                    pass

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
