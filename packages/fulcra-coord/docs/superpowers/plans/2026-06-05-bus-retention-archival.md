# Bus Retention / Archival Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move terminal/aged bus state (done/abandoned tasks, spent digest markers, dead-agent presence) out of the hot path so reads and reconcile stay fast, while preserving recoverable history — safely across machines, with no new scheduler.

**Architecture:** Terminal+aged tasks are crash-safely *moved* to `archive/tasks/<YYYY-MM>/<id>.json` with an append-only per-id index shard `archive/index/<id>.json`; spent digest markers and dead-agent presence are `fulcra file delete`d (platform soft-delete keeps them restorable). The pass is folded into `cmd_reconcile`, self-throttled to ~once/day via a first-host-wins `retention/last-run.json` marker (the digest-marker pattern), bounded by a per-run cap + a time budget that composes with reconcile's existing deadline, and is best-effort end to end (never raises into the tick). Hot-path exclusion is automatic: moving a body out of `tasks/` removes it from the `tasks/` listing that feeds the views and self-heal, with zero filter code.

**Tech Stack:** stdlib-only Python; unittest+pytest; Fulcra Files bus (no CAS); reconcile-folded throttled pass; fulcra file delete (soft-delete).

---

## Pre-flight notes

### Grounded signatures (read in `fulcra_coord/`, exact as of `feat/bus-retention` @ origin/main)

**`views.py`**
- `_now() -> datetime` — `datetime.now(timezone.utc)` (line 161). Tz-aware UTC.
- `_parse_dt(iso: str) -> Optional[datetime]` (line 165) — ISO-8601 → tz-aware UTC datetime or None; coerces naive→UTC. **All datetime gates MUST go through this — never lexical.**
- `_age_hours(updated_at: str, now: datetime) -> float` (line 137) — missing/unparseable → `+inf`.
- `_done_at(t) -> str` (line 183) — resolves `done.done_at` (full body) OR flat `done_at` (summary), falling back to `updated_at`. Use this to pick the archive month.
- `is_stale(task, now=None, stale_hours=None) -> bool` (line 147) — pattern to mirror for the new predicates: `status` gate → `now` default → `_age_hours >= threshold`.
- `_stale_hours(...)` (line 49) — the env-resolution idiom: explicit arg > env > default, `try/except ValueError` fallback. New `*_RETENTION_DAYS` resolvers mirror this exactly.
- `task_summary` field shape (from `schema.py:592`, the dict every view consumes): `{id, title, status, priority, workstream, owner_agent, assignee, last_touched_by, current_summary, next_action, blocked_on, not_before, due, done_at, acked_by, updated_at}` (+ `stale` stamped by `_summary_with_stale`). The index-shard fields (`id, title, status, workstream, owner_agent, done_at`) are all present on a summary.
- `build_summaries(tasks, updated_at=None)` (line 826) and **every** builder (`build_index`, `build_active`, `build_search_index`, `build_all_views` line 994, …) iterate ONLY over the `tasks` LIST handed to them. They do **no** I/O and never read the `tasks/` listing themselves.

**`cli.py`**
- `_print_json(data) -> None` (line 29) — `print(json.dumps(data, indent=2))`.
- `_info/_warn/_err(msg)` (lines 37–45) — logging.
- `_cache_remote_task(task_id, backend=None) -> Optional[dict]` (line 105).
- `_load_task(task_id, *, backend=None) -> Optional[dict]` (line 305) — cache then `_cache_remote_task`.
- `_load_all_tasks(backend=None) -> list[dict]` (line 118) — **seeds ids from `index.json` (`active` + `recent_done`), `views/search-index.json` (`records`), and `views/next.json` (`tasks`)**, then fetches those bodies concurrently. An id that appears in NONE of those views is not loaded.
- `_load_summaries_for_rebuild(task, *, backend=None)` (line 198) — write-path rebuild source; **SELF-HEAL (lines 274–289)** lists `f"{remote.remote_root()}/tasks/"` and re-includes any `tasks/<id>.json` present-but-absent from the aggregate. This is the listing whose membership a `tasks/` move shrinks.
- `cmd_reconcile(args, backend=None) -> int` (line 3274) — `t0 = time.monotonic()`, `timeout = int(os.environ.get("FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS","90"))`, `deadline = t0 + timeout`. Loads `all_tasks = _load_all_tasks(...)`, builds + uploads views, then `_reconcile_presence(...)`, then `_sweep_review_routes(...)` (best-effort try/except). **`_run_retention` hooks in AFTER `_sweep_review_routes`, BEFORE the `if failures:` return.** It receives `all_tasks`, `now` (the existing `now = datetime.now(timezone.utc)` at line 3295), and the existing `deadline` so its budget *composes with*, never double-counts, reconcile's deadline.
- `_digest_marker_path(window, now) -> str` (line 2142) — `f"{remote_root()}/digest/markers/{day}-{window}.json"`, `day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")`. **Reuse this exact path-shape idiom for `retention/last-run.json`.**
- `_claim_digest_marker(window, now, *, backend=None) -> bool` (line 2153) — first-writer-wins: `download_json` → if present return False; else `upload_json(marker)` and return its bool; whole body in `try/except: return False`. **The retention throttle claim is this pattern, adapted to one path keyed by date.**
- `cmd_search(args, backend=None) -> int` (line 3398) — reads cached `search-index` view, else `_load_task_summaries` + `views.search_tasks`. The `--archived` branch is additive (after the hot results).
- `identity.resolve_agent()` (identity.py:267) — the `by` stamp for markers.
- `remote.remote_root()` via `from . import remote_root` — root for path helpers.

**`remote.py`**
- `upload_json(data, remote_path, *, backend=None, timeout=None) -> bool` (line 180).
- `download_json(remote_path, *, backend=None, timeout=None) -> Optional[dict]` (line 129).
- `stat(remote_path, *, backend=None) -> Optional[dict]` (line 90) — used for archive read-back verification.
- `list_files(prefix, *, backend=None, timeout=None) -> list[str]` (line 196) — shells `<backend> list <prefix>`, returns full paths. Used to list `archive/index/`, `digest/markers/`, `presence/`.
- Path helpers: `task_remote_path(id)` (line 307) = `{remote_root()}/tasks/{id}.json`; `view_remote_path`, `presence_remote_path(slug)` (line 326), `presence_view_path()` (line 334). New archive/retention helpers go here, same idiom.
- **`remote.py` has NO `delete` today — Task 6 adds `remote.delete(remote_path, *, backend=None) -> bool` wrapping `fulcra file delete <PATH>`.** Confirmed the real `fulcra-api file` CLI exposes `delete PATH` (soft-delete) and `restore VERSION_ID`.

**`entry.py`**
- `build_parser()` (line 13): `sub = p.add_subparsers(dest="command", required=True)`. `search` parser at line 250 (`query` positional + `--format`). Add `--archived`/`--all` there; add a new `restore` parser.
- `COMMAND_MAP` (line 442) maps name → `_cli.cmd_*`. Add `"restore": _cli.cmd_restore`.
- `main()` (line 481) reads `FULCRA_COORD_BACKEND` into `backend` and dispatches.

**Tests** (`packages/fulcra-coord/tests/`)
- Run: `uv run --extra dev python -m pytest -q` (stdlib `unittest` classes, run under pytest).
- Fake backend (`tests/fake_fulcra_backend.py`) speaks `stat/download/upload/list/--help`; `list` uses `rglob("*")` (recursive). **It has NO `delete` — Task 6 adds a `delete` subcommand to it** (resolve path under `FULCRA_FAKE_ROOT`, `unlink`, return 0 / return 1 if absent).
- Two faking styles, both used here: (a) `backend=["false"]` + `patch("fulcra_coord.cli.remote.download_json", ...)`/`upload_json`/`list_files`/`delete` for unit isolation (see `test_operator_digest.py:282–343`); (b) a real `FULCRA_FAKE_ROOT` + `FULCRA_COORD_BACKEND="python <fake_fulcra_backend.py>"` end-to-end for the move/crash/concurrency integration tests.
- **NEVER commit uv.lock churn:** after any test run, `git checkout -- uv.lock packages/fulcra-coord/uv.lock`.

### Load-bearing constraints (honor in every task)
1. **Append-only per-id index shards.** `archive/index/<id>.json`, one distinct path per task, written once, never mutated. There is deliberately NO single `archive/index.json` — Files has no CAS, so a shared mutable index would let concurrent archivers clobber each other's read-modify-write appends. Distinct paths ⇒ concurrency-safe by construction.
2. **Write-then-delete crash-safe move.** Order is strictly: upload archive body → verify it landed (`stat`/read-back) → only then `delete tasks/<id>.json` → write the index shard. A crash anywhere leaves a recoverable duplicate (body in both places), never a lost task. Idempotent: archiving an already-archived id is a no-op.
3. **Parse-don't-lex timestamps.** Every age/cutoff gate goes through `views._parse_dt`. No string comparison of ISO timestamps anywhere.
4. **Never-raises.** `_run_retention` and every helper it calls are best-effort: per-item failures log and are skipped; nothing escapes into the reconcile tick.
5. **Time-budget composes with reconcile's deadline.** `_run_retention` takes the existing `deadline` from `cmd_reconcile` and stops archiving when `time.monotonic()` nears it (a fraction, e.g. leave ≥ a few seconds). It does NOT create a second independent timeout that could push past reconcile's 90s ceiling.
6. **Machine-agnostic.** All state (throttle marker, archive tree, index shards) on the bus; first-host-to-run-today wins; idempotent + per-task archive; convergent.
7. **No loss by construction** (follows from 1 + 2).

