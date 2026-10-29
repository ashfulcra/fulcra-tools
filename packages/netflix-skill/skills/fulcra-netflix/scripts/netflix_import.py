#!/usr/bin/env python3
"""Parse Netflix exports locally or import native Watched Events.

Upload requires the installed adapter and matching Collect runtime. Check-only
uses only the standard library. Source identities retain the historical parser
algorithms; native primary UUIDs also include the destination.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path


# --- constants ---

INGEST_VERSION = 2  # bump ONLY with a coordinated det-id prefix change
CHUNK_SIZE = 500  # only the in-flight chunk is durable; tests may shrink this
ADAPTER_CONTRACT_VERSION = 1


# --- ported parsing helpers ---

# Ported from packages/media-helpers/fulcra_media/importers/netflix.py — keep in sync
_NETFLIX_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2})$")
_SEASON_NUM_RE = re.compile(r"Season\s+(\d+)")
_EPISODE_NUM_RE = re.compile(r"Episode\s+(\d+)")
_YEAR_ONLY_RE = re.compile(r"^\d{4}$")


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
# are byte-identical; native primary UUIDs also include the destination.)
def det_id_slim(date_str: str, raw_title: str, occurrence: int) -> str:
    h = hashlib.sha256(f"{date_str}|{raw_title}|{occurrence}".encode()).hexdigest()
    # Preserve the historical v2 source prefix and raw hash inputs. Its event
    # uses noon UTC and a one-second sentinel. Native primary UUIDs also bind
    # the destination; source compatibility alone is no cross-tool no-op proof.
    return f"com.fulcra.media.netflix.v2.{h[:16]}"


@dataclass
class Event:
    """One parsed viewing session, variant-agnostic.

    `det_id` retains the deterministic source identity. The native builder
    derives a destination-qualified primary UUID; identical IDs still require
    exact payload proof. `fingerprint` is separate cross-source provenance,
    never a source alias or permission to skip a record.

    `confidence` is `"low"` for the slim variant (Netflix gives only a date,
    so the time-of-day is synthesized) and `"high"` for the rich/GDPR variant
    (a real UTC timestamp and duration). Downstream consumers use this to
    decide whether to trust the exact time or just the fact that the title
    was watched that day.

    `external` seeds `external_ids` in the native record
    with variant-specific context (e.g. `occurrence_index` for slim,
    `profile`/`device_type` for rich). Canonical `content_fingerprint` and
    `timestamp_confidence` values take precedence over variant metadata.
    """

    title: str
    note: str
    start: datetime
    end: datetime
    det_id: str
    fingerprint: str
    confidence: str            # "low" | "high"
    external: dict = field(default_factory=dict)


# Ported from packages/media-helpers/fulcra_media/importers/base.py — keep in sync
_SLUG_KEEP_RE = re.compile(r"[^a-z0-9\- ]+")


# Ported from packages/media-helpers/fulcra_media/importers/base.py — keep in sync
def _slugify(value: str) -> str:
    """Lowercase, strip non-alphanumeric (except spaces and hyphens), collapse runs to hyphens."""
    s = _SLUG_KEEP_RE.sub("", (value or "").lower())
    # Treat existing hyphens as word boundaries equivalent to spaces, then collapse.
    parts = [p for p in re.split(r"[\s-]+", s) if p]
    return "-".join(parts)


# Ported from packages/media-helpers/fulcra_media/importers/base.py — keep in sync
# (only the "tv" and "movie" branches; music/podcast/workout/book dropped — YAGNI
# for the Netflix-only skill.)
def content_fingerprint(kind: str, **fields) -> str:
    """Build a stable cross-source content identifier.

    kind="tv":       requires show, season:int, episode:int
    kind="movie":    requires title; optional year
    """
    if kind == "tv":
        return f"tv:{_slugify(fields['show'])}:s{fields['season']:02d}e{fields['episode']:02d}"
    if kind == "movie":
        base = f"movie:{_slugify(fields['title'])}"
        year = fields.get("year")
        return f"{base}:y{year}" if year else base
    raise ValueError(f"unknown fingerprint kind: {kind!r}")


# Ported from packages/media-helpers/fulcra_media/importers/netflix.py — keep in sync
# (that file's `_fingerprint_from_joined_title`, renamed here to the public
# `fingerprint_from_joined_title` since this script has no private/public
# module boundary of its own.)
def fingerprint_from_joined_title(raw_title: str, *, is_episode: bool | None = None) -> str:
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


# Ported from packages/media-helpers/fulcra_media/importers/netflix.py — keep in sync
_RICH_EXPECTED_COLS = [
    "Profile Name", "Start Time", "Duration", "Attributes", "Title",
    "Supplemental Video Type", "Device Type", "Bookmark", "Latest Bookmark", "Country",
]


# Ported from packages/media-helpers/fulcra_media/importers/netflix.py — keep in sync
_EPISODE_MARKERS = ("Season ", "Episode ", "Limited Series", "Chapter ", "Volume ")


# Ported from packages/media-helpers/fulcra_media/importers/netflix.py — keep in sync
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


# Ported from packages/media-helpers/fulcra_media/importers/netflix.py — keep in sync
# (that file's `_det_id_rich`, renamed here to `det_id_rich` for the same
# no-private-boundary reason as `det_id_slim`.)
def det_id_rich(profile: str, start_time_str: str, raw_title: str) -> str:
    h = hashlib.sha256(f"{profile}|{start_time_str}|{raw_title}".encode()).hexdigest()
    return f"com.fulcra.media.netflix-rich.{h[:16]}"


# Ported from packages/media-helpers/fulcra_media/importers/netflix.py — keep in sync
def _parse_hmmss(value: str) -> timedelta:
    """Parse H:MM:SS into a timedelta."""
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError(f"not a H:MM:SS duration: {value!r}")
    h, m, s = (int(p) for p in parts)
    return timedelta(hours=h, minutes=m, seconds=s)


# --- slim variant ---

def parse_slim(csv_path: Path):
    """Yield slim events with the historical row/identity semantics."""
    with Path(csv_path).open(newline="", encoding="utf-8-sig") as source:
        yield from _slim_rows(csv.DictReader(source))


def _slim_rows(reader):
    occurrence: Counter = Counter()
    if reader.fieldnames != ["Title", "Date"]:
        raise ValueError(
            f"not a Netflix slim CSV (header {reader.fieldnames!r}); "
            "expected exactly Title,Date"
        )
    for row in reader:
        raw_title, date_str = row["Title"], row["Date"]
        try:
            d = parse_netflix_date(date_str)
        except ValueError as e:
            # Row context (line number + title) turns a bare parse error
            # into something a user can act on without opening a debugger.
            raise ValueError(f"row {reader.line_num} ({raw_title!r}): {e}") from e
        idx = occurrence[(date_str, raw_title)]
        occurrence[(date_str, raw_title)] += 1
        note, title = make_note_and_title(raw_title)
        start = datetime.combine(d, time(12, 0), tzinfo=timezone.utc)
        yield Event(
            title=title, note=note,
            start=start,
            # Preserve the historical one-second point-in-time sentinel.
            end=start + timedelta(seconds=1),
            det_id=det_id_slim(date_str, raw_title, idx),
            fingerprint=fingerprint_from_joined_title(raw_title),
            confidence="low",
            external={
                "time_estimated": True, "point_in_time": True,
                "occurrence_index": idx, "raw_date": date_str,
            },
        )


# --- rich variant ---

def parse_rich(csv_path: Path):
    """Yield sessions with historical rich identity and trailer filtering."""
    with Path(csv_path).open(newline="", encoding="utf-8-sig") as source:
        yield from _rich_rows(csv.DictReader(source))


def _rich_rows(reader):
    if reader.fieldnames != _RICH_EXPECTED_COLS:
        raise ValueError(
            f"not a Netflix GDPR ViewingActivity.csv (header {reader.fieldnames!r})"
        )
    for row in reader:
        if (row.get("Supplemental Video Type") or "").strip():
            continue                      # trailers/hooks/previews
        raw_title = row["Title"]
        start_str = row["Start Time"]
        try:
            start = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc)
            dur = _parse_hmmss(row["Duration"])
        except ValueError as e:
            # Row context (line number + title) turns a bare parse error
            # into something a user can act on without opening a debugger.
            raise ValueError(f"row {reader.line_num} ({raw_title!r}): {e}") from e
        note, title = _extract_title_rich(raw_title)
        # Ported from fulcra-media's rich parser verbatim: is_episode is a
        # plain bool here (never None) — _extract_title_rich already sets
        # note == title for movies, so `note != title` alone would miss
        # single-colon-marker edge cases; OR-ing in the marker check
        # matches fulcra-media's exact condition so fingerprints agree
        # byte-for-byte between the two tools.
        is_episode = note != title or any(
            marker in raw_title for marker in _EPISODE_MARKERS)
        yield Event(
            title=title, note=note,
            start=start, end=start + dur,
            det_id=det_id_rich(row["Profile Name"], start_str, raw_title),
            fingerprint=fingerprint_from_joined_title(
                raw_title, is_episode=is_episode),
            confidence="high",
            external={
                "profile": row["Profile Name"],
                "device_type": row.get("Device Type", ""),
            },
        )


# --- variant detect ---

def _variant(header):
    if header == ["Title", "Date"]:
        return "slim"
    if header == _RICH_EXPECTED_COLS:
        return "rich"
    raise ValueError(f"unrecognized Netflix CSV header: {header!r}")


def detect_variant(csv_path: Path) -> str:
    """Retained path probe; main uses the one-open reader below."""
    with Path(csv_path).open(newline="", encoding="utf-8-sig") as source:
        return _variant(next(csv.reader(source), None))


def _read_events(csv_path):
    with Path(csv_path).open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        variant = _variant(reader.fieldnames)
        events = list((_slim_rows if variant == "slim" else _rich_rows)(reader))
    _validate_events(events)
    return variant, events


def _validate_events(events):
    """Pure whole-file codec and duplicate proof, also used by check-only."""
    seen = {}
    for event in events:
        if (any(not isinstance(value, str) for value in
                (event.title, event.note, event.det_id, event.fingerprint))
                or not event.det_id.strip() or event.confidence not in ("low", "medium", "high")
                or not isinstance(event.external, dict)):
            raise ValueError("invalid Netflix event fields")
        if any(not isinstance(value, datetime) or value.tzinfo is None
               or value.utcoffset() is None for value in (event.start, event.end)):
            raise ValueError("invalid Netflix event time")
        duration = (event.end - event.start).total_seconds()
        if duration < 0 or not duration.is_integer():
            raise ValueError("invalid Netflix event duration")
        payload = json.dumps({**vars(event), "start": event.start.isoformat(),
                              "end": event.end.isoformat()}, sort_keys=True, allow_nan=False)
        if event.det_id in seen and seen[event.det_id] != payload:
            raise ValueError("conflicting Netflix input identity")
        seen[event.det_id] = payload


def _target(value):
    """Syntactic Event pin proof before runtime/auth/source access."""
    from uuid import UUID

    if value is None:
        return None
    base, separator, identity = value.partition("/")
    if base != "Event" or not separator or identity.lower() != str(UUID(identity)):
        raise ValueError("invalid native destination")
    return "Event/" + str(UUID(identity))


class _Arguments(argparse.ArgumentParser):
    def error(self, message):
        # argparse's ordinary error output repeats raw argument values.
        raise ValueError("invalid Netflix options")


def main(argv: list[str] | None = None) -> int:
    ap = _Arguments(description="Import Netflix history as native Watched Events.")
    ap.add_argument("csv_path")
    ap.add_argument("--json", action="store_true", dest="json_out", help="emit one JSON envelope")
    ap.add_argument("--check-only", action="store_true", help="pure local parse and count")
    ap.add_argument("--data-type", help="optional owned Event/UUID destination pin")
    ap.add_argument("--no-verify", action="store_true", help="unsupported: native verification is mandatory")
    raw_args = sys.argv[1:] if argv is None else argv
    invalid_options = False
    try:
        args = ap.parse_args(raw_args)
    except ValueError:
        invalid_options = True
        args = argparse.Namespace(json_out="--json" in raw_args)
    # Original keys survive; counts_semantics versions their native meanings.
    env = {"importer": "netflix", "variant": None, "ok": False, "total": 0,
           "posted": 0, "skipped_existing": None, "verified": None,
           "would_post": None, "errors": [], "counts_semantics": "native-v1-complete-groups",
           "status": "failed", "excluded": 0, "duplicate_input": 0,
           "recovery": {"verified": 0, "excluded": 0}}
    writer = None
    stage = "args"

    def observe(current):
        nonlocal writer
        writer = current

    def read_source():
        nonlocal stage
        stage = "parse"
        env["variant"], events = _read_events(Path(args.csv_path))
        env["total"] = len(events)
        return events

    try:
        if invalid_options:
            raise ValueError("invalid Netflix options")
        destination = _target(args.data_type)
        if args.no_verify:
            raise ValueError("native verification is mandatory")
        if args.check_only:
            events = read_source()
            env["would_post"] = len(events)
            env["status"] = "checked"
        else:
            stage = "runtime"
            from fulcra_netflix import native

            if (type(native.CONTRACT_VERSION) is not int
                    or native.CONTRACT_VERSION != ADAPTER_CONTRACT_VERSION):
                raise RuntimeError("incompatible installed Netflix adapter")
            native.require_runtime()
            stage = "recovery"
            with native.open_netflix(native.authority(destination), on_writer=observe) as current:
                events = read_source()
                stage = "current"
                current.run_import(events, env["variant"], chunk_size=CHUNK_SIZE)
                stage = "close"
            env["status"] = "complete"
        env["ok"] = True
    except Exception:
        # Source/parser/HTTP/runtime exceptions can contain credentials or rows.
        # Report a static stage only, after context cleanup preserves evidence.
        messages = {
            "args": "Invalid options; use an owned Event/UUID and mandatory verification.",
            "parse": "Cannot read or validate Netflix CSV; correct the original source.",
            "runtime": "Matching installed Netflix/Collect runtime required; use the pinned checkout environment.",
            "recovery": "Recovery UNKNOWN or blocked; retain original pending work and authority.",
            "current": "Current import blocked or UNKNOWN; retain pending work and original source.",
            "close": "Runtime cleanup failed; retain pending work and review verified lower bounds.",
        }
        env["errors"].append({"stage": stage, "message": messages[stage]})
        env["status"] = "unknown" if stage in ("recovery", "current", "close") else "failed"
    finally:
        if writer is not None:
            progress = writer.progress
            env.update(posted=progress.verified, verified=progress.verified,
                       excluded=progress.excluded, duplicate_input=progress.duplicate_input)
            env["recovery"] = {"verified": writer.recovery.verified,
                               "excluded": writer.recovery.excluded}
    print(json.dumps(env, sort_keys=True) if args.json_out else _human_summary(env))
    return 0 if env["ok"] else 2


def _human_summary(env):
    if not env["ok"]:
        error = env["errors"][0]
        return (f"FAILED at {error['stage']}: {error['message']} "
                f"Current verified lower bound={env['verified']}; recovery={env['recovery']}")
    if env["would_post"] is not None:
        return f"would post {env['would_post']} of {env['total']} events ({env['variant']})"
    return (f"native import complete: verified={env['verified']} excluded={env['excluded']}; "
            f"recovery={env['recovery']}")


if __name__ == "__main__":
    sys.exit(main())
