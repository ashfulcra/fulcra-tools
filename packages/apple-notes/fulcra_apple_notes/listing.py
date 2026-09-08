"""Parse `fulcra-api file list` output.

The listing renders times in 12-HOUR form with AM/PM ("06:58PM"), so
comparing those strings lexically silently inverts the midnight hour:
"12:05AM" sorts after "01:05AM" while actually being earlier. Any
freshness check built on string comparison is therefore wrong for one hour
of every day -- which is exactly the kind of bug that only shows up in
production, at night. Parse to a datetime; never compare the strings.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re

_ROW_RE = re.compile(
    r"^\s*(?P<size>[\d.]+\s*(?:B|KiB|MiB|GiB))\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<time>\d{2}:\d{2}(?:AM|PM))\s+UTC\s+"
    r"(?P<name>.+?)\s*$")


@dataclass(frozen=True)
class Entry:
    name: str
    size_text: str
    modified: datetime


def parse(output: str) -> list[Entry]:
    entries: list[Entry] = []
    for line in (output or "").splitlines():
        if line.rstrip().endswith("/"):
            continue  # a directory
        match = _ROW_RE.match(line)
        if not match:
            continue
        stamp = f"{match.group('date')} {match.group('time')}"
        try:
            # %I + %p, never %H: the source is 12-hour.
            when = datetime.strptime(stamp, "%Y-%m-%d %I:%M%p").replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
        entries.append(Entry(name=match.group("name"),
                             size_text=match.group("size"), modified=when))
    return entries
