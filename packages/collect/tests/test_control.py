"""The control socket — newline-delimited JSON request/response over a UDS."""
from __future__ import annotations

import threading
from pathlib import Path

from fulcra_collect.control import ControlServer, send_request


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


def test_send_request_to_a_dead_socket_raises(tmp_path: Path):
    import pytest
    with pytest.raises(ConnectionError):
        send_request(tmp_path / "nonexistent.sock", {"cmd": "status"})
