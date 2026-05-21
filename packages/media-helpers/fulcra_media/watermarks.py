"""Per-importer watermark management on top of state.watermarks.

Watermark semantics depend on the importer's natural cursor:
  - API-poll importers (lastfm, trakt, ...) store an ISO 8601 timestamp via
    set_iso/get_iso (the latest item's start_time after a successful import).
  - Snapshot importers (apple-podcasts, ...) store {sha256, path, mtime} as
    JSON via set_snapshot/get_snapshot (detect when the underlying file
    actually changed to avoid no-op work).
  - One-shot importers (GDPR exports) don't use watermarks at all.

Strings are stored in state.watermarks (dict[str, str]); typed accessors live
here so each importer doesn't reinvent the parsing.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .state import State


def get(state: State, importer: str) -> str | None:
    """Raw string watermark, or None if unset."""
    return state.watermarks.get(importer)


def set_(state: State, importer: str, value: str) -> None:
    """Set the raw string watermark. Caller-provided format."""
    state.watermarks[importer] = value


def clear(state: State, importer: str) -> None:
    """Drop the watermark for `importer`."""
    state.watermarks.pop(importer, None)


def get_iso(state: State, importer: str) -> datetime | None:
    """Parse the stored watermark as ISO 8601, returning a tz-aware datetime.

    Returns None on any parse failure (treat as "no watermark") so a corrupted
    state.json doesn't crash future runs.
    """
    raw = state.watermarks.get(importer)
    if not raw:
        return None
    try:
        # Accept Z suffix as UTC (Python's fromisoformat dislikes it pre-3.11)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def set_iso(state: State, importer: str, value: datetime) -> None:
    """Store a tz-aware datetime as ISO 8601."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    state.watermarks[importer] = value.isoformat()


def get_snapshot(state: State, importer: str) -> dict[str, Any] | None:
    """Read the snapshot watermark dict, or None on absent/invalid JSON."""
    raw = state.watermarks.get(importer)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def set_snapshot(state: State, importer: str, snapshot: dict[str, Any]) -> None:
    """Store a snapshot watermark dict as JSON string."""
    state.watermarks[importer] = json.dumps(snapshot, sort_keys=True)
