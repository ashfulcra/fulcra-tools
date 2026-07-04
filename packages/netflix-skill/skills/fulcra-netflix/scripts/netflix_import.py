#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""Import a Netflix viewing-history CSV into a Fulcra Watched annotation.

Self-contained (PEP 723): run with `uv run netflix_import.py <csv> --json`.
Parsing logic and deterministic source-id schemes are ported verbatim from
fulcra-media (packages/media-helpers/fulcra_media/importers/netflix.py) so
records dedup perfectly against fulcra-media imports of the same history.
Wire format mirrors fulcra-common wire.py; dedup is source-id based.
"""
from __future__ import annotations

import csv
import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

API_BASE = "https://api.fulcradynamics.com"
DEF_NAME = "Watched"
DEF_MARKER = "com.fulcradynamics.annotation.media.watched"
INGEST_VERSION = 2  # bump ONLY with a coordinated det-id prefix change


# Ported from packages/media-helpers/fulcra_media/importers/netflix.py — keep in sync
_NETFLIX_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2})$")


# Ported from packages/media-helpers/fulcra_media/importers/netflix.py — keep in sync
def parse_netflix_date(value: str) -> date:
    """Parse Netflix's M/D/YY into a date. Two-digit years are 20YY."""
    m = _NETFLIX_DATE_RE.match(value or "")
    if not m:
        raise ValueError(f"not a Netflix slim date: {value!r}")
    month, day, year2 = (int(x) for x in m.groups())
    return date(2000 + year2, month, day)


# Ported from packages/media-helpers/fulcra_media/importers/netflix.py — keep in sync
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


# Ported from packages/media-helpers/fulcra_media/importers/netflix.py — keep in sync
# (that file's `_det_id`, renamed here to `det_id_slim`; hash string and prefix
# are byte-identical so records dedup across tools.)
def det_id_slim(date_str: str, raw_title: str, occurrence: int) -> str:
    h = hashlib.sha256(f"{date_str}|{raw_title}|{occurrence}".encode()).hexdigest()
    # v2: point-in-time at noon UTC, zero duration (vs v1's fake 21:00 UTC + estimated durations).
    # Versioning the prefix so Fulcra's implicit ingest-time dedup doesn't reject the v2
    # events as duplicates of the v1 events — Fulcra silently drops POSTs whose source IDs
    # match existing records, even when the originating annotation def is soft-deleted.
    return f"com.fulcra.media.netflix.v2.{h[:16]}"


@dataclass
class Event:
    title: str
    note: str
    start: datetime
    end: datetime
    det_id: str
    fingerprint: str
    confidence: str            # "low" | "high"
    external: dict = field(default_factory=dict)


def fingerprint_from_joined_title(raw_title, *, is_episode=None):
    return ""   # TEMPORARY stub — Task 3 ports the real fingerprint logic


def parse_slim(csv_path: Path):
    occurrence: Counter = Counter()
    with Path(csv_path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ["Title", "Date"]:
            raise ValueError(
                f"not a Netflix slim CSV (header {reader.fieldnames!r}); "
                "expected exactly Title,Date"
            )
        for row in reader:
            raw_title, date_str = row["Title"], row["Date"]
            d = parse_netflix_date(date_str)
            idx = occurrence[(date_str, raw_title)]
            occurrence[(date_str, raw_title)] += 1
            note, title = make_note_and_title(raw_title)
            start = datetime.combine(d, time(12, 0), tzinfo=timezone.utc)
            yield Event(
                title=title, note=note,
                start=start,
                # Fulcra silently drops DurationAnnotation events with
                # start_time == end_time; use a 1-second duration so the
                # event actually indexes (see fulcra-media netflix.py ~line 153).
                end=start + timedelta(seconds=1),
                det_id=det_id_slim(date_str, raw_title, idx),
                fingerprint=fingerprint_from_joined_title(raw_title),
                confidence="low",
                external={
                    "time_estimated": True, "point_in_time": True,
                    "occurrence_index": idx, "raw_date": date_str,
                },
            )
