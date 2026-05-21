"""Apple Podcasts importer — reads macOS MTLibrary.sqlite."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .base import NormalizedEvent, content_fingerprint

DEFAULT_DB_PATH = Path(os.path.expanduser(
    "~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts/Documents/MTLibrary.sqlite"
))
MAC_EPOCH_OFFSET = 978307200  # seconds between Unix epoch and Core Data epoch (2001-01-01 UTC)

# Hard cap on snapshotting the on-device DB. MTLibrary.sqlite can be several
# hundred MB; when iCloud is mid-sync or the file is otherwise I/O-stalled, an
# unbounded copy blocks indefinitely. Snapshotting via a killable subprocess
# with this timeout makes a stalled DB fail fast with a clear error.
SNAPSHOT_TIMEOUT_SECONDS = 120


class SnapshotError(RuntimeError):
    """Raised when the on-device DB cannot be snapshotted (stalled/inaccessible)."""


def _det_id(zuuid: str, zlastdateplayed: float) -> str:
    h = hashlib.sha256(f"{zuuid}|{zlastdateplayed}".encode()).hexdigest()
    return f"com.fulcra.media.apple-podcasts.v1.{h[:16]}"


def parse_db(db_path: Path) -> Iterator[NormalizedEvent]:
    """Yield one NormalizedEvent per completed episode in the DB snapshot.

    Filters: ZPLAYSTATE=3 (played) AND ZHASBEENPLAYED=1 AND
    ZPLAYSTATEMANUALLYSET=0 AND (ZPLAYHEAD / ZDURATION) > 0.9 AND
    ZLASTDATEPLAYED IS NOT NULL.
    """
    # Snapshot db + sidecars to a tempdir so we never touch the live DB
    src = Path(db_path)
    snap_dir = Path(tempfile.mkdtemp(prefix="apple-podcasts-snap-"))
    snap_db = snap_dir / src.name
    conn = None
    try:
        for ext in ("", "-wal", "-shm"):
            candidate = Path(str(src) + ext)
            if not candidate.exists():
                continue
            dest = snap_dir / candidate.name
            # Snapshot via `cp -c` — an APFS copy-on-write clone. This is the
            # key fix: a plain data copy (shutil.copy2 / `cp`) of the live
            # MTLibrary.sqlite performs a bulk sequential read, which blocks
            # indefinitely while Apple Podcasts holds the file and macOS's
            # file provider arbitrates access — the importer then appears to
            # hang forever. `clonefile(2)` duplicates block references with no
            # data read, completing instantly, and the resulting clone (a
            # separate inode outside the protected container) reads cleanly.
            # `cp -c` falls back to a normal copy when cloning is unsupported
            # (cross-volume / non-APFS); the timeout below still bounds that
            # case. Run as a killable subprocess so any fallback copy that
            # does stall fails fast with a clear error instead of hanging.
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
                    f"Apple Podcasts DB snapshot timed out after "
                    f"{SNAPSHOT_TIMEOUT_SECONDS}s copying {candidate.name} "
                    f"({size_mb:.0f}MB). The on-device SQLite file is likely "
                    f"I/O-stalled (iCloud sync) or inaccessible."
                ) from exc
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or b"").decode(errors="replace").strip()
                raise SnapshotError(
                    f"Apple Podcasts DB snapshot failed copying "
                    f"{candidate.name}: {stderr[:200]}"
                ) from exc
        conn = sqlite3.connect(snap_db)
        cur = conn.cursor()
        # Filter rationale (refined against real Apple Podcasts data):
        # After an episode completes, ZPLAYSTATE resets to 0, ZHASBEENPLAYED
        # becomes NULL, and ZPLAYHEAD resets to 0. The only durable signals
        # of completion are ZPLAYCOUNT (incremented per completion) and
        # ZLASTDATEPLAYED (the timestamp of the last play). The spec's
        # original ZPLAYSTATE=3 + playhead/duration check missed everything
        # — every episode in a real library matched 0 rows.
        cur.execute("""
            SELECT
              e.ZUUID,
              e.ZLASTDATEPLAYED,
              p.ZTITLE,
              COALESCE(e.ZCLEANEDTITLE, e.ZTITLE),
              e.ZDURATION,
              COALESCE(e.ZPLAYCOUNT, 0)
            FROM ZMTEPISODE e
            JOIN ZMTPODCAST p ON p.Z_PK = e.ZPODCAST
            WHERE COALESCE(e.ZPLAYCOUNT, 0) > 0
              AND COALESCE(e.ZPLAYSTATEMANUALLYSET, 0) = 0
              AND e.ZLASTDATEPLAYED IS NOT NULL
        """)
        for zuuid, mac_last, show_title, ep_title, duration_s, play_count in cur.fetchall():
            unix_last = mac_last + MAC_EPOCH_OFFSET
            end = datetime.fromtimestamp(unix_last, tz=timezone.utc)
            dur_seconds = max(int(duration_s or 1), 1)
            start = end - timedelta(seconds=dur_seconds)
            fp = content_fingerprint("podcast", show=show_title or "", title=ep_title or "")
            yield NormalizedEvent(
                importer="apple-podcasts",
                service="apple-podcasts",
                category="listened",
                note=f"{show_title} – {ep_title}",
                title=show_title,
                start_time=start,
                end_time=end,
                deterministic_id=_det_id(zuuid, mac_last),
                timestamp_confidence="medium",
                external_ids={
                    "zuuid": zuuid,
                    "show": show_title,
                    "episode_title": ep_title,
                    "duration_seconds": duration_s,
                    "play_count": int(play_count),
                    "content_fingerprint": fp,
                    "raw_mac_last_played": mac_last,
                },
            )
    finally:
        if conn is not None:
            conn.close()
        shutil.rmtree(snap_dir, ignore_errors=True)


PODCASTS_REL = Path("Library/Group Containers/243LU875E5.groups.com.apple.podcasts/Documents/MTLibrary.sqlite")


def find_timemachine_snapshots(user_home: Path | None = None) -> list[Path]:
    """Locate MTLibrary.sqlite in every Time Machine backup.

    Uses `tmutil listbackups` to enumerate backup directories. Each backup is
    a snapshot of the entire filesystem rooted at the backup path; we look
    for the podcasts DB under <backup_root>/<user_home>/<PODCASTS_REL> and
    a Macintosh-HD prefix variant for older backup layouts.
    """
    import subprocess
    user_home = user_home or Path.home()
    home_full = str(user_home).lstrip("/")  # e.g. "Users/Scanning"
    # Also try just the last two components (Users/<name>) in case the
    # caller supplied a deeper/sandboxed home path that doesn't actually
    # nest under the backup root.
    parts = user_home.parts
    home_short = "/".join(parts[-2:]) if len(parts) >= 2 else home_full

    try:
        result = subprocess.run(
            ["tmutil", "listbackups"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    backup_roots = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    found: list[Path] = []
    home_relatives = [home_full] if home_full == home_short else [home_full, home_short]
    for root in backup_roots:
        # Common layouts:
        #   <root>/<home_relative>/<PODCASTS_REL>                  (modern)
        #   <root>/Macintosh HD/<home_relative>/<PODCASTS_REL>     (legacy)
        candidates: list[Path] = []
        for hr in home_relatives:
            candidates.append(Path(root) / hr / PODCASTS_REL)
            candidates.append(Path(root) / "Macintosh HD" / hr / PODCASTS_REL)
        for c in candidates:
            if c.exists():
                found.append(c)
                break
    return found
