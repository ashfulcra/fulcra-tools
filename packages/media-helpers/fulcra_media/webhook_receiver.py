"""Long-running HTTP webhook receiver for Plex + Jellyfin.

Single-endpoint HTTP server that listens for media play events from either
- Plex (multipart/form-data with a `payload` JSON field) or
- Jellyfin (application/json body from jellyfin-plugin-webhook),
translates them into NormalizedEvents, and ingests one batch per request.

This is a long-running server (unlike the rest of the importers, which are
one-shot CLIs). It uses only stdlib (`http.server.ThreadingHTTPServer`) so
we avoid adding fastapi/starlette/flask just for a single endpoint.

Endpoints:
    POST /webhook   accept a webhook from Plex or Jellyfin
    GET  /health    liveness probe; returns {"ok": true, ...}

Security model:
    --bearer-token X   require `Authorization: Bearer X` (or `?token=X` query
                       string for Plex, whose webhook config doesn't allow
                       custom request headers)
    no bearer + host=127.0.0.1   accept loopback connections only (the
                       default; refuse remote-IP source addresses with 403)
    no bearer + host!=127.0.0.1   refuse to start the server entirely
                       (caller-side check, in cli.py)

TLS is intentionally out of scope — front this with caddy/nginx if you want
HTTPS. See the wizard docs.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import hmac
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .cli_common import safe_exc_message
from .fulcra import FulcraClient
from .importers.base import NormalizedEvent, content_fingerprint
from .state import State

# Plex sends many event types; we ingest exactly one. The others are noise
# we explicitly drop (returning 204).
PLEX_INGESTED_EVENTS = {"media.scrobble"}
# Jellyfin's plugin emits the same shape with different event names.
JELLYFIN_INGESTED_EVENTS = {"PlaybackStop"}
# Hard cap on webhook request body. Plex's largest scrobble payloads
# are ~16 KB (multipart with the rich metadata block); Jellyfin's
# `PlaybackStop` is ~2 KB JSON. 1 MB is well above any legitimate
# payload and prevents a single malicious request with an inflated
# `Content-Length` from exhausting memory via `self.rfile.read(N)`.
MAX_BODY_BYTES = 1 * 1024 * 1024
# Threshold (fraction of runtime) for treating a Jellyfin PlaybackStop as
# a genuine view; otherwise the user bailed early and we drop the event.
JELLYFIN_VIEW_THRESHOLD = 0.75


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _det_id(importer: str, *, server_id: str, account_id: str,
            content_id: str, now: datetime) -> str:
    """Build a deterministic id for the event.

    Truncating `now` to the minute is deliberate: two webhook hits for the
    same scrobble within a few seconds (e.g. a flapping client) should dedup
    to the same Fulcra source-id, but the same content rewatched hours later
    must be a new event. Source-id dedup at the Fulcra layer + minute-level
    truncation gives us both.
    """
    minute = now.replace(second=0, microsecond=0).isoformat()
    h = hashlib.sha256(
        f"{server_id}|{account_id}|{content_id}|{minute}".encode()
    ).hexdigest()
    return f"com.fulcra.media.{importer}.v1.{h[:16]}"


def normalize_plex(payload: dict, *, now: datetime | None = None
                   ) -> NormalizedEvent | None:
    """Translate a Plex webhook payload to a NormalizedEvent.

    Returns None for events we ignore (everything except media.scrobble) and
    for payloads we can't parse meaningfully (missing Metadata, etc.).
    """
    if payload.get("event") not in PLEX_INGESTED_EVENTS:
        return None
    md = payload.get("Metadata") or {}
    if not md:
        return None
    media_type = md.get("type")
    if media_type not in ("episode", "movie"):
        return None
    now = now or datetime.now(timezone.utc)

    title = (md.get("title") or "").strip() or "(untitled)"
    duration_ms = md.get("duration")
    try:
        duration_ms_int = int(duration_ms) if duration_ms is not None else 0
    except (TypeError, ValueError):
        duration_ms_int = 0
    if duration_ms_int > 0:
        start = now - timedelta(milliseconds=duration_ms_int)
    else:
        start = now
    end = now

    rating_key = str(md.get("ratingKey") or md.get("guid") or "")
    server_id = (payload.get("Server") or {}).get("uuid") or ""
    account_id = str((payload.get("Account") or {}).get("id") or "")
    device = (payload.get("Player") or {}).get("title") or ""

    external: dict[str, Any] = {
        "rating_key": rating_key,
        "server_id": server_id,
        "account_id": account_id,
        "device": device,
        "duration_ms": duration_ms_int,
    }
    guid = md.get("guid")
    if guid:
        external["guid"] = guid

    if media_type == "episode":
        show = (md.get("grandparentTitle") or "").strip() or "(unknown show)"
        season = int(md.get("parentIndex") or 0)
        episode = int(md.get("index") or 0)
        note = f"{show} S{season:02d}E{episode:02d} – {title}"
        external["content_fingerprint"] = content_fingerprint(
            "tv", show=show, season=season, episode=episode,
        )
    else:  # movie
        note = title
        year = md.get("year")
        external["content_fingerprint"] = content_fingerprint(
            "movie", title=title, year=year,
        )

    return NormalizedEvent(
        importer="plex",
        service="plex",
        category="watched",
        note=note,
        title=title,
        start_time=start,
        end_time=end,
        deterministic_id=_det_id(
            "plex", server_id=server_id, account_id=account_id,
            content_id=rating_key, now=now,
        ),
        timestamp_confidence="high",
        external_ids=external,
    )


def normalize_jellyfin(payload: dict, *, now: datetime | None = None
                       ) -> NormalizedEvent | None:
    """Translate a jellyfin-plugin-webhook payload to a NormalizedEvent.

    Returns None for events we ignore (only PlaybackStop is tracked) and
    for early bailouts (position/runtime < JELLYFIN_VIEW_THRESHOLD).
    """
    event = payload.get("Event") or payload.get("NotificationType")
    if event not in JELLYFIN_INGESTED_EVENTS:
        return None

    item = payload.get("Item") or {}
    item_type = item.get("Type")
    if item_type not in ("Episode", "Movie"):
        return None

    runtime_ticks = item.get("RunTimeTicks") or 0
    position_ticks = payload.get("PlaybackPositionTicks") or 0
    try:
        runtime_ticks = int(runtime_ticks)
        position_ticks = int(position_ticks)
    except (TypeError, ValueError):
        runtime_ticks = position_ticks = 0
    if runtime_ticks <= 0:
        return None
    if (position_ticks / runtime_ticks) < JELLYFIN_VIEW_THRESHOLD:
        return None

    # 1 tick = 100ns. duration in seconds.
    duration_s = runtime_ticks / 10_000_000.0
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(seconds=duration_s)
    end = now

    title = (item.get("Name") or "").strip() or "(untitled)"
    item_id = str(item.get("Id") or "")
    server_id = (payload.get("Server") or {}).get("Id") or ""
    user_id = str((payload.get("User") or {}).get("Id") or "")
    device = (payload.get("Session") or {}).get("DeviceName") or ""

    external: dict[str, Any] = {
        "item_id": item_id,
        "server_id": server_id,
        "account_id": user_id,
        "device": device,
        "duration_seconds": int(duration_s),
    }

    if item_type == "Episode":
        show = (item.get("SeriesName") or "").strip() or "(unknown show)"
        season = int(item.get("ParentIndexNumber") or 0)
        episode = int(item.get("IndexNumber") or 0)
        note = f"{show} S{season:02d}E{episode:02d} – {title}"
        external["content_fingerprint"] = content_fingerprint(
            "tv", show=show, season=season, episode=episode,
        )
    else:
        note = title
        # Jellyfin gives ProductionYear or a PremiereDate string
        year = item.get("ProductionYear")
        if not year:
            premiere = item.get("PremiereDate") or ""
            year = premiere[:4] if premiere[:4].isdigit() else None
        external["content_fingerprint"] = content_fingerprint(
            "movie", title=title, year=year,
        )

    return NormalizedEvent(
        importer="jellyfin",
        service="jellyfin",
        category="watched",
        note=note,
        title=title,
        start_time=start,
        end_time=end,
        deterministic_id=_det_id(
            "jellyfin", server_id=server_id, account_id=user_id,
            content_id=item_id, now=now,
        ),
        timestamp_confidence="high",
        external_ids=external,
    )


# ---------------------------------------------------------------------------
# Request body parsing
# ---------------------------------------------------------------------------

class WebhookParseError(ValueError):
    """Raised when a request body can't be parsed into a known shape."""


