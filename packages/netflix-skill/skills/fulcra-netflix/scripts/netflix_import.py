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

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import httpx

# --- constants ---

API_BASE = "https://api.fulcradynamics.com"
DEF_NAME = "Watched"
DEF_MARKER = "com.fulcradynamics.annotation.media.watched"
INGEST_VERSION = 2  # bump ONLY with a coordinated det-id prefix change
CHUNK_SIZE = 500  # records per batch POST; module-level so tests can shrink it


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


# --- rich variant ---

def parse_rich(csv_path: Path):
    """Parse a Netflix rich (GDPR) CSV into Events.

    The rich variant has 10 columns including a real UTC Start Time and a
    Duration in H:MM:SS, so timestamps here carry "high" confidence unlike
    the estimated-noon timestamps parse_slim produces. Rows with a non-empty
    Supplemental Video Type (TRAILER, HOOK, PROMOTIONAL, etc.) are dropped —
    they're not real viewing sessions, just autoplay previews Netflix logs
    alongside the real ones.
    """
    with Path(csv_path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
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

def detect_variant(csv_path: Path) -> str:
    """Sniff a Netflix export's CSV header to pick slim vs. rich parsing."""
    with Path(csv_path).open(newline="", encoding="utf-8-sig") as f:
        header = next(csv.reader(f), None)
    if header == ["Title", "Date"]:
        return "slim"
    if header == _RICH_EXPECTED_COLS:
        return "rich"
    raise ValueError(f"unrecognized Netflix CSV header: {header!r}")


# --- wire format ---

# Mirrors packages/fulcra-common/fulcra_common/wire.py — keep in sync
def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Mirrors packages/fulcra-common/fulcra_common/wire.py — keep in sync
#
# WHY: Fulcra dedups on source-id, not content — so metadata.source[0]
# (the deterministic det_id from det_id_slim/det_id_rich) IS the
# idempotency key for a re-run of this importer. The content_fingerprint
# deliberately lives in data.external_ids instead of the source array:
# fulcra-media's slim importer puts it there too, so any twin-dedup
# tooling that cross-references fingerprints between this script and
# fulcra-media sees the same shape in the same place, even though the
# two tools' det_ids differ and won't dedup against each other on
# source-id alone.
def build_record(ev: Event, *, def_id: str) -> dict:
    payload = {
        "title": ev.title,
        "note": ev.note,
        # int() truncation is inert here: both the slim variant (whole-second
        # synthetic 1s duration) and the rich variant (H:MM:SS source data)
        # always produce whole-second spans, so no sub-second precision is lost.
        "duration_seconds": int((ev.end - ev.start).total_seconds()),
        "external_ids": {
            # ev.external merges first so the injected keys below always win —
            # ev.external must never itself define content_fingerprint or
            # timestamp_confidence, since a silent override here would corrupt
            # the dedup/confidence contract without any visible error.
            **ev.external,
            "content_fingerprint": ev.fingerprint,
            "timestamp_confidence": ev.confidence,
        },
    }
    return {
        "specversion": 1,
        "data": json.dumps(payload, sort_keys=True),
        "metadata": {
            "data_type": "DurationAnnotation",
            "recorded_at": {"start_time": iso_z(ev.start), "end_time": iso_z(ev.end)},
            "tags": [],
            "source": [ev.det_id, f"com.fulcradynamics.annotation.{def_id}"],
            "content_type": "application/json",
        },
    }


# Mirrors packages/fulcra-common/fulcra_common/wire.py — keep in sync
def encode_batch(records: list[dict]) -> bytes:
    return b"\n".join(json.dumps(r, sort_keys=True).encode() for r in records)


# --- fulcra api ---

def ensure_watched_def(client: httpx.Client) -> str:
    """Resolve the Watched def by namespace marker; create once if absent.

    The marker in `description` (not the name, not a cached UUID) is the
    identity — it's also how downstream pool consumers find this data.
    """
    resp = client.get("/user/v1alpha1/annotation")
    resp.raise_for_status()
    for d in resp.json():
        if d.get("description") == DEF_MARKER and d.get("annotation_type") == "duration":
            return d["id"]
    body = {
        "name": DEF_NAME,
        "description": DEF_MARKER,
        "annotation_type": "duration",
        "measurement_spec": {
            "measurement_type": "duration", "value_type": "duration", "unit": None,
        },
        "tags": [],       # API 422s without it, even on duration defs
        "spec": None,
    }
    resp = client.post("/user/v1alpha1/annotation", json=body)
    resp.raise_for_status()
    return resp.json()["id"]


def get_token() -> str:
    """Mint a bearer via the fulcra-api CLI. Never persisted, never printed."""
    candidates = []
    if shutil.which("fulcra-api"):
        candidates.append(["fulcra-api", "auth", "print-access-token"])
    if shutil.which("uv"):
        candidates.append(["uv", "tool", "run", "fulcra-api",
                           "auth", "print-access-token"])
    last_err = "fulcra-api CLI not found (install: uv tool install fulcra-api)"
    for cmd in candidates:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=60, check=True).stdout.strip()
            if out:
                return out
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            last_err = f"{cmd[0]}: {getattr(e, 'stderr', '') or e}"
    raise RuntimeError(f"could not mint Fulcra token — {last_err}")


