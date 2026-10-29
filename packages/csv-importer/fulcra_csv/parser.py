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

from .events import (
    DURATION,
    INSTANT,
    VALID_TYPES,
    ColumnMap,
    GenericEvent,
    coerce_value,
    derive_source_id,
)


def parse_value(v: Any) -> str:
    """Coerce a CSV value to clean str. Strips whitespace; preserves empty."""
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
    annotation_type: str = DURATION,
    strict: bool = False,
    required_columns: tuple[str, ...] = (),
) -> Iterator[GenericEvent]:
    """Yield GenericEvent per CSV row.

    column_map: how CSV columns map to logical fields. Defaults to literal
        column names ('timestamp', 'title', ...) — caller usually overrides.
    tz: timezone for naive timestamps (dateparser hint).
    source_id_prefix: deterministic id prefix.
    default_tag: fallback tag when colmap.tag is None or the row's tag cell
        is empty.
    sentinel_duration_seconds: end_time = start_time + this many seconds
        when neither end_time nor duration columns are present. Fulcra
        silently drops zero-duration events, so the default is 1. Ignored
        when annotation_type='instant'.
    annotation_type: 'duration' (default) or 'instant'. Instant events
        carry no end_time in GenericEvent (the v1 codec emits equal times).
    strict: opt-in whole-import diagnostics; default parser skips stay unchanged.
    required_columns: explicitly requested optional columns that strict mode must find.
    """
    if annotation_type not in VALID_TYPES:
        raise ValueError(f"annotation_type must be one of {VALID_TYPES}")
    colmap = column_map or ColumnMap()
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, strict=strict)
        if colmap.timestamp not in (reader.fieldnames or []):
            raise ValueError(
                f"timestamp column {colmap.timestamp!r} not found in CSV header "
                f"{reader.fieldnames!r}"
            )
        if strict:
            headers = reader.fieldnames or []
            if len(headers) != len(set(headers)):
                raise ValueError("CSV header contains duplicate columns")
            requested = [
                colmap.timestamp,
                colmap.end_time,
                colmap.duration_seconds,
                colmap.note,
                colmap.subtitle,
                colmap.source_id,
                colmap.tag,
                colmap.value,
            ]
            # Default title remains optional for value-only consumers; callers
            # can separately identify explicitly requested columns.
            requested += list(required_columns)
            requested += [col for col, _ in colmap.data_fields + colmap.extras]
            if any(col and col not in headers for col in requested):
                raise ValueError("CSV header is missing an explicitly mapped column")
            for pairs in (colmap.data_fields, colmap.extras):
                keys = [key for _, key in pairs]
                if len(keys) != len(set(keys)) or any(not col or not key for col, key in pairs):
                    raise ValueError("CSV mapping destinations must be unique and nonempty")
        for row in _rows(reader, strict=strict):
            if strict and (None in row or any(value is None for value in row.values())):
                raise ValueError(f"CSV row {reader.line_num}: malformed column count")
            try:
                ts_raw = parse_value(row.get(colmap.timestamp))
                if not ts_raw:
                    if strict:
                        raise ValueError(f"CSV row {reader.line_num}: timestamp is blank")
                    continue
                start = _parse_ts(ts_raw, tz=tz)

                end: datetime | None
                if annotation_type == INSTANT:
                    end = None
                elif colmap.end_time and parse_value(row.get(colmap.end_time)):
                    end = _parse_ts(parse_value(row[colmap.end_time]), tz=tz)
                elif colmap.duration_seconds and parse_value(row.get(colmap.duration_seconds)):
                    end = start + timedelta(seconds=int(float(row[colmap.duration_seconds])))
                else:
                    end = start + timedelta(seconds=sentinel_duration_seconds)

                note, title = _build_note(row, colmap)
                value: Any = None
                if colmap.value:
                    value = coerce_value(parse_value(row.get(colmap.value)), colmap.value_type)

                # An event needs at least SOMETHING — note, title, or value.
                if not note and value is None:
                    if strict:
                        raise ValueError(f"CSV row {reader.line_num}: note/title/value are empty")
                    continue
                if not note and value is not None:
                    # Synthesize a note from the value when no text is given.
                    note = str(value)

                tag = parse_value(row.get(colmap.tag)) if colmap.tag else ""
                tag = tag or default_tag

                explicit_id = parse_value(row.get(colmap.source_id)) if colmap.source_id else ""
                source_id = derive_source_id(
                    source_id_prefix,
                    ts_raw,
                    note,
                    tag,
                    explicit_id,
                )

                data_fields: dict[str, Any] = {}
                for col, key in colmap.data_fields:
                    val = parse_value(row.get(col))
                    if val:
                        data_fields[key] = val

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
                    value=value,
                    annotation_type=annotation_type,
                    data_fields=data_fields,
                    external_ids=extras,
                )
            except (ValueError, TypeError, OverflowError) as exc:
                if strict:
                    raise ValueError(f"CSV row {reader.line_num}: invalid row ({exc})") from None
                raise


def _rows(reader, *, strict):
    while True:
        try:
            yield next(reader)
        except StopIteration:
            return
        except csv.Error:
            if strict:
                raise ValueError(f"CSV row {reader.line_num}: malformed CSV syntax") from None
            raise
