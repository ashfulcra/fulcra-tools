#!/usr/bin/env python3
"""Stateful fake of the ``fulcra-api file`` CLI for facade tests.

The ``fulcra_coord.remote`` layer drives all Fulcra I/O by shelling out to a
backend command (resolved from ``FULCRA_COORD_BACKEND`` in tests). This script
emulates that backend against a local directory so the facade's real
write+rebuild path (task upload + view fan-out + stat-based concurrency) runs
end-to-end without touching live Fulcra.

It mirrors exactly the four subcommands the package uses:
  * ``stat <remote_path>``            -> prints JSON stat (size/version) or exits 1
  * ``download <remote_path> -``      -> prints file contents or exits 1
  * ``upload <local_tmp> <remote>``   -> copies bytes, bumps a version counter
  * ``list <prefix>``                 -> prints matching remote paths
  * ``--help``                        -> exit 0 (used by check_cli_available)

State root is taken from ``FULCRA_FAKE_ROOT`` (a real local directory). Remote
paths like ``/coordination/tasks/X.json`` map to files under that root.
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
    # Strip leading slash so it nests under root rather than escaping it.
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
        remote_path = argv[1]
        local = _local_for(root, remote_path)
        if not local.exists():
            return 1
        data = local.read_bytes()
        # Strong identity key: content hash as the version. stat_changed() uses
        # ``version`` as a definitive change signal, so a re-upload with new
        # bytes is detected.
        version = hashlib.sha1(data).hexdigest()
        print(json.dumps({"path": remote_path, "size": len(data), "version": version}))
        return 0

    if cmd == "download":
        remote_path = argv[1]
        local = _local_for(root, remote_path)
        if not local.exists():
            return 1
        sys.stdout.write(local.read_text())
        return 0

    if cmd == "upload":
        local_src = Path(argv[1])
        remote_path = argv[2]
        if not local_src.exists():
            return 1
        dst = _local_for(root, remote_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(local_src.read_text())
        return 0

    if cmd == "list":
        prefix = argv[1]
        target = _local_for(root, prefix)
        if not target.exists():
            return 0
        for p in sorted(target.rglob("*")):
            if p.is_file():
                rel = p.relative_to(root)
                print("/" + str(rel))
        return 0

    sys.stderr.write(f"fake backend: unknown command {cmd!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
