"""Timestamp formatting — the bus's canonical wall-clock string convention.

Leaf module (depends only on the stdlib) so any layer — cli, retention, presence,
the future feature modules — can format a bus timestamp without importing cli (and
without an import cycle). Centralizes the "UTC, microsecond precision, trailing Z"
shape in one place: if that convention ever changes, it changes here once.
"""

from __future__ import annotations

from datetime import datetime, timezone


def iso_z(dt: datetime) -> str:
    """Format a datetime as the bus timestamp convention: UTC, microsecond
    precision, trailing ``Z`` (not ``+00:00``). The single source of truth for
    that convention — used by ``now_iso`` and by the marker/health writers that
    must stamp an INJECTED ``now`` (kept testable) rather than wall-clock."""
    return dt.astimezone(timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")


def now_iso() -> str:
    """The current instant in the bus timestamp convention (see ``iso_z``)."""
    return iso_z(datetime.now(timezone.utc))
