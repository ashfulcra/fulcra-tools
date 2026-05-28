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

from fulcra_common.cross_source_fingerprint import (
    watched_movie_fingerprint,
    watched_tv_fingerprint,
)

from .base import NormalizedEvent, _slugify, content_fingerprint


_NETFLIX_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2})$")
_SEASON_NUM_RE = re.compile(r"Season\s+(\d+)")
_EPISODE_NUM_RE = re.compile(r"Episode\s+(\d+)")
_YEAR_ONLY_RE = re.compile(r"^\d{4}$")


def _fingerprint_from_joined_title(raw_title: str, *, is_episode: bool | None = None) -> str:
    """Cross-source fingerprint from a Netflix-style joined title.

    Patterns recognized:
      "Movie Name"                      -> movie:movie-name
      "Show: Season 1: Episode 5"       -> tv:show:s01e05
      "BEEF: Season 2: Episode Title"   -> tv:beef:s02:episode-title
      "Anthology: 2026: Episode Title"  -> tv:anthology:s2026:episode-title
      "Show: Limited Series: Episode 1" -> tv:show:limited-series:e01
      "Show: Some Season: Ep Title"     -> tv:show:some-season:ep-title
      "Show: Episode Title"             -> tv:show:episode-title (2-part TV)
      "Dune: Part Two"                  -> movie:dune-part-two   (2-part movie, when is_episode=False)

    `is_episode`: caller's signal about whether this is a TV episode. When
    None (e.g. the slim importer, which has no extra context), we infer:
    3+ colon segments → TV; 2 segments without Season/Episode markers
    defaults to TV (most Netflix joined titles are episodic).
    """
    parts = [p.strip() for p in raw_title.split(":")]
    show = parts[0]
    season_match = _SEASON_NUM_RE.search(raw_title)
    episode_match = _EPISODE_NUM_RE.search(raw_title)

    # Most-specific signal first.
    if season_match and episode_match:
        return content_fingerprint(
            "tv",
            show=show,
            season=int(season_match.group(1)),
            episode=int(episode_match.group(1)),
        )

    if is_episode is False or len(parts) == 1:
        return content_fingerprint("movie", title=raw_title)

    rest = parts[1:]
    if len(parts) >= 3:
        season_part = rest[0]
        if season_match and season_match.group(0) in season_part:
            season_seg = f"s{int(season_match.group(1)):02d}"
        elif _YEAR_ONLY_RE.match(season_part):
            season_seg = f"s{season_part}"
        else:
            season_seg = _slugify(season_part)

        episode_remainder = ": ".join(rest[1:])
        if episode_match and episode_match.group(0) in episode_remainder:
            episode_seg = f"e{int(episode_match.group(1)):02d}"
        else:
            episode_seg = _slugify(episode_remainder)

        return f"tv:{_slugify(show)}:{season_seg}:{episode_seg}"

    # 2 parts: default TV unless is_episode explicitly False (handled above).
    return f"tv:{_slugify(show)}:{_slugify(rest[0])}"


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


def _det_id(date_str: str, raw_title: str, occurrence: int) -> str:
    h = hashlib.sha256(f"{date_str}|{raw_title}|{occurrence}".encode()).hexdigest()
    # v2: point-in-time at noon UTC, zero duration (vs v1's fake 21:00 UTC + estimated durations).
    # Versioning the prefix so Fulcra's implicit ingest-time dedup doesn't reject the v2
    # events as duplicates of the v1 events — Fulcra silently drops POSTs whose source IDs
    # match existing records, even when the originating annotation def is soft-deleted.
    return f"com.fulcra.media.netflix.v2.{h[:16]}"


def parse_slim(csv_path: Path) -> Iterator[NormalizedEvent]:
    """Parse a Netflix slim CSV (Title, Date) into NormalizedEvents.

    The slim variant has no time or duration data. We emit one point-in-time
    event per row at 12:00 UTC on the date — start_time == end_time. The
    timestamp_confidence is 'low' and external_ids carries both
    `time_estimated: true` and `point_in_time: true`. Idempotency key
    incorporates an occurrence index so same-day rewatches produce distinct
    events.
    """
    occurrence_counter: Counter[tuple[str, str]] = Counter()

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ["Title", "Date"]:
            raise ValueError(
                f"unexpected Netflix CSV header {reader.fieldnames!r}; "
                "parse_slim handles the 2-column variant only — use parse_rich for the GDPR export"
            )
        for row in reader:
            raw_title = row["Title"]
            date_str = row["Date"]
            d = parse_netflix_date(date_str)
            key = (date_str, raw_title)
            idx = occurrence_counter[key]
            occurrence_counter[key] += 1

            note, title = make_note_and_title(raw_title)
            instant = datetime.combine(d, time(12, 0, 0), tzinfo=timezone.utc)
            # Fulcra silently drops DurationAnnotation events with
            # start_time == end_time; use a 1-second duration so the event
            # actually indexes. Still effectively a point at noon UTC.
            end_instant = instant + timedelta(seconds=1)

            fp = _fingerprint_from_joined_title(raw_title)

            yield NormalizedEvent(
                importer="netflix-slim",
                service="netflix",
                category="watched",
                note=note,
                title=title,
                start_time=instant,
                end_time=end_instant,
                deterministic_id=_det_id(date_str, raw_title, idx),
                timestamp_confidence="low",
                external_ids={
                    "time_estimated": True,
                    "point_in_time": True,
                    "occurrence_index": idx,
                    "raw_date": date_str,
                    "content_fingerprint": fp,
                },
            )