---

## File Structure

| File | Created/Modified | Responsibility |
|------|------------------|----------------|
| `fulcra_coord/views.py` | **Modified** | Pure policy predicates `is_archivable_task`, `is_prunable_marker`, `is_prunable_presence` + their env resolvers (`_retention_days`, `_marker_retention_days`, `_presence_retention_days`). No I/O. Lives here beside `is_stale`/`is_aged_out_broadcast` (the other pure age predicates). |
| `fulcra_coord/remote.py` | **Modified** | `delete(remote_path, *, backend=None) -> bool` (wraps `fulcra file delete`). Archive/retention path helpers: `archive_task_path(id, month)`, `archive_index_path(id)`, `archive_index_prefix()`, `retention_marker_path(now)`, `digest_markers_prefix()`, `presence_prefix()`. |
| `fulcra_coord/cli.py` | **Modified** | `_archive_task` (crash-safe move + idempotency), `_write_index_shard`, `_read_index_shards`/`_list_index_shards`, `_claim_retention_marker`, `_run_retention` (folded into `cmd_reconcile`), `_prune_markers`, `_prune_dead_presence`, `cmd_restore`, `--archived` branch in `cmd_search`. |
| `fulcra_coord/entry.py` | **Modified** | `search --archived/--all` flag; `restore` subparser; `COMMAND_MAP["restore"]`. |
| `tests/fake_fulcra_backend.py` | **Modified** | `delete <remote_path>` subcommand (unlink under `FULCRA_FAKE_ROOT`). |
| `tests/test_retention.py` | **Created** | All retention tests (policy, move, shards, search/restore, run/throttle/bound, prune). |
| `fulcra_coord/__init__.py` | **Modified** | `__version__` 0.7.0 → 0.8.0. |
| `CHANGELOG.md` | **Modified** | New `## [0.8.0]` section atop the existing 0.7.0 entry. |

---

## Task 1 — Pure policy predicates in `views.py`

The most-tested, zero-I/O core. Do first; everything else depends on these gates.

### 1.1 — Env resolvers + `is_archivable_task`

- [ ] **Failing test.** Append to a new `tests/test_retention.py`:
```python
import os
import unittest
from datetime import datetime, timedelta, timezone

from fulcra_coord import views


def _dt(days_ago, now):
    return (now - timedelta(days=days_ago)).isoformat(timespec="microseconds").replace("+00:00", "Z")


class TestIsArchivableTask(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

    def test_terminal_and_aged_is_archivable(self):
        for status in ("done", "abandoned"):
            t = {"status": status, "done_at": _dt(31, self.now), "updated_at": _dt(31, self.now)}
            self.assertTrue(views.is_archivable_task(t, self.now, 30), status)

    def test_terminal_but_recent_is_not(self):
        t = {"status": "done", "done_at": _dt(29, self.now), "updated_at": _dt(29, self.now)}
        self.assertFalse(views.is_archivable_task(t, self.now, 30))

    def test_exactly_at_cutoff_is_archivable(self):
        # cutoff is "older than N days" => age >= N days qualifies (>= boundary).
        t = {"status": "done", "done_at": _dt(30, self.now), "updated_at": _dt(30, self.now)}
        self.assertTrue(views.is_archivable_task(t, self.now, 30))

    def test_non_terminal_never_archivable_even_if_ancient(self):
        for status in ("active", "waiting", "blocked", "proposed"):
            t = {"status": status, "updated_at": _dt(999, self.now)}
            self.assertFalse(views.is_archivable_task(t, self.now, 30), status)

    def test_uses_done_at_over_updated_at(self):
        # done long ago but updated recently (e.g. a late annotation) => not aged.
        t = {"status": "done", "done_at": _dt(5, self.now), "updated_at": _dt(5, self.now)}
        self.assertFalse(views.is_archivable_task(t, self.now, 30))

    def test_missing_timestamps_not_archivable(self):
        # a clockless terminal task: +inf age would archive it, but we choose the
        # SAFE direction for a destructive move — don't archive without a parseable
        # done/updated timestamp (opposite of is_stale's fail-toward-surfacing).
        t = {"status": "done"}
        self.assertFalse(views.is_archivable_task(t, self.now, 30))

    def test_env_default_is_30(self):
        self.assertEqual(views._retention_days(), 30.0)
        os.environ["FULCRA_COORD_RETENTION_DAYS"] = "7"
        try:
            self.assertEqual(views._retention_days(), 7.0)
        finally:
            del os.environ["FULCRA_COORD_RETENTION_DAYS"]
```

- [ ] **Run, expect FAIL** (no such attribute):
  `uv run --extra dev python -m pytest tests/test_retention.py::TestIsArchivableTask -v`

- [ ] **Minimal impl.** In `views.py`, after `is_stale` (≈ line 159), add:
```python
# Default age (days) after which a TERMINAL (done/abandoned) task is moved out
# of the hot path into the cold archive. 30d keeps a month of finished work
# instantly visible in recently-done/search before it cold-stores. Tunable via
# FULCRA_COORD_RETENTION_DAYS for a fleet that wants a longer/shorter hot window.
RETENTION_DAYS_DEFAULT = 30
# Spent digest dedup markers older than this are pruned (deleted). They are
# regenerable guards with no history value; 7d is ample slack past the daily
# windows that could still consult them.
MARKER_RETENTION_DAYS_DEFAULT = 7
# Dead-agent presence records older than this are pruned. Presence is a live
# snapshot, not history; a record untouched for 30d is a long-departed agent.
PRESENCE_RETENTION_DAYS_DEFAULT = 30


def _retention_days(days=None):
    """Resolve the task archive age (days): explicit arg > env > default (mirrors
    _stale_hours). A non-numeric FULCRA_COORD_RETENTION_DAYS falls back to the
    default rather than crashing the best-effort retention pass."""
    if days is not None:
        return float(days)
    raw = os.environ.get("FULCRA_COORD_RETENTION_DAYS", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(RETENTION_DAYS_DEFAULT)


def _marker_retention_days(days=None):
    """Resolve the digest-marker prune age (days): explicit arg > env > default."""
    if days is not None:
        return float(days)
    raw = os.environ.get("FULCRA_COORD_MARKER_RETENTION_DAYS", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(MARKER_RETENTION_DAYS_DEFAULT)


def _presence_retention_days(days=None):
    """Resolve the dead-presence prune age (days): explicit arg > env > default."""
    if days is not None:
        return float(days)
    raw = os.environ.get("FULCRA_COORD_PRESENCE_RETENTION_DAYS", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(PRESENCE_RETENTION_DAYS_DEFAULT)


def is_archivable_task(task, now=None, retention_days=None):
    """True when a task is terminal (done/abandoned) AND aged past the retention
    window, so it should be cold-archived out of the hot path.

    Aged is measured from the done/abandoned timestamp (_done_at: nested
    done.done_at OR flat done_at, falling back to updated_at), PARSED via
    _parse_dt (never lexical). Non-terminal statuses (active/waiting/blocked/
    proposed) are live work and NEVER qualify regardless of age.

    SAFE DIRECTION on a missing/unparseable timestamp: unlike is_stale (which
    fails toward SURFACING a clockless task), archiving is a destructive MOVE, so
    a clockless terminal task is NOT archived — we never move what we can't date.
    Boundary: age >= retention_days qualifies (a task done exactly N days ago is
    archivable), matching the recently-done cutoff's >= semantics."""
    if task.get("status") not in ("done", "abandoned"):
        return False
    if now is None:
        now = _now()
    dt = _parse_dt(_done_at(task))
    if dt is None:
        return False
    return (now - dt).total_seconds() / 86400.0 >= _retention_days(retention_days)
```

- [ ] **Run, expect PASS** (same command).
- [ ] **Commit:** `feat(retention): add is_archivable_task policy predicate + env resolvers`

### 1.2 — `is_prunable_marker`

- [ ] **Failing test.** Add `TestIsPrunableMarker` to `tests/test_retention.py`:
```python
class TestIsPrunableMarker(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

    def test_old_marker_prunable(self):
        path = "/coordination/digest/markers/2026-05-20-morning.json"  # 16d old
        self.assertTrue(views.is_prunable_marker(path, self.now, 7))

    def test_recent_marker_kept(self):
        path = "/coordination/digest/markers/2026-06-02-evening.json"  # 3d old
        self.assertFalse(views.is_prunable_marker(path, self.now, 7))

    def test_exactly_at_cutoff_prunable(self):
        path = "/coordination/digest/markers/2026-05-29-morning.json"  # 7d old
        self.assertTrue(views.is_prunable_marker(path, self.now, 7))

    def test_unparseable_date_kept(self):
        # never delete something we can't date — safe direction for a destructive op.
        self.assertFalse(views.is_prunable_marker("/x/digest/markers/garbage.json", self.now, 7))
        self.assertFalse(views.is_prunable_marker("/x/digest/markers/.json", self.now, 7))
```

