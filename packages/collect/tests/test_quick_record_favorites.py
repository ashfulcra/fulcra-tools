"""Tests for the per-machine quick-record favorites store.

Covers:

* ``load()`` returns an empty set when the file is absent.
* ``save()`` writes the documented JSON shape with 0600 permissions.
* ``add()`` / ``remove()`` round-trip; idempotent on duplicates / misses.
* ``is_favorite()`` honours saved state.
* ``prune()`` drops orphan entries and leaves a no-op untouched.
* A failed atomic write leaves no stray ``.tmp`` files behind.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from fulcra_collect import quick_record_favorites as favs
from fulcra_collect.config import config_dir


# collect_home fixture lives in conftest.py and points FULCRA_COLLECT_HOME
# at a temp dir for the test — everything below writes there.


def test_load_returns_empty_set_when_file_absent(collect_home: Path):
    assert favs.load() == set()


def test_save_writes_expected_shape(collect_home: Path):
    favs.save({"def-a", "def-b"})
    path = config_dir() / favs.FILE_NAME
    assert path.exists()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["version"] == favs.SCHEMA_VERSION
    # Sorted for deterministic file contents.
    assert doc["favorites"] == ["def-a", "def-b"]


def test_saved_file_has_0600_permissions(collect_home: Path):
    favs.save({"def-a"})
    path = config_dir() / favs.FILE_NAME
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_add_then_load_round_trip(collect_home: Path):
    favs.add("def-1")
    favs.add("def-2")
    favs.add("def-1")  # duplicate is a no-op
    assert favs.load() == {"def-1", "def-2"}


def test_remove_then_load_round_trip(collect_home: Path):
    favs.save({"def-a", "def-b"})
    favs.remove("def-a")
    favs.remove("def-missing")  # idempotent
    assert favs.load() == {"def-b"}


def test_add_and_remove_ignore_empty_strings(collect_home: Path):
    """The daemon dispatcher shouldn't accidentally persist empty UUIDs
    if a client sends them."""
    favs.add("")
    favs.remove("")
    assert favs.load() == set()


def test_is_favorite_matches_persisted_state(collect_home: Path):
    favs.save({"def-x"})
    assert favs.is_favorite("def-x") is True
    assert favs.is_favorite("def-other") is False
    assert favs.is_favorite("") is False


def test_load_tolerates_corrupt_file(collect_home: Path):
    """A torn write or hand-edit shouldn't crash callers — return empty
    so the menubar simply shows the recency fallback."""
    path = config_dir() / favs.FILE_NAME
    path.write_text("{not json", encoding="utf-8")
    assert favs.load() == set()


def test_load_tolerates_unexpected_schema(collect_home: Path):
    """A file shaped differently (e.g. a future schema we don't know
    yet, or a list at the top level) returns empty rather than blowing
    up."""
    path = config_dir() / favs.FILE_NAME
    path.write_text(json.dumps(["def-1", "def-2"]), encoding="utf-8")
    assert favs.load() == set()


def test_load_drops_non_string_entries(collect_home: Path):
    """A malformed favorites list with mixed types still yields a
    set of the valid string IDs."""
    path = config_dir() / favs.FILE_NAME
    path.write_text(
        json.dumps({"version": 1, "favorites": ["good", 42, None, "also-good", ""]}),
        encoding="utf-8",
    )
    assert favs.load() == {"good", "also-good"}


def test_prune_drops_orphan_ids(collect_home: Path):
    favs.save({"def-a", "def-b", "def-orphan"})
    dropped = favs.prune({"def-a", "def-b"})
    assert dropped == {"def-orphan"}
    assert favs.load() == {"def-a", "def-b"}


def test_prune_is_noop_when_nothing_to_drop(collect_home: Path):
    """Don't touch the file when prune has nothing to do — keeps the
    mtime stable across no-op soft-deletes."""
    favs.save({"def-a"})
    path = config_dir() / favs.FILE_NAME
    mtime_before = path.stat().st_mtime
    # Force-different mtime by sleeping isn't reliable in tests — instead,
    # assert the return value (the surface contract) and that the file
    # contents are unchanged.
    dropped = favs.prune({"def-a", "def-b"})
    assert dropped == set()
    assert favs.load() == {"def-a"}
    # mtime should still be at-or-before what it was — we didn't rewrite.
    assert path.stat().st_mtime == mtime_before


def test_prune_with_empty_store_is_noop(collect_home: Path):
    """An account with no favorites at all shouldn't create the file just
    to record "still empty"."""
    path = config_dir() / favs.FILE_NAME
    assert not path.exists()
    dropped = favs.prune({"def-a"})
    assert dropped == set()
    assert not path.exists()


def test_save_atomic_write_leaves_no_temp_on_error(
        collect_home: Path, monkeypatch):
    """If the os.replace step fails mid-write, the partial temp file must
    be cleaned up so the config dir doesn't accumulate .tmp leftovers
    across crashes.
    """
    real_replace = os.replace

    def _boom_replace(src, dst):
        # Simulate a filesystem error (e.g. cross-device link, disk full)
        # at the rename step. Cleans up so the test directory leak check
        # still passes.
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom_replace)

    with pytest.raises(OSError):
        favs.save({"def-a"})

    monkeypatch.setattr(os, "replace", real_replace)

    # No stray .tmp files left behind.
    d = config_dir()
    leftovers = list(d.glob(f".{favs.FILE_NAME}.*.tmp"))
    assert leftovers == [], f"unexpected temp files: {leftovers}"
    # And the real favorites file was never created (we never reached replace).
    assert not (d / favs.FILE_NAME).exists()
