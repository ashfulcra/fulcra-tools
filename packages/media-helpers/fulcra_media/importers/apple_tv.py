"""Apple TV app importer — watch history from the local UTS service cache.

The macOS TV app's service process keeps a CFURL cache at
``~/Library/Group Containers/group.tvappservices.container/Cache.db``
(a protected group container — the daemon needs Full Disk Access
to read it) holding JSON responses from
``uts-api.itunes.apple.com/uts/v3/canvases/Roots/tahoma_watchnow`` — the
Watch Now canvas, auto-refreshed every few hours even when the app isn't
being used. Two shelves in those payloads carry watch signals:

1. **Up Next** (``displayType == "upNextLockup"``): items with
   ``showTitle`` / ``title`` / ``seasonNumber`` / ``episodeNumber`` /
   ``type`` and a ``timestamp`` that is the REAL last-activity time.
   ``context`` distinguishes what the timestamp means:
     - ``Continue``     — the user had activity on THAT item at timestamp
                          (mid-episode / mid-movie). High-confidence.
     - ``NextEpisode``  — the PREVIOUS episode was completed ~timestamp.
                          We emit an event for episode N-1 (same season)
                          when N >= 2; N == 1 is skipped because the prior
                          episode would be the previous season's finale,
                          whose episode number is not in the payload.
     - ``NextSeason``   — the prior season's finale was completed
                          ~timestamp, but its episode number is unknowable
                          from the payload, so we skip it entirely rather
                          than guess.
     - ``AddedToUpNext`` (Recently Added) / ``AvailableNow`` (Now
       Available) — catalog noise, ignored.

2. **Recently Watched** (``displayType == "playHistory"``, header title
   "Recently Watched", shelf URL contains ``uts.col.PlayHistory``): ~20
   items in most-recent-first WATCH ORDER. Items carry ``releaseDate``
   (the original AIR date — NEVER usable as a watch time) and no
   watched-at timestamp. We emit one idempotent event per (show, season,
   episode) EVER, timestamped at the cfurl snapshot fetch time (an upper
   bound of the true watch time) with ``timestamp_confidence="low"``.

CFURL cache mechanics (learned the hard way):
  - Copy Cache.db + -wal + -shm to a temp snapshot before reading (the
    live DB is busy); mirror apple_podcasts.py's clonefile snapshot with
    an I/O-stall timeout.
  - Join ``cfurl_cache_response r`` (request_key = URL, time_stamp) with
    ``cfurl_cache_receiver_data d`` on entry_ID.
  - If ``d.isDataOnFS``, the body is a FILE named by the receiver_data
    string under ``<cache dir>/fsCachedData/<name>`` — read from the
    ORIGINAL directory, not the snapshot (fs-cached files aren't in the
    db). Otherwise receiver_data IS the body.
  - Bodies may be gzip, zlib, or raw JSON — try in that order.
  - Parse ALL tahoma_watchnow snapshots, not just the newest — several
    per day exist and older ones may hold history items already evicted
    from the newest.

Everything here is fail-soft: malformed JSON, missing shelves, unknown
context strings, and missing fsCachedData files are skipped with a debug
log. Only a missing cache (TV app never ran) or a stalled snapshot copy
raise.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fulcra_common.cross_source_fingerprint import (
    watched_movie_fingerprint,
    watched_tv_fingerprint,
)

from .base import NormalizedEvent, content_fingerprint

logger = logging.getLogger("fulcra_media.importers.apple_tv")

DEFAULT_CACHE_DIR = Path(os.path.expanduser(
    "~/Library/Group Containers/group.tvappservices.container"
))
CACHE_DB_NAME = "Cache.db"
FS_CACHED_DATA_DIR = "fsCachedData"

# The Watch Now canvas — every snapshot of it (and its nextToken pages)
# can hold the Up Next and Recently Watched shelves.
CANVAS_URL_MARKER = "uts/v3/canvases/Roots/tahoma_watchnow"
# Direct shelf-page fetches of the play-history collection.
HISTORY_URL_MARKERS = ("RecentlyWatched", "PlayHistory")

# Snapshot deadline. Unlike apple_podcasts (a 347MB library where a legit copy
# can genuinely take tens of seconds), this cache is ~1-2MB, so a healthy
# APFS clonefile completes in well under a second. A copy that runs longer
# than a few seconds is NOT slow I/O — it means the process reading the cache
# lacks Full Disk Access. The cache lives in another app's group container
# (~/Library/Group Containers/group.tvappservices.container), and on Sequoia+
# a read of it by a process without FDA parks INSIDE the open(2) syscall
# rather than failing fast (verified live 2026-07-07 by sampling a hung
# reader: the stack sits in __open; stat(2) succeeds, every data open blocks
# — cp, sqlite, raw read alike — while control reads of non-container paths
# return instantly). FDA is per-binary: it must be granted to the DAEMON's
# actual interpreter (the resolved uv cpython, not the .venv/bin/python
# symlink, and not Terminal). With that grant on, opens return instantly and
# scheduled runs are silent; without it, fail fast and retry next interval
# rather than pinning a worker.
SNAPSHOT_TIMEOUT_SECONDS = 20


class SnapshotError(RuntimeError):
    """Raised when the cache DB cannot be snapshotted (stalled/inaccessible)."""


# ---------------------------------------------------------------------------
# CFURL cache access
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    """One decoded cache body: the request URL, when the response was
    fetched (cfurl time_stamp, UTC), and the decompressed body bytes."""
    url: str
    fetched_at: datetime
    body: bytes


def _snapshot_db(cache_dir: Path) -> Path:
    """Clone Cache.db (+ -wal/-shm sidecars) into a fresh tempdir and return
    the snapshot dir. Uses `cp -c` for the normal APFS clonefile(2) path,
    with a killable subprocess timeout so an active TV app cannot pin the
    worker indefinitely when macOS serializes reads of its group container.
    Caller must shutil.rmtree the returned dir."""
    src = cache_dir / CACHE_DB_NAME
    snap_dir = Path(tempfile.mkdtemp(prefix="apple-tv-cache-snap-"))
    try:
        for ext in ("", "-wal", "-shm"):
            candidate = Path(str(src) + ext)
            if not candidate.exists():
                continue
            dest = snap_dir / candidate.name
            try:
                subprocess.run(
                    ["cp", "-c", str(candidate), str(dest)],
                    check=True,
                    capture_output=True,
                    timeout=SNAPSHOT_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                try:
                    size_mb = candidate.stat().st_size / 1_000_000
                except OSError:
                    size_mb = -1
                raise SnapshotError(
                    f"Apple TV cache snapshot timed out after "
                    f"{SNAPSHOT_TIMEOUT_SECONDS}s copying {candidate.name} "
                    f"({size_mb:.0f}MB) — the reading process lacks Full Disk "
                    f"Access, so opening the TV service's group container "
                    f"blocks. Fix: grant FDA to the daemon's interpreter "
                    f"(System Settings > Privacy & Security > Full Disk "
                    f"Access, add the resolved uv cpython binary, toggle on) "
                    f"and restart the daemon. This run is skipped; the next "
                    f"scheduled run retries."
                ) from exc
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or b"").decode(errors="replace").strip()
                raise SnapshotError(
                    f"Apple TV cache snapshot failed copying "
                    f"{candidate.name}: {stderr[:200]}"
                ) from exc
        return snap_dir
    except BaseException:
        shutil.rmtree(snap_dir, ignore_errors=True)
        raise


def decode_body(raw: bytes) -> bytes:
    """Decompress a cfurl body: gzip, then bare zlib, then raw passthrough.
    (Real-world tahoma_watchnow bodies have been observed both gzipped and
    raw depending on which layer wrote them.)"""
    try:
        return gzip.decompress(raw)
    except OSError:
        pass
    try:
        return zlib.decompress(raw)
    except zlib.error:
        return raw


def _parse_db_timestamp(value) -> datetime | None:
    """cfurl_cache_response.time_stamp — 'YYYY-MM-DD HH:MM:SS' in UTC
    (SQLite CURRENT_TIMESTAMP). Defensive: also accepts ISO strings with
    offsets and numeric epochs."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _url_matches(url: str) -> bool:
    if CANVAS_URL_MARKER in url:
        return True
    return any(marker in url for marker in HISTORY_URL_MARKERS)


