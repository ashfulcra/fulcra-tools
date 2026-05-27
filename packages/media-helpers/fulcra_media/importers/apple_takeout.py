"""Apple Data & Privacy takeout — Apple TV playback importer.

Apple ships TV-watch data in two related CSVs inside the Apple Media
Services bundle:

  Apple_Media_Services/Stores Activity/Other Activity/Video Play Activity.csv
    The RICH per-watch event log — ~126 columns, multiple rows per session
    (PLAY_START, PLAY_END, PAUSE, etc.). UTC Start Time / UTC End Time /
    Play Duration give true playback windows. Preferred source.

  Apple_Media_Services/Stores Activity/Play Position Information/Playback Activity.csv
    A 6-column SPARSE current-state summary — one row per item, no
    historical events. Last activity timestamp is the only datetime. Used
    as a fallback when the rich file is absent (older takeouts and some
    region exports don't include it).

This module is intentionally tolerant of column drift. Apple has changed
this takeout's column set more than once; we name only the fields we use
and let extras pass through. The strict-header assertion that older
versions of this importer carried (``_EXPECTED_COLS == reader.fieldnames``)
is gone — that's exactly the kind of brittleness that broke the importer
in late-2025 when Apple added new fields.

Input shapes accepted (importer tries each):
  - Plain CSV file path → parse directly
  - Directory → recursively look for ``Video Play Activity.csv`` first,
    then ``Playback Activity.csv``
  - ``.zip`` file → search every member for either CSV
  - Nested zip case: outer takeout zips often contain an inner
    ``Apple_Media_Services.zip``. We open inner zips automatically so the
    user can hand us the file they downloaded verbatim.
"""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from fulcra_common.cross_source_fingerprint import (
    watched_movie_fingerprint,
    watched_tv_fingerprint,
)

from .base import NormalizedEvent, content_fingerprint

# Columns we *require* on the rich Video Play Activity CSV. Extras are
# ignored. Names match the live 2026 Apple takeout header.
_REQUIRED_VIDEO_COLS: tuple[str, ...] = (
    "Event Type",
    "Content Title",
    "UTC Start Time",
    "UTC End Time",
    "Play Duration",
)

# Columns we *require* on the sparse Playback Activity CSV.
_REQUIRED_PLAYBACK_COLS: tuple[str, ...] = (
    "Item Description",
    "Last activity timestamp",
    "Playback position",
)

# Apple's Video Play Activity Event Type taxonomy has changed multiple
# times. As of the 2026 takeout shape, the values are camelCase strings
# `playActivity` / `seekActivity` / etc. Older exports used SCREAMING_SNAKE
# (PLAY / PLAY_END / SKIP_FORWARD). We accept both vintages by lower-casing
# the value and matching against the accepted-set; pauses, scrubs, and
# seek-rows are dropped because they inflate the ingest by an order of
# magnitude without adding "I watched this" signal.
#
# Verified against a real 2026 takeout (2026-05-26): 31,297 'playActivity'
# rows + 18,704 'seekActivity' rows in the first 50k. The seek rows are
# excluded; only playActivity becomes an annotation.
_VIDEO_PLAY_EVENT_TYPES_LOWER: frozenset[str] = frozenset({
    # 2026+ camelCase shape
    "playactivity",
    # Pre-2025 SCREAMING_SNAKE shape (kept for legacy exports)
    "play", "play_end",
})

# Below this many MILLISECONDS we treat a row as a scrub/click rather than
# a real watch. The `Play Duration` column is in milliseconds in the 2026
# takeout shape (sample: 1,564,011 ms ≈ 26 min for a real Yellowjackets
# episode). Older exports used seconds in `Play Duration (Seconds)` —
# handled by the legacy parser below.
_VIDEO_MIN_DURATION_MS = 30_000


def _det_id(start: str, title: str, ep_title: str, device_model: str) -> str:
    h = hashlib.sha256(f"{start}|{title}|{ep_title}|{device_model}".encode()).hexdigest()
    return f"com.fulcra.media.apple-takeout.v1.{h[:16]}"