- [ ] **Run, expect FAIL.** `uv run --extra dev python -m pytest tests/test_retention.py::TestIsPrunableMarker -v`
- [ ] **Minimal impl.** In `views.py`:
```python
import re as _re  # module-level if not already imported

_MARKER_DATE_RE = _re.compile(r"/(\d{4}-\d{2}-\d{2})-[^/]+\.json$")


def is_prunable_marker(path, now=None, marker_days=None):
    """True when a digest dedup marker file is older than the marker-retention
    window and should be pruned (deleted).

    The marker path is digest/markers/<YYYY-MM-DD>-<window>.json (see
    cli._digest_marker_path). We extract the embedded UTC DATE and parse it via
    _parse_dt — never a lexical compare. A path that doesn't match the expected
    shape (no parseable date) is KEPT, not pruned: we never delete what we can't
    date. Boundary: age >= marker_days prunes."""
    if now is None:
        now = _now()
    m = _MARKER_DATE_RE.search(path)
    if not m:
        return False
    dt = _parse_dt(m.group(1) + "T00:00:00Z")
    if dt is None:
        return False
    return (now - dt).total_seconds() / 86400.0 >= _marker_retention_days(marker_days)
```

- [ ] **Run, expect PASS.**
- [ ] **Commit:** `feat(retention): add is_prunable_marker policy predicate`

### 1.3 — `is_prunable_presence`

- [ ] **Failing test.** Add `TestIsPrunablePresence`:
```python
class TestIsPrunablePresence(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

    def _rec(self, days_ago):
        ls = (self.now - timedelta(days=days_ago)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        return {"agent": "claude-code:h:r", "last_seen": ls}

    def test_long_dead_prunable(self):
        self.assertTrue(views.is_prunable_presence(self._rec(31), self.now, 30))

    def test_recently_seen_kept(self):
        self.assertFalse(views.is_prunable_presence(self._rec(2), self.now, 30))

    def test_exactly_at_cutoff_prunable(self):
        self.assertTrue(views.is_prunable_presence(self._rec(30), self.now, 30))

    def test_missing_last_seen_kept(self):
        self.assertFalse(views.is_prunable_presence({"agent": "x"}, self.now, 30))
```

- [ ] **Run, expect FAIL.** `uv run --extra dev python -m pytest tests/test_retention.py::TestIsPrunablePresence -v`
- [ ] **Minimal impl.** In `views.py`:
```python
def is_prunable_presence(record, now=None, presence_days=None):
    """True when a presence record's last_seen is older than the presence-
    retention window — a long-departed agent whose live snapshot is now noise.

    last_seen parsed via _parse_dt (never lexical). A missing/unparseable
    last_seen is KEPT (safe direction: don't delete an undatable record).
    Boundary: age >= presence_days prunes. Presence is a derived view, so a
    pruned record also drops from the presence aggregate on the next rebuild."""
    if now is None:
        now = _now()
    dt = _parse_dt(record.get("last_seen", ""))
    if dt is None:
        return False
    return (now - dt).total_seconds() / 86400.0 >= _presence_retention_days(presence_days)
```

- [ ] **Run, expect PASS.** Then `git checkout -- uv.lock packages/fulcra-coord/uv.lock`.
- [ ] **Commit:** `feat(retention): add is_prunable_presence policy predicate`

---

## Task 2 — Crash-safe archive move (`_archive_task`) + path helpers

Depends on Task 1 (uses `_done_at` for the month). Adds the remote path helpers and the move.

### 2.1 — Archive path helpers in `remote.py`

- [ ] **Failing test.** Add `TestArchivePaths` to `tests/test_retention.py`:
```python
from fulcra_coord import remote


class TestArchivePaths(unittest.TestCase):
    def setUp(self):
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/coordination"

    def tearDown(self):
        os.environ.pop("FULCRA_COORD_REMOTE_ROOT", None)

    def test_archive_task_path(self):
        self.assertEqual(remote.archive_task_path("t-1", "2026-05"),
                         "/coordination/archive/tasks/2026-05/t-1.json")

    def test_archive_index_path_and_prefix(self):
        self.assertEqual(remote.archive_index_path("t-1"),
                         "/coordination/archive/index/t-1.json")
        self.assertEqual(remote.archive_index_prefix(), "/coordination/archive/index/")

    def test_retention_marker_path(self):
        now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(remote.retention_marker_path(now),
                         "/coordination/retention/last-run.json")
```
(`FULCRA_COORD_REMOTE_ROOT` is honored by `remote_root()`; confirm the env var name in `fulcra_coord/__init__.py` and adjust if it differs.)

- [ ] **Run, expect FAIL.** `uv run --extra dev python -m pytest tests/test_retention.py::TestArchivePaths -v`
- [ ] **Minimal impl.** In `remote.py`, after `presence_view_path()` (≈ line 339):
```python
def archive_task_path(task_id, month):
    """Cold-archive body path: archive/tasks/<YYYY-MM>/<id>.json. Month is the
    done/abandoned month, so the archive is browsable by when work finished."""
    return f"{remote_root()}/archive/tasks/{month}/{task_id}.json"


def archive_index_path(task_id):
    """Per-id cold-index SHARD path. Append-only, one distinct path per task —
    NO shared archive/index.json, because Files has no CAS and a shared mutable
    index would let concurrent archivers clobber each other's appends."""
    return f"{remote_root()}/archive/index/{task_id}.json"


def archive_index_prefix():
    """List prefix for the cold-index shards (search --archived, restore lookup)."""
    return f"{remote_root()}/archive/index/"


def retention_marker_path(now):
    """First-host-wins daily throttle marker. ONE path per day (date is INSIDE
    the JSON, not the filename) so today's run reads a stable path and any host
    claims the SAME file — the digest-marker first-writer-wins pattern, but a
    single rolling file rather than per-window."""
    return f"{remote_root()}/retention/last-run.json"


def digest_markers_prefix():
    """List prefix for digest dedup markers (marker prune)."""
    return f"{remote_root()}/digest/markers/"


def presence_prefix():
    """List prefix for per-agent presence records (dead-presence prune)."""
    return f"{remote_root()}/presence/"
```

- [ ] **Run, expect PASS.**
- [ ] **Commit:** `feat(retention): add archive/retention remote path helpers`

### 2.2 — `_archive_task` crash-safe move + idempotency

