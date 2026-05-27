"""SQLite-backed unified state store. Owns the connection lifecycle,
schema migrations, and the low-level row CRUD that ``state.py`` (and the
future per-package state modules) wrap with typed dataclasses.

WAL mode + a fresh connection per process makes the multi-process write
story safe: workers and the daemon main process can update state
concurrently without lost-writes or torn reads. The atomic-write
tempfile+rename idiom (today's JSON pattern) is no longer needed.

Phase 1 owns the ``plugin_state`` table only. Per-package state and
favorites move in later phases.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import config_dir

LATEST_VERSION = 2

_LOG = logging.getLogger("fulcra_collect.db")

# One connection per (process, thread). SQLite connection objects are
# not safe to share across threads by default, and we want each worker
# subprocess to get its own connection too (subprocess fork resets
# thread-locals, so this falls out naturally).
_tls = threading.local()

# Serialises the FIRST connection's init on a fresh database. The
# ``PRAGMA journal_mode=WAL`` switchover takes an EXCLUSIVE lock on the
# database file, and SQLite has a well-known quirk where SQLITE_BUSY
# during that switchover does NOT invoke the busy_handler — so
# ``busy_timeout`` is ignored and the loser of a race immediately gets
# ``sqlite3.OperationalError: database is locked``. The lock also
# serialises ``migrate()``, which writes the initial ``schema_version``
# row and would PRIMARY-KEY-collide if two threads ran the same
# migration concurrently. Once the db is already in WAL mode subsequent
# ``PRAGMA journal_mode=WAL`` calls are true no-ops with no lock
# contention, so this lock is only ever briefly contested on first boot.
_init_lock = threading.Lock()


def default_path() -> Path:
    """Where the unified state db lives. Honours ``FULCRA_COLLECT_HOME``
    via the existing ``config_dir()`` helper."""
    return config_dir() / "state.db"


def open(path: Path | None = None) -> sqlite3.Connection:  # noqa: A001
    """Open (or return the cached) connection for this thread. Runs
    ``migrate()`` once on first open for the (path, thread) pair.

    Idempotent — callers can open() per-process or share via the
    thread-local cache; both are safe."""
    target = path if path is not None else default_path()
    target_key = str(target.resolve() if target.exists() else target)

    cache = getattr(_tls, "conns", None)
    if cache is None:
        cache = {}
        _tls.conns = cache
    cached = cache.get(target_key)
    if cached is not None:
        return cached

    target.parent.mkdir(parents=True, exist_ok=True)

    # Serialise the actual connection init across threads in this
    # process. See the ``_init_lock`` comment for why this is required
    # (PRAGMA journal_mode=WAL race + migrate() row-collisions).
    with _init_lock:
        # isolation_level=None → autocommit mode, which is what we want
        # with WAL: every statement is its own transaction unless wrapped
        # in BEGIN/COMMIT. This matches the per-statement update pattern
        # in state.save() and avoids the implicit-transaction quirks of
        # sqlite3's default mode.
        conn = sqlite3.connect(
            str(target), isolation_level=None, check_same_thread=True,
        )
        conn.row_factory = sqlite3.Row
        # busy_timeout still earns its keep on every-day contention
        # (concurrent writers, checkpointers) even though it cannot
        # rescue us from the journal-mode switchover race — that race
        # is handled by ``_init_lock`` above.
        conn.execute("PRAGMA busy_timeout=5000")  # 5s on contention
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")

        # Tighten file perms (db + WAL sidecars) to owner-only, matching
        # the restrictive mode the JSON state files used. Best-effort:
        # on a fresh create the sidecars may not exist yet, in which
        # case chmod errors are silently ignored.
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass

        migrate(conn)

    cache[target_key] = conn
    return conn


def close_all() -> None:
    """Close every cached connection for this thread. Primarily for
    tests that swap the active config_dir between cases."""
    cache = getattr(_tls, "conns", None)
    if not cache:
        return
    for conn in cache.values():
        try:
            conn.close()
        except Exception:
            pass
    _tls.conns = {}


# ---- migrations ------------------------------------------------------


def _current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied schema_version, or 0 if the table
    doesn't exist yet (fresh db)."""
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='schema_version'",
    ).fetchone()
    if row is None:
        return 0
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM schema_version",
    ).fetchone()
    return int(row["v"])


def _record_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
        (version, datetime.now(timezone.utc).isoformat()),
    )


