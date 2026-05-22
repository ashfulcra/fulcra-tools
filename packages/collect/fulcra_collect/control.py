"""The control plane — a Unix-domain-socket server with a newline-delimited
JSON request/response protocol. Filesystem-permissioned; no TCP port.
The `fulcra-collect` CLI (and later the menubar UI) are its clients.

macOS caps AF_UNIX paths at 104 characters. When the requested socket path
is longer, `serve_forever` binds to a short hash-named file in the system
temp directory and leaves a symlink at the original path so callers that
just resolve the path (including `send_request`) transparently reach it.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

Handler = Callable[[dict], dict]

# macOS hard-caps AF_UNIX paths at 104 bytes; Linux allows 108.
_MAX_SOCK_PATH = 104


def _short_bind_path(long_path: Path) -> Path:
    """Return a short substitute path in the system temp dir."""
    digest = hashlib.sha1(str(long_path).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"fc_{digest}.sock"


def _read_line(conn: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        b = conn.recv(4096)
        if not b:
            break
        chunks.append(b)
        if b.endswith(b"\n"):
            break
    return b"".join(chunks)


class ControlServer:
    """Serves one handler over a UDS. `serve_forever` blocks; call it in a
    thread. `shutdown` stops it."""

    def __init__(self, socket_path: Path, handler: Handler) -> None:
        self._path = Path(socket_path)
        self._handler = handler
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._bind_path: Path | None = None  # actual socket file (may differ from _path)

    def wait_ready(self, timeout: float) -> None:
        if not self._ready.wait(timeout):
            raise TimeoutError("control server did not become ready")

    def serve_forever(self) -> None:
        # Determine the actual filesystem path we'll bind to. AF_UNIX on
        # macOS has a 104-byte limit; fall back to a short temp path + symlink.
        if len(str(self._path)) <= _MAX_SOCK_PATH:
            bind_path = self._path
            symlink_needed = False
        else:
            bind_path = _short_bind_path(self._path)
            symlink_needed = True

        self._bind_path = bind_path

        for p in (bind_path, self._path):
            if p.exists() or p.is_symlink():
                p.unlink()

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(bind_path))
        self._sock.listen(8)
        self._sock.settimeout(0.2)

        if symlink_needed:
            os.symlink(str(bind_path), str(self._path))

        self._ready.set()
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            with conn:
                self._serve_one(conn)
        self._sock.close()
        for p in (self._path, bind_path):
            if p.exists() or p.is_symlink():
                p.unlink()

    def _serve_one(self, conn: socket.socket) -> None:
        raw = _read_line(conn)
        try:
            request = json.loads(raw.decode() or "{}")
            reply = self._handler(request)
        except Exception as exc:  # noqa: BLE001 — a bad request must not kill the server
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        conn.sendall(json.dumps(reply).encode() + b"\n")

    def shutdown(self) -> None:
        self._stop.set()


def send_request(socket_path: Path, request: dict, *, timeout: float = 5.0) -> dict:
    """Connect to a ControlServer, send one request, return its reply."""
    # Resolve symlinks so the connect path is always within the AF_UNIX limit.
    resolved = Path(os.path.realpath(socket_path))
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(str(resolved))
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        raise ConnectionError(f"fulcra-collect daemon not reachable at {socket_path}") from exc
    try:
        conn.sendall(json.dumps(request).encode() + b"\n")
        return json.loads(_read_line(conn).decode())
    finally:
        conn.close()
