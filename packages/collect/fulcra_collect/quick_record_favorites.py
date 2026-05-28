"""Quick-record favorites storage — which annotation defs surface
prominently in the menubar's quick-record popover.

Per-machine, local file at ``<config_dir>/quick_record_favorites.json``.

Storage shape::

    {
      "version": 1,
      "favorites": ["<def-uuid>", ...]
    }

Cross-machine sync is deferred to a separate task (#65) — Fulcra doesn't
yet expose the right primitive to attach this preference to the user's
account. For v1, favorites are local to the machine the daemon runs on.

Surfaces that read this file:

* ``Daemon._quick_record_list`` — pinned defs sort first and the cap is
  relaxed when any favorites are set.
* The web UI Settings page — bulk multi-select editor.
* The menubar popover — per-row star toggle.

All three call the daemon command pair
``get_quick_record_favorites`` / ``set_quick_record_favorites`` so the
file is opened in exactly one process; the writes are atomic (tempfile
+ ``os.replace``) and protected by 0600 permissions.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from .config import config_dir


FILE_NAME = "quick_record_favorites.json"
SCHEMA_VERSION = 1


def _path() -> Path:
    return config_dir() / FILE_NAME


def load() -> set[str]:
    """Return the favorite def_ids as a set. Returns an empty set when
    the file is missing, unreadable, or schema-mismatched — first-launch
    callers see "no favorites" and fall back to the recency-only listing.
    """
    path = _path()
    if not path.exists():
        return set()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.getLogger("fulcra_collect.quick_record_favorites").warning(
            "quick_record_favorites.json unreadable; treating as empty",
        )
        return set()
    if not isinstance(doc, dict):
        return set()
    raw = doc.get("favorites")
    if not isinstance(raw, list):
        return set()
    # Keep order out of the set; defensively drop non-string entries so
    # one bad row doesn't poison the rest.
    return {x for x in raw if isinstance(x, str) and x}


def save(favorites: set[str]) -> None:
    """Atomically persist the favorites set.

    Writes a uniquely-named temp file in the same directory then
    ``os.replace``s it into place so a concurrent reader never sees a
    half-written file. Same pattern as ``state.save``.

    The file is created with 0600 permissions — favorites aren't a
    secret in themselves but the config directory is owner-only, so we
    match the surrounding convention.
    """
    payload = json.dumps(
        {"version": SCHEMA_VERSION, "favorites": sorted(favorites)},
        indent=2, sort_keys=True,
    )
    d = config_dir()
    path = d / FILE_NAME
    fd, tmp = tempfile.mkstemp(
        dir=d, prefix=f".{FILE_NAME}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add(def_id: str) -> None:
    """Add ``def_id`` to the favorites set and persist."""
    if not def_id:
        return
    current = load()
    if def_id in current:
        return
    current.add(def_id)
    save(current)


def remove(def_id: str) -> None:
    """Drop ``def_id`` from the favorites set and persist. No-op if it
    wasn't a favorite."""
    if not def_id:
        return
    current = load()
    if def_id not in current:
        return
    current.discard(def_id)
    save(current)


def is_favorite(def_id: str) -> bool:
    """Convenience predicate. Loads the file every call — favorites is
    small so the I/O is negligible, and the daemon already caches the
    quick-record list itself (which is where this is read in bulk)."""
    if not def_id:
        return False
    return def_id in load()


def prune(known_def_ids: set[str]) -> set[str]:
    """Drop favorites that no longer correspond to a live def.

    Called from the soft-delete path so the favorites file doesn't
    accumulate orphan UUIDs after the user removes a def. Returns the
    set of def_ids that were dropped (informational; the caller can
    log them).

    No-op when no favorites are stored. Doesn't rewrite the file unless
    something actually changed — avoids touching the mtime on every
    delete that misses.
    """
    current = load()
    if not current:
        return set()
    dropped = current - known_def_ids
    if not dropped:
        return set()
    save(current - dropped)
    return dropped
