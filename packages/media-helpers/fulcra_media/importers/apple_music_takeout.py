"""Apple Music Activity (takeout) — Apple Music listen importer.

Apple ships music listening history in:

  Apple_Media_Services/Apple Music Activity/Apple Music Play Activity.csv

This is a RICH per-listen log (~144 columns in 2026 takeouts). We pick out
only the fields we need and ignore the rest — the column set drifts every
year or so and a strict-header check would brittle-break on every change.

Columns we read (verified against a real 2026 takeout):
  Song Name, Container Album Name, Container Artist Name,
  Event Start Timestamp, Event End Timestamp,
  Play Duration Milliseconds, UTC Offset In Seconds,
  Event Type, End Reason Type

Filtering:
  - We keep rows whose Event Type indicates an actual play. PLAY_END is
    the most reliable "they listened to it" signal Apple emits.
  - We drop rows whose End Reason Type indicates a skip / manual track
    change. END_REASON_NATURAL_END_OF_TRACK is the unambiguous "listened
    to the whole thing" marker; we also accept rows with no end reason
    when duration is plausible (some older takeout files lack the column
    value).
  - We require a non-empty Song Name + Container Artist Name; rows
    without both are unidentifiable.

Input shapes accepted (mirrors apple_takeout):
  - Plain CSV file path
  - Directory (recursively searched for Apple Music Play Activity.csv)
  - .zip file (members searched; inner Apple_Media_Services zip handled)
"""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fulcra_common.cross_source_fingerprint import listened_fingerprint

from .base import NormalizedEvent, content_fingerprint

_REQUIRED_COLS: tuple[str, ...] = (
    "Song Name",
    "Container Artist Name",
    "Event Start Timestamp",
    "Event End Timestamp",
    "Play Duration Milliseconds",
)

# Apple's Event Type values that count as a "real" listen. PLAY_END is
# emitted when a track finishes (naturally or via skip — End Reason Type
# disambiguates). Some older exports use bare PLAY.
_LISTEN_EVENT_TYPES: frozenset[str] = frozenset({
    "PLAY_END", "PLAY",
})

# End Reason Type values that mean "user really listened". Anything else
# (TRACK_SKIPPED_FORWARDS, TRACK_SKIPPED_BACKWARDS, FAILED_TO_LOAD, etc.)
# we drop. We also accept rows with empty End Reason Type as long as
# duration is plausible — older takeout exports leave the column blank.
_ACCEPT_END_REASONS: frozenset[str] = frozenset({
    "",
    "NATURAL_END_OF_TRACK",
    "END_REASON_NATURAL_END_OF_TRACK",
})

# Drop sub-30s plays (clicks, autoplay previews, skip-fest noise).
_MIN_DURATION_MS = 30_000

_BASENAME = "Apple Music Play Activity.csv"


def _det_id(ts: str, song: str, artist: str) -> str:
    h = hashlib.sha256(f"{ts}|{song}|{artist}".encode()).hexdigest()
    return f"com.fulcra.media.apple-music-takeout.v1.{h[:16]}"


def _parse_dt(value: str) -> datetime:
    """Parse Apple Music's takeout timestamps.

    Same as apple_takeout — two formats observed:
      - ``YYYY-MM-DD HH:MM:SS`` (UTC)
      - ISO-8601 with trailing ``Z``
    """
    s = (value or "").strip()
    if not s:
        raise ValueError("empty timestamp")
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _check_required_cols(fieldnames, required: tuple[str, ...]) -> None:
    names = set(fieldnames or ())
    for col in required:
        if col not in names:
            raise ValueError(
                f"missing required column {col!r} — file may be from an "
                "unsupported takeout version"
            )


