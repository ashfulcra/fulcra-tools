"""Local Day One SQLite reader."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fulcra_dayone.readers.local_db import read_local_db
from dayone_test_helpers import build_dayone_db


def test_reads_entries_from_a_core_data_db(tmp_path: Path):
    db = tmp_path / "DayOne.sqlite"
    build_dayone_db(db)
    entries = read_local_db(db)
    # Entry CCC333 has empty text and is skipped.
    assert {e.uuid for e in entries} == {"AAA111", "BBB222"}


def test_maps_journal_tags_location_and_photo_count(tmp_path: Path):
    db = tmp_path / "DayOne.sqlite"
    build_dayone_db(db)
    entries = read_local_db(db)
    first = next(e for e in entries if e.uuid == "AAA111")
    assert first.journal == "Personal"
    assert first.tags == ("travel", "work")  # sorted
    assert first.location == "Cafe"
    assert first.photo_count == 2
    assert first.starred is True
    assert first.creation_date.year == 2024 and first.creation_date.hour == 9


def test_entry_with_no_location_or_tags(tmp_path: Path):
    db = tmp_path / "DayOne.sqlite"
    build_dayone_db(db)
    entries = read_local_db(db)
    second = next(e for e in entries if e.uuid == "BBB222")
    assert second.location is None
    assert second.tags == ("travel",)
    assert second.photo_count == 0


def test_missing_z_primarykey_raises_a_schema_error(tmp_path: Path):
    db = tmp_path / "DayOne.sqlite"
    build_dayone_db(db)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE Z_PRIMARYKEY")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="schema not recognized"):
        read_local_db(db)


def test_missing_database_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        read_local_db(tmp_path / "nope.sqlite")


def test_snapshot_permission_denied_gives_actionable_fda_message(
    tmp_path: Path, monkeypatch
):
    """When the daemon process lacks Full Disk Access, both the `cp -c`
    clone and the shutil.copy2 fallback hit EPERM on the TCC-protected
    Day One container. The user should see an actionable "grant Full
    Disk Access" message — matching the apple-podcasts pattern — not a
    raw `PermissionError: [Errno 1] Operation not permitted` traceback.
    """
    import shutil as _sh
    import subprocess as _sp

    from fulcra_dayone.readers import local_db as _ldb

    db = tmp_path / "DayOne.sqlite"
    build_dayone_db(db)

    # cp -c exits non-zero (what happens under a TCC denial)...
    def _cp_fails(*args, **kwargs):
        raise _sp.CalledProcessError(1, ["cp", "-c"])

    # ...and the copy2 fallback hits the real EPERM.
    def _copy2_eperm(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(_ldb.subprocess, "run", _cp_fails)
    monkeypatch.setattr(_ldb.shutil, "copy2", _copy2_eperm)

    with pytest.raises(PermissionError, match="Full Disk Access"):
        read_local_db(db)