_RICH_EXPECTED_COLS = [
    "Profile Name", "Start Time", "Duration", "Attributes", "Title",
    "Supplemental Video Type", "Device Type", "Bookmark", "Latest Bookmark", "Country",
]


_EPISODE_MARKERS = ("Season ", "Episode ", "Limited Series", "Chapter ", "Volume ")


def _extract_title_rich(raw_title: str) -> tuple[str, str]:
    """For rich-variant titles, distinguish movies from episodes.

    Returns (note, title). Movies (no episode-shape marker in the string)
    keep their full title intact even if they have colon subtitles
    (e.g. "Dune: Part Two"). Episodes (with Season/Episode/Limited Series
    markers) get title set to the show name (first colon-separated segment).
    """
    if ":" not in raw_title:
        return raw_title, raw_title
    if not any(marker in raw_title for marker in _EPISODE_MARKERS):
        # Movie with colon subtitle
        return raw_title, raw_title
    # Episode — first colon-separated part is the show
    parts = [p.strip() for p in raw_title.split(":")]
    return raw_title, parts[0]


def _det_id_rich(profile: str, start_time_str: str, raw_title: str) -> str:
    h = hashlib.sha256(f"{profile}|{start_time_str}|{raw_title}".encode()).hexdigest()
    return f"com.fulcra.media.netflix-rich.{h[:16]}"


def _parse_hmmss(value: str) -> timedelta:
    """Parse H:MM:SS into a timedelta."""
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError(f"not a H:MM:SS duration: {value!r}")
    h, m, s = (int(p) for p in parts)
    return timedelta(hours=h, minutes=m, seconds=s)


def parse_rich(csv_path: Path) -> Iterator[NormalizedEvent]:
    """Parse a Netflix rich (GDPR) CSV into NormalizedEvents.

    The rich variant has 10 columns including UTC Start Time, Duration in
    H:MM:SS, Profile Name, Device Type, and Country. Rows with non-empty
    Supplemental Video Type (TRAILER, HOOK, PROMOTIONAL, etc.) are dropped.
    """
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != _RICH_EXPECTED_COLS:
            raise ValueError(
                f"unexpected Netflix CSV header {reader.fieldnames!r}; "
                f"parse_rich handles the 10-column GDPR variant only — use parse_slim "
                f"for the in-app 2-column download"
            )
        for row in reader:
            if (row.get("Supplemental Video Type") or "").strip():
                continue
            raw_title = row["Title"]
            start_str = row["Start Time"]
            start = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            duration = _parse_hmmss(row["Duration"])
            end = start + duration

            note, title = _extract_title_rich(raw_title)
            profile = (row.get("Profile Name") or "").strip()
            # _extract_title_rich already distinguishes episodes (it splits
            # on colons when episode markers are present) from movies (which
            # keep their full title). Pass that signal through.
            is_episode = note != title or any(marker in raw_title for marker in _EPISODE_MARKERS)
            fp = _fingerprint_from_joined_title(raw_title, is_episode=is_episode)

            # Cross-source fingerprint: movies dedup on (time, title);
            # TV needs (time, show, S, E) so we only emit when both numeric
            # season and episode are extractable from the joined title.
            cross: str | None
            if is_episode:
                season_match = _SEASON_NUM_RE.search(raw_title)
                episode_match = _EPISODE_NUM_RE.search(raw_title)
                if season_match and episode_match:
                    cross = watched_tv_fingerprint(
                        timestamp=start, show=title,
                        season=int(season_match.group(1)),
                        episode=int(episode_match.group(1)),
                    )
                else:
                    cross = None
            else:
                cross = watched_movie_fingerprint(timestamp=start, title=title)

            yield NormalizedEvent(
                importer="netflix-rich",
                service="netflix",
                category="watched",
                note=note,
                title=title,
                start_time=start,
                end_time=end,
                deterministic_id=_det_id_rich(profile, start_str, raw_title),
                timestamp_confidence="high",
                external_ids={
                    "profile": profile,
                    "device_type": (row.get("Device Type") or "").strip(),
                    "country": (row.get("Country") or "").strip(),
                    "bookmark": (row.get("Bookmark") or "").strip(),
                    "content_fingerprint": fp,
                },
                extra_source_ids=(cross,) if cross else (),
            )


def parse_auto(csv_path: Path) -> Iterator[NormalizedEvent]:
    """Inspect CSV header and dispatch to parse_slim or parse_rich."""
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
    if header == ["Title", "Date"]:
        yield from parse_slim(csv_path)
    elif header == _RICH_EXPECTED_COLS:
        yield from parse_rich(csv_path)
    else:
        raise ValueError(
            f"unrecognized Netflix CSV header {header!r}; "
            "expected slim ['Title', 'Date'] or rich 10-column GDPR variant"
        )
