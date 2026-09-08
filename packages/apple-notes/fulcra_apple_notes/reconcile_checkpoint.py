"""Private, body-free checkpoints for one resumable reconciliation scan."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .reconcile import VaultObservation

VERSION = 1


def scope_key(container: Path, state: dict) -> str:
    """Bind cached reads to one container and the exact sync content baseline."""
    source = {"container": str(container.resolve()),
              "version": state.get("version"),
              "notes": state.get("notes", {}),
              "attachments": state.get("attachments", {})}
    return hashlib.sha256(json.dumps(source, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


@contextmanager
def locked(path: Path):
    """Refuse overlapping writers instead of resurrecting a completed scan."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path) + ".lock", flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Apple Notes reconciliation is already running") from exc
        yield
    finally:
        os.close(fd)


def load(path: Path, scope: str) -> dict[str, VaultObservation]:
    if path.is_symlink():
        raise RuntimeError("Reconciliation checkpoint must not be a symbolic link")
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError):
        return {}  # Corrupt cache is not evidence; reread the source files.
    if not isinstance(raw, dict) or raw.get("version") != VERSION or raw.get("scope") != scope:
        return {}
    entries = raw.get("observations")
    if not isinstance(entries, dict):
        return {}
    observations = {}
    for uuid, value in entries.items():
        if not isinstance(uuid, str) or not isinstance(value, dict):
            return {}
        if set(value) != {"exists", "has_fence", "body_hash"}:
            return {}
        exists, fence, digest = value["exists"], value["has_fence"], value["body_hash"]
        if type(exists) is not bool or type(fence) is not bool:
            return {}
        if not exists and fence:
            return {}
        if fence:
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{16}", digest):
                return {}
        elif digest is not None:
            return {}
        observations[uuid] = VaultObservation(exists, fence, digest)
    return observations


def save(path: Path, scope: str, observations: dict[str, VaultObservation]) -> None:
    """Atomic replacement with a private file, including on an existing path."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"version": VERSION, "scope": scope,
                       "observations": {uuid: asdict(obs) for uuid, obs in observations.items()}},
                      stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def clear(path: Path) -> None:
    path.unlink(missing_ok=True)
