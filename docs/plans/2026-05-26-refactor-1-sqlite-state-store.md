# Refactor 1: SQLite-backed unified state store

**Task:** to-be-filed (use #67)
**Priority:** highest of the three refactors
**Why now:** removes the per-plugin vs per-package state-store fork that has caused real user-visible bugs (#29), eliminates the unprotected multi-process write risk, and gives us schema migrations for free.

## The current problem

Today we have **two parallel state stores**, plus an ad-hoc third for favorites/auth-fingerprint/config:

1. **Per-plugin state** at `~/.config/fulcra-collect/state/<plugin_id>.json` — written by `runner.py` (in the worker subprocess) and by `daemon.py` (in the main process). 6 fields per file: `plugin_id`, `last_run`, `consecutive_failures`, `watermark`, `last_outcome`, `last_error`, `definition_id`, `override_definition_name`.

2. **Per-package state** at `~/.config/fulcra-{attention,media,dayone}/state.json` — written by importer code directly. Holds package-specific data: media has `watched_definition_id`/`listened_definition_id`/`read_definition_id` + `tag_ids` dict; attention has `attention_definition_id` + `tag_ids` + `watermarks` per client; dayone similarly.

3. **Ad-hoc files**:
   - `~/.config/fulcra-collect/quick_record_favorites.json` (Sprint #64)
   - `~/.config/fulcra-collect/auth-fingerprint` (account-switch detection)
   - `~/.config/fulcra-collect/web-token`, `~/.config/fulcra-collect/web-url`
   - `~/.config/fulcra-collect/config.toml`

### Concrete pain this has caused

- **#29:** wizard's `definition_picker` writes per-plugin state (`<id>.json:definition_id`); Attention extension reads per-package state (`fulcra-attention/state.json:attention_definition_id`). The dashboard said "Attention is set" because the wizard had succeeded; the extension said "no definition" because it was reading a different file. Patched with lazy-migration in the extension route. Underlying fork still there.
- **Multi-process write risk:** runner.py (worker subprocess) writes plugin state; daemon (main process) writes plugin state. No file locking. Atomic-write via tempfile+rename minimizes the window but doesn't eliminate concurrent-update lost-write. We've been lucky.
- **No versioning:** when I added `override_definition_name` to `PluginState` today, every existing serialized file had to gracefully handle a missing key. `state.load()` uses `doc.get(key, default)` which works but is fragile — a typo would silently swallow data.
- **No multi-row queries:** dashboard wants "all plugins' status for display". Today: `for pid in registry.plugins: state.load(pid)` — N file reads. SQLite: one SELECT.
- **Cross-store consistency:** soft-deleting a definition from Settings has to walk `daemon.registry.plugins`, load each per-plugin state, check `definition_id`, clear, save. PLUS the per-package states have their own `*_definition_id` fields that also need clearing — currently not done, see TODO in `delete_definition_route`. SQLite: one UPDATE with a WHERE clause.

## The proposed shape

One SQLite database at `~/.config/fulcra-collect/state.db` with these tables:

```sql
-- Per-plugin runtime state (the current "per-plugin JSON files" table)
CREATE TABLE plugin_state (
    plugin_id                 TEXT PRIMARY KEY,
    last_run                  TEXT,                  -- ISO 8601
    last_outcome              TEXT,                  -- 'done' | 'error' | 'timeout' | NULL
    last_error                TEXT,
    consecutive_failures      INTEGER NOT NULL DEFAULT 0,
    watermark                 TEXT,                  -- plugin-defined ISO 8601
    definition_id             TEXT,                  -- Fulcra annotation def UUID
    override_definition_name  TEXT,                  -- one-shot from definition_picker
    updated_at                TEXT NOT NULL          -- ISO 8601
);

-- Per-package state replaces the per-package JSON files. Same data,
-- normalised. Tag-ids and watermarks become child tables to avoid
-- the JSON-dict-in-a-cell antipattern.
CREATE TABLE package_definition (
    package_id    TEXT NOT NULL,    -- 'media', 'attention', 'dayone'
    role          TEXT NOT NULL,    -- 'watched' | 'listened' | 'read' | 'attention' | 'journal'
    definition_id TEXT NOT NULL,    -- Fulcra UUID
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (package_id, role)
);

CREATE TABLE package_tag (
    package_id  TEXT NOT NULL,
    tag_name    TEXT NOT NULL,
    tag_id      TEXT NOT NULL,      -- Fulcra UUID
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (package_id, tag_name)
);

CREATE TABLE package_watermark (
    package_id  TEXT NOT NULL,
    client_id   TEXT NOT NULL,      -- e.g. extension client ID, host name
    watermark   TEXT NOT NULL,      -- ISO 8601
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (package_id, client_id)
);

-- Favorites (#64) — was a JSON file with a single array.
CREATE TABLE quick_record_favorite (
    definition_id TEXT PRIMARY KEY
);

-- Schema-version table for migrations. Every connection runs
-- `_migrate_to_latest(conn)` at open time.
CREATE TABLE schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
```

## API shape

A new module `packages/collect/fulcra_collect/db.py` owning the SQLite connection lifecycle + migrations:

```python
def open(path: Path | None = None) -> sqlite3.Connection: ...
def migrate(conn) -> None: ...  # idempotent; brings any schema to latest
```

The existing `state.py` becomes a thin layer over the db module — keeps the `PluginState` dataclass shape and the `load(plugin_id)` / `save(state)` API for back-compat. Internally it queries plugin_state and constructs a PluginState.

Per-package state similarly: each package (attention, media-helpers, dayone) keeps its `State` class shape, but the load/save now hit the db. Their state.py files become thin shims OR they call into `fulcra_collect.db` directly (depends on dependency direction).

## Multi-process write safety

SQLite handles this natively with `PRAGMA journal_mode = WAL` (write-ahead log). Readers don't block writers, writers don't block readers, and the OS-level fsync semantics make torn writes impossible. We get this for free.

Specifically:
- Open connection with `sqlite3.connect(path, isolation_level=None)` (autocommit) + `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL` (the WAL-friendly default)
- Worker subprocesses open their own connection, write their state, close
- Daemon main process opens its own connection, reads/writes as needed
- Menubar app opens its own read-only connection
- All concurrent reads/writes safe

## Migration story

`db.py:migrate(conn)` checks the `schema_version` table and applies migrations in order:

- `001_initial.sql` — creates the tables above
- `002_import_existing_json.py` — one-shot Python migration that reads the existing JSON files and bulk-inserts into the SQLite tables, then renames the JSON files to `*.json.migrated` (so we can roll back if needed but don't trip the JSON code path)

Migration runs idempotently at daemon startup, BEFORE any state read/write. If the user runs an OLD daemon binary against a NEW db (downgrade), they get a clear error from a schema_version > known_max check.

## Backwards compatibility shim

For one release cycle, `state.py:load()` and per-package `State` classes keep their existing surface — they just hit SQLite under the hood. The JSON-file readers stay until we're sure the migration completed cleanly. Then the readers get deleted.

## Test strategy

- New module `tests/test_db.py` covers schema creation, migration idempotency, WAL mode, concurrent access
- Every existing `test_state.py` and per-package `test_state.py` is updated to use a temp SQLite db via fixture
- The JSON→SQLite migration is exercised in `tests/test_db_migration.py` with synthetic JSON fixtures

## Time estimate

- Phase 1: db module + plugin_state migration (1 batch, ~3 hours) — get the per-plugin store on SQLite, keep per-package JSON
- Phase 2: per-package store migration (1 batch, ~2 hours) — attention/media/dayone all move
- Phase 3: favorites + ad-hoc files (1 batch, ~1 hour) — favorites already abstracted in #64 so easy
- Phase 4: delete the JSON-reader fallbacks (1 batch, after a few weeks of soak)

Total: 2 focused sessions. Phase 4 just deletes code.

## Risks

1. **PluginState API change discoveries:** every test that constructs a PluginState directly will need updates. Estimate ~30 test sites to touch. Manageable but tedious.
2. **Cross-package import dependency:** if package-state-via-db means `fulcra_attention.state` imports `fulcra_collect.db`, the dependency direction reverses. Today attention is below collect in the dep graph. Need to confirm collect → attention import isn't created by accident. If it is, the db module goes in `fulcra-common` instead.
3. **Watermarks tab is per-client (machine ID, extension ID).** Today's `state.watermarks` dict-in-a-JSON file becomes a child table. The migration has to flatten the dict into rows.

## Recommendation

Land Phase 1 first. The plugin_state migration is the highest-leverage piece — it kills the multi-process write risk AND establishes the db pattern for everyone else to follow. Phase 2 + 3 can wait a week after that lands without losing value.
