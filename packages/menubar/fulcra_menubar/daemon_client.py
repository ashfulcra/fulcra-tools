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
        except (ConnectionError, OSError) as exc:
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

    def quick_record_list(self) -> dict:
        """Return Moment annotation definitions for the menubar quick-record
        surface. The daemon caches the result for 60 seconds."""
        return self._send({"cmd": "quick_record_list"})

    def record_annotation(self, definition_id: str,
                          *,
                          comment: str | None = None,
                          start_time: str | None = None,
                          end_time: str | None = None) -> dict:
        """Write one annotation to Fulcra immediately.

        Modes:

        - Moment (default): pass neither ``start_time`` nor ``end_time``.
          The daemon writes a MomentAnnotation timestamped at now.

        - Duration: pass BOTH ``start_time`` AND ``end_time`` as
          ISO-8601 UTC strings (trailing 'Z' is accepted). The daemon
          writes a DurationAnnotation. Partial spec (only one of the
          two) returns ok=False.

        Parameters
        ----------
        definition_id:
            UUID of the Fulcra annotation definition to record.
        comment:
            Optional free-text comment attached to the annotation.
        start_time, end_time:
            Optional ISO-8601 strings spanning a Duration record.
        """
        req: dict = {"cmd": "record_annotation", "definition_id": definition_id}
        if comment is not None:
            req["comment"] = comment
        if start_time is not None:
            req["start_time"] = start_time
        if end_time is not None:
            req["end_time"] = end_time
        return self._send(req)

    def get_quick_record_favorites(self) -> dict:
        """Return the user's pinned annotation def_ids — the menubar's
        per-row star toggle reads this once at popover open to colour
        the star icon. Always succeeds (an absent file = []) so the
        UI doesn't need an error path."""
        return self._send({"cmd": "get_quick_record_favorites"})

    def set_quick_record_favorites(self, def_ids: list[str]) -> dict:
        """Replace the favorites list with ``def_ids`` and ask the daemon
        to invalidate its quick-record cache so the next list call
        reflects the new pin state immediately."""
        return self._send({
            "cmd": "set_quick_record_favorites",
            "favorites": list(def_ids),
        })

    def delete_annotation(self, source_id: str) -> dict:
        """Soft-delete a previously-recorded annotation by source_id.

        IMPORTANT: this is a SOFT marker, not a hard delete. The daemon
        writes a tombstone annotation referencing the original source_id;
        the original event remains on the user's Fulcra timeline because
        Fulcra has no per-event delete primitive (verified 2026-05-26).
        See ``daemon._delete_annotation`` and the menubar popover's
        "Recently recorded" tooltip for the full caveat surfaced to
        the user.
        """
        return self._send({"cmd": "delete_annotation", "source_id": source_id})