_BOUNDARY_RE = re.compile(r"boundary=([^;\s]+)", re.IGNORECASE)


def _parse_plex_multipart(body: bytes, content_type: str) -> dict:
    """Pull the `payload` field from a multipart/form-data body.

    Python 3.13 removed `cgi`. We use `email.parser` which has always been
    the underlying multipart parser anyway, plus a tiny content-type regex
    to extract the boundary.
    """
    m = _BOUNDARY_RE.search(content_type or "")
    if not m:
        raise WebhookParseError("multipart body missing boundary")
    boundary = m.group(1).strip('"').strip("'")

    # email.parser wants headers + body in one bytes blob.
    blob = (
        f"Content-Type: multipart/form-data; boundary={boundary}\r\n\r\n"
    ).encode("ascii") + body
    try:
        msg = email.message_from_bytes(blob, policy=email.policy.default)
    except Exception as exc:
        raise WebhookParseError(f"could not parse multipart body: {exc}") from exc

    if not msg.is_multipart():
        raise WebhookParseError("body was not multipart after parsing")

    for part in msg.iter_parts():
        # Form fields have Content-Disposition: form-data; name="..."
        disp = part.get("Content-Disposition") or ""
        m2 = re.search(r'name="([^"]+)"', disp)
        if not m2:
            continue
        if m2.group(1) != "payload":
            continue
        raw = part.get_payload(decode=True)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WebhookParseError(f"invalid JSON in payload: {exc}") from exc
    raise WebhookParseError("multipart body missing 'payload' field")


