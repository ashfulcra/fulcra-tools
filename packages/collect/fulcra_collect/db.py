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
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from .config import config_dir

LATEST_VERSION = 5

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
    if current < 3:
        _migration_003_forwarded_attention(conn)
        _record_version(conn, 3)
    if current < 4:
        _migration_004_forwarded_events(conn)
        _record_version(conn, 4)
    if current < 5:
        _migration_005_definition_validated_at(conn)
        _record_version(conn, 5)


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


def _migration_003_forwarded_attention(conn: sqlite3.Connection) -> None:
    """Create the ``forwarded_attention`` dedup table (HISTORICAL — the
    route it served is retired; the table is retained for schema
    compatibility on existing databases).

    The FORMER daemon route ``POST /api/extension/attention`` (removed —
    the Attention extension is now fully relayless and POSTs straight to
    the Fulcra API) forwarded each attention event to Fulcra keyed by a
    DETERMINISTIC ``source_id`` (``com.fulcra.attention.v2.<hash>``).
    Fulcra does NOT dedupe by ``source_id`` at write time — it's a
    query-time hint only — so a re-POSTed event (the extension's
    outbox-flush concurrency bug re-sent the same entries many times)
    created a PERMANENT duplicate in the user's Fulcra account. This table
    was the daemon's memory of which attention ``source_id``s it had
    already forwarded (``source_id`` PRIMARY KEY + ``INSERT OR IGNORE``
    made the check-and-record atomic under a storm of identical concurrent
    POSTs). Migrations are append-only, so it still runs on new
    databases."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS forwarded_attention (
            source_id    TEXT PRIMARY KEY,
            forwarded_at TEXT NOT NULL
        );
        """,
    )


def _migration_004_forwarded_events(conn: sqlite3.Connection) -> None:
    """Generalise the attention dedup table into a per-event dedup keyed on
    an arbitrary ``dedup_key``.

    PR #20 added ``forwarded_attention`` to stop the FORMER attention route
    (retired; see migration 003's note) from re-forwarding a re-POSTed
    event (the extension's then outbox-flush storm). The media import path
    needs the SAME guarantee, keyed on a *set* of dedup keys per event
    (``deterministic_id`` ∪ the ``com.fulcra.content.*`` cross-source
    fingerprints) rather than one attention ``source_id``. This table
    generalises that: any string key, ``INSERT OR IGNORE`` against the
    PRIMARY KEY does the atomic claim under concurrency.

    Migration safety: every existing ``forwarded_attention.source_id`` is
    copied into ``forwarded_events`` so an attention source_id that was
    already forwarded by a pre-#004 daemon is still recognised as a
    duplicate after upgrade — no attention claim is lost. The
    ``forwarded_attention`` table is intentionally left in place for schema
    compatibility (it backed ``claim_attention_source_id``, itself retained
    as historical/compatibility-only alongside the retired route); the copy
    means both tables agree on the historical rows."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS forwarded_events (
            dedup_key    TEXT PRIMARY KEY,
            forwarded_at TEXT NOT NULL
        );
        """,
    )
    # Preserve already-forwarded attention claims: copy each historical
    # source_id into the generalised table so dedup-key consumers still
    # treat it as a duplicate post-upgrade. The
    # forwarded_attention table may not exist on a brand-new db where 003
    # and 004 run back-to-back from migrate() — but 003 always runs first
    # in that loop, so the table is present whenever 004 runs.
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='forwarded_attention'",
    ).fetchone()
    if row is not None:
        conn.execute(
            "INSERT OR IGNORE INTO forwarded_events (dedup_key, forwarded_at) "
            "SELECT source_id, forwarded_at FROM forwarded_attention",
        )


