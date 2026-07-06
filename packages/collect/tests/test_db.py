"""Phase 1 of refactor #1 (#67): the SQLite-backed unified state store.

These tests pin the connection / migration contract — schema creation
on first open, migration idempotency, WAL mode, and the downgrade
guard. Higher-level round-trip behaviour is covered in test_state.py.
"""
from __future__ import annotations

import sqlite3

import pytest

from fulcra_collect import db


def test_open_creates_db_file_and_applies_migrations(collect_home):
    db_path = collect_home / "state.db"
    assert not db_path.exists()
    conn = db.open()
    assert db_path.exists()
    # All migrations should be applied — schema_version row at LATEST.
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM schema_version",
    ).fetchone()
    assert row["v"] == db.LATEST_VERSION


def test_open_is_idempotent(collect_home):
    """Second open() on the same thread returns the cached connection
    (object identity) and does not re-apply migrations."""
    conn1 = db.open()
    conn2 = db.open()
    assert conn1 is conn2
    # Migrations recorded exactly once per version, not twice.
    rows = conn1.execute(
        "SELECT version, COUNT(*) AS c FROM schema_version GROUP BY version",
    ).fetchall()
    for r in rows:
        assert r["c"] == 1, f"version {r['version']} applied {r['c']} times"


def test_wal_mode_is_enabled(collect_home):
    conn = db.open()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_migrate_refuses_when_db_is_newer_than_binary(collect_home):
    """Older daemon binary against a db a newer binary wrote: refuse
    rather than silently dropping rows the binary doesn't understand."""
    conn = db.open()
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
        (db.LATEST_VERSION + 1, "2999-01-01T00:00:00+00:00"),
    )
    with pytest.raises(RuntimeError, match="newer than this binary"):
        db.migrate(conn)


def test_plugin_state_table_has_the_phase1_columns(collect_home):
    """Pins the column set so a future column-add migration has to bump
    LATEST_VERSION rather than silently changing the schema."""
    conn = db.open()
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(plugin_state)").fetchall()
    }
    assert cols == {
        "plugin_id", "last_run", "last_outcome", "last_error",
        "consecutive_failures", "watermark", "definition_id",
        "override_definition_name", "definition_validated_at", "updated_at",
    }


def test_migrate_on_a_blank_connection_runs_all_migrations(tmp_path):
    """Direct-connection path (no db.open cache): a brand-new sqlite
    connection runs every migration in order."""
    path = tmp_path / "fresh.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    versions = [
        r["version"]
        for r in conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
    ]
    assert versions == list(range(1, db.LATEST_VERSION + 1))


def test_forwarded_attention_table_has_the_dedup_columns(collect_home):
    """Migration 003 creates the attention-dedup table with source_id as
    the PRIMARY KEY (the constraint that makes INSERT OR IGNORE atomic)."""
    conn = db.open()
    cols = {
        r["name"]
        for r in conn.execute(
            "PRAGMA table_info(forwarded_attention)"
        ).fetchall()
    }
    assert cols == {"source_id", "forwarded_at"}
    pk = [
        r["name"]
        for r in conn.execute(
            "PRAGMA table_info(forwarded_attention)"
        ).fetchall()
        if r["pk"]
    ]
    assert pk == ["source_id"]


def test_claim_attention_source_id_is_idempotent(collect_home):
    """First claim of a source_id returns True (forward it); every repeat
    returns False (skip the duplicate). Exactly one row persists."""
    conn = db.open()
    assert db.claim_attention_source_id(conn, "com.fulcra.attention.v2.abc") is True
    assert db.claim_attention_source_id(conn, "com.fulcra.attention.v2.abc") is False
    assert db.claim_attention_source_id(conn, "com.fulcra.attention.v2.abc") is False
    # A different source_id is independently claimable.
    assert db.claim_attention_source_id(conn, "com.fulcra.attention.v2.xyz") is True
    rows = conn.execute(
        "SELECT source_id FROM forwarded_attention ORDER BY source_id"
    ).fetchall()
    assert [r["source_id"] for r in rows] == [
        "com.fulcra.attention.v2.abc",
        "com.fulcra.attention.v2.xyz",
    ]


