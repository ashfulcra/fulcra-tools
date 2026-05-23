from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from fulcra_menubar.daemon_client import DaemonClient, DaemonUnavailable


@pytest.fixture
def fake_daemon(tmp_path):
    """A UDS server that answers each request from a queue of canned
    JSON replies. Yields (socket_path, replies_list, requests_seen).

    macOS caps AF_UNIX paths at 104 bytes. pytest's tmp_path sits deep
    under /private/var/folders/… which easily exceeds the limit, so we
    bind the test socket in a short-named subdirectory of the system
    temp dir instead.
    """
    short_dir = Path(tempfile.gettempdir()) / f"fc-test-{os.getpid()}"
    short_dir.mkdir(mode=0o700, exist_ok=True)
    sock_path = short_dir / "ctl.sock"
    if sock_path.exists():
        sock_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(4)

    replies: list[dict] = []
    seen: list[dict] = []
    stop = threading.Event()

    def serve():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                if not buf:
                    continue
                seen.append(json.loads(buf.split(b"\n", 1)[0].decode()))
                reply = replies.pop(0) if replies else {"ok": False, "error": "no canned reply"}
                conn.sendall(json.dumps(reply).encode() + b"\n")

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield sock_path, replies, seen
    finally:
        stop.set()
        t.join(timeout=1.0)
        server.close()


def test_status_round_trip(fake_daemon):
    sock_path, replies, seen = fake_daemon
    replies.append({"ok": True, "plugins": [], "load_errors": {}})

    client = DaemonClient(socket_path=sock_path)
    out = client.status()

    assert out == {"ok": True, "plugins": [], "load_errors": {}}
    assert seen == [{"cmd": "status"}]


def test_run_sends_plugin_id(fake_daemon):
    sock_path, replies, seen = fake_daemon
    replies.append({"ok": True, "started": True})

    client = DaemonClient(socket_path=sock_path)
    out = client.run("lastfm")

    assert out["started"] is True
    assert seen == [{"cmd": "run", "plugin": "lastfm"}]


def test_set_and_delete_credential(fake_daemon):
    sock_path, replies, seen = fake_daemon
    replies.extend([{"ok": True}, {"ok": True}])

    client = DaemonClient(socket_path=sock_path)
    assert client.set_credential("lastfm", "session_key", "abc")["ok"] is True
    assert client.delete_credential("lastfm", "session_key")["ok"] is True

    assert seen == [
        {"cmd": "set_credential", "plugin": "lastfm",
         "key": "session_key", "secret": "abc"},
        {"cmd": "delete_credential", "plugin": "lastfm", "key": "session_key"},
    ]


def test_socket_missing_raises_daemon_unavailable(tmp_path):
    client = DaemonClient(socket_path=tmp_path / "does-not-exist.sock")
    with pytest.raises(DaemonUnavailable):
        client.status()


def test_hung_daemon_raises_daemon_unavailable():
    """A daemon that accepts the connection but never sends a reply must
    raise DaemonUnavailable (via the socket timeout / OSError path) rather
    than propagating a bare TimeoutError that would kill the polling thread.

    Uses a short 0.5 s timeout so the test suite stays fast.
    """
    import os
    import tempfile

    short_dir = Path(tempfile.gettempdir()) / f"fc-hung-{os.getpid()}"
    short_dir.mkdir(mode=0o700, exist_ok=True)
    sock_path = short_dir / "hung.sock"
    if sock_path.exists():
        sock_path.unlink()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    stop = threading.Event()

    def _accept_and_hang():
        server.settimeout(1.0)
        try:
            conn, _ = server.accept()
            # Accept but deliberately never send a reply; hold until test ends.
            stop.wait(timeout=5.0)
            conn.close()
        except socket.timeout:
            pass

    t = threading.Thread(target=_accept_and_hang, daemon=True)
    t.start()
    try:
        client = DaemonClient(socket_path=sock_path, timeout=0.5)
        with pytest.raises(DaemonUnavailable):
            client.status()
    finally:
        stop.set()
        t.join(timeout=2.0)
        server.close()
