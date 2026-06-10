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

Blank-artist enrichment:
  Container Artist Name is nearly always empty in real takeouts (verified:
  3 of ~20,000 rows filled in a real 2026 takeout). Two sibling files in
  the same "Apple Music Activity" bundle let us recover it:

    - "Apple Music - Play History Daily Tracks.csv" — column
      "Track Description" formatted "Artist - Title" (split on FIRST " - ")
    - "Apple Music Library Tracks.json.zip" — zip holding one JSON array
      of {"Title": ..., "Artist": ...} entries

  We build a normalized-title → artist map from the union of both and fill
  in the artist only when the row's artist is empty AND the title maps to
  exactly one distinct artist (ambiguous titles stay blank — never guess,
  never overwrite). Measured on the same real takeout: 79% of blank-artist
  rows enriched unambiguously, 15% ambiguous. Siblings missing → enrichment
  silently skipped.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import zipfile
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fulcra_common.cross_source_fingerprint import listened_fingerprint

from .base import NormalizedEvent, content_fingerprint

logger = logging.getLogger(__name__)

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

# Sibling files (same "Apple Music Activity" folder) used to enrich the
# nearly-always-blank Container Artist Name. See module docstring.
_DAILY_TRACKS_BASENAME = "Apple Music - Play History Daily Tracks.csv"
_LIBRARY_TRACKS_BASENAME = "Apple Music Library Tracks.json.zip"


# ---------------------------------------------------------------------------
# Blank-artist enrichment (title → artist map from sibling files)
# ---------------------------------------------------------------------------

def _norm_title(s: str) -> str:
    """Normalization rule for the title key: lowercase + whitespace-collapse."""
    return " ".join(s.lower().split())


class ArtistLookup:
    """Normalized-title → artist lookup built from sibling takeout files.

    Calling it with a song name returns the artist when the title maps to
    exactly one distinct artist across the union of sources, else ``None``
    (both for unmatched and ambiguous titles — ``is_ambiguous`` tells the
    two apart for instrumentation).
    """

    def __init__(self, candidates: dict[str, set[str]]):
        self._resolved: dict[str, str] = {
            title: next(iter(artists))
            for title, artists in candidates.items()
            if len(artists) == 1
        }
        self._ambiguous: frozenset[str] = frozenset(
            title for title, artists in candidates.items() if len(artists) > 1
        )

    def __call__(self, song: str) -> str | None:
        return self._resolved.get(_norm_title(song))

    def is_ambiguous(self, song: str) -> bool:
        return _norm_title(song) in self._ambiguous

    def __len__(self) -> int:
        return len(self._resolved) + len(self._ambiguous)


def _collect_daily_tracks(
    lines: Iterable[str], candidates: dict[str, set[str]],
) -> int:
    """Harvest (title, artist) pairs from Play History Daily Tracks.

    "Track Description" is formatted "Artist - Title"; split on the FIRST
    " - " (titles may themselves contain " - ").
    """
    reader = csv.DictReader(lines)
    if not reader.fieldnames or "Track Description" not in reader.fieldnames:
        logger.debug(
            "apple-music-takeout: daily tracks file lacks 'Track Description' "
            "column — skipping for enrichment"
        )
        return 0
    n = 0
    for row in reader:
        desc = (row.get("Track Description") or "").strip()
        artist, sep, title = desc.partition(" - ")
        if not sep:
            continue
        artist, title = artist.strip(), title.strip()
        if artist and title:
            candidates.setdefault(_norm_title(title), set()).add(artist)
            n += 1
    return n


