"""Per-plugin persisted state — last run, last outcome, failure count,
the plugin's own watermark string, and the cached Fulcra definition id.

Phase 1 of refactor #1 (task #67) moved the backing store from one JSON
file per plugin (under ``~/.config/fulcra-collect/state/<id>.json``) to a
single SQLite database at ``~/.config/fulcra-collect/state.db``. The
public surface is unchanged: ``PluginState``, ``load(plugin_id)`` and
``save(state)`` still behave the same way. The atomic-write tempfile
idiom is gone — WAL-mode SQLite gives us safe concurrent writes for
free, including across worker subprocesses.

A one-shot import in ``db.py:_migration_002_import_plugin_state_json``
moves any pre-existing JSON files into the table on first daemon boot
and renames them to ``*.json.migrated`` so we can recover them by hand
during the soak period.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import db


@dataclass
class PluginState:
    plugin_id: str
    last_run: datetime | None = None
    last_outcome: str | None = None      # "done" | "error" | "timeout"
    last_error: str | None = None
    consecutive_failures: int = 0
    watermark: str | None = None         # ISO string, plugin-defined
    definition_id: str | None = None     # adopted-by-resolver Fulcra def id
    # When set, the next resolve will use this exact name (verbatim, no
    # machine-id suffix) instead of the plugin's canonical_definition_name.
    # Set by the web UI's definition picker when the user types a custom
    # name for "Create new"; cleared once the resolver has used it.
    override_definition_name: str | None = None

    def record_finish(self, *, outcome: str, when: datetime,
                       error: str | None = None) -> None:
        """Record a finished run. A non-"done" outcome increments the
        consecutive-failure count; "done" resets it."""
        self.last_run = when
        self.last_outcome = outcome
        self.last_error = error
        if outcome == "done":
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1


def load(plugin_id: str) -> PluginState:
    """Read this plugin's row out of the unified state db. A missing
    row (the plugin has never run) returns a fresh PluginState — same
    semantics as the legacy "file doesn't exist" branch."""
    conn = db.open()
    row = db.fetch_plugin_state(conn, plugin_id)
    if row is None:
        return PluginState(plugin_id=plugin_id)
    last_run_str = row["last_run"]
    last_run: datetime | None
    try:
        last_run = datetime.fromisoformat(last_run_str) if last_run_str else None
    except (TypeError, ValueError):
        # A row with a malformed timestamp shouldn't crash the daemon
        # loop. Mirrors the corrupt-file fallback the JSON loader used
        # to provide.
        last_run = None
    return PluginState(
        plugin_id=plugin_id,
        last_run=last_run,
        last_outcome=row["last_outcome"],
        last_error=row["last_error"],
        consecutive_failures=int(row["consecutive_failures"] or 0),
        watermark=row["watermark"],
        definition_id=row["definition_id"],
        override_definition_name=row["override_definition_name"],
    )


def save(st: PluginState) -> None:
    """Persist ``st`` via an INSERT OR REPLACE. WAL mode makes this
    safe under concurrent writers (the daemon main process + N worker
    subprocesses), so no tempfile-and-rename dance is needed."""
    conn = db.open()
    db.upsert_plugin_state(
        conn,
        plugin_id=st.plugin_id,
        last_run=st.last_run.isoformat() if st.last_run else None,
        last_outcome=st.last_outcome,
        last_error=st.last_error,
        consecutive_failures=st.consecutive_failures,
        watermark=st.watermark,
        definition_id=st.definition_id,
        override_definition_name=st.override_definition_name,
    )
