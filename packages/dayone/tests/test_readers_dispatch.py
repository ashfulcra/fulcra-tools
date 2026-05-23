"""readers.read dispatch."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fulcra_dayone.readers import read
from dayone_test_helpers import build_dayone_db

_SAMPLE = {"entries": [
    {"uuid": "AAA111", "creationDate": "2024-01-15T09:30:00Z", "text": "hi"},
]}


def test_read_uses_json_export_for_a_folder(tmp_path: Path):
    folder = tmp_path / "export"
    folder.mkdir()
    (folder / "Personal.json").write_text(json.dumps(_SAMPLE), encoding="utf-8")
    entries = read(folder, local_db=False, db_path=None)
    assert {e.uuid for e in entries} == {"AAA111"}


def test_read_uses_local_db_when_requested(tmp_path: Path):
    db = tmp_path / "DayOne.sqlite"
    build_dayone_db(db)
    entries = read(None, local_db=True, db_path=db)
    assert {e.uuid for e in entries} == {"AAA111", "BBB222"}


def test_read_without_source_or_local_db_raises():
    with pytest.raises(ValueError, match="export path"):
        read(None, local_db=False, db_path=None)
