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
  * ``--help``                      -> exit 0

State root comes from ``FULCRA_FAKE_ROOT`` (a real local directory). Remote
paths like ``/coordination-demo/tasks/X.json`` map to files under that root.

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
            return 1
        data = local.read_bytes()
        version = hashlib.sha1(data).hexdigest()
        print(json.dumps({"path": argv[1], "size": len(data), "version": version}))
        return 0

    if cmd == "download":
        local = _local_for(root, argv[1])
        if not local.exists():
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
            if p.is_file():
                print("/" + str(p.relative_to(root)))
        return 0

    sys.stderr.write(f"fake backend: unknown command {cmd!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
