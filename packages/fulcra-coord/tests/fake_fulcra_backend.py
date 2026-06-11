#!/usr/bin/env python3
"""Stateful fake of the ``fulcra-api file`` CLI for fulcra-coord tests.

The ``fulcra_coord.remote`` layer drives all Fulcra I/O by shelling out to a
backend command (resolved from ``FULCRA_COORD_BACKEND``). This script emulates
that backend against a local directory so the demo seed's real upload +
view-rebuild path runs end-to-end without touching a live Fulcra account.

Mirrors the subcommands the package uses:
  * ``stat <remote_path>``          -> JSON stat (size/version) or exit 1
  * ``download <remote_path> -``    -> file contents or exit 1
  * ``upload <local_tmp> <remote>`` -> copy bytes
  * ``list <prefix>``               -> matching remote paths
  * ``delete <remote_path>``        -> unlink the file (exit 1 if absent)
  * ``--help``                      -> exit 0

State root comes from ``FULCRA_FAKE_ROOT`` (a real local directory). Remote
paths like ``/coordination-demo/tasks/X.json`` map to files under that root.

TOMBSTONES (the platform's SOFT delete, 2026-06-11): the real Fulcra Files
DELETE keeps version history, so ``stat`` on a deleted file still answers
(with the prior version's metadata) while ``download`` fails with a
not-found-class error. Tests model that by placing a ``<path>.tombstone``
sibling file (holding the prior version's content) where the live body would
be: ``stat`` then reports the prior version, ``download`` exits 1 with the
404-shaped stderr the real CLI emits, and ``list`` never shows the path.
The default ``delete`` below stays a hard unlink — most tests predate the
tombstone work and pin behaviors on plain absence; tombstone tests opt in by
writing the sibling file themselves.

This duplicates ``adapters/chatgpt/facade/tests/fake_fulcra_backend.py`` on
purpose: the two test suites run from different working directories and must not
import across the adapter boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def _root() -> Path:
    root = os.environ.get("FULCRA_FAKE_ROOT")
    if not root:
        sys.stderr.write("FULCRA_FAKE_ROOT not set\n")
        sys.exit(2)
    p = Path(root)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _local_for(root: Path, remote_path: str) -> Path:
    rel = remote_path.lstrip("/")
    return root / rel


def _tombstone_for(local: Path) -> Path:
    """The soft-delete marker for ``local`` (see module docstring)."""
    return Path(str(local) + ".tombstone")


def main(argv: list[str]) -> int:
    if not argv:
        return 0
    cmd = argv[0]

    if cmd == "--help":
        print("fake fulcra-api file backend")
        return 0

    root = _root()

    if cmd == "stat":
        local = _local_for(root, argv[1])
        if not local.exists():
            tomb = _tombstone_for(local)
            if tomb.exists():
                # Soft-deleted: the real CLI still reports the version history
                # of a deleted file — the prior version's metadata, here.
                data = tomb.read_bytes()
                version = hashlib.sha1(data).hexdigest()
                print(json.dumps({"path": argv[1], "size": len(data),
                                  "version": version, "previous_versions": 1}))
                return 0
            return 1
        data = local.read_bytes()
        version = hashlib.sha1(data).hexdigest()
        print(json.dumps({"path": argv[1], "size": len(data), "version": version}))
        return 0

    if cmd == "download":
        local = _local_for(root, argv[1])
        if not local.exists():
            if _tombstone_for(local).exists():
                # Soft-deleted: the current version is a delete marker — the
                # download fails DETERMINISTICALLY with the not-found-class
                # stderr the real CLI emits (NOT transient per #167's
                # classifier; version history remains restorable).
                sys.stderr.write("Error: HTTP Error 404: Not Found\n")
            return 1
        sys.stdout.write(local.read_text())
        return 0

    if cmd == "upload":
        local_src = Path(argv[1])
        if not local_src.exists():
            return 1
        dst = _local_for(root, argv[2])
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(local_src.read_text())
        return 0

    if cmd == "list":
        target = _local_for(root, argv[1])
        if not target.exists():
            return 0
        for p in sorted(target.rglob("*")):
            # Tombstone markers are emulator state, not listable remote files
            # (the real CLI does not list deleted paths).
            if p.is_file() and not p.name.endswith(".tombstone"):
                print("/" + str(p.relative_to(root)))
        return 0

    if cmd == "delete":
        # Soft-delete in the real CLI; here a plain unlink is enough for the
        # archive move's write-then-delete to be observable. Absent file -> exit 1
        # (mirrors the real CLI erroring on a missing path), so remote.delete
        # returns False and _archive_task's idempotent stat-gate stays honest.
        local = _local_for(root, argv[1])
        if not local.exists():
            return 1
        local.unlink()
        return 0

    sys.stderr.write(f"fake backend: unknown command {cmd!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
