"""Pure functions for humanizing durations and timestamps."""
from __future__ import annotations

import re


def humanize_minutes(minutes: int) -> str:
    """Convert a minute count to a human-readable string.

    Examples:
        0 → '0 minutes'
        1 → '1 minute'
        30 → '30 minutes'
        60 → '1 hour'
        360 → '6 hours'
        90 → '1h 30m'
        1440 → '1 day'
        2880 → '2 days'
    """
    if minutes == 0:
        return "0 minutes"
    if minutes == 1:
        return "1 minute"
    if minutes < 60:
        return f"{minutes} minutes"
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} day" if days == 1 else f"{days} days"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{minutes // 60}h {minutes % 60}m"


# Pattern matches one number followed by an optional unit token.
# Recognised units: h / hr / hrs / hour / hours, m / min / mins / minute /
# minutes, s / sec / secs / second / seconds. Whitespace between number
# and unit is optional; multiple tokens can chain (e.g. "1h 30m").
_DURATION_TOKEN_RE = re.compile(
    r"""
    \s*
    (\d+(?:\.\d+)?)        # the magnitude
    \s*
    (h(?:ours?|rs?)?
     |m(?:in(?:ute)?s?)?
     |s(?:ec(?:ond)?s?)?
    )?                     # optional unit
    """,
    re.IGNORECASE | re.VERBOSE,
)

_UNIT_TO_SECONDS = {
    "h": 3600.0,
    "m": 60.0,
    "s": 1.0,
}


def parse_duration_seconds(text: str) -> float | None:
    """Parse a free-text duration like "90m", "1h 30m", "45 min", "2h",
    "30s" into total seconds. Returns None for empty input or
    unparseable garbage.

    Used by the menubar's quick-record popover when the user types a
    duration into the inline field on a Duration-type annotation row.
    Tolerant of mixed casing, internal whitespace, and trailing
    pluralisations ("hours", "mins", "seconds") because users will
    type whatever feels natural.

    A bare integer with no unit is treated as MINUTES — that's the
    common shorthand ("90" means 90 minutes). A bare decimal with no
    unit is rejected as ambiguous.
    """
    if text is None:
        return None
    s = text.strip()
    if not s:
        return None

    total = 0.0
    pos = 0
    matched_any = False
    # Allow a leading bare integer (interpreted as minutes) ONLY when
    # the whole string is a single integer.
    if s.isdigit():
        return float(int(s)) * 60.0

    while pos < len(s):
        m = _DURATION_TOKEN_RE.match(s, pos)
        if not m or m.end() == pos:
            return None
        magnitude_str, unit_token = m.group(1), m.group(2)
        if unit_token is None:
            # An unaccompanied number in a chained expression is invalid
            # — "1 30" is garbage, "1h 30" is also garbage. The user
            # must say "1h 30m" explicitly.
            return None
        try:
            magnitude = float(magnitude_str)
        except ValueError:
            return None
        unit_key = unit_token[0].lower()
        if unit_key not in _UNIT_TO_SECONDS:
            return None
        total += magnitude * _UNIT_TO_SECONDS[unit_key]
        matched_any = True
        pos = m.end()
        # Skip trailing whitespace between tokens
        while pos < len(s) and s[pos].isspace():
            pos += 1

    if not matched_any or pos != len(s):
        return None
    if total <= 0:
        return None
    return total
