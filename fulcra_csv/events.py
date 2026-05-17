"""Shared types: GenericEvent and ColumnMap."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ColumnMap:
    """Tells the parser which CSV column holds which logical field.

    All field values are case-sensitive column headers in the CSV. Set a
    field to None if the column is absent. The timestamp + at least one
    of (note, title) are the only required fields; everything else is
    optional metadata.

    extras: extra column->key mapping to surface in external_ids.
    """
    timestamp: str = "timestamp"
    end_time: str | None = None  # if absent, end_time = start_time + 1s sentinel
    duration_seconds: str | None = None  # alternative to end_time
    title: str | None = "title"
    note: str | None = None  # if None, derived from title (+ subtitle)
    subtitle: str | None = None  # e.g. artist for music, show for podcasts
    source_id: str | None = None  # if None, derived from a hash of the row
    tag: str | None = None  # service/source tag (e.g. 'spotify')
    extras: tuple[tuple[str, str], ...] = ()  # ((col, key), ...) extras to lift into external_ids


@dataclass
class GenericEvent:
    """A single row from a CSV, normalized for annotation ingest.

    Note: this is service-agnostic. The fulcra-media wrapper adds
    Watched/Listened category + service tags on top.
    """
    start_time: datetime
    end_time: datetime
    note: str
    title: str | None
    source_id: str
    tag: str | None = None
    external_ids: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.start_time.tzinfo is None or self.end_time.tzinfo is None:
            raise ValueError("start_time and end_time must be timezone-aware")


_SLUG_KEEP_RE = re.compile(r"[^a-z0-9\- ]+")


def slugify(value: str) -> str:
    """Lowercase, strip non-alphanumeric (except spaces and hyphens), collapse to hyphens."""
    s = _SLUG_KEEP_RE.sub("", (value or "").lower())
    parts = [p for p in re.split(r"[\s-]+", s) if p]
    return "-".join(parts)


def derive_source_id(prefix: str, *fields: Any) -> str:
    """Build a stable source id from arbitrary fields.

    Caller decides which fields are the natural dedup key (e.g. timestamp +
    title + tag). All non-None fields stringify via str(); None fields are
    rendered as the literal empty string so they don't shift the hash.
    """
    payload = "|".join("" if f is None else str(f) for f in fields)
    h = hashlib.sha256(payload.encode()).hexdigest()
    return f"{prefix}.{h[:16]}"