def migrate(conn: sqlite3.Connection) -> None:
    """Apply migrations 1..LATEST_VERSION as needed. No-op if already at
    latest. Refuses to run when the db is at a version > LATEST — the
    user is running an older daemon binary against a db that a newer
    binary populated, and silently dropping rows would be worse than
    a clear error."""
    current = _current_version(conn)
    if current > LATEST_VERSION:
        raise RuntimeError(
            f"state.db schema version {current} is newer than this "
            f"binary supports (max {LATEST_VERSION}). Either upgrade "
            f"fulcra-collect or restore a backup of state.db.",
        )
    if current < 1:
        _migration_001_initial(conn)
        _record_version(conn, 1)
    if current < 2:
        _migration_002_import_plugin_state_json(conn)
        _record_version(conn, 2)


def _migration_001_initial(conn: sqlite3.Connection) -> None:
    """Create the Phase-1 tables: plugin_state + schema_version."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plugin_state (
            plugin_id                 TEXT PRIMARY KEY,
            last_run                  TEXT,
            last_outcome              TEXT,
            last_error                TEXT,
            consecutive_failures      INTEGER NOT NULL DEFAULT 0,
            watermark                 TEXT,
            definition_id             TEXT,
            override_definition_name  TEXT,
            updated_at                TEXT NOT NULL
        );
        """,
    )


def _migration_002_import_plugin_state_json(conn: sqlite3.Connection) -> None:
    """One-shot import of the legacy ``state/<plugin_id>.json`` files into
    the ``plugin_state`` table. After a successful row insert the source
    file is renamed to ``<name>.json.migrated`` — kept on disk for a
    soak period so a botched migration can be recovered by hand.

    A malformed or unreadable file is skipped with a warning rather than
    failing the whole migration: a daemon that refuses to start because
    a single corrupted state file slipped through would be much worse
    than losing one plugin's failure-count history."""
    legacy_dir = config_dir() / "state"
    if not legacy_dir.exists():
        return  # fresh install — nothing to import
    for path in sorted(legacy_dir.glob("*.json")):
        # *.json.migrated would re-trip the glob if Python's matcher
        # ever evolves, but the explicit guard makes the intent obvious.
        if path.name.endswith(".migrated"):
            continue
        plugin_id = path.stem
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning(
                "migration: skipping unreadable state file %s (%s)",
                path, exc,
            )
            continue
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO plugin_state (
                    plugin_id, last_run, last_outcome, last_error,
                    consecutive_failures, watermark, definition_id,
                    override_definition_name, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plugin_id,
                    doc.get("last_run"),
                    doc.get("last_outcome"),
                    doc.get("last_error"),
                    int(doc.get("consecutive_failures", 0) or 0),
                    doc.get("watermark"),
                    doc.get("definition_id"),
                    doc.get("override_definition_name"),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        except sqlite3.Error as exc:
            _LOG.warning(
                "migration: skipping %s — insert failed (%s)", path, exc,
            )
            continue
        # Rename only after the insert succeeded so we don't lose data
        # to a half-applied migration.
        try:
            path.rename(path.with_suffix(".json.migrated"))
        except OSError as exc:
            _LOG.warning(
                "migration: imported %s but couldn't rename it (%s); "
                "re-running migration will be a no-op for this row but "
                "the legacy file will keep reappearing in the glob",
                path, exc,
            )


# ---- low-level CRUD used by state.py --------------------------------


def fetch_plugin_state(conn: sqlite3.Connection,
                       plugin_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM plugin_state WHERE plugin_id = ?",
        (plugin_id,),
    ).fetchone()


def upsert_plugin_state(conn: sqlite3.Connection, *, plugin_id: str,
                        last_run: str | None,
                        last_outcome: str | None,
                        last_error: str | None,
                        consecutive_failures: int,
                        watermark: str | None,
                        definition_id: str | None,
                        override_definition_name: str | None) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO plugin_state (
            plugin_id, last_run, last_outcome, last_error,
            consecutive_failures, watermark, definition_id,
            override_definition_name, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plugin_id, last_run, last_outcome, last_error,
            int(consecutive_failures), watermark, definition_id,
            override_definition_name,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def all_plugin_ids(conn: sqlite3.Connection) -> list[str]:
    """Every plugin_id with at least one row. Used by the account-switch
    invalidate path that needs to enumerate which plugins have cached
    definition_ids worth clearing."""
    rows = conn.execute("SELECT plugin_id FROM plugin_state").fetchall()
    return [r["plugin_id"] for r in rows]