# ---- forwarded_events / claim_dedup_keys (component 3) ---------------


def test_forwarded_events_table_has_the_dedup_columns(collect_home):
    """Migration 004 creates the generalised dedup table with dedup_key as
    the PRIMARY KEY (the constraint that makes INSERT OR IGNORE atomic)."""
    conn = db.open()
    cols = {
        r["name"]
        for r in conn.execute(
            "PRAGMA table_info(forwarded_events)"
        ).fetchall()
    }
    assert cols == {"dedup_key", "forwarded_at"}
    pk = [
        r["name"]
        for r in conn.execute("PRAGMA table_info(forwarded_events)").fetchall()
        if r["pk"]
    ]
    assert pk == ["dedup_key"]


def test_claim_dedup_keys_new_keys_returns_true_and_records_all(collect_home):
    """A brand-new key set → True, and every key is recorded so a later
    twin sharing only one key still collides."""
    conn = db.open()
    keys = {"com.fulcra.media.netflix.aaa", "com.fulcra.content.tv.v1.bbb"}
    assert db.claim_dedup_keys(conn, keys) is True
    recorded = {
        r["dedup_key"]
        for r in conn.execute("SELECT dedup_key FROM forwarded_events").fetchall()
    }
    assert keys <= recorded


def test_claim_dedup_keys_any_present_returns_false(collect_home):
    """If ANY key in the set was already claimed, the event is a duplicate
    → False (skip). A cross-source twin shares only the content fingerprint
    but must still be rejected."""
    conn = db.open()
    assert db.claim_dedup_keys(
        conn, {"det-A", "com.fulcra.content.music.v1.shared"}
    ) is True
    # Twin: different per-source deterministic_id, SAME content fingerprint.
    assert db.claim_dedup_keys(
        conn, {"det-B", "com.fulcra.content.music.v1.shared"}
    ) is False


def test_claim_dedup_keys_empty_set_is_vacuously_new(collect_home):
    conn = db.open()
    assert db.claim_dedup_keys(conn, set()) is True


def test_claim_dedup_keys_exactly_one_true_under_concurrency(collect_home):
    """N threads claim the SAME key set simultaneously → exactly one True."""
    import threading

    from fulcra_collect import config as _config

    home = _config.config_dir()
    keys = {"det-concurrent", "com.fulcra.content.movie.v1.x"}
    N = 25
    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(N)

    def _claim():
        # Each thread opens its own connection to the SAME db file (the
        # thread-local cache means we can't share one connection across
        # threads). This mirrors the worker-subprocess reality.
        conn = db.open(home / "state.db")
        barrier.wait()
        ok = db.claim_dedup_keys(conn, keys)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=_claim) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r) == 1, results


def test_claim_dedup_keys_persists_across_reopen(collect_home):
    """Once recorded, a claim survives a db connection drop + reopen."""
    conn = db.open()
    assert db.claim_dedup_keys(conn, {"persist-key"}) is True
    db.close_all()
    conn2 = db.open()
    assert db.claim_dedup_keys(conn2, {"persist-key"}) is False