def make_api_client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        headers={"authorization": f"Bearer {token}"},
        timeout=60.0,
        follow_redirects=False,   # same 3xx-token-leak posture as fulcra-common
    )


class PartialPostError(RuntimeError):
    """A batch POST failed partway; carries how many records DID land.

    Batches are POSTed sequentially, and each one that succeeds is durably
    committed server-side before the next is attempted. If a later chunk
    fails, the earlier ones aren't rolled back — so a plain re-raise of the
    underlying httpx error would leave the caller with no way to know that
    `posted_so_far` records already made it to the server. Wrapping the
    failure in this exception lets main() report a truthful `posted` count
    in the JSON envelope instead of the default 0.
    """
    def __init__(self, posted_so_far: int, cause: httpx.HTTPError):
        super().__init__(str(cause))
        self.posted_so_far = posted_so_far
        self.cause = cause


def post_batch(client: httpx.Client, records: list[dict], *, chunk_size: int = 500) -> int:
    posted = 0
    for i in range(0, len(records), chunk_size):
        chunk = records[i:i + chunk_size]
        try:
            resp = client.post(
                "/ingest/v1/record/batch",
                content=encode_batch(chunk),
                headers={"content-type": "application/x-jsonl"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            # `posted` already reflects every prior chunk that succeeded;
            # a FIRST-chunk failure means posted_so_far == 0, which flows
            # through the same PartialPostError path as any later chunk.
            raise PartialPostError(posted, e) from e
        posted += len(chunk)
    return posted


def readback_verify(def_id: str, events: list, *, sample: int = 3) -> int | None:
    """Best-effort: for a few events, ask the CLI whether a record with our
    det_id landed. Returns count verified, or None when the CLI is absent.
    Indexing lag makes 0 a soft signal, not a failure (envelope reports both).
    """
    cli = (["fulcra-api"] if shutil.which("fulcra-api")
           else ["uv", "tool", "run", "fulcra-api"] if shutil.which("uv") else None)
    if not cli:
        return None
    verified = 0
    for ev in events[:sample]:
        window_start = iso_z(ev.start - timedelta(hours=1))
        window_end = iso_z(ev.end + timedelta(hours=1))
        try:
            out = subprocess.run(
                cli + ["get-records", f"DurationAnnotation/{def_id}",
                       window_start, window_end],
                capture_output=True, text=True, timeout=60, check=True).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        if ev.det_id in out:
            verified += 1
    return verified


# --- cli ---

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Import a Netflix viewing-history CSV into Fulcra as Watched annotations.")
    ap.add_argument("csv_path")
    ap.add_argument("--json", action="store_true", dest="json_out",
                    help="emit one-line JSON envelope (stable contract)")
    ap.add_argument("--check-only", action="store_true",
                    help="parse + count only; no POST")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip post-import readback sampling")
    args = ap.parse_args(argv)

    # Envelope keys are a stable, append-only contract: scripts/CI consuming
    # --json output can rely on every key always being present (None when
    # not applicable to the run), and on old keys never being removed or
    # repurposed. Add new keys; never delete or rename existing ones.
    env = {"importer": "netflix", "variant": None, "ok": False, "total": 0,
           "posted": 0, "skipped_existing": None, "verified": None,
           "would_post": None, "errors": []}

    def finish(code: int) -> int:
        print(json.dumps(env, sort_keys=True) if args.json_out
              else _human_summary(env))
        return code

    try:
        variant = detect_variant(Path(args.csv_path))
        env["variant"] = variant
        parser = parse_slim if variant == "slim" else parse_rich
        # Materialize fully BEFORE any POST: a bad row aborts pre-post,
        # so partial imports can't happen mid-file.
        events = list(parser(Path(args.csv_path)))
        env["total"] = len(events)
    except OSError as e:
        # Covers FileNotFoundError, PermissionError, IsADirectoryError, etc.
        # ValueError is caught separately below for parse failures; the two
        # hierarchies are disjoint, so branch order between them doesn't
        # matter. One exception: UnicodeDecodeError is a ValueError subclass
        # (a bad-encoding CSV surfaces via csv/str decoding, not open()), so
        # it's intentionally routed to the "parse" stage below, not here.
        env["errors"].append({"stage": "args", "message": str(e)})
        return finish(2)
    except ValueError as e:
        env["errors"].append({"stage": "parse", "message": str(e)})
        return finish(2)

    if args.check_only:
        env["would_post"] = len(events)
        env["ok"] = True
        return finish(0)

    try:
        token = get_token()
    except RuntimeError as e:
        env["errors"].append({"stage": "auth", "message": str(e)})
        return finish(2)

    try:
        with make_api_client(token) as client:
            def_id = ensure_watched_def(client)
            records = [build_record(ev, def_id=def_id) for ev in events]
            env["posted"] = post_batch(client, records, chunk_size=CHUNK_SIZE)
    except PartialPostError as e:
        # A batch mid-run failed, but earlier chunks already landed
        # server-side — report that truthfully instead of the default 0.
        # Classification of `e.cause` mirrors the httpx.HTTPStatusError/
        # HTTPError handling below exactly, since e.cause IS one of those.
        env["posted"] = e.posted_so_far
        cause = e.cause
        if isinstance(cause, httpx.HTTPStatusError):
            stage = "auth" if cause.response.status_code in (401, 403) else "post"
            message = f"HTTP {cause.response.status_code} on {cause.request.url.path}"
        else:
            stage = "post"
            message = str(cause)
        env["errors"].append({"stage": stage, "message": message})
        return finish(2)
    except httpx.HTTPStatusError as e:
        # Stage discriminator by status: 401/403 means the token was bad
        # (an auth-stage problem, even though it surfaces mid-POST), anything
        # else is a genuine post-stage failure. Message is deliberately just
        # "HTTP <code> on <path>" — never the token, never the response body,
        # which could echo back request contents or leak server internals.
        stage = "auth" if e.response.status_code in (401, 403) else "post"
        env["errors"].append({"stage": stage,
                              "message": f"HTTP {e.response.status_code} on {e.request.url.path}"})
        return finish(2)
    except httpx.HTTPError as e:
        env["errors"].append({"stage": "post", "message": str(e)})
        return finish(2)

    if not args.no_verify:
        env["verified"] = readback_verify(def_id, events)

    env["ok"] = True
    return finish(0)


def _human_summary(env: dict) -> str:
    if not env["ok"]:
        e = env["errors"][0] if env["errors"] else {}
        return f"FAILED at {e.get('stage', '?')}: {e.get('message', '')}"
    if env["would_post"] is not None:
        return f"would post {env['would_post']} of {env['total']} events ({env['variant']})"
    v = env["verified"]
    if v is None:
        verified_note = "skipped"
    elif v == 0:
        # 0 verified is a real (if soft) signal distinct from "didn't check" —
        # most often indexing hasn't caught up yet, not that the import
        # failed, so say so rather than printing a bare, alarming "0".
        verified_note = "0 (indexing may be lagging)"
    else:
        verified_note = str(v)
    return (f"imported {env['posted']}/{env['total']} ({env['variant']}); "
            f"verified sample: {verified_note}")


if __name__ == "__main__":
    sys.exit(main())
