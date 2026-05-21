"""Read a Day One JSON export (.zip or unzipped folder) into DayOneEntry[]."""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from ..entry import DayOneEntry


def _parse_date(raw: str) -> datetime:
    # Day One JSON dates look like "2024-01-15T09:30:00Z".
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc)


def _compose_location(loc: dict) -> str | None:
    for key in ("placeName", "localityName", "administrativeArea", "country"):
        val = loc.get(key)
        if val:
            return str(val)
    return None


def _entries_from_json(path: Path) -> tuple[list[DayOneEntry], int]:
    journal = path.stem
    doc = json.loads(path.read_text(encoding="utf-8"))
    out: list[DayOneEntry] = []
    skipped = 0
    for raw in doc.get("entries", []):
        uuid = raw.get("uuid")
        created = raw.get("creationDate")
        if not uuid or not created:
            skipped += 1
            continue
        text = raw.get("text", "") or ""
        loc = raw.get("location") or {}
        out.append(DayOneEntry(
            uuid=uuid,
            creation_date=_parse_date(created),
            text=text,
            tags=tuple(raw.get("tags", []) or []),
            starred=bool(raw.get("starred", False)),
            journal=journal,
            location=_compose_location(loc) if loc else None,
            photo_count=len(raw.get("photos", []) or []),
            word_count=len(text.split()),
        ))
    return out, skipped


def _read_folder(folder: Path) -> list[DayOneEntry]:
    json_files = sorted(folder.rglob("*.json"))
    if not json_files:
        raise ValueError(f"no .json files found in Day One export: {folder}")
    out: list[DayOneEntry] = []
    skipped = 0
    for jf in json_files:
        entries, n = _entries_from_json(jf)
        out.extend(entries)
        skipped += n
    if skipped:
        print(f"json_export: skipped {skipped} entries missing uuid/creationDate",
              file=sys.stderr)
    return out


def read_json_export(source: Path) -> list[DayOneEntry]:
    """Read a Day One JSON export. `source` is a .zip or an unzipped folder."""
    if source.is_file() and source.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(source) as zf:
                zf.extractall(tmp)
            return _read_folder(Path(tmp))
    if source.is_dir():
        return _read_folder(source)
    raise ValueError(f"not a Day One JSON export (.zip or folder): {source}")
