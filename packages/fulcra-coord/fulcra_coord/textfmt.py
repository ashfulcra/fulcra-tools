"""Human-readable relative-time formatters — the "how long" surface.

Leaf module (stdlib only) so every read surface that renders a timestamp for the
operator — needs-me / resume / status / agents / presence / the digest — shares one
implementation and can import it without reaching back into cli (no import cycle).
Each formatter is best-effort: an unparseable/empty value renders a sentinel
("?" / "soon" / "") rather than crashing a read-only view.
"""

from __future__ import annotations

from datetime import datetime, timezone


def age_str(updated_at: str) -> str:
    """Human-legible age of a timestamp, e.g. "3h" / "2d" / "12m" / "just now".

    Used by needs-me / resume to show "how long it's been" — the third thing the
    human wants at a glance (who, what, how long). Best-effort: an unparseable
    timestamp renders "?" rather than crashing the read-only view."""
    try:
        dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "?"
    # BUG 6a: a tz-less stored timestamp parses NAIVE, and subtracting it from the
    # AWARE now raised TypeError (not caught above) — crashing a read-only view.
    # Coerce a naive parse to UTC, matching views._parse_dt, so any stored shape
    # yields a sane age instead of a crash.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def until_str(when: str) -> str:
    """Time-until-actionable, e.g. "in 4d" / "in 18h" / "now". Best-effort:
    an unparseable/empty value renders "soon" so the upcoming line never breaks
    on a bad not_before."""
    try:
        dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "soon"
    secs = (dt - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return "now"
    if secs < 3600:
        return f"in {int(secs // 60)}m"
    if secs < 86400:
        return f"in {int(secs // 3600)}h"
    return f"in {int(secs // 86400)}d"


def due_str(due: str) -> str:
    """A compact calendar date for a deadline, e.g. "Jun 8". Empty/unparseable
    -> "" so the caller can drop the "(due ...)" clause entirely."""
    if not due:
        return ""
    try:
        dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    return f"{dt:%b} {dt.day}"
