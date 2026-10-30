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

    def _send(self, request: dict, *, max_response_bytes: int | None = None) -> dict:
        try:
            if max_response_bytes is not None:
                return _send_request(self.socket_path, request, timeout=self.timeout,
                                     max_response_bytes=max_response_bytes)
            return _send_request(self.socket_path, request, timeout=self.timeout)
        except (ConnectionError, OSError) as exc:
            raise DaemonUnavailable(str(exc)) from exc

    # ---- commands --------------------------------------------------

    def quick_record_v2(self, operation: str, **fields) -> dict:
        """Forward a frozen scoped request; never infer or replace its scope.

        Large responses are bounded independently of the unchanged 64-KiB
        request limit. This transport never generates action IDs or retries;
        the durable interactive client owns intent before the first call.
        """
        request = {**fields, "cmd": "quick_record_v2", "operation": operation}
        return self._send(request, max_response_bytes=8 * 1024 * 1024)

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

    def delete_definition(self, def_id: str) -> dict:
        """Soft-delete an annotation definition.

        Wraps the daemon's UDS command introduced in SP2 task 1.
        Returns the daemon's reply dict — {"ok": True, "cleared_plugins":
        [...]} on success, {"ok": False, "code": "...", "error": "..."}
        on any failure mode. The `code` field is one of
        "bad_request"/"unauthorized"/"not_found"/"timeout"/"upstream_error"
        so callers can branch on it rather than parse the error text.
        Caller is responsible for surfacing the error to the user
        (e.g., NSAlert) and not retrying the same UUID.

        Args:
            def_id: the annotation definition UUID.
        """
        return self._send({"cmd": "delete_definition", "def_id": def_id})