def iter_cache_entries(cache_dir: Path = DEFAULT_CACHE_DIR) -> Iterator[CacheEntry]:
    """Yield decoded bodies for every cached UTS response we care about,
    oldest snapshot first (so 'first occurrence' means 'earliest fetch').

    Raises RuntimeError when the cache DB doesn't exist, SnapshotError when
    it can't be copied. Individual bad rows (missing fsCachedData file,
    undecodable body) are skipped with a debug log.
    """
    cache_dir = Path(cache_dir)
    db_path = cache_dir / CACHE_DB_NAME
    if not db_path.exists():
        raise RuntimeError(
            f"TV app cache not found at {db_path} — open the TV app once "
            f"so macOS creates its Watch Now cache."
        )
    snap_dir = _snapshot_db(cache_dir)
    conn = None
    try:
        conn = sqlite3.connect(snap_dir / CACHE_DB_NAME)
        cur = conn.cursor()
        cur.execute("""
            SELECT r.request_key, r.time_stamp, d.isDataOnFS, d.receiver_data
            FROM cfurl_cache_response r
            JOIN cfurl_cache_receiver_data d ON d.entry_ID = r.entry_ID
            ORDER BY r.time_stamp ASC, r.entry_ID ASC
        """)
        for url, time_stamp, is_on_fs, receiver_data in cur.fetchall():
            if not url or not _url_matches(url):
                continue
            fetched_at = _parse_db_timestamp(time_stamp)
            if fetched_at is None:
                logger.debug("apple-tv: unparseable time_stamp %r for %s",
                             time_stamp, url[:120])
                continue
            if is_on_fs:
                # receiver_data is a FILENAME under <cache dir>/fsCachedData/.
                # Read it from the ORIGINAL cache dir — the fs-cached files
                # are not inside the sqlite snapshot.
                name = (
                    receiver_data.decode("utf-8", errors="replace")
                    if isinstance(receiver_data, (bytes, bytearray))
                    else str(receiver_data)
                )
                fs_path = cache_dir / FS_CACHED_DATA_DIR / name
                try:
                    raw = fs_path.read_bytes()
                except OSError:
                    # The app prunes fsCachedData independently of the db;
                    # dangling rows are normal. Skip quietly.
                    logger.debug("apple-tv: fsCachedData file missing: %s", fs_path)
                    continue
            else:
                if receiver_data is None:
                    logger.debug("apple-tv: empty receiver_data for %s", url[:120])
                    continue
                raw = (
                    bytes(receiver_data)
                    if isinstance(receiver_data, (bytes, bytearray, memoryview))
                    else str(receiver_data).encode("utf-8")
                )
            yield CacheEntry(url=url, fetched_at=fetched_at, body=decode_body(raw))
    finally:
        if conn is not None:
            conn.close()
        shutil.rmtree(snap_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Canvas / shelf parsing
# ---------------------------------------------------------------------------

def parse_item_timestamp(value) -> datetime | None:
    """Parse an Up Next item ``timestamp`` — observed as epoch-milliseconds,
    but parse ISO strings and epoch-seconds defensively too."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        try:
            value = float(s)
        except ValueError:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        # 13-digit epochs are milliseconds; 10-digit are seconds.
        seconds = float(value) / 1000.0 if abs(value) > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _sha16(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _det_id(payload: str) -> str:
    return f"com.fulcra.media.apple-tv.v1.{_sha16(payload)}"


def _episode_note(show: str, season: int, episode: int, title: str | None) -> str:
    base = f"{show} S{season:02d}E{episode:02d}"
    return f"{base} – {title}" if title else base


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# context values (canonical) → semantics. localizedContext is the display
# string; fall back to it for robustness across app versions.
_CONTINUE = {"Continue"}
_NEXT_EPISODE = {"NextEpisode", "Next Episode"}
_NEXT_SEASON = {"NextSeason", "Next Season"}
_CATALOG_NOISE = {
    "AddedToUpNext", "Recently Added",
    "AvailableNow", "Now Available",
}


def _events_from_up_next(items: list) -> Iterator[NormalizedEvent]:
    for item in items:
        if not isinstance(item, dict):
            continue
        ctx = item.get("context") or item.get("localizedContext") or ""
        loc = item.get("localizedContext") or ""
        if ctx in _CATALOG_NOISE or loc in _CATALOG_NOISE:
            continue  # catalog noise — not a watch signal
        ts = parse_item_timestamp(item.get("timestamp"))
        if ts is None:
            logger.debug("apple-tv: up-next item without usable timestamp: %r",
                         item.get("title"))
            continue
        item_type = item.get("type")

        if ctx in _CONTINUE or loc in _CONTINUE:
            yield from _continue_event(item, item_type, ts)
        elif ctx in _NEXT_EPISODE or loc in _NEXT_EPISODE:
            yield from _next_episode_event(item, item_type, ts)
        elif ctx in _NEXT_SEASON or loc in _NEXT_SEASON:
            # The signal is real (the prior season's finale was completed
            # ~ts) but the finale's episode number is not in the payload.
            # Guessing would fabricate history; skip and log.
            logger.debug(
                "apple-tv: skipping NextSeason signal for %r — prior-season "
                "finale episode number is not derivable from the payload",
                item.get("showTitle") or item.get("title"))
        else:
            logger.debug("apple-tv: unknown up-next context %r/%r for %r — skipped",
                         ctx, loc, item.get("showTitle") or item.get("title"))


def _continue_event(item: dict, item_type, ts: datetime) -> Iterator[NormalizedEvent]:
    """Continue == real activity on this exact item at ``ts``. High conf."""
    day = ts.date().isoformat()
    ext: dict = {
        "kind": "continue",
        "context": item.get("context"),
        "raw_timestamp": item.get("timestamp"),
    }
    if item.get("id"):
        ext["apple_tv_id"] = item["id"]

    if item_type == "Episode":
        show = item.get("showTitle")
        season = _int_or_none(item.get("seasonNumber"))
        episode = _int_or_none(item.get("episodeNumber"))
        if not show or season is None or episode is None:
            logger.debug("apple-tv: Continue episode missing show/S/E: %r", item.get("title"))
            return
        note = _episode_note(show, season, episode, item.get("title"))
        ext.update(show=show, season=season, episode=episode,
                   content_fingerprint=content_fingerprint(
                       "tv", show=show, season=season, episode=episode))
        cross = watched_tv_fingerprint(
            timestamp=ts, show=show, season=season, episode=episode)
        det = _det_id(f"apple-tv|continue|{show}|S{season}E{episode}|{day}")
        title = show
    elif item_type == "Movie":
        title = item.get("title")
        if not title:
            logger.debug("apple-tv: Continue movie without title — skipped")
            return
        note = title
        ext["content_fingerprint"] = content_fingerprint("movie", title=title)
        cross = watched_movie_fingerprint(timestamp=ts, title=title)
        det = _det_id(f"apple-tv|continue|{title}|movie|{day}")
    else:
        # A bare "Show" item can't be pinned to an episode — skip.
        logger.debug("apple-tv: Continue item of type %r not attributable — skipped",
                     item_type)
        return

    yield NormalizedEvent(
        importer="apple-tv",
        service="apple-tv",
        category="watched",
        note=note,
        title=title,
        start_time=ts,
        # We know the user was active at ts, not for how long. 1-second
        # sentinel (start == end events are silently dropped by Fulcra).
        end_time=ts + timedelta(seconds=1),
        deterministic_id=det,
        timestamp_confidence="high",
        external_ids=ext,
        extra_source_ids=(cross,) if cross else (),
    )


def _next_episode_event(item: dict, item_type, ts: datetime) -> Iterator[NormalizedEvent]:
    """NextEpisode on episode N means episode N-1 (same season) was
    completed ~ts. Only emit when the prior episode is computable: N >= 2.
    N == 1's predecessor is the previous season's finale, whose number
    isn't in the payload — skipped, same honesty rule as NextSeason.

    The timestamp is when Up Next flipped to the next episode, i.e.
    approximately (not exactly) the completion moment → medium confidence,
    and per the conservative design no cross-source time-bucket fingerprint
    (those are reserved for high-confidence timestamps).
    """
    if item_type != "Episode":
        logger.debug("apple-tv: NextEpisode on non-episode type %r — skipped", item_type)
        return
    show = item.get("showTitle")
    season = _int_or_none(item.get("seasonNumber"))
    episode = _int_or_none(item.get("episodeNumber"))
    if not show or season is None or episode is None:
        logger.debug("apple-tv: NextEpisode missing show/S/E: %r", item.get("title"))
        return
    if episode < 2:
        logger.debug(
            "apple-tv: NextEpisode for %s S%dE%d — prior episode is the "
            "previous season's finale (number unknown); skipped",
            show, season, episode)
        return
    prior = episode - 1
    day = ts.date().isoformat()
    yield NormalizedEvent(
        importer="apple-tv",
        service="apple-tv",
        category="watched",
        # The prior episode's title isn't in the payload — note is S/E only.
        note=_episode_note(show, season, prior, None),
        title=show,
        start_time=ts,
        end_time=ts + timedelta(seconds=1),
        deterministic_id=_det_id(f"apple-tv|completed|{show}|S{season}E{prior}|{day}"),
        timestamp_confidence="medium",
        external_ids={
            "kind": "completed_prior_episode",
            "context": item.get("context"),
            "derived_from": "next_episode",
            "show": show,
            "season": season,
            "episode": prior,
            "raw_timestamp": item.get("timestamp"),
            "content_fingerprint": content_fingerprint(
                "tv", show=show, season=season, episode=prior),
        },
    )


def _events_from_history(items: list, fetched_at: datetime) -> Iterator[NormalizedEvent]:
    """Recently Watched — most-recent-first watch ORDER, no watch times.

    One event per (show, season, episode) EVER: the det_id has no timestamp
    component, so re-parses and later snapshots are naturally idempotent.
    start_time is the cfurl snapshot fetch time — the upper bound of the
    true watch time — with timestamp_confidence="low". ``releaseDate`` is
    the original AIR date and must NEVER be used as a watch time; we keep
    it in external_ids as metadata only.
    """
    for rank, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        ext: dict = {
            "kind": "history",
            "watch_order_rank": rank,  # 0 == most recent in this snapshot
            "snapshot_time": fetched_at.isoformat(),
            "time_estimated": True,
            "point_in_time": True,
        }
        if item.get("releaseDate") is not None:
            # Metadata only — the original air date, NOT a watch time.
            ext["release_date_ms"] = item["releaseDate"]
        if item.get("id"):
            ext["apple_tv_id"] = item["id"]

        if item_type == "Episode":
            show = item.get("showTitle")
            season = _int_or_none(item.get("seasonNumber"))
            episode = _int_or_none(item.get("episodeNumber"))
            if not show or season is None or episode is None:
                logger.debug("apple-tv: history episode missing show/S/E: %r",
                             item.get("title"))
                continue
            note = _episode_note(show, season, episode, item.get("title"))
            title = show
            ext.update(show=show, season=season, episode=episode,
                       content_fingerprint=content_fingerprint(
                           "tv", show=show, season=season, episode=episode))
            det = _det_id(f"apple-tv|history|{show}|S{season}E{episode}")
        elif item_type == "Movie":
            title = item.get("title")
            if not title:
                logger.debug("apple-tv: history movie without title — skipped")
                continue
            note = title
            ext["content_fingerprint"] = content_fingerprint("movie", title=title)
            det = _det_id(f"apple-tv|history|{title}|movie")
        else:
            logger.debug("apple-tv: history item of type %r not attributable — skipped",
                         item_type)
            continue

        yield NormalizedEvent(
            importer="apple-tv",
            service="apple-tv",
            category="watched",
            note=note,
            title=title,
            start_time=fetched_at,
            end_time=fetched_at + timedelta(seconds=1),
            deterministic_id=det,
            timestamp_confidence="low",
            external_ids=ext,
        )


def _is_history_shelf(shelf: dict) -> bool:
    if shelf.get("displayType") == "playHistory":
        return True
    header = shelf.get("header")
    if isinstance(header, dict) and header.get("title") == "Recently Watched":
        return True
    url = shelf.get("url") or ""
    return "uts.col.PlayHistory" in url


def _shelves_of(payload: dict) -> list:
    """Extract the shelf list from a canvas payload or a shelf-page payload."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    shelves: list = []
    canvas = data.get("canvas")
    if isinstance(canvas, dict) and isinstance(canvas.get("shelves"), list):
        shelves.extend(canvas["shelves"])
    # Deeper shelf-page fetches return a single shelf object.
    shelf = data.get("shelf")
    if isinstance(shelf, dict):
        shelves.append(shelf)
    return shelves


def parse_canvas_payload(payload: dict, fetched_at: datetime) -> Iterator[NormalizedEvent]:
    """Yield NormalizedEvents from one decoded UTS payload.

    Fail-soft: shelves that aren't dicts, have no items, or aren't one of
    the two watch-signal shelves are skipped silently.
    """
    for shelf in _shelves_of(payload):
        if not isinstance(shelf, dict):
            continue
        items = shelf.get("items")
        if not isinstance(items, list) or not items:
            continue
        if shelf.get("displayType") == "upNextLockup":
            yield from _events_from_up_next(items)
        elif _is_history_shelf(shelf):
            yield from _events_from_history(items, fetched_at)


# ---------------------------------------------------------------------------
# Whole-cache scan
# ---------------------------------------------------------------------------

@dataclass
class CacheScan:
    """Result of scanning the whole cache: deduped events plus the health
    stats the plugin's health_check surfaces."""
    events: list[NormalizedEvent] = field(default_factory=list)
    newest_fetch: datetime | None = None
    snapshot_count: int = 0


def scan_cache(cache_dir: Path = DEFAULT_CACHE_DIR) -> CacheScan:
    """Scan every matching cache entry (oldest first) and merge events.

    Events are deduped on deterministic_id with first-occurrence-wins:
    for history items that pins start_time to the EARLIEST snapshot that
    contained the item (the tightest upper bound of the watch time);
    Continue/completed events from repeated snapshots of the same activity
    collapse because their det_id includes the activity day.
    """
    scan = CacheScan()
    seen: dict[str, NormalizedEvent] = {}
    for entry in iter_cache_entries(cache_dir):
        scan.snapshot_count += 1
        if scan.newest_fetch is None or entry.fetched_at > scan.newest_fetch:
            scan.newest_fetch = entry.fetched_at
        try:
            payload = json.loads(entry.body)
        except (ValueError, UnicodeDecodeError):
            logger.debug("apple-tv: undecodable cache body for %s (fetched %s)",
                         entry.url[:120], entry.fetched_at)
            continue
        for event in parse_canvas_payload(payload, entry.fetched_at):
            if event.deterministic_id not in seen:
                seen[event.deterministic_id] = event
    scan.events = list(seen.values())
    return scan


def parse_cache(cache_dir: Path = DEFAULT_CACHE_DIR) -> list[NormalizedEvent]:
    """All watch events currently derivable from the TV app's UTS cache."""
    return scan_cache(cache_dir).events