def test_migration_004_preserves_existing_forwarded_attention_rows(tmp_path):
    """Upgrade safety: a db that already has forwarded_attention rows (from a
    pre-#004 daemon) carries them into forwarded_events so an attention
    source_id already forwarded is still recognised as a duplicate."""
    path = tmp_path / "upgrade.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Bring the db up to version 3 only (pre-generalisation).
    db._migration_001_initial(conn)
    db._record_version(conn, 1)
    db._migration_002_import_plugin_state_json(conn)
    db._record_version(conn, 2)
    db._migration_003_forwarded_attention(conn)
    db._record_version(conn, 3)
    conn.execute(
        "INSERT INTO forwarded_attention (source_id, forwarded_at) VALUES (?, ?)",
        ("com.fulcra.attention.v2.legacy", "2025-01-01T00:00:00+00:00"),
    )
    # Now run the rest of the migrations (004).
    db.migrate(conn)
    recorded = {
        r["dedup_key"]
        for r in conn.execute("SELECT dedup_key FROM forwarded_events").fetchall()
    }
    assert "com.fulcra.attention.v2.legacy" in recorded
    # And a re-claim of that legacy key is correctly rejected.
    assert db.claim_dedup_keys(conn, {"com.fulcra.attention.v2.legacy"}) is False


def test_unclaim_dedup_keys_releases_so_a_reclaim_succeeds(collect_home):
    """unclaim deletes the rows, so a key that was claimed can be claimed
    again afterwards (the POST-failure retry path)."""
    conn = db.open()
    keys = {"det-X", "com.fulcra.content.tv.v1.zzz"}
    assert db.claim_dedup_keys(conn, keys) is True
    # Re-claim now blocked.
    assert db.claim_dedup_keys(conn, keys) is False
    # Release, then it's claimable again.
    db.unclaim_dedup_keys(conn, keys)
    rows = {
        r["dedup_key"]
        for r in conn.execute("SELECT dedup_key FROM forwarded_events").fetchall()
    }
    assert not (keys & rows)
    assert db.claim_dedup_keys(conn, keys) is True


def test_unclaim_dedup_keys_only_deletes_named_keys(collect_home):
    """unclaim is scoped: it removes exactly the keys passed, leaving other
    claimed rows (e.g. a sibling batch's) intact."""
    conn = db.open()
    assert db.claim_dedup_keys(conn, {"keep-1"}) is True
    assert db.claim_dedup_keys(conn, {"drop-1", "drop-2"}) is True
    db.unclaim_dedup_keys(conn, {"drop-1", "drop-2"})
    remaining = {
        r["dedup_key"]
        for r in conn.execute("SELECT dedup_key FROM forwarded_events").fetchall()
    }
    assert "keep-1" in remaining
    assert "drop-1" not in remaining
    assert "drop-2" not in remaining


def test_unclaim_dedup_keys_empty_and_missing_are_noops(collect_home):
    conn = db.open()
    db.unclaim_dedup_keys(conn, set())          # empty → no-op
    db.unclaim_dedup_keys(conn, {"never-claimed"})  # absent → no-op, no error


def test_claim_dedup_keys_repeated_key_in_one_set_is_not_self_duplicate(
        collect_home):
    """Defensive: a key repeated WITHIN one event's set (a list-passing
    caller) must not be mis-flagged as already-present — the event is new."""
    conn = db.open()
    assert db.claim_dedup_keys(conn, ["dup", "dup", "other"]) is True


def test_migration_005_adds_definition_validated_at_to_existing_dbs(tmp_path):
    """Upgrade safety: a version-4 db (pre-gate) gains the
    definition_validated_at column via ALTER, and pre-existing rows read
    back as NULL — 'never validated', which fails open into a normal
    validation on the next run."""
    path = tmp_path / "upgrade5.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    db._migration_001_initial(conn)
    db._record_version(conn, 1)
    db._migration_002_import_plugin_state_json(conn)
    db._record_version(conn, 2)
    db._migration_003_forwarded_attention(conn)
    db._record_version(conn, 3)
    db._migration_004_forwarded_events(conn)
    db._record_version(conn, 4)
    conn.execute(
        "INSERT INTO plugin_state (plugin_id, consecutive_failures, updated_at) "
        "VALUES ('legacy', 0, '2026-01-01T00:00:00+00:00')",
    )
    db.migrate(conn)
    row = db.fetch_plugin_state(conn, "legacy")
    assert row["definition_validated_at"] is None