- [ ] **Failing test.** Add `TestArchiveTask` to `tests/test_retention.py` (uses the real fake backend end-to-end so the move's file effects are observable). Pattern follows `test_demo_seed.py`'s `FULCRA_FAKE_ROOT`/`FULCRA_COORD_BACKEND` setup:
```python
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fulcra_coord import cli

_FAKE = str(Path(__file__).parent / "fake_fulcra_backend.py")


class _FakeBus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fc-retention-")
        os.environ["FULCRA_FAKE_ROOT"] = self.tmp
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/coordination"
        os.environ["FULCRA_COORD_BACKEND"] = f"{sys.executable} {_FAKE}"
        self.backend = [sys.executable, _FAKE]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("FULCRA_FAKE_ROOT", "FULCRA_COORD_REMOTE_ROOT", "FULCRA_COORD_BACKEND"):
            os.environ.pop(k, None)

    def _put(self, remote_path, obj):
        p = Path(self.tmp) / remote_path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj))

    def _exists(self, remote_path):
        return (Path(self.tmp) / remote_path.lstrip("/")).exists()

    def _read(self, remote_path):
        return json.loads((Path(self.tmp) / remote_path.lstrip("/")).read_text())


class TestArchiveTask(_FakeBus):
    def _task(self, tid="t-1"):
        return {"id": tid, "title": "old work", "status": "done",
                "workstream": "ws", "owner_agent": "claude-code:h:r",
                "done_at": "2026-05-01T00:00:00Z", "updated_at": "2026-05-01T00:00:00Z"}

    def test_move_writes_archive_deletes_original_writes_shard(self):
        t = self._task()
        self._put("/coordination/tasks/t-1.json", t)
        ok = cli._archive_task(t, backend=self.backend)
        self.assertTrue(ok)
        self.assertTrue(self._exists("/coordination/archive/tasks/2026-05/t-1.json"))
        self.assertFalse(self._exists("/coordination/tasks/t-1.json"))
        shard = self._read("/coordination/archive/index/t-1.json")
        self.assertEqual(shard["id"], "t-1")
        self.assertEqual(shard["archive_path"], "/coordination/archive/tasks/2026-05/t-1.json")
        for f in ("title", "status", "workstream", "owner_agent", "done_at", "archived_at"):
            self.assertIn(f, shard)

    def test_idempotent_already_archived_is_noop(self):
        t = self._task()
        self.assertTrue(cli._archive_task(t, backend=self.backend))  # first
        self.assertTrue(cli._archive_task(t, backend=self.backend))  # second: no-op, still True
        self.assertTrue(self._exists("/coordination/archive/tasks/2026-05/t-1.json"))
        self.assertTrue(self._exists("/coordination/archive/index/t-1.json"))

    def test_crash_between_archive_and_delete_completes_next_pass(self):
        # Simulate a crash AFTER the body landed in archive but BEFORE delete:
        # both copies present, no shard. Next _archive_task must finish the move.
        t = self._task()
        self._put("/coordination/tasks/t-1.json", t)
        self._put("/coordination/archive/tasks/2026-05/t-1.json", t)  # duplicate from crash
        ok = cli._archive_task(t, backend=self.backend)
        self.assertTrue(ok)
        self.assertFalse(self._exists("/coordination/tasks/t-1.json"))  # original now gone
        self.assertTrue(self._exists("/coordination/archive/index/t-1.json"))  # shard written

    def test_upload_failure_leaves_original_intact(self):
        # If the archive upload fails (verify finds nothing), the original is NOT
        # deleted — no-loss by construction.
        t = self._task()
        self._put("/coordination/tasks/t-1.json", t)
        with patch("fulcra_coord.cli.remote.upload_json", return_value=False):
            ok = cli._archive_task(t, backend=self.backend)
        self.assertFalse(ok)
        self.assertTrue(self._exists("/coordination/tasks/t-1.json"))
```
(Add `from unittest.mock import patch` at the top of the test module.)

- [ ] **Run, expect FAIL** (no `cli._archive_task`):
  `uv run --extra dev python -m pytest tests/test_retention.py::TestArchiveTask -v`

- [ ] **Minimal impl.** In `cli.py`, near the other write helpers (after `_load_task`, ≈ line 311), add:
```python
def _archive_month(task):
    """The <YYYY-MM> the task is archived under: the done/abandoned month, or the
    updated month as a fallback. Parsed via views._parse_dt (never lexical)."""
    dt = views._parse_dt(views._done_at(task))
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m")


def _archive_index_shard(task, archive_path):
    """The append-only cold-index shard body for an archived task. Fields are a
    subset of task_summary plus the archive bookkeeping; written once, never
    mutated (one distinct path per id => concurrency-safe, no CAS)."""
    return {
        "schema": "fulcra.coordination.archive_index.v1",
        "id": task.get("id", ""),
        "title": task.get("title", ""),
        "status": task.get("status", ""),
        "workstream": task.get("workstream", ""),
        "owner_agent": task.get("owner_agent", ""),
        "done_at": views._done_at(task),
        "archived_at": _now_iso(),
        "archive_path": archive_path,
    }


def _archive_task(task, *, backend=None):
    """Crash-safely MOVE a terminal+aged task out of the hot path into the cold
    archive. Returns True on a completed (or already-complete) move, False if the
    move could not be safely completed (caller logs + retries next pass).

    ORDER (no-loss by construction): upload archive body -> VERIFY it landed
    (stat) -> only THEN delete tasks/<id>.json -> write the per-id index shard.
    A crash anywhere leaves the body in BOTH places (a recoverable duplicate),
    never lost. IDEMPOTENT: if the archive body already exists we skip the
    upload, still ensure the original is deleted and the shard exists, so
    archiving an already-archived id (or finishing a crashed move) is a no-op.

    BEST-EFFORT: any backend error returns False rather than raising; the only
    irreversible step (delete) runs strictly after a positive read-back."""
    tid = task.get("id")
    if not tid:
        return False
    try:
        archive_path = remote.archive_task_path(tid, _archive_month(task))
        task_path = remote.task_remote_path(tid)
        # (1) ensure the body is in the archive (idempotent): upload only if absent.
        if remote.stat(archive_path, backend=backend) is None:
            if not remote.upload_json(task, archive_path, backend=backend):
                return False
        # (2) VERIFY it landed before any delete — the no-loss gate.
        if remote.stat(archive_path, backend=backend) is None:
            return False
        # (3) only now remove the hot copy (idempotent: a missing original is fine).
        if remote.stat(task_path, backend=backend) is not None:
            remote.delete(task_path, backend=backend)
        # (4) write the append-only index shard if absent (idempotent).
        if remote.stat(remote.archive_index_path(tid), backend=backend) is None:
            remote.upload_json(_archive_index_shard(task, archive_path),
                               remote.archive_index_path(tid), backend=backend)
        return True
    except Exception:
        return False
```
This needs `remote.delete` (Task 6 adds it to `remote.py` + fake backend). **Reorder: pull the `remote.delete` impl + fake-backend `delete` forward into THIS task** so `_archive_task` tests can run — i.e. do Task 6.1 (the `remote.delete` wrapper + fake-backend `delete` subcommand) before 2.2, then the prune logic that consumes it stays in Task 6. (Mark Task 6.1 done here.)

- [ ] **Run, expect PASS.** Then `git checkout -- uv.lock packages/fulcra-coord/uv.lock`.
- [ ] **Commit:** `feat(retention): crash-safe _archive_task move with idempotency`

---

## Task 3 — Index shard read/list helpers

Depends on Task 2 (shards exist). Adds the cold read path used by `search --archived` and `restore`.

### 3.1 — `_list_index_shards` / `_read_index_shard`

- [ ] **Failing test.** Add `TestIndexShards(_FakeBus)`:
```python
class TestIndexShards(_FakeBus):
    def _shard(self, tid):
        return {"schema": "fulcra.coordination.archive_index.v1", "id": tid,
                "title": f"task {tid}", "status": "done", "workstream": "ws",
                "owner_agent": "a", "done_at": "2026-05-01T00:00:00Z",
                "archived_at": "2026-06-05T00:00:00Z",
                "archive_path": f"/coordination/archive/tasks/2026-05/{tid}.json"}

    def test_lists_all_shards(self):
        for tid in ("t-1", "t-2", "t-3"):
            self._put(f"/coordination/archive/index/{tid}.json", self._shard(tid))
        shards = cli._list_index_shards(backend=self.backend)
        self.assertEqual({s["id"] for s in shards}, {"t-1", "t-2", "t-3"})

    def test_empty_archive_returns_empty(self):
        self.assertEqual(cli._list_index_shards(backend=self.backend), [])

    def test_read_single_shard(self):
        self._put("/coordination/archive/index/t-9.json", self._shard("t-9"))
        s = cli._read_index_shard("t-9", backend=self.backend)
        self.assertEqual(s["archive_path"], "/coordination/archive/tasks/2026-05/t-9.json")
        self.assertIsNone(cli._read_index_shard("missing", backend=self.backend))
```

- [ ] **Run, expect FAIL.** `uv run --extra dev python -m pytest tests/test_retention.py::TestIndexShards -v`
- [ ] **Minimal impl.** In `cli.py`:
```python
def _read_index_shard(task_id, *, backend=None):
    """Read one archived task's cold-index shard, or None if not archived."""
    return remote.download_json(remote.archive_index_path(task_id), backend=backend)


def _list_index_shards(*, backend=None):
    """List every cold-index shard (archive/index/<id>.json) as parsed dicts.

    Best-effort: a failed listing or a single unreadable shard contributes
    nothing rather than raising. O(archived) — paid ONLY on the opt-in cold path
    (search --archived), never on hot reads. Reuses remote.list_files; the fake
    backend's recursive list returns exactly the shard files under the prefix."""
    out = []
    try:
        for path in remote.list_files(remote.archive_index_prefix(), backend=backend):
            if not path.endswith(".json"):
                continue
            shard = remote.download_json(path, backend=backend)
            if shard and shard.get("id"):
                out.append(shard)
    except Exception:
        pass
    return out
```

- [ ] **Run, expect PASS.** `git checkout -- uv.lock packages/fulcra-coord/uv.lock`.
- [ ] **Commit:** `feat(retention): cold-index shard read/list helpers`

---

## Task 4 — `search --archived` + `restore` command + wiring

Depends on Task 3 (shard reads) and Task 2 (move, for `restore`'s reverse).

### 4.1 — `search --archived`

- [ ] **Failing test.** Add `TestSearchArchived`. Uses unit-isolation faking (`backend=["false"]`, patch `_list_index_shards` + the hot `cache.read_cached_view`):
```python
class TestSearchArchived(unittest.TestCase):
    def _args(self, query, archived=False, fmt="json"):
        ns = type("A", (), {})()
        ns.query, ns.archived, ns.format = query, archived, fmt
        return ns

    def test_default_search_does_not_list_archive(self):
        with patch("fulcra_coord.cli.cache.read_cached_view", return_value=None), \
             patch("fulcra_coord.cli._load_task_summaries", return_value=[]), \
             patch("fulcra_coord.cli._list_index_shards") as shards:
            rc = cli.cmd_search(self._args("anything"), backend=["false"])
        self.assertEqual(rc, 0)
        shards.assert_not_called()

    def test_archived_search_finds_shard_match(self):
        shard = {"id": "t-1", "title": "migrate the widget", "status": "done",
                 "workstream": "ws", "owner_agent": "a", "done_at": "x",
                 "archive_path": "/coordination/archive/tasks/2026-05/t-1.json"}
        out = io.StringIO()
        with patch("fulcra_coord.cli.cache.read_cached_view", return_value=None), \
             patch("fulcra_coord.cli._load_task_summaries", return_value=[]), \
             patch("fulcra_coord.cli._list_index_shards", return_value=[shard]), \
             contextlib.redirect_stdout(out):
            rc = cli.cmd_search(self._args("widget", archived=True), backend=["false"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        ids = [r["id"] for r in payload["results"]]
        self.assertIn("t-1", ids)
```
(Add `import io, contextlib` to the test module.)

- [ ] **Run, expect FAIL.** `uv run --extra dev python -m pytest tests/test_retention.py::TestSearchArchived -v`
- [ ] **Minimal impl.** In `cmd_search`, after the hot `results` are computed and BEFORE the format/print block (≈ line 3424), add:
```python
    # --archived (alias --all): additionally scan the cold archive index shards.
    # Default search stays hot-only (fast); the archive is O(archived) and paid
    # only when explicitly requested. Matches on the same fields as hot search.
    if getattr(args, "archived", False):
        q = query.lower()
        seen = {r.get("id") for r in results}
        for shard in _list_index_shards(backend=backend):
            if shard.get("id") in seen:
                continue
            text = " ".join([shard.get("title", ""), shard.get("workstream", ""),
                             shard.get("owner_agent", "")]).lower()
            if q in text:
                results.append({
                    "id": shard.get("id", ""), "title": shard.get("title", ""),
                    "status": shard.get("status", ""), "priority": "",
                    "workstream": shard.get("workstream", ""),
                    "owner_agent": shard.get("owner_agent", ""),
                    "archived": True, "archive_path": shard.get("archive_path", ""),
                })
```

- [ ] **Run, expect PASS.**
- [ ] **Commit:** `feat(retention): search --archived scans cold index shards`

### 4.2 — `cmd_restore`

- [ ] **Failing test.** Add `TestRestore(_FakeBus)`:
```python
class TestRestore(_FakeBus):
    def _args(self, tid, fmt="table"):
        ns = type("A", (), {})()
        ns.task_id, ns.format = tid, fmt
        return ns

    def test_restore_moves_body_back_and_deletes_shard(self):
        body = {"id": "t-1", "title": "old", "status": "done",
                "done_at": "2026-05-01T00:00:00Z", "updated_at": "2026-05-01T00:00:00Z"}
        ap = "/coordination/archive/tasks/2026-05/t-1.json"
        self._put(ap, body)
        self._put("/coordination/archive/index/t-1.json",
                  {"id": "t-1", "archive_path": ap})
        rc = cli.cmd_restore(self._args("t-1"), backend=self.backend)
        self.assertEqual(rc, 0)
        self.assertTrue(self._exists("/coordination/tasks/t-1.json"))
        self.assertFalse(self._exists("/coordination/archive/index/t-1.json"))

    def test_restore_unknown_id_is_error(self):
        rc = cli.cmd_restore(self._args("nope"), backend=self.backend)
        self.assertEqual(rc, 1)
```

- [ ] **Run, expect FAIL.** `uv run --extra dev python -m pytest tests/test_retention.py::TestRestore -v`
- [ ] **Minimal impl.** In `cli.py` (near `cmd_search`):
```python
def cmd_restore(args, backend=None):
    """Restore a cold-archived task back into the hot path.

    Reverses _archive_task: reads the task's archive/index/<id>.json shard for
    its archive_path, downloads the archived body, uploads it back to
    tasks/<id>.json, then deletes the index shard. The NEXT reconcile re-includes
    it in views (the body is back in the tasks/ listing the self-heal enumerates).
    Nothing is one-way. NOTE this is a bus-level MOVE, independent of the platform
    'fulcra file restore' (which restores a deleted file's prior VERSION by UUID);
    archived tasks were moved, not deleted, so we move them back ourselves.

    Order mirrors the archive's no-loss ordering: write the hot copy and VERIFY it
    landed before deleting the shard, so a crash leaves a recoverable state."""
    tid = args.task_id
    shard = _read_index_shard(tid, backend=backend)
    if not shard:
        _err(f"No archived task {tid!r} (no archive/index/{tid}.json shard).")
        return 1
    archive_path = shard.get("archive_path") or remote.archive_task_path(tid, "")
    body = remote.download_json(archive_path, backend=backend)
    if not body:
        _err(f"Archived body for {tid!r} not found at {archive_path}.")
        return 1
    task_path = remote.task_remote_path(tid)
    if not remote.upload_json(body, task_path, backend=backend):
        _err(f"Failed to restore body for {tid!r}.")
        return 1
    if remote.stat(task_path, backend=backend) is None:
        _err(f"Restore of {tid!r} did not verify; left archive shard intact.")
        return 1
    remote.delete(remote.archive_index_path(tid), backend=backend)
    _info(f"Restored {tid} to {task_path}. Run reconcile to re-incorporate into views.")
    return 0
```

- [ ] **Run, expect PASS.** `git checkout -- uv.lock packages/fulcra-coord/uv.lock`.
- [ ] **Commit:** `feat(retention): restore command moves an archived task back`

### 4.3 — `entry.py` wiring

- [ ] **Failing test.** Add `TestWiring`:
```python
class TestWiring(unittest.TestCase):
    def test_restore_in_command_map(self):
        from fulcra_coord import entry
        self.assertIn("restore", entry.COMMAND_MAP)
        self.assertIs(entry.COMMAND_MAP["restore"], cli.cmd_restore)

    def test_search_parses_archived_flag(self):
        from fulcra_coord import entry
        ns = entry.build_parser().parse_args(["search", "q", "--archived"])
        self.assertTrue(ns.archived)
        ns2 = entry.build_parser().parse_args(["search", "q", "--all"])
        self.assertTrue(ns2.archived)
        ns3 = entry.build_parser().parse_args(["search", "q"])
        self.assertFalse(ns3.archived)

    def test_restore_parses_task_id(self):
        from fulcra_coord import entry
        ns = entry.build_parser().parse_args(["restore", "t-1"])
        self.assertEqual(ns.task_id, "t-1")
        self.assertEqual(ns.command, "restore")
```

- [ ] **Run, expect FAIL.** `uv run --extra dev python -m pytest tests/test_retention.py::TestWiring -v`
- [ ] **Minimal impl.** In `entry.py`, in the `search` parser block (≈ line 250):
```python
    sp.add_argument("--archived", "--all", dest="archived", action="store_true",
                    help="Also search the cold archive (archive/index shards). "
                         "Default search is hot-only (fast).")
```
After the `search` block, add:
```python
    # ---- restore ----
    sp = sub.add_parser("restore",
                        help="Restore a cold-archived task back into the hot path")
    sp.add_argument("task_id", metavar="TASK-ID")
    sp.add_argument("--format", choices=["table", "json"], default="table")
```
In `COMMAND_MAP` (≈ line 477), add `"restore": _cli.cmd_restore,`.

- [ ] **Run, expect PASS.** `git checkout -- uv.lock packages/fulcra-coord/uv.lock`.
- [ ] **Commit:** `feat(retention): wire restore command and search --archived flag`

---

## Task 5 — `_run_retention` folded into `cmd_reconcile` (throttle + bound + archive loop)

Depends on Tasks 1, 2. The orchestration: first-host-wins daily throttle, bounded cap + time budget, best-effort archive loop. (Prune lives in Task 6; this task wires `_run_retention`'s archive half + the throttle.)

### 5.1 — `_claim_retention_marker` (first-host-wins daily throttle)

- [ ] **Failing test.** Add `TestRetentionMarker`:
```python
class TestRetentionMarker(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/coordination"

    def tearDown(self):
        os.environ.pop("FULCRA_COORD_REMOTE_ROOT", None)

    def test_absent_marker_is_claimed(self):
        with patch("fulcra_coord.cli.remote.download_json", return_value=None), \
             patch("fulcra_coord.cli.remote.upload_json", return_value=True):
            self.assertTrue(cli._claim_retention_marker(self.now, backend=["false"]))

    def test_today_marker_blocks_second_host(self):
        today = {"date": "2026-06-05", "by": "other-host"}
        with patch("fulcra_coord.cli.remote.download_json", return_value=today), \
             patch("fulcra_coord.cli.remote.upload_json") as up:
            self.assertFalse(cli._claim_retention_marker(self.now, backend=["false"]))
        up.assert_not_called()

    def test_yesterday_marker_allows_new_claim(self):
        yest = {"date": "2026-06-04", "by": "x"}
        with patch("fulcra_coord.cli.remote.download_json", return_value=yest), \
             patch("fulcra_coord.cli.remote.upload_json", return_value=True):
            self.assertTrue(cli._claim_retention_marker(self.now, backend=["false"]))

    def test_claim_error_skips(self):
        with patch("fulcra_coord.cli.remote.download_json", side_effect=RuntimeError):
            self.assertFalse(cli._claim_retention_marker(self.now, backend=["false"]))
```

- [ ] **Run, expect FAIL.** `uv run --extra dev python -m pytest tests/test_retention.py::TestRetentionMarker -v`
- [ ] **Minimal impl.** In `cli.py` (near `_claim_digest_marker`, ≈ line 2186):
```python
def _claim_retention_marker(now, *, backend=None):
    """First-host-wins daily throttle for the retention pass — the digest-marker
    pattern (line 2153), one rolling file keyed by date-INSIDE-the-JSON.

    Read retention/last-run.json: if its date == today (UTC) another host already
    ran today -> return False (skip). Else write {date, by, at} and re-read; if a
    different host's stamp won the claim, return False (they run, we skip). Files
    has no CAS, so two hosts can rarely both see today's marker absent and both
    proceed — ACCEPTED and harmless (mirrors the digest marker): the archive step
    is idempotent + per-task, so a double-run just re-archives already-archived
    ids as no-ops. The marker is a THROTTLE, not a lock. Any error -> skip (never
    risk an unbounded concurrent pass; next tick/day retries). Never raises."""
    try:
        path = remote.retention_marker_path(now)
        today = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        existing = remote.download_json(path, backend=backend)
        if existing is not None and existing.get("date") == today:
            return False
        me = identity.resolve_agent()
        marker = {
            "schema": "fulcra.coordination.retention_marker.v1",
            "date": today, "by": me,
            "at": now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        }
        if not remote.upload_json(marker, path, backend=backend):
            return False
        # Re-read: if a racing host's stamp landed instead of ours, yield to them.
        confirm = remote.download_json(path, backend=backend)
        if confirm is not None and confirm.get("by") not in (me, None):
            return False
        return True
    except Exception:
        return False
```

- [ ] **Run, expect PASS.**
- [ ] **Commit:** `feat(retention): first-host-wins daily throttle marker`

### 5.2 — `_run_retention` archive loop (bounded + time-budgeted + best-effort)

- [ ] **Failing test.** Add `TestRunRetention(_FakeBus)`:
```python
class TestRunRetention(_FakeBus):
    def _terminal(self, tid, days_ago=40):
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        return {"id": tid, "title": tid, "status": "done", "workstream": "ws",
                "owner_agent": "a", "done_at": ts, "updated_at": ts}

    def _active(self, tid):
        ts = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        return {"id": tid, "title": tid, "status": "active", "workstream": "ws",
                "owner_agent": "a", "updated_at": ts}

    def test_archives_only_terminal_aged(self):
        tasks = [self._terminal("old-1"), self._terminal("old-2"), self._active("live-1")]
        for t in tasks:
            self._put(f"/coordination/tasks/{t['id']}.json", t)
        now = datetime.now(timezone.utc)
        with patch("fulcra_coord.cli._claim_retention_marker", return_value=True):
            res = cli._run_retention(tasks, now=now, deadline=time.monotonic() + 60, backend=self.backend)
        self.assertEqual(res["archived"], 2)
        self.assertFalse(self._exists("/coordination/tasks/old-1.json"))
        self.assertTrue(self._exists("/coordination/tasks/live-1.json"))

    def test_throttle_skips_when_already_ran(self):
        tasks = [self._terminal("old-1")]
        self._put("/coordination/tasks/old-1.json", tasks[0])
        with patch("fulcra_coord.cli._claim_retention_marker", return_value=False):
            res = cli._run_retention(tasks, now=datetime.now(timezone.utc),
                                     deadline=time.monotonic() + 60, backend=self.backend)
        self.assertEqual(res, {"skipped": True})
        self.assertTrue(self._exists("/coordination/tasks/old-1.json"))  # untouched

    def test_cap_defers_remainder(self):
        tasks = [self._terminal(f"old-{i}") for i in range(5)]
        for t in tasks:
            self._put(f"/coordination/tasks/{t['id']}.json", t)
        os.environ["FULCRA_COORD_RETENTION_MAX_PER_RUN"] = "2"
        try:
            with patch("fulcra_coord.cli._claim_retention_marker", return_value=True):
                res = cli._run_retention(tasks, now=datetime.now(timezone.utc),
                                         deadline=time.monotonic() + 60, backend=self.backend)
        finally:
            del os.environ["FULCRA_COORD_RETENTION_MAX_PER_RUN"]
        self.assertEqual(res["archived"], 2)
        self.assertEqual(res["deferred"], 3)

    def test_time_budget_stops_archiving(self):
        tasks = [self._terminal(f"old-{i}") for i in range(3)]
        for t in tasks:
            self._put(f"/coordination/tasks/{t['id']}.json", t)
        # An already-passed deadline means zero archived, never raises.
        with patch("fulcra_coord.cli._claim_retention_marker", return_value=True):
            res = cli._run_retention(tasks, now=datetime.now(timezone.utc),
                                     deadline=time.monotonic() - 1, backend=self.backend)
        self.assertEqual(res["archived"], 0)
        self.assertGreaterEqual(res["deferred"], 3)

    def test_per_item_failure_does_not_block_others(self):
        tasks = [self._terminal("good-1"), self._terminal("good-2")]
        for t in tasks:
            self._put(f"/coordination/tasks/{t['id']}.json", t)
        calls = {"n": 0}
        real = cli._archive_task
        def flaky(task, *, backend=None):
            calls["n"] += 1
            if task["id"] == "good-1":
                return False  # simulate a transient failure on one item
            return real(task, backend=backend)
        with patch("fulcra_coord.cli._claim_retention_marker", return_value=True), \
             patch("fulcra_coord.cli._archive_task", side_effect=flaky):
            res = cli._run_retention(tasks, now=datetime.now(timezone.utc),
                                     deadline=time.monotonic() + 60, backend=self.backend)
        self.assertEqual(res["archived"], 1)  # good-2 still archived

    def test_never_raises(self):
        with patch("fulcra_coord.cli._claim_retention_marker", side_effect=RuntimeError):
            res = cli._run_retention([], now=datetime.now(timezone.utc),
                                     deadline=time.monotonic() + 60, backend=self.backend)
        self.assertEqual(res, {"skipped": True})
```
(Add `import time` to the test module.)

- [ ] **Run, expect FAIL.** `uv run --extra dev python -m pytest tests/test_retention.py::TestRunRetention -v`
- [ ] **Minimal impl.** In `cli.py`, add the env resolver + `_run_retention`:
```python
def _retention_max_per_run():
    """Per-run archive cap: env FULCRA_COORD_RETENTION_MAX_PER_RUN (default 200).
    A huge first backlog drains over several daily passes rather than blowing
    reconcile's deadline. Non-numeric -> default (best-effort, never crashes)."""
    raw = os.environ.get("FULCRA_COORD_RETENTION_MAX_PER_RUN", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return 200


# Wall-clock seconds of headroom to leave before reconcile's deadline. Archiving
# stops once less than this remains, so the view uploads + presence rebuild that
# already ran keep their result and reconcile returns inside its 90s ceiling.
_RETENTION_DEADLINE_HEADROOM_SECONDS = 5.0


def _run_retention(all_tasks, *, now, deadline, backend=None):
    """The retention pass, folded into reconcile (spec §6). Best-effort: NEVER
    raises into the reconcile tick — any failure returns a result dict, logged by
    the caller. Returns {"skipped": True} when throttled/errored, else
    {"archived": N, "deferred": D, "pruned_markers": M, "pruned_presence": K}.

    1. THROTTLE: _claim_retention_marker(now) — first host today wins; others skip.
    2. ARCHIVE up to _retention_max_per_run() archivable tasks (views.
       is_archivable_task), stopping early when the TIME BUDGET (caller's
       reconcile `deadline` minus a few seconds' headroom) is nearly spent. The
       remainder is DEFERRED (counted + logged) and drains next pass.
    3. PRUNE spent markers + dead presence (Task 6 fills these in).
    Per-item isolation: one task's archive failure is skipped, not fatal."""
    try:
        if not _claim_retention_marker(now, backend=backend):
            return {"skipped": True}
    except Exception:
        return {"skipped": True}

    import time
    cap = _retention_max_per_run()
    budget_floor = deadline - _RETENTION_DEADLINE_HEADROOM_SECONDS
    candidates = [t for t in all_tasks if views.is_archivable_task(t, now)]
    archived = 0
    deferred = 0
    for t in candidates:
        if archived >= cap or time.monotonic() >= budget_floor:
            deferred += 1
            continue
        try:
            if _archive_task(t, backend=backend):
                archived += 1
            else:
                deferred += 1  # transient failure; retried next pass
        except Exception:
            deferred += 1

    pruned_markers = _prune_markers(now, backend=backend)
    pruned_presence = _prune_dead_presence(now, backend=backend)
    return {"archived": archived, "deferred": deferred,
            "pruned_markers": pruned_markers, "pruned_presence": pruned_presence}
```
For now stub `_prune_markers`/`_prune_dead_presence` to `return 0` (Task 6 implements them) so the archive tests pass; the prune tests in Task 6 will flesh them out.

- [ ] **Run, expect PASS.**
- [ ] **Commit:** `feat(retention): bounded, time-budgeted, best-effort archive pass`

### 5.3 — Fold `_run_retention` into `cmd_reconcile`

- [ ] **Failing test.** Add to `TestRunRetention` (or a `TestReconcileIntegration(_FakeBus)`):
```python
    def test_reconcile_calls_run_retention(self):
        with patch("fulcra_coord.cli._run_retention",
                   return_value={"archived": 0, "deferred": 0,
                                 "pruned_markers": 0, "pruned_presence": 0}) as rr:
            ns = type("A", (), {})()
            cli.cmd_reconcile(ns, backend=self.backend)
        rr.assert_called_once()
        # deadline kwarg must be the reconcile deadline (composes, not double-counts).
        self.assertIn("deadline", rr.call_args.kwargs)
```

- [ ] **Run, expect FAIL.** `uv run --extra dev python -m pytest tests/test_retention.py -k reconcile_calls -v`
- [ ] **Minimal impl.** In `cmd_reconcile`, after the `_sweep_review_routes(...)` try/except block (≈ line 3382) and BEFORE `if failures:` (line 3384), add:
```python
    # Retention pass (best-effort, throttled to ~once/day, bounded + time-budgeted
    # against THIS reconcile's deadline so it never double-counts the 90s ceiling).
    # Never raises into the tick; logs its tally.
    try:
        ret = _run_retention(all_tasks, now=now, deadline=deadline, backend=backend)
        if not ret.get("skipped"):
            _info(f"  Retention: archived {ret['archived']} task(s) "
                  f"(deferred {ret['deferred']}), pruned {ret['pruned_markers']} marker(s), "
                  f"{ret['pruned_presence']} dead presence.")
    except Exception as e:
        _warn(f"  Retention pass error (skipped): {e}")
```

- [ ] **Run, expect PASS.** Also run the full suite to confirm `cmd_reconcile`'s existing tests still pass: `uv run --extra dev python -m pytest tests/ -q`. Then `git checkout -- uv.lock packages/fulcra-coord/uv.lock`.
- [ ] **Commit:** `feat(retention): fold retention pass into reconcile`

---

## Task 6 — `remote.delete` + marker/presence prune (the no-history half)

`6.1` (the `remote.delete` wrapper + fake-backend `delete`) is pulled forward to before Task 2.2 (see note there). The rest — the prune functions — land here.

### 6.1 — `remote.delete` + fake-backend `delete` (DO BEFORE Task 2.2)

- [ ] **Failing test.** Add `TestRemoteDelete(_FakeBus)`:
```python
class TestRemoteDelete(_FakeBus):
    def test_delete_removes_file(self):
        self._put("/coordination/x.json", {"a": 1})
        self.assertTrue(remote.delete("/coordination/x.json", backend=self.backend))
        self.assertFalse(self._exists("/coordination/x.json"))

    def test_delete_missing_is_false(self):
        self.assertFalse(remote.delete("/coordination/nope.json", backend=self.backend))
```

- [ ] **Run, expect FAIL.** `uv run --extra dev python -m pytest tests/test_retention.py::TestRemoteDelete -v`
- [ ] **Minimal impl.** In `remote.py`, after `list_files` (≈ line 216):
```python
def delete(
    remote_path: str,
    *,
    backend: Optional[list[str]] = None,
    timeout: Optional[int] = None,
) -> bool:
    """Delete a remote file. Returns True on success.

    Wraps `fulcra file delete <PATH>` — a platform SOFT-delete (the file is
    recoverable via the platform's version history / trash), so a wrongly-pruned
    marker or presence record is not gone forever. Path-based (matches stat/
    download/upload), unlike the platform `restore` which takes a version UUID.
    Best-effort like every other wrapper: a missing CLI / non-zero exit / timeout
    returns False rather than raising, so a prune failure never crashes the
    reconcile tick."""
    cmd = (backend or _backend_cmd()) + ["delete", remote_path]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout or _write_timeout(),
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
```
In `tests/fake_fulcra_backend.py`, add before the unknown-command fallthrough:
```python
    if cmd == "delete":
        local = _local_for(root, argv[1])
        if not local.exists():
            return 1
        local.unlink()
        return 0
```
Update the module docstring's subcommand list to mention `delete`.

- [ ] **Run, expect PASS.**
- [ ] **Commit:** `feat(retention): remote.delete soft-delete wrapper + fake-backend support`

### 6.2 — `_prune_markers`

- [ ] **Failing test.** Add `TestPruneMarkers(_FakeBus)`:
```python
class TestPruneMarkers(_FakeBus):
    def test_prunes_old_keeps_recent(self):
        now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        self._put("/coordination/digest/markers/2026-05-20-morning.json", {"x": 1})  # old
        self._put("/coordination/digest/markers/2026-06-03-evening.json", {"x": 1})  # recent
        pruned = cli._prune_markers(now, backend=self.backend)
        self.assertEqual(pruned, 1)
        self.assertFalse(self._exists("/coordination/digest/markers/2026-05-20-morning.json"))
        self.assertTrue(self._exists("/coordination/digest/markers/2026-06-03-evening.json"))

    def test_empty_dir_prunes_nothing(self):
        self.assertEqual(cli._prune_markers(datetime.now(timezone.utc), backend=self.backend), 0)
```

- [ ] **Run, expect FAIL.** `uv run --extra dev python -m pytest tests/test_retention.py::TestPruneMarkers -v`
- [ ] **Minimal impl.** Replace the `_prune_markers` stub in `cli.py`:
```python
def _prune_markers(now, *, backend=None):
    """Delete spent digest dedup markers older than the marker-retention window.

    Lists digest/markers/, deletes each path views.is_prunable_marker flags.
    Markers are regenerable guards with NO history value, so they are deleted
    (platform soft-delete keeps them restorable), not archived. Best-effort: a
    failed listing prunes nothing; one failed delete is skipped, not fatal.
    Returns the count deleted."""
    n = 0
    try:
        for path in remote.list_files(remote.digest_markers_prefix(), backend=backend):
            if not path.endswith(".json"):
                continue
            if views.is_prunable_marker(path, now):
                try:
                    if remote.delete(path, backend=backend):
                        n += 1
                except Exception:
                    continue
    except Exception:
        pass
    return n
```

- [ ] **Run, expect PASS.**
- [ ] **Commit:** `feat(retention): prune spent digest markers`

### 6.3 — `_prune_dead_presence`

- [ ] **Failing test.** Add `TestPruneDeadPresence(_FakeBus)`:
```python
class TestPruneDeadPresence(_FakeBus):
    def _put_presence(self, slug, days_ago):
        ls = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        self._put(f"/coordination/presence/{slug}.json", {"agent": slug, "last_seen": ls})

    def test_prunes_dead_keeps_live(self):
        self._put_presence("dead-agent", 40)
        self._put_presence("live-agent", 1)
        n = cli._prune_dead_presence(datetime.now(timezone.utc), backend=self.backend)
        self.assertEqual(n, 1)
        self.assertFalse(self._exists("/coordination/presence/dead-agent.json"))
        self.assertTrue(self._exists("/coordination/presence/live-agent.json"))

    def test_skips_presence_aggregate_view(self):
        # The aggregate lives under views/, not presence/, so it's never listed
        # here — but guard that a malformed record without last_seen is kept.
        self._put("/coordination/presence/weird.json", {"agent": "weird"})
        n = cli._prune_dead_presence(datetime.now(timezone.utc), backend=self.backend)
        self.assertEqual(n, 0)
        self.assertTrue(self._exists("/coordination/presence/weird.json"))
```

- [ ] **Run, expect FAIL.** `uv run --extra dev python -m pytest tests/test_retention.py::TestPruneDeadPresence -v`
- [ ] **Minimal impl.** Replace the `_prune_dead_presence` stub:
```python
def _prune_dead_presence(now, *, backend=None):
    """Delete per-agent presence records for long-departed agents.

    Lists presence/, downloads each record, deletes those
    views.is_prunable_presence flags (last_seen older than the presence-retention
    window). Presence is a live SNAPSHOT, not history, so it's deleted (platform
    soft-delete keeps it restorable), not archived; a pruned agent also drops from
    the presence aggregate on the next rebuild (already a derived view). Best-
    effort, per-item isolated. Returns the count deleted."""
    n = 0
    try:
        for path in remote.list_files(remote.presence_prefix(), backend=backend):
            if not path.endswith(".json"):
                continue
            try:
                rec = remote.download_json(path, backend=backend)
                if rec and views.is_prunable_presence(rec, now):
                    if remote.delete(path, backend=backend):
                        n += 1
            except Exception:
                continue
    except Exception:
        pass
    return n
```

- [ ] **Run, expect PASS.** Run the full suite: `uv run --extra dev python -m pytest tests/ -q`. Then `git checkout -- uv.lock packages/fulcra-coord/uv.lock`.
- [ ] **Commit:** `feat(retention): prune dead-agent presence records`

### 6.4 — Verify the automatic hot-path-exclusion claim (integration test)

This is the spec's load-bearing §4 claim: a moved task disappears from the aggregate/views with zero filter code. Prove it end-to-end.

- [ ] **Failing test (then passing — it asserts existing behavior holds after a move).** Add `TestHotPathExclusion(_FakeBus)`:
```python
class TestHotPathExclusion(_FakeBus):
    def test_archived_task_absent_from_rebuilt_views(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        old = {"id": "old-1", "title": "old", "status": "done", "workstream": "ws",
               "owner_agent": "a", "done_at": old_ts, "updated_at": old_ts}
        live = {"id": "live-1", "title": "live", "status": "active", "workstream": "ws",
                "owner_agent": "a", "updated_at": datetime.now(timezone.utc).isoformat(
                    timespec="microseconds").replace("+00:00", "Z")}
        self._put("/coordination/tasks/old-1.json", old)
        self._put("/coordination/tasks/live-1.json", live)
        # Archive the old one (the move), then rebuild the self-heal source.
        self.assertTrue(cli._archive_task(old, backend=self.backend))
        # The self-heal listing (tasks/) must no longer include old-1.
        listed = remote.list_files("/coordination/tasks/", backend=self.backend)
        ids = {p.rsplit("/", 1)[-1][:-5] for p in listed if p.endswith(".json")}
        self.assertNotIn("old-1", ids)
        self.assertIn("live-1", ids)
        # And a summaries rebuild over the remaining tasks excludes it with NO filter.
        rebuilt = cli._load_summaries_for_rebuild(live, backend=self.backend)
        rebuilt_ids = {s["id"] for s in rebuilt}
        self.assertNotIn("old-1", rebuilt_ids)
        self.assertIn("live-1", rebuilt_ids)
```

- [ ] **Run, expect PASS** (this validates the design claim, not new code):
  `uv run --extra dev python -m pytest tests/test_retention.py::TestHotPathExclusion -v`. Then `git checkout -- uv.lock packages/fulcra-coord/uv.lock`.
- [ ] **Commit:** `test(retention): verify automatic hot-path exclusion after archive move`

---

## Task 7 — Version bump + CHANGELOG (rebase-aware)

- [ ] **No test** (metadata). Bump `fulcra_coord/__init__.py`: `__version__ = "0.7.0"` → `"0.8.0"`.
- [ ] **CHANGELOG.** Insert a new section ABOVE the existing `## [0.7.0] — Liveness-Aware Reviewer Routing` block (CHANGELOG is topped by 0.7.0 today):
```markdown
## [0.8.0] — Bus Retention / Archival

**Why:** The coordination bus grew without bound. Terminal (done/abandoned)
tasks stayed under `tasks/` forever — bloating `views/summaries.json` (the
aggregate behind the read perf win), growing the `tasks/` listing self-heal
enumerates on every write, and swelling recently-done/search. Digest markers and
dead-agent presence records also accumulated. Reads and reconcile slowly
degraded and the operator surfaces got noisier; nothing removed anything.

**What:**
- Terminal tasks aged past `FULCRA_COORD_RETENTION_DAYS` (default 30) are
  crash-safely MOVED to `archive/tasks/<YYYY-MM>/<id>.json` with an append-only
  per-id cold-index shard `archive/index/<id>.json` (no shared mutable index —
  Files has no CAS). Moving the body out of `tasks/` removes it from the
  aggregate, views, and self-heal automatically — zero read-path filter code.
- `search --archived` (alias `--all`) scans the cold index; default search stays
  hot-only and fast. `restore <id>` moves an archived body back into `tasks/`.
- Spent digest markers (>`FULCRA_COORD_MARKER_RETENTION_DAYS`, default 7) and
  dead-agent presence (>`FULCRA_COORD_PRESENCE_RETENTION_DAYS`, default 30) are
  soft-deleted via `fulcra file delete` (platform-restorable).
- The pass is folded into `reconcile`, self-throttled to ~once/day via a
  first-host-wins `retention/last-run.json` marker (the digest-marker pattern) —
  no new scheduler. Bounded by `FULCRA_COORD_RETENTION_MAX_PER_RUN` (default 200)
  + a time budget that composes with reconcile's deadline; best-effort
  (never raises into a tick). No data loss by construction (write→verify→delete).

**How tested:** new `tests/test_retention.py` — policy predicates (cutoff
boundaries, non-terminal exclusion), crash-safe move (write→verify→delete order,
crash-mid-move completion, idempotency), append-only shards + concurrent distinct
writes, `search --archived` / `restore`, throttle + cap + time-budget + best-
effort, marker/presence prune, and a VERIFIED automatic-hot-path-exclusion test.

---
```

- [ ] **Rebase-aware note:** if `origin/main` advanced past 0.7.0 before this lands, set `__version__` to the next minor above the new tip and retitle the section accordingly; keep the CHANGELOG entry directly above whatever the current top entry is.
- [ ] **Run full suite once more:** `uv run --extra dev python -m pytest tests/ -q`. Then `git checkout -- uv.lock packages/fulcra-coord/uv.lock`.
- [ ] **Commit:** `chore(retention): bump version 0.7.0 -> 0.8.0 + CHANGELOG`

---

## Self-review

### (a) Spec coverage — every decision maps to a task

| Spec § / decision | Task |
|---|---|
| §1 `is_archivable_task` (terminal+aged, parse-don't-lex, cutoff off-by-one) | 1.1 |
| §1 `is_prunable_marker` | 1.2 |
| §1 `is_prunable_presence` | 1.3 |
| §1 env vars `RETENTION_DAYS=30` / `MARKER_RETENTION_DAYS=7` / `PRESENCE_RETENTION_DAYS=30` | 1.1–1.3 resolvers |
| §2 `_archive_task` write→verify→delete crash-safe move | 2.2 |
| §2 idempotent already-archived no-op | 2.2 (`test_idempotent...`) |
| §2 crash-mid-move completes next pass | 2.2 (`test_crash_between...`) |
| §3 append-only per-id shards, NO single `archive/index.json` | 2.2 (`_archive_index_shard` writes distinct path), 3.1 (list), explicit in path-helper docstring |
| §3 concurrent archivers write distinct shards (no clobber) | covered by distinct-path design; asserted via 6.4 + 2.2 (each id → own path); add an explicit two-id concurrent write assertion in `TestIndexShards` if desired |
| §4 automatic hot-path exclusion (VERIFIED) | 6.4 `TestHotPathExclusion` — lists `tasks/` + rebuilds summaries, asserts moved id absent, zero filter code |
| §5 `search --archived` lists shards; default search doesn't | 4.1 |
| §5 `restore` moves body back, deletes shard | 4.2 |
| §6 throttle first-host-wins `retention/last-run.json` (digest-marker pattern) | 5.1 |
| §6 bounded cap `RETENTION_MAX_PER_RUN=200` + time budget composes with reconcile deadline | 5.2 (`test_cap_defers`, `test_time_budget_stops`) |
| §6 archive loop folded into reconcile | 5.3 |
| §6 best-effort never raises, per-item isolation | 5.2 (`test_per_item_failure`, `test_never_raises`) |
| §7 marker prune | 6.2 |
| §7 presence prune | 6.3 |
| `fulcra file delete` soft-delete | 6.1 (`remote.delete`, confirmed real CLI has it) |
| Machine-agnostic invariant | 5.1 (first-host-wins) + 2.2 (idempotent per-task) + distinct shard paths |
| Version bump 0.7.0→0.8.0 + CHANGELOG (rebase-aware, topped by 0.7.0) | 7 |

### (b) Placeholder scan
No `TODO`, no `...`, no `<fill in>`. Every code block is concrete with real signatures: `views._parse_dt`, `views._done_at`, `remote.upload_json/download_json/stat/list_files/delete`, `identity.resolve_agent`, `_now_iso`, the `cmd_reconcile` `deadline`/`now` locals at lines 3280/3295, the `search` parser at entry.py:250, `COMMAND_MAP` at entry.py:442. Env var names and defaults are exact. The only deliberately deferred bit (the `_prune_*` stubs in 5.2) is explicitly called out as filled by Task 6, and `remote.delete` is explicitly pulled forward (6.1 before 2.2).

### (c) Name / type / path consistency (checked across tasks)
- Predicate signatures uniform: `is_archivable_task(task, now=None, retention_days=None)`, `is_prunable_marker(path, now=None, marker_days=None)`, `is_prunable_presence(record, now=None, presence_days=None)` — all `now`-injectable, all parse-don't-lex, all "missing timestamp ⇒ keep (safe direction)".
- Shard path scheme identical everywhere: `archive/tasks/<YYYY-MM>/<id>.json` (body) and `archive/index/<id>.json` (shard), built only via `remote.archive_task_path` / `remote.archive_index_path` — never hand-concatenated in cli.py.
- Throttle marker path identical: `remote.retention_marker_path(now)` → `retention/last-run.json`, date INSIDE the JSON (matches `_claim_retention_marker`'s `existing.get("date") == today`).
- `_run_retention(all_tasks, *, now, deadline, backend=None)` signature matches both its caller (5.3, passes reconcile's `now` + `deadline`) and its tests (5.2).
- `_archive_task(task, *, backend=None) -> bool`, `cmd_restore(args, backend=None) -> int`, `cmd_search`'s `args.archived` flag (entry sets `dest="archived"` for both `--archived` and `--all`) all consistent between impl, tests, and entry wiring.
- Index-shard fields (`id, title, status, workstream, owner_agent, done_at, archived_at, archive_path`) match the spec §3 list exactly and are all present on `task_summary` / derivable.

### Resolved spec ambiguities (grounding-driven)
1. **`fulcra file delete` / `restore` existence.** CONFIRMED the real `fulcra-api file` CLI exposes both `delete PATH` (soft-delete) and `restore VERSION_ID`. `remote.py` had NO `delete` — Task 6.1 adds the wrapper; the fake backend needed a `delete` subcommand (also Task 6.1).
2. **`restore` semantics.** The platform `fulcra file restore` takes a VERSION UUID, not a path — it un-does a *version*, not a path-delete. Archived tasks are MOVED (not deleted), so the retention `restore` command is a bus-level move-back (download archive body → upload to `tasks/` → delete shard) and does NOT depend on platform version-restore. Platform soft-delete `restore` is only the safety net for *pruned* markers/presence. Documented in `cmd_restore`'s docstring.
3. **Remote subdir listing.** CONFIRMED `remote.list_files(prefix)` works for subdirs; the fake backend's `list` is recursive (`rglob`), so `list archive/index/`, `digest/markers/`, `presence/` all return their files. `archive/` is a SIBLING of `tasks/` (not nested), so listing `tasks/` for self-heal never accidentally catches archive bodies.
4. **Automatic hot-path exclusion mechanism (VERIFIED).** `build_summaries` and all view builders iterate only the `tasks` LIST passed in — they do no I/O. The list is sourced (a) on read/reconcile from `_load_all_tasks` (ids from `index`/`search-index`/`next` views) and (b) on write from `_load_summaries_for_rebuild`'s self-heal, which lists `tasks/`. A moved body is absent from BOTH the views (it's no longer terminal-within-cutoff and its file is gone) and the `tasks/` listing, so it drops out with zero filter code. Task 6.4 proves this end-to-end.
5. **Archivable timestamp safe-direction.** `is_stale` fails toward SURFACING (clockless ⇒ +inf ⇒ stale). For a destructive MOVE we invert: a clockless/undatable terminal task is NOT archived (and undatable markers/presence are NOT pruned). Documented in each predicate.
