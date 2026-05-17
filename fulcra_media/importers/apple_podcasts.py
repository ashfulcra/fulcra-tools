"""Apple Podcasts importer — reads macOS MTLibrary.sqlite."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .base import NormalizedEvent, content_fingerprint

DEFAULT_DB_PATH = Path(os.path.expanduser(
    "~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts/Documents/MTLibrary.sqlite"
))
MAC_EPOCH_OFFSET = 978307200  # seconds between Unix epoch and Core Data epoch (2001-01-01 UTC)


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
            if candidate.exists():
                shutil.copy2(candidate, snap_dir / candidate.name)
        conn = sqlite3.connect(snap_db)
        cur = conn.cursor()
        cur.execute("""
            SELECT
              e.ZUUID,
              e.ZLASTDATEPLAYED,
              p.ZTITLE,
              COALESCE(e.ZCLEANEDTITLE, e.ZTITLE),
              e.ZDURATION
            FROM ZMTEPISODE e
            JOIN ZMTPODCAST p ON p.Z_PK = e.ZPODCAST
            WHERE e.ZPLAYSTATE = 3
              AND e.ZHASBEENPLAYED = 1
              AND COALESCE(e.ZPLAYSTATEMANUALLYSET, 0) = 0
              AND (e.ZPLAYHEAD * 1.0 / NULLIF(e.ZDURATION, 0)) > 0.9
              AND e.ZLASTDATEPLAYED IS NOT NULL
        """)
        for zuuid, mac_last, show_title, ep_title, duration_s in cur.fetchall():
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
                    "content_fingerprint": fp,
                    "raw_mac_last_played": mac_last,
                },
            )
    finally:
        if conn is not None:
            conn.close()
        shutil.rmtree(snap_dir, ignore_errors=True)