def _migration_005_definition_validated_at(conn: sqlite3.Connection) -> None:
    """Add ``plugin_state.definition_validated_at`` — the last time the
    cached ``definition_id`` was confirmed live on the current Fulcra
    account (ISO-8601 UTC, nullable).

    Before this column every plugin run re-validated its cached def id by
    fetching the ENTIRE annotations catalog (``definition_exists``), i.e.
    one full-catalog GET per plugin per run. The RunContext gate reads
    this watermark and skips re-validation while it is fresh (15-minute
    TTL, 24-hour hard cap). NULL — including every pre-existing row —
    means "never validated", which fails open into a normal validation on
    the next run."""
    conn.execute(
        "ALTER TABLE plugin_state ADD COLUMN definition_validated_at TEXT",
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
                        override_definition_name: str | None,
                        definition_validated_at: str | None = None) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO plugin_state (
            plugin_id, last_run, last_outcome, last_error,
            consecutive_failures, watermark, definition_id,
            override_definition_name, definition_validated_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plugin_id, last_run, last_outcome, last_error,
            int(consecutive_failures), watermark, definition_id,
            override_definition_name, definition_validated_at,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def claim_attention_source_id(conn: sqlite3.Connection,
                              source_id: str) -> bool:
    """Atomically claim an attention ``source_id`` for forwarding
    (HISTORICAL/compatibility-only — its caller, the retired
    ``POST /api/extension/attention`` route, is removed and nothing in
    production calls this; retained alongside the ``forwarded_attention``
    table it backs. New dedup consumers use ``claim_dedup_keys``).

    Returned ``True`` if THIS call inserted the row (the source_id had not
    been forwarded before → the caller forwarded to Fulcra), or ``False``
    if the row already existed (a duplicate → the caller skipped).

    ``INSERT OR IGNORE`` against the ``source_id`` PRIMARY KEY made the
    check-and-record a single atomic statement, so the extension's flush
    storm (many identical source_ids POSTed near-simultaneously) resulted
    in exactly one ``True`` and the rest ``False`` — SQLite serialises the
    inserts and the unique constraint does the dedup, no application-level
    lock. ``cursor.rowcount`` is 1 when a row was actually inserted and 0
    when the IGNORE swallowed a constraint violation."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO forwarded_attention (source_id, forwarded_at) "
        "VALUES (?, ?)",
        (source_id, datetime.now(timezone.utc).isoformat()),
    )
    return cur.rowcount == 1


def claim_dedup_keys(conn: sqlite3.Connection, keys: Iterable[str]) -> bool:
    """Atomically claim a SET of dedup keys for one event.

    An event is identified by its full dedup-key set: its per-source
    ``deterministic_id`` plus any ``com.fulcra.content.*`` cross-source
    fingerprints. Returns ``True`` iff NONE of ``keys`` had been claimed
    before (the event is new → the caller should forward/POST it), or
    ``False`` if ANY key already existed (a same-run or concurrent
    cross-source twin already claimed it → the caller should SKIP). On a
    ``True`` return every key in the set is recorded, so a later twin that
    shares only *one* of the keys still sees a collision.

    Atomicity under concurrency: the whole claim runs inside a single
    ``BEGIN IMMEDIATE`` transaction. ``BEGIN IMMEDIATE`` takes the database's
    write lock up front, so two concurrent claimers are serialised — the
    second blocks (under ``busy_timeout``) until the first commits, then
    sees the first's rows and returns ``False``. This makes "exactly one
    True among N identical concurrent claims" hold even though the decision
    spans multiple INSERTs and a pre-check. We use ``INSERT OR IGNORE`` per
    key and count how many keys were *already* present: if any pre-existed,
    the event is a duplicate.

    An empty key set returns ``True`` (vacuously new) and records nothing —
    callers should never pass an empty set, but failing open here would be
    surprising; failing to a no-op True keeps the "claim then POST" caller
    behaving exactly as the un-claimed path."""
    # Dedupe the incoming keys first: a key repeated WITHIN one event's set
    # (a future list-passing caller) would otherwise hit its own just-inserted
    # row on the second occurrence and be mis-counted as already_present,
    # falsely flagging a brand-new event as a duplicate. set() makes the
    # per-key collision count reflect only PRE-EXISTING rows.
    key_list = list(set(keys))
    now = datetime.now(timezone.utc).isoformat()
    # autocommit (isolation_level=None) connections need an explicit BEGIN to
    # group the inserts; IMMEDIATE grabs the write lock now so concurrent
    # claimers serialise rather than both reading "absent" then both inserting.
    conn.execute("BEGIN IMMEDIATE")
    try:
        already_present = 0
        for key in key_list:
            cur = conn.execute(
                "INSERT OR IGNORE INTO forwarded_events "
                "(dedup_key, forwarded_at) VALUES (?, ?)",
                (key, now),
            )
            if cur.rowcount == 0:
                # IGNORE swallowed a PK collision → this key was already
                # claimed by a previous event.
                already_present += 1
        is_new = already_present == 0
        conn.execute("COMMIT")
        return is_new
    except Exception:
        conn.execute("ROLLBACK")
        raise


def unclaim_dedup_keys(conn: sqlite3.Connection, keys: Iterable[str]) -> None:
    """Release a set of previously-claimed dedup keys (delete their rows from
    ``forwarded_events``) in a single atomic transaction.

    The media import path claims an event's keys BEFORE its batch POST so a
    concurrent run is blocked from writing the same event during the POST
    window. But a media annotation is durable timeline data — if the POST
    then FAILS, the claim must be released, or the event is skipped forever on
    every future run (permanent silent loss). The caller unclaims exactly the
    keys it newly inserted for the failed batch, so on the next run those
    events pass the claim again and get retried. The only cost is a negligible
    re-dup window between the POST failure and the unclaim — far preferable to
    losing the event.

    Deletes are wrapped in one ``BEGIN IMMEDIATE`` transaction for the same
    serialise-the-writers reason ``claim_dedup_keys`` uses. Idempotent: a key
    that isn't present (e.g. another run already re-claimed and committed it)
    is simply a no-op DELETE."""
    key_list = list(set(keys))
    if not key_list:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "DELETE FROM forwarded_events WHERE dedup_key = ?",
            [(k,) for k in key_list],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def all_plugin_ids(conn: sqlite3.Connection) -> list[str]:
    """Every plugin_id with at least one row. Used by the account-switch
    invalidate path that needs to enumerate which plugins have cached
    definition_ids worth clearing."""
    rows = conn.execute("SELECT plugin_id FROM plugin_state").fetchall()
    return [r["plugin_id"] for r in rows]
