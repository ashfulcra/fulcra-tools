"""The control socket — newline-delimited JSON request/response over a UDS."""
from __future__ import annotations

import os
import socket
import stat
import tempfile
import threading
from pathlib import Path

import pytest

from fulcra_collect import control
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


def test_versioned_client_can_opt_into_larger_but_bounded_responses(tmp_path):
    sock = tmp_path / 'control.sock'
    payload = {'ok': True, 'synthetic_choices': ['x' * 1000] * 100}
    server = ControlServer(sock, lambda _: payload)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        server.wait_ready(timeout=2)
        assert send_request(sock, {'cmd': 'quick_record_v2'}, max_response_bytes=8 * 1024 * 1024) == payload
        with pytest.raises(ValueError, match='response too large'):
            send_request(sock, {'cmd': 'quick_record_v2'}, max_response_bytes=32768)
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.parametrize('limit', [True, 0, -1, 8 * 1024 * 1024 + 1, None])
def test_response_bound_is_validated_before_connecting(tmp_path, limit):
    with pytest.raises(ValueError, match='response'):
        send_request(tmp_path / 'absent.sock', {'cmd': 'status'}, max_response_bytes=limit)


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


@pytest.fixture
def short_root():
    # Keep real-socket fixtures independent of the host's potentially long TMPDIR.
    with tempfile.TemporaryDirectory(prefix="fc-", dir="/tmp") as directory:
        yield Path(directory).resolve()


def _assert_round_trip(sock: Path, *, indirect: bool) -> Path:
    """Exercise the real bind and client while retaining startup exceptions."""
    server = ControlServer(sock, lambda request: {"echo": request})
    errors = []

    def serve():
        try:
            server.serve_forever()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        try:
            server.wait_ready(timeout=2)
        except TimeoutError:
            assert not errors, f"control startup failed: {errors!r}"
            raise
        assert sock.is_symlink() is indirect
        bound = sock.resolve()
        assert len(os.fsencode(bound)) + 1 <= 104
        assert stat.S_IMODE(bound.stat().st_mode) == 0o600
        assert send_request(sock, {"cmd": "status"}) == {"echo": {"cmd": "status"}}
        return bound
    finally:
        server.shutdown()
        thread.join(timeout=2)
        assert not thread.is_alive()
        if server._sock is not None:
            server._sock.close()


@pytest.mark.parametrize("temp_component", ["t" * 150, "é" * 24], ids=["long", "multibyte"])
def test_long_profile_round_trip_with_oversized_temporary_root(short_root, monkeypatch, temp_component):
    # Catch selecting a temp root without measuring the completed encoded path.
    temporary = short_root / temp_component
    temporary.mkdir()
    monkeypatch.setattr(control.tempfile, "gettempdir", lambda: str(temporary))
    candidate = temporary / f"fulcra-collect-{os.getuid()}" / ("fc_" + "0" * 16 + ".sock")
    assert len(os.fsencode(candidate)) + 1 > 104
    if temp_component.startswith("é"):
        assert len(str(candidate)) + 1 <= 104
    profile = short_root / ("p" * 110)
    profile.mkdir()
    bound = _assert_round_trip(profile / "control.sock", indirect=True)
    assert bound.parent == Path("/tmp").resolve() / f"fulcra-collect-{os.getuid()}"


def test_multibyte_profile_round_trip_uses_encoded_length(short_root):
    # Character-count checks bind this oversized UTF-8 path directly and fail.
    sock = short_root / ("é" * 42 + ".sock")
    assert len(str(sock)) + 1 <= 104 < len(os.fsencode(sock)) + 1
    _assert_round_trip(sock, indirect=True)


@pytest.mark.parametrize("path_bytes,indirect", [(60, False), (103, False), (104, True)])
def test_profile_capacity_includes_terminator(short_root, path_bytes, indirect):
    # The strictest supported sockaddr_un has 104 bytes including its NUL.
    sock = short_root / ("s" * (path_bytes - len(os.fsencode(short_root)) - 1))
    assert len(os.fsencode(sock)) == path_bytes
    _assert_round_trip(sock, indirect=indirect)


def test_short_configured_temporary_root_is_preserved(short_root, monkeypatch):
    temporary = short_root / "tmp"
    temporary.mkdir()
    monkeypatch.setattr(control.tempfile, "gettempdir", lambda: str(temporary))
    profile = short_root / ("p" * 110)
    profile.mkdir()
    bound = _assert_round_trip(profile / "control.sock", indirect=True)
    assert bound.parent == temporary / f"fulcra-collect-{os.getuid()}"
    assert stat.S_IMODE(bound.parent.stat().st_mode) == 0o700


def test_short_bind_path_refuses_per_user_directory_alias_before_mutation(short_root, monkeypatch):
    target = short_root / "target"
    target.mkdir(mode=0o755)
    marker = target / "keep"
    marker.write_text("untouched")
    user_dir = short_root / f"fulcra-collect-{os.getuid()}"
    user_dir.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(control.tempfile, "gettempdir", lambda: str(short_root))
    with pytest.raises(OSError):
        _short_bind_path(short_root / "profile.sock")
    assert user_dir.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert marker.read_text() == "untouched"


def test_short_bind_path_refuses_non_directory(short_root, monkeypatch):
    user_dir = short_root / f"fulcra-collect-{os.getuid()}"
    user_dir.write_text("untouched")
    monkeypatch.setattr(control.tempfile, "gettempdir", lambda: str(short_root))
    with pytest.raises(OSError):
        _short_bind_path(short_root / "profile.sock")
    assert user_dir.read_text() == "untouched"


def test_short_bind_path_refuses_foreign_owner_before_chmod(short_root, monkeypatch):
    # A different effective identity sees this real, current-user-owned directory
    # as foreign; no privileged chown or second system account is needed.
    other_uid = os.getuid() + 1
    user_dir = short_root / f"fulcra-collect-{other_uid}"
    user_dir.mkdir(mode=0o755)
    monkeypatch.setattr(control.tempfile, "gettempdir", lambda: str(short_root))
    monkeypatch.setattr(control.os, "getuid", lambda: other_uid)
    with pytest.raises(PermissionError):
        _short_bind_path(short_root / "profile.sock")
    assert stat.S_IMODE(user_dir.stat().st_mode) == 0o755


def test_short_bind_path_tightens_owned_directory(short_root, monkeypatch):
    user_dir = short_root / f"fulcra-collect-{os.getuid()}"
    user_dir.mkdir(mode=0o755)
    monkeypatch.setattr(control.tempfile, "gettempdir", lambda: str(short_root))
    _short_bind_path(short_root / "profile.sock")
    assert stat.S_IMODE(user_dir.stat().st_mode) == 0o700


def test_short_bind_path_fails_explicitly_when_no_candidate_fits(short_root, monkeypatch):
    monkeypatch.setattr(control.tempfile, "gettempdir", lambda: str(short_root))
    monkeypatch.setattr(control, "_MAX_SOCK_PATH", 1)
    with pytest.raises(ValueError, match="control socket path"):
        _short_bind_path(short_root / "profile.sock")
    assert not (short_root / f"fulcra-collect-{os.getuid()}").exists()


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
