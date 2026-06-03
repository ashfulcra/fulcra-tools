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
        "override_definition_name", "updated_at",
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
