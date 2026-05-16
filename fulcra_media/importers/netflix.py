"""Netflix slim-CSV importer.

Slim variant (in-app per-profile download) has two columns: Title, Date.
Date format is M/D/YY (US, two-digit year). No time, no timezone, no duration,
no profile.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections import Counter
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .base import NormalizedEvent


_NETFLIX_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2})$")


def parse_netflix_date(value: str) -> date:
    """Parse Netflix's M/D/YY into a date. Two-digit years are 20YY."""
    m = _NETFLIX_DATE_RE.match(value or "")
    if not m:
        raise ValueError(f"not a Netflix slim date: {value!r}")
    month, day, year2 = (int(x) for x in m.groups())
    return date(2000 + year2, month, day)


def make_note_and_title(raw_title: str) -> tuple[str, str]:
    """Split Netflix's joined title into a display note + bare show title.

    Returns (note, title). For movies (no colon) note == title == raw_title.
    For shows, title is the first colon-separated part (show name), note keeps
    the full string in trimmed form. Handles malformed rows whose show name is
    blank (e.g. " : Episode 10") by returning an empty title.
    """
    parts = [p.strip() for p in raw_title.split(":")]
    # Re-join with consistent spacing; preserve a leading empty segment as ":"
    # so malformed " : Episode 10" rows surface as ": Episode 10".
    note = ": ".join(parts)
    if len(parts) == 1:
        return note, parts[0]
    return note, parts[0]


def estimate_duration(raw_title: str) -> timedelta:
    """Heuristic runtime estimate for slim-variant rows (no real duration).

    - No colon -> assume movie -> 100 min
    - Contains 'Season' or 'Episode' marker -> assume TV episode -> 30 min
    - Otherwise -> default 45 min
    """
    if ":" not in raw_title:
        return timedelta(minutes=100)
    lowered = raw_title.lower()
    if "season" in lowered or "episode" in lowered or "limited series" in lowered:
        return timedelta(minutes=30)
    return timedelta(minutes=45)


def _det_id(date_str: str, raw_title: str, occurrence: int) -> str:
    h = hashlib.sha256(f"{date_str}|{raw_title}|{occurrence}".encode()).hexdigest()
    return f"com.fulcra.media.netflix.{h[:16]}"


def parse_slim(csv_path: Path) -> Iterator[NormalizedEvent]:
    """Parse a Netflix slim CSV (Title, Date) into NormalizedEvents.

    Each row -> one event with synthetic 21:00 UTC start time and an estimated
    duration. Idempotency key incorporates an occurrence index so same-day
    rewatches produce distinct events.
    """
    occurrence_counter: Counter[tuple[str, str]] = Counter()

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ["Title", "Date"]:
            raise ValueError(
                f"unexpected Netflix CSV header {reader.fieldnames!r}; "
                "this importer handles the slim 2-column variant only"
            )
        for row in reader:
            raw_title = row["Title"]
            date_str = row["Date"]
            d = parse_netflix_date(date_str)
            key = (date_str, raw_title)
            idx = occurrence_counter[key]
            occurrence_counter[key] += 1

            note, title = make_note_and_title(raw_title)
            start = datetime.combine(d, time(21, 0, 0), tzinfo=timezone.utc)
            end = start + estimate_duration(raw_title)

            yield NormalizedEvent(
                importer="netflix-slim",
                service="netflix",
                category="watched",
                note=note,
                title=title,
                start_time=start,
                end_time=end,
                deterministic_id=_det_id(date_str, raw_title, idx),
                timestamp_confidence="low",
                external_ids={
                    "time_estimated": True,
                    "duration_estimated": True,
                    "occurrence_index": idx,
                    "raw_date": date_str,
                },
            )
