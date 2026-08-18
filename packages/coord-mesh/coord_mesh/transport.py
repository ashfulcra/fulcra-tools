"""Thin, HARD-bounded caller for `fulcra-api`. Same discipline as coord-engine.

Two contracts this module keeps:

  - Every call is time-bounded and its process group is killed on timeout, so a
    hung CLI cannot stretch a mesh fold indefinitely.
  - A failure is NEVER silently an empty result. Reads return a `Result` whose
    ``state`` is one of ok / empty / error, because "I read the peer's outbox
    and it was empty" and "I could not read the peer's outbox" are different
    facts. A mesh that conflates them reports quiet when it means blind.
"""
import json
import os
import signal
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from . import safety

OK = "ok"
EMPTY = "empty"
ERROR = "error"

DEFAULT_TIMEOUT = 60.0


class TransportError(Exception):
    """Normalized failure. Nothing else escapes a transport call."""


@dataclass
class Result:
    state: str
    rows: list = field(default_factory=list)
    detail: str = ""

    @property
    def readable(self) -> bool:
        return self.state in (OK, EMPTY)

    @property
    def unknown(self) -> bool:
        """This read proves nothing. Callers must not render it as clear."""
        return self.state == ERROR


def _command() -> list:
    return (os.environ.get("FULCRA_CMD") or "fulcra-api").split()


def run(args: list, *, timeout: float = DEFAULT_TIMEOUT):
    """Run `fulcra-api <args>`, bounded. Raises TransportError only."""
    argv = [*_command(), *args]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
    except OSError as exc:
        raise TransportError(f"exec failed: {' '.join(args)}: {exc}") from exc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        raise TransportError(f"timeout after {timeout}s: {' '.join(args)}") from exc
    return proc.returncode, out, err


def share_create(*, name: str, data_type: str, user_id: str,
                 file_prefix: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT):
    """Mint ONE scoped outbound share at a named uid.

    Both rails run on the argv actually about to execute, not on intent.
    """
    uid = safety.require_named_uid(user_id)
    args = ["share", "create", "--name", name, "--data-type", data_type,
            "--user-id", uid]
    if file_prefix:
        args += ["--file", file_prefix]
    safety.refuse_destructive("create")
    safety.refuse_share_all(args)
    return run(args, timeout=timeout)


def _parse_records(out: str) -> tuple:
    """Split a get-records payload into (rows, bad_lines), deduped by id.

    The API can repeat a row inside one read — observed live on 2026-08-18,
    where a 42-line read carried 21 distinct records.
    """
    rows, bad, seen = [], 0, set()
    for line in (out or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad += 1
            continue
        rid = row.get("id") if isinstance(row, dict) else None
        if rid:
            if rid in seen:
                continue
            seen.add(rid)
        rows.append(row)
    return rows, bad


def get_records(data_type: str, window: str, *, user_id: Optional[str] = None,
                timeout: float = DEFAULT_TIMEOUT) -> Result:
    """Read records, optionally from a PEER's account — the cross-account read
    path. A failed or partial read is ERROR, never an empty list.
    """
    args = ["get-records", data_type, window]
    if user_id:
        args += ["--user-id", safety.require_named_uid(user_id)]
    try:
        rc, out, err = run(args, timeout=timeout)
    except TransportError as exc:
        return Result(ERROR, detail=str(exc))
    if rc != 0:
        return Result(ERROR, detail=(err or out or f"rc={rc}").strip()[:400])

    rows, bad = _parse_records(out)
    if bad:
        # Partial parse is NOT ok: we cannot say what we missed.
        return Result(ERROR, rows=rows,
                      detail=f"{bad} unparseable line(s) — read is partial, not empty")
    return Result(OK if rows else EMPTY, rows=rows)


def list_incoming(*, timeout: float = DEFAULT_TIMEOUT) -> Result:
    """The incoming-share roster — the mesh JOIN signal (Leif learning:
    the join signal is the incoming-share row, not a handshake message)."""
    try:
        rc, out, err = run(["share", "list-incoming"], timeout=timeout)
    except TransportError as exc:
        return Result(ERROR, detail=str(exc))
    if rc != 0:
        return Result(ERROR, detail=(err or out or f"rc={rc}").strip()[:400])
    rows, bad = _parse_records(out)
    if bad:
        return Result(ERROR, rows=rows, detail=f"{bad} unparseable line(s)")
    return Result(OK if rows else EMPTY, rows=rows)


def list_outgoing(*, timeout: float = DEFAULT_TIMEOUT) -> Result:
    try:
        rc, out, err = run(["share", "list-outgoing"], timeout=timeout)
    except TransportError as exc:
        return Result(ERROR, detail=str(exc))
    if rc != 0:
        return Result(ERROR, detail=(err or out or f"rc={rc}").strip()[:400])
    rows, bad = _parse_records(out)
    if bad:
        return Result(ERROR, rows=rows, detail=f"{bad} unparseable line(s)")
    return Result(OK if rows else EMPTY, rows=rows)
