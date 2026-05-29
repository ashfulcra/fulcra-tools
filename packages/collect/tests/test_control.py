"""The control socket — newline-delimited JSON request/response over a UDS."""
from __future__ import annotations

import os
import socket
import stat
import threading
from pathlib import Path

import pytest

from fulcra_collect.control import (
    ControlServer,
    _read_line,
    _short_bind_path,
    send_request,
)


def test_request_response_round_trip(tmp_path: Path):
    sock = tmp_path / "control.sock"

    def handler(req: dict) -> dict:
        return {"echo": req}

    server = ControlServer(sock, handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        server.wait_ready(timeout=2.0)
        reply = send_request(sock, {"cmd": "status"})
        assert reply == {"echo": {"cmd": "status"}}
    finally:
        server.shutdown()
        t.join(timeout=2.0)


def test_handler_exception_becomes_an_error_reply(tmp_path: Path):
    sock = tmp_path / "control.sock"

    def handler(req: dict) -> dict:
        raise RuntimeError("handler broke")

    server = ControlServer(sock, handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        server.wait_ready(timeout=2.0)
        reply = send_request(sock, {"cmd": "status"})
        assert reply["ok"] is False
        assert "handler broke" in reply["error"]
    finally:
        server.shutdown()
        t.join(timeout=2.0)


def test_server_survives_a_client_that_hangs_up_before_the_reply(tmp_path: Path):
    """A client that disconnects after sending its request but before reading
    the reply makes ``conn.sendall`` raise BrokenPipeError. That must NOT kill
    the accept loop — otherwise one ill-timed menubar hang-up orphans the
    control socket and every later request gets ECONNREFUSED ("daemon not
    reachable"). The server must drop that peer and keep serving.
    """
    sock = tmp_path / "control.sock"
    in_handler = threading.Event()
    release = threading.Event()

    def handler(req: dict) -> dict:
        # Block inside the handler so the test can deterministically close the
        # client BEFORE the server writes the reply, guaranteeing the sendall
        # hits a dead peer.
        in_handler.set()
        release.wait(timeout=2.0)
        return {"ok": True, "echo": req}

    server = ControlServer(sock, handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        server.wait_ready(timeout=2.0)

        # First client: send a request, wait until the server is in the
        # handler, then hang up before the reply is written.
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Connect via the resolved path so the long-tmp_path symlink fallback
        # (AF_UNIX caps paths at 104 bytes) is honoured, same as send_request.
        c.connect(os.path.realpath(sock))
        c.sendall(b'{"cmd": "first"}\n')
        assert in_handler.wait(timeout=2.0)
        c.close()
        release.set()  # handler returns → sendall now targets the closed peer

        # The accept loop must still be alive and able to serve a fresh client.
        reply = send_request(sock, {"cmd": "second"}, timeout=2.0)
        assert reply == {"ok": True, "echo": {"cmd": "second"}}
        assert t.is_alive()
    finally:
        server.shutdown()
        t.join(timeout=2.0)


def test_send_request_to_a_dead_socket_raises(tmp_path: Path):
    with pytest.raises(ConnectionError):
        send_request(tmp_path / "nonexistent.sock", {"cmd": "status"})


def test_bound_socket_is_owner_only(tmp_path: Path):
    """I1: the UDS file must be mode 0600 — no other local user may connect."""
    sock = tmp_path / "control.sock"
    server = ControlServer(sock, lambda req: {"ok": True})
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        server.wait_ready(timeout=2.0)
        mode = stat.S_IMODE(os.stat(sock).st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    finally:
        server.shutdown()
        t.join(timeout=2.0)


def test_short_bind_path_lives_in_a_per_user_0700_dir(tmp_path: Path):
    """I1: the long-path fallback socket must sit in a per-uid 0700 dir,
    not the world-writable system temp root."""
    long_path = tmp_path / ("x" * 200) / "control.sock"
    short = _short_bind_path(long_path)
    parent = short.parent
    assert parent.is_dir()
    assert str(os.getuid()) in parent.name
    mode = stat.S_IMODE(os.stat(parent).st_mode)
    assert mode == 0o700, f"expected 0700, got {oct(mode)}"
    assert len(str(short)) <= 104


def test_read_line_rejects_an_over_limit_request():
    """I2: an unbounded stream must be capped, not grown without limit.

    The peer streams bytes with no newline from a thread (so the send
    doesn't deadlock on a full socketpair buffer); `_read_line` must
    raise once the accumulated length crosses `max_bytes`.
    """
    a, b = socket.socketpair()

    def flood() -> None:
        try:
            b.sendall(b"x" * 200_000)  # no newline — unbounded read would never stop
        except OSError:
            pass  # reader gave up and closed; expected
        finally:
            b.close()

    sender = threading.Thread(target=flood, daemon=True)
    sender.start()
    try:
        with pytest.raises(ValueError, match="control request too large"):
            _read_line(a, max_bytes=65536)
    finally:
        a.close()
        sender.join(timeout=2.0)