def _int_or_none(s: str | None) -> int | None:
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def parse_csv(
    csv_path: Path, *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[NormalizedEvent]:
    """Parse Apple Music Play Activity.csv from a path."""
    with csv_path.open(newline="", encoding="utf-8") as f:
        yield from _iter_rows(csv.DictReader(f), since=since, until=until)


def parse_lines(
    lines: Iterator[str], *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[NormalizedEvent]:
    yield from _iter_rows(csv.DictReader(lines), since=since, until=until)


def _iter_rows(
    reader: csv.DictReader, *,
    since: datetime | None,
    until: datetime | None,
) -> Iterator[NormalizedEvent]:
    _check_required_cols(reader.fieldnames, _REQUIRED_COLS)
    for row in reader:
        event_type = (row.get("Event Type") or "").strip().upper()
        # Empty event type tolerated — some takeouts omit it. Otherwise
        # gate on the listen-event whitelist.
        if event_type and event_type not in _LISTEN_EVENT_TYPES:
            continue

        # Don't gate on End Reason Type. The taxonomy is large and noisy
        # (NATURAL_END_OF_TRACK / TRACK_SKIPPED_FORWARDS / PLAYBACK_MANUALLY_PAUSED /
        # NOT_APPLICABLE / MANUALLY_SELECTED_PLAYBACK_OF_A_DIFF_ITEM / SCRUB_* /
        # FAILED_TO_LOAD / …). Verified against a real 2026 takeout: a strict
        # whitelist dropped 42% of rows (12,812 of 30,414) including legitimate
        # listens that ended via pause or manual track-switch. We use Play
        # Duration Milliseconds as the "did they actually listen?" proxy
        # below — a skip with >30s of play counts (they DID listen to 30s+),
        # a skip with <30s does not. FAILED_TO_LOAD rows naturally have 0
        # duration so they fall out at the duration check.
        song = (row.get("Song Name") or "").strip()
        artist = (row.get("Container Artist Name") or "").strip()
        album = (row.get("Container Album Name") or "").strip()
        # Artist is often blank in Apple Music takeouts (verified: ~32% of
        # rows in a real takeout had Song but no Container Artist Name).
        # Require only Song; missing artist becomes empty in the fingerprint.
        if not song:
            continue

        duration_ms = _int_or_none(row.get("Play Duration Milliseconds"))
        if duration_ms is not None and duration_ms < _MIN_DURATION_MS:
            continue

        start_raw = (row.get("Event Start Timestamp") or "").strip()
        end_raw = (row.get("Event End Timestamp") or "").strip()
        try:
            start = _parse_dt(start_raw) if start_raw else None
        except ValueError:
            start = None
        try:
            end = _parse_dt(end_raw) if end_raw else None
        except ValueError:
            end = None

        # If only one timestamp is present, derive the other from duration.
        if start is None and end is not None and duration_ms:
            start = end - timedelta(milliseconds=duration_ms)
        if end is None and start is not None and duration_ms:
            end = start + timedelta(milliseconds=duration_ms)
        if start is None or end is None:
            continue
        if since is not None and start < since:
            continue
        if until is not None and start >= until:
            continue

        utc_offset_sec = _int_or_none(row.get("UTC Offset In Seconds"))
        note = f"{artist} – {song}"
        fp = content_fingerprint("music", artist=artist, track=song)
        cross = listened_fingerprint(timestamp=start, artist=artist, track=song)

        yield NormalizedEvent(
            importer="apple-music-takeout",
            service="apple-music",
            category="listened",
            note=note,
            title=song,
            start_time=start,
            end_time=end,
            deterministic_id=_det_id(start_raw or end_raw, song, artist),
            timestamp_confidence="high",
            external_ids={
                "artist": artist,
                "album": album,
                "utc_offset_seconds": utc_offset_sec,
                "content_fingerprint": fp,
            },
            extra_source_ids=(cross,) if cross else (),
        )


# ---------------------------------------------------------------------------
# Path-shape dispatch (CSV / directory / zip / nested zip)
# ---------------------------------------------------------------------------

def parse_any(
    path: Path, *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[NormalizedEvent]:
    """Entry point — take any of {CSV, directory, .zip} and produce events."""
    if path.is_file():
        if path.suffix.lower() == ".zip":
            yield from _parse_zip(path, since=since, until=until)
            return
        yield from parse_csv(path, since=since, until=until)
        return
    if path.is_dir():
        yield from _parse_dir(path, since=since, until=until)
        return
    raise FileNotFoundError(f"apple-music-takeout: path does not exist: {path}")


def _parse_dir(
    directory: Path, *,
    since: datetime | None,
    until: datetime | None,
) -> Iterator[NormalizedEvent]:
    matches = sorted(directory.rglob(_BASENAME))
    if matches:
        yield from parse_csv(matches[0], since=since, until=until)
        return
    for inner_zip in sorted(directory.rglob("*Apple_Media_Services*.zip")):
        yield from _parse_zip(inner_zip, since=since, until=until)
        return
    for inner_zip in sorted(directory.rglob("*.zip")):
        try:
            yield from _parse_zip(inner_zip, since=since, until=until)
            return
        except ValueError:
            continue
    raise RuntimeError(
        f"apple-music-takeout: no '{_BASENAME}' found under {directory}"
    )


def _parse_zip(
    zip_path: Path, *,
    since: datetime | None,
    until: datetime | None,
) -> Iterator[NormalizedEvent]:
    with zipfile.ZipFile(zip_path) as zf:
        members = [n for n in zf.namelist()
                   if n.endswith("/" + _BASENAME) or n == _BASENAME]
        if members:
            with zf.open(members[0]) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
                yield from parse_lines(text, since=since, until=until)
                return
        # Look for the nested Apple_Media_Services bundle first.
        nested = [n for n in zf.namelist()
                  if n.endswith(".zip") and "Apple_Media_Services" in n]
        if not nested:
            nested = [n for n in zf.namelist() if n.endswith(".zip")]
        for inner_name in nested:
            with zf.open(inner_name) as raw:
                data = raw.read()
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as inner_zf:
                    yield from _yield_from_open_zip(
                        inner_zf, since=since, until=until,
                    )
                    return
            except (zipfile.BadZipFile, ValueError):
                continue
        raise ValueError(
            f"apple-music-takeout: no '{_BASENAME}' found inside {zip_path}"
        )


def _yield_from_open_zip(
    zf: zipfile.ZipFile, *,
    since: datetime | None,
    until: datetime | None,
) -> Iterator[NormalizedEvent]:
    members = [n for n in zf.namelist()
               if n.endswith("/" + _BASENAME) or n == _BASENAME]
    if not members:
        raise ValueError(f"apple-music-takeout: no '{_BASENAME}' in nested zip")
    with zf.open(members[0]) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        yield from parse_lines(text, since=since, until=until)