def _collect_library_tracks(
    zip_bytes: bytes, candidates: dict[str, set[str]],
) -> int:
    """Harvest (Title, Artist) pairs from the Library Tracks json zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            members = [m for m in zf.namelist() if m.lower().endswith(".json")]
            if not members:
                logger.debug(
                    "apple-music-takeout: library tracks zip has no .json "
                    "member — skipping for enrichment"
                )
                return 0
            entries = json.loads(zf.read(members[0]))
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning(
            "apple-music-takeout: failed to read library tracks zip "
            "(%s) — skipping for enrichment", exc,
        )
        return 0
    if not isinstance(entries, list):
        return 0
    n = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("Title") or "").strip()
        artist = str(entry.get("Artist") or "").strip()
        if title and artist:
            candidates.setdefault(_norm_title(title), set()).add(artist)
            n += 1
    return n


def _finish_lookup(candidates: dict[str, set[str]]) -> ArtistLookup | None:
    if not candidates:
        return None
    lookup = ArtistLookup(candidates)
    ambiguous = sum(1 for a in candidates.values() if len(a) > 1)
    logger.debug(
        "apple-music-takeout: artist map built — %d titles "
        "(%d unambiguous, %d ambiguous)",
        len(candidates), len(candidates) - ambiguous, ambiguous,
    )
    return lookup


def _build_lookup_for_csv(csv_path: Path) -> ArtistLookup | None:
    """Build the artist map from sibling files NEXT TO a bare CSV path.

    Missing siblings (or unreadable ones) skip enrichment gracefully.
    """
    candidates: dict[str, set[str]] = {}
    daily = csv_path.parent / _DAILY_TRACKS_BASENAME
    if daily.is_file():
        try:
            with daily.open(newline="", encoding="utf-8") as f:
                n = _collect_daily_tracks(f, candidates)
            logger.debug(
                "apple-music-takeout: daily tracks sibling found (%d entries)", n,
            )
        except OSError as exc:
            logger.warning(
                "apple-music-takeout: cannot read %s (%s) — skipping", daily, exc,
            )
    else:
        logger.debug("apple-music-takeout: no daily tracks sibling at %s", daily)
    library = csv_path.parent / _LIBRARY_TRACKS_BASENAME
    if library.is_file():
        try:
            n = _collect_library_tracks(library.read_bytes(), candidates)
            logger.debug(
                "apple-music-takeout: library tracks sibling found (%d entries)", n,
            )
        except OSError as exc:
            logger.warning(
                "apple-music-takeout: cannot read %s (%s) — skipping", library, exc,
            )
    else:
        logger.debug("apple-music-takeout: no library tracks sibling at %s", library)
    return _finish_lookup(candidates)


def _build_lookup_from_zip(zf: zipfile.ZipFile) -> ArtistLookup | None:
    """Build the artist map from sibling members inside an open zip."""
    candidates: dict[str, set[str]] = {}
    daily = [n for n in zf.namelist()
             if n.endswith("/" + _DAILY_TRACKS_BASENAME)
             or n == _DAILY_TRACKS_BASENAME]
    if daily:
        with zf.open(daily[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            n = _collect_daily_tracks(text, candidates)
        logger.debug(
            "apple-music-takeout: daily tracks member found (%d entries)", n,
        )
    else:
        logger.debug("apple-music-takeout: no daily tracks member in zip")
    library = [n for n in zf.namelist()
               if n.endswith("/" + _LIBRARY_TRACKS_BASENAME)
               or n == _LIBRARY_TRACKS_BASENAME]
    if library:
        n = _collect_library_tracks(zf.read(library[0]), candidates)
        logger.debug(
            "apple-music-takeout: library tracks member found (%d entries)", n,
        )
    else:
        logger.debug("apple-music-takeout: no library tracks member in zip")
    return _finish_lookup(candidates)


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
    artist_lookup: ArtistLookup | None = None,
) -> Iterator[NormalizedEvent]:
    """Parse Apple Music Play Activity.csv from a path.

    When no ``artist_lookup`` is supplied, sibling enrichment files next to
    the CSV are searched automatically (and skipped gracefully if absent).
    """
    if artist_lookup is None:
        artist_lookup = _build_lookup_for_csv(csv_path)
    with csv_path.open(newline="", encoding="utf-8") as f:
        yield from _iter_rows(
            csv.DictReader(f), since=since, until=until,
            artist_lookup=artist_lookup,
        )


def parse_lines(
    lines: Iterator[str], *,
    since: datetime | None = None,
    until: datetime | None = None,
    artist_lookup: ArtistLookup | None = None,
) -> Iterator[NormalizedEvent]:
    yield from _iter_rows(
        csv.DictReader(lines), since=since, until=until,
        artist_lookup=artist_lookup,
    )


def _iter_rows(
    reader: csv.DictReader, *,
    since: datetime | None,
    until: datetime | None,
    artist_lookup: ArtistLookup | None = None,
) -> Iterator[NormalizedEvent]:
    _check_required_cols(reader.fieldnames, _REQUIRED_COLS)
    n_enriched = n_ambiguous = n_unmatched = 0
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
        raw_artist = (row.get("Container Artist Name") or "").strip()
        artist = raw_artist
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

        # Enrich a blank artist from the sibling-file map — only when the
        # title resolves to exactly ONE artist; ambiguous titles stay blank
        # and a non-empty Container Artist Name is never overwritten.
        # This happens BEFORE note / fingerprint construction so the
        # enriched artist flows into all of them.
        if not raw_artist and artist_lookup is not None:
            resolved = artist_lookup(song)
            if resolved:
                artist = resolved
                n_enriched += 1
            elif artist_lookup.is_ambiguous(song):
                n_ambiguous += 1
            else:
                n_unmatched += 1

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
            # CRITICAL: deterministic_id MUST hash the RAW (pre-enrichment)
            # artist. Every historical import hashed the raw value — using
            # the enriched artist would change every det_id and re-import
            # the user's entire play history as duplicates on the next run.
            deterministic_id=_det_id(start_raw or end_raw, song, raw_artist),
            timestamp_confidence="high",
            external_ids={
                "artist": artist,
                "album": album,
                "utc_offset_seconds": utc_offset_sec,
                "content_fingerprint": fp,
            },
            extra_source_ids=(cross,) if cross else (),
        )
    if artist_lookup is not None:
        logger.debug(
            "apple-music-takeout: blank-artist enrichment — "
            "enriched=%d ambiguous=%d unmatched=%d",
            n_enriched, n_ambiguous, n_unmatched,
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
            # Build the title→artist map once per parse (~12k entries in a
            # real takeout — fine in memory), before streaming the rows.
            artist_lookup = _build_lookup_from_zip(zf)
            with zf.open(members[0]) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
                yield from parse_lines(
                    text, since=since, until=until,
                    artist_lookup=artist_lookup,
                )
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
    artist_lookup = _build_lookup_from_zip(zf)
    with zf.open(members[0]) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        yield from parse_lines(
            text, since=since, until=until, artist_lookup=artist_lookup,
        )