def _parse_json_body(body: bytes) -> dict:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookParseError(f"invalid JSON body: {exc}") from exc
    if not isinstance(data, dict):
        raise WebhookParseError("JSON body must be an object")
    return data


# ---------------------------------------------------------------------------
# Receiver state (shared across all threaded request handlers)
# ---------------------------------------------------------------------------

class ReceiverContext:
    """Threadsafe shared state for the webhook server.

    Holds the FulcraClient, the loaded State, and the runtime counters that
    the /health endpoint reports.
    """

    def __init__(self, *, client: FulcraClient, state: State,
                 bearer_token: str | None, host: str) -> None:
        self.client = client
        self.state = state
        self.bearer_token = bearer_token
        self.host = host
        self._lock = threading.Lock()
        self.received = 0
        self.posted = 0
        self.skipped = 0

    def bump_received(self) -> None:
        with self._lock:
            self.received += 1

    def add_outcome(self, *, posted: int, skipped: int) -> None:
        with self._lock:
            self.posted += posted
            self.skipped += skipped

    def health(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "definition_id": self.state.watched_definition_id,
                "received": self.received,
                "posted": self.posted,
                "skipped": self.skipped,
            }


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class WebhookHandler(BaseHTTPRequestHandler):
    """Dispatch /webhook (POST) + /health (GET).

    The handler reads `self.server.context` (set by `make_server`) for the
    shared state. Each request runs on its own thread thanks to the
    ThreadingHTTPServer subclass.
    """

    # Quiet the default access log noise; we emit our own line per request
    # via the parent's `serve_with_stream`.
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Route stdlib's noisy default log to the server's chosen stream
        srv = self.server
        stream = getattr(srv, "log_stream", None)
        if stream is None:
            return
        try:
            stream.write(
                f"{self.address_string()} - - "
                f"[{self.log_date_time_string()}] {format % args}\n"
            )
            stream.flush()
        except Exception:
            pass

    # ---- routing ----

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(200, self._context().health())
            return
        if path == "/webhook":
            # Webhook posts only. Be explicit so misconfig is loud.
            self._send_json(
                405,
                {"ok": False, "error": "POST required"},
                extra_headers={"Allow": "POST"},
            )
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path != "/webhook":
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        ctx = self._context()
        if not self._authorize(ctx):
            return  # _authorize wrote a response already

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send_json(400, {"ok": False, "error": "bad content-length"})
            return
        # Reject oversized requests BEFORE reading them. Without this cap a
        # caller can send `Content-Length: 2147483648` and force a 2 GB
        # `rfile.read`, exhausting the plugin's memory. The auth gate is
        # in front of us, but Plex/Jellyfin auth tokens get exposed to
        # the media server itself — a compromise there shouldn't take the
        # daemon out.
        if length > MAX_BODY_BYTES:
            self._send_json(
                413,
                {"ok": False, "error": "payload too large",
                 "limit": MAX_BODY_BYTES},
            )
            return
        body = self.rfile.read(length) if length > 0 else b""
        content_type = self.headers.get("Content-Type") or ""

        try:
            payload = self._parse(body, content_type)
            event = self._normalize(payload, content_type)
        except WebhookParseError as exc:
            self._send_json(
                400,
                {"ok": False, "error": "parse_failed",
                 "message": safe_exc_message(exc)},
            )
            return
        except Exception as exc:  # pragma: no cover - defensive
            self._send_json(
                400,
                {"ok": False, "error": "normalize_failed",
                 "message": safe_exc_message(exc)},
            )
            return

        ctx.bump_received()
        if event is None:
            # Recognized event type we don't ingest (media.play, etc.)
            self.send_response(204)
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            return

        try:
            ctx.client.ingest_batch([event], ctx.state)
        except Exception as exc:
            self._send_json(
                502,
                {"ok": False, "error": "ingest_failed",
                 "message": safe_exc_message(exc)},
            )
            return
        ctx.add_outcome(posted=1, skipped=0)
        self._send_json(200, {"posted": 1, "skipped": 0})

    # ---- helpers ----

    def _context(self) -> ReceiverContext:
        return self.server.context  # type: ignore[attr-defined]

    def _authorize(self, ctx: ReceiverContext) -> bool:
        """Enforce bearer token OR loopback-only.

        Returns True if the request is OK to proceed; otherwise writes a
        4xx response and returns False.
        """
        if ctx.bearer_token:
            # Two ways to present: Authorization header (Jellyfin's plugin
            # supports custom headers), or ?token=... query string (Plex's
            # webhook URL is fixed; appending a query param is the workaround).
            header = self.headers.get("Authorization", "")
            token = ""
            if header.lower().startswith("bearer "):
                token = header[7:].strip()
            if not token:
                q = parse_qs(urlsplit(self.path).query)
                qs_token = q.get("token", [""])[0]
                token = qs_token
            # Constant-time compare. Receiver may be reachable from a
            # non-loopback interface (--host 0.0.0.0 is supported, with
            # the bearer-token requirement enforced), so a naive `!=`
            # would expose a per-byte timing oracle.
            if not hmac.compare_digest(token, ctx.bearer_token):
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return False
            return True

        # No bearer token configured: refuse non-loopback clients.
        client_ip = self.client_address[0] if self.client_address else ""
        # IPv4 loopback or IPv6 loopback.
        if client_ip in ("127.0.0.1", "::1") or client_ip.startswith("127."):
            return True
        self._send_json(
            403,
            {"ok": False, "error": "forbidden",
             "message": "non-loopback source without bearer token"},
        )
        return False

    def _parse(self, body: bytes, content_type: str) -> dict:
        """Dispatch to the right parser by content-type."""
        ct = (content_type or "").lower()
        if ct.startswith("multipart/form-data"):
            return _parse_plex_multipart(body, content_type)
        if ct.startswith("application/json"):
            return _parse_json_body(body)
        raise WebhookParseError(f"unsupported content-type: {content_type!r}")

    def _normalize(self, payload: dict, content_type: str
                   ) -> NormalizedEvent | None:
        ct = (content_type or "").lower()
        if ct.startswith("multipart/form-data"):
            return normalize_plex(payload)
        return normalize_jellyfin(payload)

    def _send_json(self, status: int, body: dict,
                   *, extra_headers: dict[str, str] | None = None) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        # Close per-request to keep things simple — webhooks are
        # low-volume by nature and persistent connections are not worth
        # the BaseHTTPRequestHandler complexity.
        self.send_header("Connection", "close")
        self.close_connection = True
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------

class _FastThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that skips HTTPServer.server_bind's getfqdn() call.

    `socket.getfqdn('127.0.0.1')` blocks for ~35s on macOS in some DNS
    configurations (Python 3.14 / current libc resolver behavior). We
    don't use `server_name` anywhere, so bypass it.
    """

    def server_bind(self) -> None:
        # Skip HTTPServer.server_bind entirely; call up two levels.
        import socketserver
        socketserver.TCPServer.server_bind(self)
        host, port = self.socket.getsockname()[:2]
        self.server_name = host  # don't resolve; cheap + good enough
        self.server_port = port


def make_server(
    *,
    host: str,
    port: int,
    state: State,
    client: FulcraClient,
    bearer_token: str | None,
    log_stream=None,
) -> ThreadingHTTPServer:
    """Build a ThreadingHTTPServer wired to a fresh ReceiverContext.

    The caller is responsible for `serve_forever`/`shutdown` lifecycle.
    """
    ctx = ReceiverContext(
        client=client, state=state,
        bearer_token=bearer_token, host=host,
    )
    server = _FastThreadingHTTPServer((host, port), WebhookHandler)
    server.context = ctx  # type: ignore[attr-defined]
    server.log_stream = log_stream  # type: ignore[attr-defined]
    return server
