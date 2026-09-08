"""Fulcra Files I/O for the Apple Notes sync.

Deliberately NOT reusing ``fulcra_vault.store``: that wrapper normalises
every path through ``normalize_note_path``, which forces a ``.md`` suffix.
That is right for notes and wrong for the two other things this plugin
writes -- binary attachments and a JSON state file -- which would become
``.png.md`` and ``.json.md``. This module therefore speaks absolute Fulcra
paths and adds a binary upload path, while keeping the same error
classification the vault package uses (a positively-missing file is a
different fact from a transport failure, and conflating them is how a sync
decides to overwrite something it merely failed to read).
"""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile


class VaultIOError(RuntimeError):
    """Transport failure -- the state of the remote file is UNKNOWN."""


class MissingFile(VaultIOError):
    """The file is positively absent (distinct from a failed read)."""


def _cli() -> list[str]:
    env = os.environ.get("FULCRA_CLI_COMMAND", "").strip()
    if env:
        return shlex.split(env)
    from fulcra_common.client import find_fulcra_cli
    found = find_fulcra_cli()
    if found:
        return [found]
    raise VaultIOError("Fulcra CLI is unavailable. Reinstall Collect or install fulcra-api.")


def _is_missing(stderr: str) -> bool:
    low = (stderr or "").lower()
    return "not found" in low or "no such file" in low or "404" in low


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    cmd = [*_cli(), "file", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VaultIOError(f"fulcra-api file {' '.join(args[:2])}: {exc}") from exc


def _detail(stderr: str, limit: int = 400) -> str:
    """Summarise a CLI failure.

    Keeps the TAIL, not the head: the callee is a Python CLI, so a failure
    arrives as a traceback whose actual exception is on the last line.
    Truncating from the front throws away the only part that identifies the
    fault -- which is exactly what happened to the first upload failure this
    plugin hit in production.
    """
    text = (stderr or "").strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def read_text(remote: str, *, timeout: int = 60) -> str:
    result = _run(["download", remote, "-"], timeout)
    if result.returncode == 0:
        return result.stdout
    if _is_missing(result.stderr):
        raise MissingFile(remote)
    raise VaultIOError(f"download {remote} failed: {_detail(result.stderr)}")


def write_text(remote: str, content: str, *, timeout: int = 120) -> None:
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         suffix=".md", delete=False) as fh:
            fh.write(content)
            tmp = fh.name
        result = _run(["upload", tmp, remote], timeout)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
    if result.returncode != 0:
        raise VaultIOError(f"upload {remote} failed: {_detail(result.stderr)}")


def upload_file(local_path: str, remote: str, *, timeout: int = 300) -> None:
    """Upload a local file verbatim (used for binary attachments)."""
    result = _run(["upload", local_path, remote], timeout)
    if result.returncode != 0:
        raise VaultIOError(f"upload {remote} failed: {_detail(result.stderr)}")


def list_dir(remote: str, *, timeout: int = 120) -> str:
    """Raw `file list` output for a directory.

    Note that an empty result cannot prove the directory is absent -- the
    listing renders identically for a real-but-empty directory and for a
    path that does not exist. Use stat() for existence.
    """
    result = _run(["list", remote], timeout)
    if result.returncode != 0:
        raise VaultIOError(f"list {remote} failed: {_detail(result.stderr)}")
    return result.stdout


def stat(remote: str, *, timeout: int = 60) -> str | None:
    """Return stat output, or None when the file is positively absent.

    ``list`` cannot prove absence -- it returns an identical empty result
    for a real-but-empty directory and for a path that does not exist --
    so existence checks go through stat.
    """
    result = _run(["stat", remote], timeout)
    if result.returncode == 0:
        return result.stdout
    if _is_missing(result.stderr):
        return None
    raise VaultIOError(f"stat {remote} failed: {_detail(result.stderr)}")
