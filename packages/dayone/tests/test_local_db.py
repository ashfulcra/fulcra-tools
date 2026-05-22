"""Local Day One SQLite reader."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fulcra_dayone.readers.local_db import read_local_db
from tests.conftest import build_dayone_db


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
