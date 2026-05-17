"""CSV → GenericEvent parser.

Reads any well-formed CSV with a header row and a configurable column
mapping. Timestamps are parsed with dateparser so almost any human-readable
format works ("2024-05-10 14:33", "May 10, 2024 at 02:33PM", "1715366000",
ISO 8601 with tz, etc.).
"""
from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

import dateparser

from .events import ColumnMap, GenericEvent, derive_source_id


def parse_value(v: Any) -> str:
    """Coerce a CSV value (which is always str via csv.DictReader) to clean str.

    Strips whitespace. Preserves emptiness so callers can decide what to skip.
    """
    if v is None:
        return ""
    return str(v).strip()


def _parse_ts(value: str, *, tz: tzinfo) -> datetime:
    """Parse a flexible timestamp string into a tz-aware datetime."""
    dt = dateparser.parse(
        value,
        settings={
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": str(tz) if hasattr(tz, "key") else "UTC",
            "TO_TIMEZONE": str(tz) if hasattr(tz, "key") else "UTC",
        },
    )
    if dt is None:
        raise ValueError(f"unparseable timestamp: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def _build_note(row: dict, colmap: ColumnMap) -> tuple[str, str | None]:
    """Return (note, title). If colmap.note is set, use it verbatim.
    Otherwise compose from title + subtitle.
    """
    title = parse_value(row.get(colmap.title)) if colmap.title else ""
    subtitle = parse_value(row.get(colmap.subtitle)) if colmap.subtitle else ""
    note_col = parse_value(row.get(colmap.note)) if colmap.note else ""
    if note_col:
        return note_col, title or None
    if subtitle and title:
        return f"{subtitle} – {title}", title
    return title, title or None


def parse_csv(
    csv_path: Path,
    *,
    column_map: ColumnMap | None = None,
    tz: tzinfo = timezone.utc,
    source_id_prefix: str = "fulcra-csv.v1",
    default_tag: str | None = None,
    sentinel_duration_seconds: int = 1,
) -> Iterator[GenericEvent]:
    """Yield GenericEvent per CSV row.

    column_map: how CSV columns map to logical fields. Defaults to literal
    column names ('timestamp', 'title', ...) — caller usually overrides.

    tz: timezone for naive timestamps (dateparser hint). Has no effect on
    timestamps that already carry tzinfo.

    source_id_prefix: deterministic id prefix. e.g. "com.fulcra.csv.v1".

    default_tag: fallback tag when colmap.tag is None or the row's tag cell
    is empty.

    sentinel_duration_seconds: end_time = start_time + this many seconds
    when neither end_time nor duration columns are present. Fulcra silently
    drops zero-duration events, so the default is 1.
    """
    colmap = column_map or ColumnMap()
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if colmap.timestamp not in (reader.fieldnames or []):
            raise ValueError(
                f"timestamp column {colmap.timestamp!r} not found in CSV header "
                f"{reader.fieldnames!r}"
            )
        for row in reader:
            ts_raw = parse_value(row.get(colmap.timestamp))
            if not ts_raw:
                continue
            start = _parse_ts(ts_raw, tz=tz)

            if colmap.end_time and parse_value(row.get(colmap.end_time)):
                end = _parse_ts(parse_value(row[colmap.end_time]), tz=tz)
            elif colmap.duration_seconds and parse_value(row.get(colmap.duration_seconds)):
                end = start + timedelta(seconds=int(float(row[colmap.duration_seconds])))
            else:
                end = start + timedelta(seconds=sentinel_duration_seconds)

            note, title = _build_note(row, colmap)
            if not note:
                continue

            tag = parse_value(row.get(colmap.tag)) if colmap.tag else ""
            tag = tag or default_tag

            # Always hash with the timestamp — `source_id` columns typically
            # hold per-content ids (e.g. a Spotify track id), not per-row ids,
            # so two plays of the same content at different times must still
            # produce distinct source_ids.
            explicit_id = (
                parse_value(row.get(colmap.source_id)) if colmap.source_id else ""
            )
            source_id = derive_source_id(
                source_id_prefix, ts_raw, note, tag, explicit_id,
            )

            extras: dict[str, Any] = {}
            for col, key in colmap.extras:
                val = parse_value(row.get(col))
                if val:
                    extras[key] = val

            yield GenericEvent(
                start_time=start,
                end_time=end,
                note=note,
                title=title,
                source_id=source_id,
                tag=tag,
                external_ids=extras,
            )
