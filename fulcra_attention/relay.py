"""Loopback HTTP relay — accepts browse pings from the Chrome extension.

Stdlib-only (http.server.ThreadingHTTPServer). Mirrors the shape of
fulcra-media's webhook_receiver but with a different endpoint and
payload schema. Bearer-token authentication required.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .fulcra import FulcraClient
from .ingest import build_attention_event
from .state import State


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
            event = build_attention_event(payload, state=ctx.state)
        except (KeyError, ValueError) as exc:
            self._send_json(400, {"ok": False, "error": "bad payload", "message": str(exc)})
            return

        try:
            ctx.client.ingest_batch([event])
        except Exception as exc:
            self._send_json(502, {"ok": False, "error": "ingest_failed", "message": str(exc)})
            return

        ctx.bump(posted=1)
        self._send_json(200, {"posted": 1, "dropped": 0})


def make_server(*, host: str, port: int, context: ReceiverContext) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), AttentionHandler)
    server.context = context  # type: ignore[attr-defined]
    return server
