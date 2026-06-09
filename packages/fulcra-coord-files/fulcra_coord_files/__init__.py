"""fulcra-coord-files — the no-CAS Fulcra Files object-store transport.

Extracted from ``fulcra_coord.remote`` as the first step of the event-substrate
migration, so the event layer can depend on a small, documented store contract
instead of the whole coordination package. See ``store`` for the full NO-CAS
contract (immutable uniquely-named blobs; stat/version is a staleness hint, not a
correctness guarantee).

This package owns ONLY the wire transport. Path-layout policy
(``*_remote_path`` / ``*_prefix`` helpers, anchored on the bus's
``remote_root()``) stays in ``fulcra_coord.remote``.
"""

from __future__ import annotations

from .store import (
    check_cli_available,
    check_file_commands,
    check_remote_access,
    delete,
    download,
    download_json,
    list_files,
    list_json,
    probe_reachable,
    stat,
    stat_changed,
    upload,
    upload_json,
)

__version__ = "0.1.0"

__all__ = [
    "stat",
    "download",
    "download_json",
    "upload",
    "upload_json",
    "delete",
    "list_files",
    "list_json",
    "stat_changed",
    "check_cli_available",
    "check_file_commands",
    "probe_reachable",
    "check_remote_access",
]
