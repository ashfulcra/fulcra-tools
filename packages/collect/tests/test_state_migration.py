"""Phase 1 of refactor #1 (#67): one-shot JSON → SQLite migration.

Synthesises pre-Phase-1 ``state/<plugin_id>.json`` files in a tmp
config dir, opens db, and asserts the rows land in plugin_state. Also
covers idempotency (re-running the migration doesn't double-import) and
malformed-file tolerance (the daemon must still start)."""
from __future__ import annotations

import json

from fulcra_collect import db, state


def _write_legacy_state(collect_home, plugin_id: str, payload: dict) -> None:
    """Drop a legacy state/<plugin_id>.json file in the right place."""
    state_dir = collect_home / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / f"{plugin_id}.json").write_text(json.dumps(payload))


def test_legacy_json_is_imported_into_plugin_state_on_first_open(
        collect_home):
    _write_legacy_state(collect_home, "lastfm", {
        "plugin_id": "lastfm",
        "last_run": "2026-05-23T10:00:00+00:00",
        "last_outcome": "done",
        "last_error": None,
        "consecutive_failures": 0,
        "watermark": "2026-05-23T09:59:00+00:00",
        "definition_id": "def-uuid-from-json",
        "override_definition_name": None,
    })
    _write_legacy_state(collect_home, "trakt", {
        "plugin_id": "trakt",
        "last_run": "2026-05-23T11:00:00+00:00",
        "last_outcome": "error",
        "last_error": "boom",
        "consecutive_failures": 2,
        "watermark": None,
        "definition_id": None,
    })

    st_lastfm = state.load("lastfm")
    assert st_lastfm.last_outcome == "done"
    assert st_lastfm.definition_id == "def-uuid-from-json"
    assert st_lastfm.watermark == "2026-05-23T09:59:00+00:00"

    st_trakt = state.load("trakt")
    assert st_trakt.last_outcome == "error"
    assert st_trakt.last_error == "boom"
    assert st_trakt.consecutive_failures == 2

    # Source files renamed → never imported a second time.
    state_dir = collect_home / "state"
    assert not (state_dir / "lastfm.json").exists()
    assert not (state_dir / "trakt.json").exists()
    assert (state_dir / "lastfm.json.migrated").exists()
    assert (state_dir / "trakt.json.migrated").exists()


def test_migration_is_idempotent_on_a_clean_db(collect_home):
    """Re-running migrate() on an up-to-date db is a no-op (the
    schema_version check short-circuits before touching the files)."""
    _write_legacy_state(collect_home, "lastfm", {
        "plugin_id": "lastfm",
        "last_run": "2026-05-23T10:00:00+00:00",
        "last_outcome": "done",
        "consecutive_failures": 0,
    })
    conn = db.open()  # first run: imports + records version
    # Manually invoke a second time — must not double-record schema
    # versions or re-walk the (now-renamed) JSON files.
    db.migrate(conn)
    rows = conn.execute(
        "SELECT version, COUNT(*) AS c FROM schema_version GROUP BY version",
    ).fetchall()
    for r in rows:
        assert r["c"] == 1


def test_malformed_json_file_is_skipped_with_a_warning(collect_home, caplog):
    """A torn/corrupt legacy file must not stop the daemon from
    starting — that would be a much worse failure mode than losing the
    failure-count history for that one plugin."""
    state_dir = collect_home / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "broken.json").write_text("{ not json")
    _write_legacy_state(collect_home, "good", {
        "plugin_id": "good",
        "last_run": "2026-05-23T10:00:00+00:00",
        "last_outcome": "done",
        "consecutive_failures": 0,
    })

    with caplog.at_level("WARNING", logger="fulcra_collect.db"):
        # Migration runs as a side-effect of opening the db.
        state.load("good")

    # The good file landed.
    assert state.load("good").last_outcome == "done"
    # And the bad file is still on disk (we didn't rename it because
    # we never imported it) — but the daemon kept going.
    assert (state_dir / "broken.json").exists()
    assert any("broken" in r.getMessage() for r in caplog.records)


def test_migration_on_fresh_install_is_a_no_op(collect_home):
    """No legacy state/ directory → no work, no errors. The daemon's
    first-run path must not require a pre-existing files tree."""
    # No state/ dir created. Open should succeed and create an empty
    # plugin_state table.
    conn = db.open()
    rows = conn.execute("SELECT * FROM plugin_state").fetchall()
    assert rows == []
