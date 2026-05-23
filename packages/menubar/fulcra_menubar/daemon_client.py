"""Typed wrapper around fulcra_collect.control.send_request.

The menubar always speaks to the daemon through this class — never
opens a UDS socket directly. Each method maps to one control-socket
command. Connection errors raise DaemonUnavailable so callers can
treat 'daemon stopped' as a single, known state.
"""
from __future__ import annotations

from pathlib import Path

from fulcra_collect import config as _config
from fulcra_collect.control import send_request as _send_request


class DaemonUnavailable(RuntimeError):
    """Raised when the control socket is missing, refusing connections,
    or the daemon is not on PATH. The UI maps this to the 'Daemon
    stopped' state and shows the bootstrap card."""


def default_socket_path() -> Path:
    return _config.config_dir() / "control.sock"


class DaemonClient:
    """One instance per menubar app. Stateless apart from the socket
    path; safe to call from any thread (the underlying send_request
    opens a fresh connection per call)."""

    def __init__(self, *, socket_path: Path | None = None, timeout: float = 5.0) -> None:
        self.socket_path = socket_path or default_socket_path()
        self.timeout = timeout

    # ---- request plumbing ------------------------------------------

    def _send(self, request: dict) -> dict:
        try:
            return _send_request(self.socket_path, request, timeout=self.timeout)
        except ConnectionError as exc:
            raise DaemonUnavailable(str(exc)) from exc

    # ---- commands --------------------------------------------------

    def status(self) -> dict:
        return self._send({"cmd": "status"})

    def run(self, plugin_id: str) -> dict:
        return self._send({"cmd": "run", "plugin": plugin_id})

    def reload(self) -> dict:
        return self._send({"cmd": "reload"})

    def version(self) -> dict:
        return self._send({"cmd": "version"})

    def credential_status(self, plugin_id: str) -> dict:
        return self._send({"cmd": "credential_status", "plugin": plugin_id})

    def set_credential(self, plugin_id: str, key: str, secret: str) -> dict:
        return self._send({
            "cmd": "set_credential", "plugin": plugin_id,
            "key": key, "secret": secret,
        })

    def delete_credential(self, plugin_id: str, key: str) -> dict:
        return self._send({
            "cmd": "delete_credential", "plugin": plugin_id, "key": key,
        })
