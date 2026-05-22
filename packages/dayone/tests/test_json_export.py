"""JSON-export reader: .zip and folder."""
from __future__ import annotations

import json
import zipfile
from datetime import timezone
from pathlib import Path

import pytest

from fulcra_dayone.readers.json_export import read_json_export

SAMPLE = {
    "metadata": {"version": "1.0"},
    "entries": [
        {
            "uuid": "AAA111",
            "creationDate": "2024-01-15T09:30:00Z",
            "text": "First entry body",
            "tags": ["work", "travel"],
            "starred": True,
            "location": {"placeName": "Cafe", "country": "USA"},
            "photos": [{"identifier": "p1"}],
        },
        {
            "uuid": "BBB222",
            "creationDate": "2024-02-20T14:00:00Z",
            "text": "Second entry, no tags",
        },
    ],
}


def _write_export_folder(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Personal.json").write_text(json.dumps(SAMPLE), encoding="utf-8")
    return folder


def test_reads_a_folder_export(tmp_path: Path):
    folder = _write_export_folder(tmp_path / "export")
    entries = read_json_export(folder)
    assert {e.uuid for e in entries} == {"AAA111", "BBB222"}
    first = next(e for e in entries if e.uuid == "AAA111")
    assert first.journal == "Personal"
    assert first.tags == ("work", "travel")
    assert first.starred is True
    assert first.location == "Cafe"
    assert first.photo_count == 1
    assert first.creation_date.tzinfo == timezone.utc
    assert first.creation_date.hour == 9


def test_second_entry_has_empty_optionals(tmp_path: Path):
    folder = _write_export_folder(tmp_path / "export")
    entries = read_json_export(folder)
    second = next(e for e in entries if e.uuid == "BBB222")
    assert second.tags == ()
    assert second.starred is False
    assert second.location is None
    assert second.photo_count == 0


def test_reads_a_zip_export(tmp_path: Path):
    folder = _write_export_folder(tmp_path / "export")
    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(folder / "Personal.json", "Personal.json")
    entries = read_json_export(zip_path)
    assert {e.uuid for e in entries} == {"AAA111", "BBB222"}


def test_journal_name_comes_from_the_json_filename(tmp_path: Path):
    folder = tmp_path / "export"
    folder.mkdir()
    (folder / "Travel Journal.json").write_text(json.dumps(SAMPLE), encoding="utf-8")
    entries = read_json_export(folder)
    assert all(e.journal == "Travel Journal" for e in entries)


def test_malformed_json_file_is_skipped_not_fatal(tmp_path: Path):
    folder = tmp_path / "export"
    folder.mkdir()
    # One good journal, one corrupt — the good one must still import.
    (folder / "Personal.json").write_text(json.dumps(SAMPLE), encoding="utf-8")
    (folder / "Broken.json").write_text("{not valid json", encoding="utf-8")
    entries = read_json_export(folder)
    assert {e.uuid for e in entries} == {"AAA111", "BBB222"}


def test_rejects_a_path_that_is_neither_zip_nor_folder(tmp_path: Path):
    bogus = tmp_path / "notes.txt"
    bogus.write_text("hi")
    with pytest.raises(ValueError, match="not a Day One JSON export"):
        read_json_export(bogus)
