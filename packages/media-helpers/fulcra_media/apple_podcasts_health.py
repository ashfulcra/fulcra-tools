"""Apple Podcasts health check for the fulcra-collect daemon.

Called by the daemon's /api/plugin/apple-podcasts/health_check route to
verify the on-device Podcasts SQLite database is readable and to report
how many played episodes will be imported on the next scheduled run.

Mirrors the shape of lastfm_health.py / trakt_health.py — same return
contract (HealthResult), same defensive try/except taxonomy. The
wizard's generic test_connection renderer surfaces `summary` in its
success banner and `preview` as a small bullet list, which lets the
user sanity-check that they're seeing their own data before clicking
Next.

Importantly, the COUNT(*) below mirrors the WHERE clause used by
``fulcra_media.importers.apple_podcasts.parse_db`` so the number the
wizard shows matches what the next scheduled run will actually import.
If you change one, change the other.
"""
from __future__ import annotations

import glob
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fulcra_collect.plugin import HealthResult

from .importers.apple_podcasts import MAC_EPOCH_OFFSET

# The same glob pattern the permission_check uses — keep them in sync. We
# don't import it from collect_plugins.py to avoid a circular import (this
# module is imported by collect_plugins.py).
_DB_GLOB = str(
    Path.home() / "Library/Group Containers/*.podcasts*/Documents/MTLibrary.sqlite"
)


def _resolve_db_path(ctx) -> str | None:
    """Resolve which on-device DB file we'd open.

    Honors an explicit ``db_path`` config override (used by tests and by
    advanced users pointing at a Time-Machine-extracted snapshot); falls
    back to the same glob the permission_check uses for the real path.
    Returns None when no file is found.
    """
    raw = ctx.config.get("db_path") if hasattr(ctx, "config") else None
    if raw:
        return raw if Path(raw).exists() else None
    candidates = glob.glob(_DB_GLOB)
    return candidates[0] if candidates else None


def apple_podcasts_health_check(ctx) -> HealthResult:
    """Open the Podcasts SQLite DB read-only and count played episodes.

    Returns HealthResult(ok=True) when the DB opens and the COUNT query
    returns — even when the count is zero (that's a friendly nudge, not
    an error). Returns ok=False when no DB file is found, when sqlite
    refuses to open it (typically Full Disk Access not granted), or when
    any other exception surfaces.

    The preview lists the three most recently played episodes so the
    user can confirm they're looking at their own library and not a
    stale snapshot.
    """
    db_path = _resolve_db_path(ctx)
    if not db_path:
        return HealthResult(
            ok=False,
            summary=(
                "No Podcasts database found. Apple Podcasts may not be "
                "installed or has never run on this Mac."
            ),
        )

    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=2.0
        )
        try:
            cur = conn.cursor()
            # Same WHERE as parse_db so the count matches what Run will
            # import. See the comment block in importers/apple_podcasts.py
            # for why ZPLAYCOUNT > 0 is the durable completion signal.
            cur.execute("""
                SELECT COUNT(*)
                FROM ZMTEPISODE
                WHERE COALESCE(ZPLAYCOUNT, 0) > 0
                  AND COALESCE(ZPLAYSTATEMANUALLYSET, 0) = 0
                  AND ZLASTDATEPLAYED IS NOT NULL
            """)
            (count,) = cur.fetchone()

            if count == 0:
                return HealthResult(
                    ok=True,
                    summary=(
                        "No played episodes yet — listen to one in "
                        "Apple Podcasts then come back."
                    ),
                )

            # Preview the three most recent so the user can confirm
            # they're seeing their own library. Same join shape as
            # parse_db; we just LIMIT + ORDER instead of streaming.
            cur.execute("""
                SELECT
                  p.ZTITLE,
                  COALESCE(e.ZCLEANEDTITLE, e.ZTITLE),
                  e.ZLASTDATEPLAYED
                FROM ZMTEPISODE e
                JOIN ZMTPODCAST p ON p.Z_PK = e.ZPODCAST
                WHERE COALESCE(e.ZPLAYCOUNT, 0) > 0
                  AND COALESCE(e.ZPLAYSTATEMANUALLYSET, 0) = 0
                  AND e.ZLASTDATEPLAYED IS NOT NULL
                ORDER BY e.ZLASTDATEPLAYED DESC
                LIMIT 3
            """)
            preview = []
            for show, episode, mac_last in cur.fetchall():
                watched_at = ""
                if mac_last is not None:
                    try:
                        watched_at = datetime.fromtimestamp(
                            float(mac_last) + MAC_EPOCH_OFFSET,
                            tz=timezone.utc,
                        ).isoformat()
                    except (TypeError, ValueError, OverflowError):
                        watched_at = ""
                preview.append({
                    "title": f"{show or '?'} — {episode or '?'}",
                    "watched_at": watched_at,
                })

            return HealthResult(
                ok=True,
                summary=(
                    f"Found {count} played episode"
                    f"{'s' if count != 1 else ''} ready to import."
                ),
                preview=preview,
            )
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if (
            "permission" in msg
            or "unable to open" in msg
            or "authorization denied" in msg
        ):
            return HealthResult(
                ok=False,
                summary=(
                    "Can't open the Podcasts database. Grant Full Disk "
                    "Access to the terminal running fulcra-collect in "
                    "System Settings -> Privacy & Security -> Full Disk "
                    "Access, then try again."
                ),
            )
        return HealthResult(
            ok=False,
            summary=f"sqlite error opening the Podcasts database: {exc}",
        )
    except Exception as exc:
        return HealthResult(
            ok=False,
            summary=f"Unexpected: {type(exc).__name__}: {exc}",
        )
