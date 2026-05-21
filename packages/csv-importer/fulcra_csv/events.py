"""Shared types: GenericEvent and ColumnMap."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Allowed values for annotation_type. Maps to Fulcra's recorded_at shape:
# duration -> {start_time, end_time}; instant -> {start_time} only.
DURATION = "duration"
INSTANT = "instant"
VALID_TYPES = {DURATION, INSTANT}


@dataclass(frozen=True)
class ColumnMap:
    """Tells the parser which CSV column holds which logical field.

    All field values are case-sensitive column headers in the CSV. Set a
    field to None if the column is absent.

    The timestamp + one of (note, title, value) are the only hard
    requirements; everything else is optional metadata.

    extras: ((col, key), ...) — lift CSV column into external_ids[key]
    data_fields: ((col, key), ...) — lift CSV column into the top-level
        annotation data payload (alongside note, title, value)
    """
    timestamp: str = "timestamp"
    end_time: str | None = None  # required for duration unless duration_seconds is set
    duration_seconds: str | None = None
    title: str | None = "title"
    note: str | None = None  # if None and title is set, built from title (+ subtitle)
    subtitle: str | None = None  # joined with title for note (artist, show, ...)
    source_id: str | None = None
    tag: str | None = None
    value: str | None = None  # numeric / scalar measurement value
    value_type: str = "float"  # 'float' | 'int' | 'str' | 'bool'
    data_fields: tuple[tuple[str, str], ...] = ()
    extras: tuple[tuple[str, str], ...] = ()


def coerce_value(raw: str, value_type: str) -> Any:
    """Coerce a CSV cell to the requested type. Empty input returns None."""
    raw = (raw or "").strip()
    if raw == "":
        return None
    if value_type == "float":
        return float(raw)
    if value_type == "int":
        return int(float(raw))  # tolerate "180.0"
    if value_type == "bool":
        return raw.lower() in ("1", "true", "yes", "y", "t")
    if value_type == "str":
        return raw
    raise ValueError(f"unknown value_type: {value_type!r}")


@dataclass
class GenericEvent:
    """A single row from a CSV, normalized for annotation ingest.

    `end_time` is None for instant annotations; required for duration.
    `value` is the measurement value (None for non-measurement events).
    `data_fields` are extra fields written into the annotation data payload
    (i.e. data.<key> = <val>) — distinct from external_ids which are nested
    under data.external_ids.
    """
    start_time: datetime
    note: str
    title: str | None
    source_id: str
    end_time: datetime | None = None
    tag: str | None = None
    value: Any = None
    annotation_type: str = DURATION  # "duration" | "instant"
    data_fields: dict[str, Any] = field(default_factory=dict)
    external_ids: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.start_time.tzinfo is None:
            raise ValueError("start_time must be timezone-aware")
        if self.end_time is not None and self.end_time.tzinfo is None:
            raise ValueError("end_time must be timezone-aware")
        if self.annotation_type not in VALID_TYPES:
            raise ValueError(
                f"annotation_type must be one of {VALID_TYPES}, got {self.annotation_type!r}"
            )
        if self.annotation_type == DURATION and self.end_time is None:
            raise ValueError("duration events require end_time")


_SLUG_KEEP_RE = re.compile(r"[^a-z0-9\- ]+")


def slugify(value: str) -> str:
    """Lowercase, strip non-alphanumeric (except spaces and hyphens), collapse to hyphens."""
    s = _SLUG_KEEP_RE.sub("", (value or "").lower())
    parts = [p for p in re.split(r"[\s-]+", s) if p]
    return "-".join(parts)


def derive_source_id(prefix: str, *fields: Any) -> str:
    """Build a stable source id from arbitrary fields.

    Caller decides which fields are the natural dedup key (e.g. timestamp +
    title + tag + explicit_id). All non-None fields stringify via str(); None
    fields are rendered as the literal empty string so they don't shift the hash.
    """
    payload = "|".join("" if f is None else str(f) for f in fields)
    h = hashlib.sha256(payload.encode()).hexdigest()
    return f"{prefix}.{h[:16]}"
