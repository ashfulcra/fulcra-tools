"""Select which Day One entries to import."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from .entry import DayOneEntry


def select(
    entries: Iterable[DayOneEntry],
    *,
    tags: frozenset[str] = frozenset(),
    journals: frozenset[str] = frozenset(),
    since: datetime | None = None,
    until: datetime | None = None,
    starred_only: bool = False,
) -> list[DayOneEntry]:
    """Return the entries matching ALL active filters. A filter is
    inactive (matches everything) when its set is empty / its value is
    None / the flag is False."""
    out: list[DayOneEntry] = []
    for e in entries:
        if tags and not (set(e.tags) & tags):
            continue
        if journals and e.journal not in journals:
            continue
        if since is not None and e.creation_date < since:
            continue
        if until is not None and e.creation_date > until:
            continue
        if starred_only and not e.starred:
            continue
        out.append(e)
    return out