def _det_id_playback(timestamp: str, item_desc: str) -> str:
    h = hashlib.sha256(f"playback|{timestamp}|{item_desc}".encode()).hexdigest()
    return f"com.fulcra.media.apple-takeout.v1.{h[:16]}"


def _parse_dt(value: str) -> datetime:
    """Parse Apple's takeout timestamps.

    Two formats observed in the wild:
      - ``YYYY-MM-DD HH:MM:SS`` (assumed UTC) — the common case
      - ISO-8601 with trailing ``Z`` — newer exports
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
    if not s:
        return None
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return None


def _float_or_none(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_video_play_activity_csv(
    csv_path: Path, *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[NormalizedEvent]:
    """Parse the rich Video Play Activity CSV.

    `since` is an optional tz-aware UTC datetime cutoff — rows with
    ``UTC Start Time`` strictly older than ``since`` are skipped.
    `until` is the symmetric upper bound — rows with start at or after
    ``until`` are skipped. Pass None for either to mean "no bound" (the
    importer doesn't apply defaults; the plugin layer does).
    """
    with csv_path.open(newline="", encoding="utf-8") as f:
        yield from _iter_video_rows(csv.DictReader(f), since=since, until=until)


def parse_video_play_activity_lines(
    lines: Iterator[str], *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[NormalizedEvent]:
    """Parse the rich Video Play Activity CSV from an iterable of text lines.

    Used internally when streaming a CSV out of a zip member without
    materialising the whole file on disk first.
    """
    yield from _iter_video_rows(csv.DictReader(lines), since=since, until=until)


def _iter_video_rows(
    reader: csv.DictReader, *,
    since: datetime | None,
    until: datetime | None,
) -> Iterator[NormalizedEvent]:
    _check_required_cols(reader.fieldnames, _REQUIRED_VIDEO_COLS)
    for row in reader:
        # 2026 takeouts use camelCase ("playActivity"); pre-2025 used
        # SCREAMING_SNAKE ("PLAY" / "PLAY_END"). Lower-case to compare
        # against both vintages in one set. See _VIDEO_PLAY_EVENT_TYPES_LOWER.
        event_type = (row.get("Event Type") or "").strip().lower()
        if event_type not in _VIDEO_PLAY_EVENT_TYPES_LOWER:
            continue
        start_raw = (row.get("UTC Start Time") or "").strip()
        end_raw = (row.get("UTC End Time") or "").strip()
        if not start_raw or not end_raw:
            continue
        try:
            start = _parse_dt(start_raw)
            end = _parse_dt(end_raw)
        except ValueError:
            continue
        if since is not None and start < since:
            continue
        if until is not None and start >= until:
            continue
        # `Play Duration` is in milliseconds in the 2026 takeout shape
        # (verified against a real takeout). Compare against the ms
        # threshold; rows shorter than the threshold are scrubs/clicks.
        duration_ms = _float_or_none(row.get("Play Duration"))
        if duration_ms is not None and duration_ms < _VIDEO_MIN_DURATION_MS:
            continue

        title_field = (row.get("Content Title") or "").strip()
        ep_name = (row.get("Content Episode Name") or "").strip()
        season_name = (row.get("Content Season Name") or "").strip()
        episode_str = (row.get("Episode Number") or "").strip()
        device_model = (row.get("Hardware Model") or "").strip()
        store_front = (row.get("Store Front Name") or "").strip()
        subscription = (row.get("Subscription Channel") or "").strip()

        # Detect TV vs movie. The rich CSV does not have a single
        # "Content Type" column we can trust, but a non-empty episode
        # name or episode number is a reliable TV signal.
        season_num = _int_or_none(
            "".join(ch for ch in season_name if ch.isdigit())
        )
        episode_num = _int_or_none(episode_str)
        cross: str | None
        if (ep_name or episode_num) and title_field:
            if season_num is not None and episode_num is not None:
                note = f"{title_field} S{season_num:02d}E{episode_num:02d} – {ep_name}".strip()
                fp = content_fingerprint("tv", show=title_field,
                                         season=season_num, episode=episode_num)
                cross = watched_tv_fingerprint(
                    timestamp=start, show=title_field,
                    season=season_num, episode=episode_num,
                )
            else:
                # TV without numeric season/episode — fall back to title-only
                # fingerprint so we still produce something stable. No cross-
                # source fingerprint (we'd need S/E to dedup against Trakt).
                note = f"{title_field} – {ep_name}".strip(" –")
                fp = content_fingerprint("movie", title=title_field)
                cross = None
            title = title_field
        else:
            note = title_field
            title = title_field
            fp = content_fingerprint("movie", title=title_field)
            cross = watched_movie_fingerprint(timestamp=start, title=title_field)

        yield NormalizedEvent(
            importer="apple-takeout",
            service="apple-tv",
            category="watched",
            note=note,
            title=title,
            start_time=start,
            end_time=end,
            deterministic_id=_det_id(start_raw, title_field, ep_name, device_model),
            timestamp_confidence="high",
            external_ids={
                "device_model": device_model,
                "store_front": store_front,
                "subscription_channel": subscription,
                "content_fingerprint": fp,
            },
            extra_source_ids=(cross,) if cross else (),
        )


def parse_playback_activity_csv(
    csv_path: Path, *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[NormalizedEvent]:
    """Parse the sparse Playback Activity CSV (fallback).

    One event per row at ``Last activity timestamp``. Duration is taken
    from ``Playback position`` (seconds — observed in the live takeout;
    if Apple ever switches to ms we'll see absurd end_times and need to
    revisit). ``timestamp_confidence`` is downgraded to "low" because
    these timestamps reflect the most recent interaction with an item,
    not necessarily a watch session.
    """
    with csv_path.open(newline="", encoding="utf-8") as f:
        yield from _iter_playback_rows(csv.DictReader(f), since=since, until=until)


def parse_playback_activity_lines(
    lines: Iterator[str], *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[NormalizedEvent]:
    yield from _iter_playback_rows(csv.DictReader(lines), since=since, until=until)


def _iter_playback_rows(
    reader: csv.DictReader, *,
    since: datetime | None,
    until: datetime | None,
) -> Iterator[NormalizedEvent]:
    _check_required_cols(reader.fieldnames, _REQUIRED_PLAYBACK_COLS)
    for row in reader:
        ts_raw = (row.get("Last activity timestamp") or "").strip()
        if not ts_raw:
            continue
        try:
            ts = _parse_dt(ts_raw)
        except ValueError:
            continue
        if since is not None and ts < since:
            continue
        if until is not None and ts >= until:
            continue
        item_desc = (row.get("Item Description") or "").strip()
        if not item_desc:
            continue
        position = _float_or_none(row.get("Playback position")) or 0.0
        from datetime import timedelta as _td
        end = ts + _td(seconds=max(position, 1.0))
        fp = content_fingerprint("movie", title=item_desc)
        # Playback Activity timestamps are "last interaction" — confidence
        # low, but still bucket-bucket within the 5-min window if another
        # source happens to align.
        cross = watched_movie_fingerprint(timestamp=ts, title=item_desc)
        yield NormalizedEvent(
            importer="apple-takeout",
            service="apple-tv",
            category="watched",
            note=item_desc,
            title=item_desc,
            start_time=ts,
            end_time=end,
            deterministic_id=_det_id_playback(ts_raw, item_desc),
            timestamp_confidence="low",
            external_ids={
                "content_fingerprint": fp,
                "playback_position_seconds": position,
                "source_file": "Playback Activity.csv",
            },
            extra_source_ids=(cross,) if cross else (),
        )


# ---------------------------------------------------------------------------
# Backwards-compat shim
#
# The old `parse_playback_csv(path)` accepted the 12-column 2024-era
# Playback Activity.csv and produced PLAY events. The shape is gone from
# real takeouts, but the function name is referenced from
# collect_plugins.py and the legacy test fixture. The shim now routes any
# file it's given through the auto-detect entry point — same return type,
# new behaviour.
# ---------------------------------------------------------------------------

def parse_playback_csv(
    csv_path: Path, *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[NormalizedEvent]:
    """Deprecated: prefer ``parse_any`` / the importer entry point.

    Kept so external callers (and the legacy in-tree fixture) keep working.
    Auto-detects by header: if the file looks like Video Play Activity we
    parse it as such; otherwise we try Playback Activity.
    """
    yield from parse_any_csv(csv_path, since=since, until=until)


def parse_any_csv(
    csv_path: Path, *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[NormalizedEvent]:
    """Auto-detect which Apple takeout CSV this is and dispatch."""
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        names = set(reader.fieldnames or ())
        if "UTC Start Time" in names and "Content Title" in names:
            yield from _iter_video_rows(reader, since=since, until=until)
        elif "Last activity timestamp" in names and "Item Description" in names:
            yield from _iter_playback_rows(reader, since=since, until=until)
        elif "Start Time" in names and "Title" in names:
            # Old (pre-2025) 12-column Playback Activity.csv shape. Some
            # users still have stale exports around; parse them as
            # best-effort PLAY events.
            yield from _iter_legacy_playback_rows(reader, since=since, until=until)
        else:
            raise ValueError(
                f"unrecognised Apple takeout CSV header {sorted(names)!r}; "
                "expected Video Play Activity or Playback Activity columns"
            )


def _iter_legacy_playback_rows(
    reader: csv.DictReader, *,
    since: datetime | None,
    until: datetime | None,
) -> Iterator[NormalizedEvent]:
    """Parser for the pre-2025 12-column Playback Activity.csv shape.

    Kept for users with old exports. Filters Event Type == PLAY and emits
    one event per row using Start Time / End Time directly.
    """
    for row in reader:
        if (row.get("Event Type") or "").strip().upper() != "PLAY":
            continue
        start_raw = (row.get("Start Time") or "").strip()
        end_raw = (row.get("End Time") or "").strip()
        if not start_raw or not end_raw:
            continue
        try:
            start = _parse_dt(start_raw)
            end = _parse_dt(end_raw)
        except ValueError:
            continue
        if since is not None and start < since:
            continue
        if until is not None and start >= until:
            continue
        content_type = (row.get("Content Type") or "").strip()
        title_field = (row.get("Title") or "").strip()
        ep_title = (row.get("Episode Title") or "").strip()
        season_str = (row.get("Season Number") or "").strip()
        episode_str = (row.get("Episode Number") or "").strip()
        device_type = (row.get("Device Type") or "").strip()
        device_model = (row.get("Device Model") or "").strip()
        country = (row.get("Country") or "").strip()
        cross: str | None
        if content_type == "TV Episode" and season_str and episode_str:
            season = int(season_str)
            episode = int(episode_str)
            note = f"{title_field} S{season:02d}E{episode:02d} – {ep_title}"
            title = title_field
            fp = content_fingerprint("tv", show=title_field, season=season, episode=episode)
            cross = watched_tv_fingerprint(
                timestamp=start, show=title_field, season=season, episode=episode,
            )
        else:
            note = title_field
            title = title_field
            fp = content_fingerprint("movie", title=title_field)
            cross = watched_movie_fingerprint(timestamp=start, title=title_field)
        yield NormalizedEvent(
            importer="apple-takeout",
            service="apple-tv",
            category="watched",
            note=note,
            title=title,
            start_time=start,
            end_time=end,
            deterministic_id=_det_id(start_raw, title_field, ep_title, device_model),
            timestamp_confidence="high",
            external_ids={
                "device_type": device_type,
                "device_model": device_model,
                "country": country,
                "content_fingerprint": fp,
            },
            extra_source_ids=(cross,) if cross else (),
        )


# ---------------------------------------------------------------------------
# Path-shape dispatch (CSV / directory / zip / nested zip)
# ---------------------------------------------------------------------------

_VIDEO_BASENAME = "Video Play Activity.csv"
_PLAYBACK_BASENAME = "Playback Activity.csv"


def parse_any(
    path: Path, *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[NormalizedEvent]:
    """Entry point for the collect plugin: take whatever path the user
    handed us and produce NormalizedEvents.

    Resolution order:
      1. If `path` is a CSV → parse_any_csv
      2. If `path` is a directory → look for Video Play Activity.csv first,
         then Playback Activity.csv (recursive)
      3. If `path` is a zip → scan its members; recurse into any
         ``*Apple_Media_Services*.zip`` it contains
    """
    if path.is_file():
        if path.suffix.lower() == ".zip":
            yield from _parse_zip(path, since=since, until=until)
            return
        yield from parse_any_csv(path, since=since, until=until)
        return
    if path.is_dir():
        yield from _parse_dir(path, since=since, until=until)
        return
    raise FileNotFoundError(f"apple-takeout: path does not exist: {path}")


def _parse_dir(
    directory: Path, *,
    since: datetime | None,
    until: datetime | None,
) -> Iterator[NormalizedEvent]:
    video = sorted(directory.rglob(_VIDEO_BASENAME))
    if video:
        yield from parse_video_play_activity_csv(video[0], since=since, until=until)
        return
    playback = sorted(directory.rglob(_PLAYBACK_BASENAME))
    if playback:
        yield from parse_playback_activity_csv(playback[0], since=since, until=until)
        return
    # Also look for a nested Apple_Media_Services zip inside the directory.
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
        f"apple-takeout: no '{_VIDEO_BASENAME}' or '{_PLAYBACK_BASENAME}' "
        f"found under {directory}"
    )


def _parse_zip(
    zip_path: Path, *,
    since: datetime | None,
    until: datetime | None,
) -> Iterator[NormalizedEvent]:
    """Search a zip (and any inner Apple_Media_Services zip) for either
    of our target CSVs."""
    with zipfile.ZipFile(zip_path) as zf:
        # First pass: direct CSV members. Prefer the rich Video CSV.
        video_members = [n for n in zf.namelist()
                         if n.endswith("/" + _VIDEO_BASENAME) or n == _VIDEO_BASENAME]
        if video_members:
            with zf.open(video_members[0]) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
                yield from parse_video_play_activity_lines(
                    text, since=since, until=until,
                )
                return
        playback_members = [n for n in zf.namelist()
                            if n.endswith("/" + _PLAYBACK_BASENAME) or n == _PLAYBACK_BASENAME]
        if playback_members:
            with zf.open(playback_members[0]) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
                yield from parse_playback_activity_lines(
                    text, since=since, until=until,
                )
                return
        # Second pass: nested zip (the common Apple Takeout shape — outer
        # archive contains Apple_Media_Services.zip alongside other
        # service bundles).
        nested = [n for n in zf.namelist()
                  if n.endswith(".zip") and "Apple_Media_Services" in n]
        if not nested:
            # Last-ditch: try any inner zip.
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
            f"apple-takeout: no '{_VIDEO_BASENAME}' or '{_PLAYBACK_BASENAME}' "
            f"found inside {zip_path}"
        )


def _yield_from_open_zip(
    zf: zipfile.ZipFile, *,
    since: datetime | None,
    until: datetime | None,
) -> Iterator[NormalizedEvent]:
    """Yield events from an already-opened ZipFile (used for nested zips)."""
    video_members = [n for n in zf.namelist()
                     if n.endswith("/" + _VIDEO_BASENAME) or n == _VIDEO_BASENAME]
    if video_members:
        with zf.open(video_members[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            yield from parse_video_play_activity_lines(
                text, since=since, until=until,
            )
            return
    playback_members = [n for n in zf.namelist()
                        if n.endswith("/" + _PLAYBACK_BASENAME) or n == _PLAYBACK_BASENAME]
    if playback_members:
        with zf.open(playback_members[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            yield from parse_playback_activity_lines(
                text, since=since, until=until,
            )
            return
    raise ValueError(
        "apple-takeout: no target CSV in nested zip"
    )
