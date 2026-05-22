"""Day One readers — dispatch to the JSON export or the local database."""
from __future__ import annotations

from pathlib import Path

from ..entry import DayOneEntry
from .json_export import read_json_export
from .local_db import read_local_db


def read(
    source: Path | None, *, local_db: bool, db_path: Path | None,
) -> list[DayOneEntry]:
    """Read Day One entries. With `local_db` True, read the local
    database (`db_path` optional); otherwise read the JSON export at
    `source` (a .zip or a folder)."""
    if local_db:
        return read_local_db(db_path)
    if source is None:
        raise ValueError("provide an export path (.zip or folder), or use --local-db")
    return read_json_export(source)
