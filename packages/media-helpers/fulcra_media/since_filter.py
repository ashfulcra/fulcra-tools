"""Parse the ``since`` / ``until`` settings shared by takeout-style importers.

Takeout files (Apple Media Services, Apple Music Activity, etc.) can run to
hundreds of thousands of rows covering a decade. Without window knobs, every
plugin run would re-process every row, hammer Fulcra with duplicate-ingest
work, and (more importantly) frighten the user the first time they hand us a
multi-million-event archive.

This module is the one place that turns the user-facing ``since`` and
``until`` settings into tz-aware UTC ``datetime`` cutoffs the importers can
compare ``event.start_time`` against. Both ``apple_takeout`` and
``apple_music_takeout`` share it; any future takeout importer should reuse
it instead of re-rolling the format.

The ``until`` knob exists primarily to make Apple takeouts coexist with
realtime sources (Last.fm for music, Trakt for video, ...). The user sets
``until`` to the date their realtime source started and the takeout fills
only the historical gap, sidestepping the duplicate-write problem until
cross-source dedup ships.

Accepted formats (same for both ``since`` and ``until``):
  - ``"all"`` (or empty string / None) — no bound, returns ``None``
  - ``"<N>d"`` / ``"<N>w"`` / ``"<N>m"`` / ``"<N>y"`` — N days / weeks /
    months / years back from now. Months are treated as 30 days and years
    as 365 days; this is a coarse ingest cutoff, not a calendar boundary,
    so the dateutil dependency would be overkill.
  - ``"YYYY-MM-DD"`` — absolute cutoff (UTC start-of-day)

Semantics:
  - ``since`` filters out events strictly BEFORE the returned datetime.
  - ``until`` filters out events AT OR AFTER the returned datetime.

Returns a timezone-aware ``datetime`` (UTC) representing the cutoff, or
``None`` for "no bound". Raises ``ValueError`` on unparseable input —
callers should surface that to the user via ``ctx.progress(ok=False, ...)``
rather than swallowing it (otherwise the user sees a silent no-op import).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


_RELATIVE = re.compile(r"^\s*(\d+)\s*([dwmy])\s*$", re.IGNORECASE)


def parse_window(spec: str | None, *, now: datetime | None = None) -> datetime | None:
    """Resolve a ``since``/``until`` spec string to a UTC datetime cutoff.

    Args:
        spec: The raw setting value (typically from
            ``ctx.config.get("since")`` or ``ctx.config.get("until")``).
        now: Override "current time" for testing. Defaults to
            ``datetime.now(UTC)``.

    Returns:
        A tz-aware UTC ``datetime`` cutoff, or ``None`` to mean "no bound".

    Raises:
        ValueError: If ``spec`` is non-empty and doesn't match any accepted
        format.
    """
    if spec is None:
        return None
    s = spec.strip()
    if not s or s.lower() == "all":
        return None
    now = now or datetime.now(timezone.utc)
    m = _RELATIVE.match(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit == "d":
            return now - timedelta(days=n)
        if unit == "w":
            return now - timedelta(weeks=n)
        if unit == "m":
            # approx months as 30 days — calendar-accurate isn't worth a
            # dateutil dep for an "ingest cutoff" knob.
            return now - timedelta(days=30 * n)
        if unit == "y":
            return now - timedelta(days=365 * n)
    # absolute YYYY-MM-DD
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(
            f"Unrecognised window spec {spec!r}; use 'all', '<N>d/w/m/y', "
            "or 'YYYY-MM-DD'."
        ) from exc


# Backwards-compat alias. ``parse_since`` predated the addition of ``until``
# and is still imported by `collect_plugins.py` and the existing test suite.
# Same semantics — the spec format is symmetric across since/until — so the
# alias is a straight rename, not a wrapper.
parse_since = parse_window
