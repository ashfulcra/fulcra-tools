# Coordination-System Health Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a silently-degraded coordination bus *visible* by having each host self-report a `health/<slug>.json` record on every successful reconcile, judging staleness of that self-report as the degradation signal, and surfacing it through a `health` command, the `doctor` fold, and the twice-daily operator digest.

**Architecture:** `cmd_reconcile` writes a per-host health record as its *own* failure-isolated `remote.upload_json` call, placed AFTER the `if failures: return 1` guard so a failed reconcile leaves its record stale (the contract). A pure `views.assess_infra_health()` reads all `health/*.json` plus bus markers and renders a status (healthy / degraded / outage / not-reporting) gated ONLY on reconcile-staleness; the `health` command, `doctor`, and the operator digest all reuse it. Retention prunes `health/` on the dead-presence window.

**Tech Stack:** stdlib-only Python; unittest+pytest; Fulcra Files bus (no CAS); reconcile-piggybacked health record; staleness-as-signal.

---

## Pre-flight notes

All line numbers are against `origin/main` @ `b45a459` in the worktree
`/private/tmp/fc-health/packages/fulcra-coord/`.

### THE SUCCESS-POINT PLACEMENT (the #1 thing reviewers check)

`cmd_reconcile` is `fulcra_coord/cli.py:3628`. Its tail (lines `3740`–`3751`):

```python
3740	    if failures:
3741	        _warn(f"  View upload failures: {failures}")
3742	        ops_log.log_op("reconcile", status="partial", detail=f"failed views: {failures}")
3743	        # Do NOT clear op markers — views are still broken and need another reconcile run.
3744	        return 1
3745	
3746	    for m in needs_repair:
3747	        cache.clear_op_marker(m["op_id"])
3748	
3749	    ops_log.log_op("reconcile", status="ok", detail=f"{len(all_tasks)} tasks, {len(all_views)} views")
3750	    _info(f"  Reconcile complete. {len(all_views)} views refreshed.")
3751	    return 0
```

**The health write MUST go between line 3744 (`return 1`) and line 3746 (the
`for m in needs_repair` marker-clear), i.e. on the success side of the
`if failures: return 1` guard.** Concretely, insert a best-effort block right
after line 3744's guard closes (just before `for m in needs_repair:` at 3746).

WHY exactly there (do not move it):
- It must be AFTER `if failures: return 1` (3740–3744). The view uploads happen
  in a `ThreadPoolExecutor` batch at lines `3700`–`3704` that **completes before**
  `failures` is even evaluated. If the health file were a member of that batch
  (or written before line 3740), it would upload on a *failing* reconcile and
  falsely read healthy — the v1 review's #1 hole. Placing it after the guard
  means a partial/failed reconcile `return 1`s first and its record stays stale.
- It must NOT be gated on the best-effort sub-passes `_sweep_review_routes`
  (3723–3726) and `_run_retention` (3731–3738). Those already ran *above* the
  failures guard and are designed to never fail the tick (each wrapped in
  `try/except: pass` / logged). They are upstream of our insertion point, so by
  construction the health write is not gated on them — keep it that way. Their
  flakiness must never suppress a healthy heartbeat.
- It must be its OWN `remote.upload_json` call wrapped best-effort: a health-write
  failure logs and never changes `cmd_reconcile`'s return code (still `return 0`
  at 3751).

### Runtime locals in scope at the insertion point (3744→3746)

These are all live and assembled into the record (no new I/O for the counts):
- `t0 = time.monotonic()` (3632) → `duration_s = round(time.monotonic() - t0, 3)`.
- `now = datetime.now(timezone.utc)` (3649) → `reconcile_at`.
- `deadline = t0 + timeout` (3634) — available if needed; not used in the record.
- `all_tasks` (3642/3645) → `tasks_loaded = len(all_tasks)` and
  `bus_task_count = len(all_tasks)` (same number; both recorded per spec §1 field
  list — `tasks_loaded` is the load count, `bus_task_count` the bus size; identical
  here, kept as two fields for forward-compat / spec fidelity).
- `all_views` (3659) → `views_refreshed = len(all_views)`.
- `needs_repair` (3637) → `repair_backlog = len(needs_repair)`.
- `failures == []` is guaranteed true at this point (we are past the guard).

### Signatures grounded (exact)

**remote.py**
- `upload_json(data: dict, remote_path: str, *, backend=None, timeout=None) -> bool` (`remote.py:180`). Returns True on success; never raises (delegates to `upload`, which catches `TimeoutExpired/FileNotFoundError/OSError`).
- `download_json(remote_path: str, *, backend=None, timeout=None) -> Optional[dict]` (`remote.py:129`). Returns None on missing/garbage.
- `list_files(prefix: str, *, backend=None, timeout=None) -> list[str]` (`remote.py:220`). Returns `[]` on error.
- `delete(remote_path: str, *, backend=None) -> bool` (`remote.py:196`). Soft-delete; never raises.
- `remote_root() -> str` (`fulcra_coord/__init__.py:21`, re-exported; in `cli.py` it is imported as `from . import ... remote_root as get_remote_root` inside `cmd_doctor` at 3894). Returns `/coordination`-style root.
- Path-helper convention (`remote.py:331`–`398`): `f"{remote_root()}/tasks/{id}.json"`, `presence_prefix() -> f"{remote_root()}/presence/"` (396), `retention_marker_path(now) -> f"{remote_root()}/retention/last-run.json"` (383), `digest_markers_prefix() -> f"{remote_root()}/digest/markers/"` (391). **New helpers `health_remote_path(slug)` and `health_prefix()` mirror these exactly** (Task 2).

**views.py**
- `_parse_dt(iso: str) -> Optional[datetime]` (`views.py:166`) — tz-aware UTC, naive→UTC coerce, unparseable→None. **All datetime gates use this; never lexical.**
- `_now() -> datetime` (`views.py:162`) — `datetime.now(timezone.utc)`.
- `agent_slug(agent: str) -> str` (`views.py:455`) — colon-collapsing filesystem slug; `*`→`broadcast`. This is the slug used for `health/<slug>.json` (same as the listener/inbox slug; `listener.agent_slug` is re-exported and `_inbox_surface_path` uses `listener.agent_slug`).
- `is_prunable_presence(record, now=None, presence_days=None) -> bool` (`views.py:306`) — `last_seen` older than the presence-retention window (`_presence_retention_days`, env `FULCRA_COORD_PRESENCE_RETENTION_DAYS`, default `PRESENCE_RETENTION_DAYS_DEFAULT` ≈ 30d). Undatable→KEPT (returns False). Task 5 mirrors this for `health/` keyed on `reconcile_at`.
- `_presence_retention_days(days=None) -> float` (`views.py:246`) — the window Task 5 reuses for `health/`.
- `is_prunable_marker(path, now=None, marker_days=None) -> bool` (`views.py:286`) + `_MARKER_DATE_RE = re.compile(r"/(\d{4}-\d{2}-\d{2})-[^/]+\.json$")` (`views.py:283`) — used as the model for reading the freshest `digest/markers/<YYYY-MM-DD>-<window>.json` date (bus-global `digest_last_emit`).
- `build_operator_digest(summaries, presence, *, human, now=None, since=None) -> dict` (`views.py:1196`) — returns `{schema, human, now, since, blocked_on_you, upcoming, per_agent, stale}`. Task 4 adds an `infra` key.
- `agent_slug` returns lowercased; record `host` is the raw `socket.gethostname().split('.')[0]` (matching `identity.derived_agent()` at `identity.py:99`) and `agent` is `identity.resolve_agent()`.

**cli.py**
- `cmd_reconcile(args, backend=None) -> int` (`cli.py:3628`) — Task 2 insertion (above).
- `_render_digest(digest, *, window) -> tuple[str, str]` (`cli.py:2200`) — Task 4 appends the infra line to `sections`.
- `cmd_digest` (`cli.py:2493`) builds via `build_operator_digest` (2518) and renders via `_render_digest` (2525). No change to `cmd_digest` itself; Task 4 changes the builder + renderer.
- `_run_retention(all_tasks, *, now, deadline, backend=None) -> dict` (`cli.py:2440`); `_prune_dead_presence(now, *, backend=None) -> int` (`cli.py:2412`) is the exact mirror for Task 5's `_prune_dead_health`.
- `cmd_doctor(args, backend=None) -> int` (`cli.py:3891`); its tail before the final summary is the `[Annotations]` block ending `cli.py:3997`, then `_info(f"\n{'='*50}")` (3999) and `return 0 if ok_all else 1` (4001). Task 3 folds a `[Fleet health]` block in just before line 3999.
- `_inbox_surface_path(agent) -> Path` (`cli.py:2039`): `cache.cache_root() / f"inbox-pending-{listener.agent_slug(agent)}.json"`. Its `.stat().st_mtime` is the `listener_last_fire` source (listener writes it every tick unconditionally → "listener fired"; meaningful only in *this host's own* record). Best-effort: `os.path.getmtime`, absent→None.
- `_claim_retention_marker`/`_claim_digest_marker` (`cli.py:2321`/`2286`) show the marker read pattern; the health record's `retention_last_run` reads `remote.download_json(remote.retention_marker_path(now))` and takes `.get("date")`/`.get("at")`.

**Heartbeat interval (degraded-default = interval×3)**
- There is **no env override** for the heartbeat interval; the only source is `heartbeat.INTERVAL_MIN_DEFAULT = 20` (minutes) at `heartbeat.py:52`. So the degraded default is `INTERVAL_MIN_DEFAULT * 60 * 3 = 3600s` (1h). The reader `views._health_degraded_seconds()` resolves env `FULCRA_COORD_HEALTH_DEGRADED_SECONDS` first, then falls back to `heartbeat.INTERVAL_MIN_DEFAULT * 60 * 3`. (Import `heartbeat` lazily inside the reader to avoid any import cycle — `views.py` must stay import-light; use `from . import heartbeat` inside the function.)

**entry.py**
- `build_parser()` (`entry.py:13`); subparser pattern, e.g. `sub.add_parser("capabilities", ...)` then `sp.add_argument("--format", choices=["table", "json"], default="table")` (267–271). `COMMAND_MAP` (`entry.py:451`) maps `"capabilities": _cli.cmd_capabilities` (472). Task 3 adds `"health"` subparser + `--format` + `"health": _cli.cmd_health`.

**Test conventions**
- Run: `uv run --extra dev python -m pytest -q` (stdlib unittest classes under pytest). Never commit `uv.lock`.
- `tests/conftest.py` is autouse-hermetic: redirects `XDG_CACHE_HOME` to a temp dir and defaults `FULCRA_COORD_BACKEND=false` so an unmocked remote op shells out to `false` (no-op), never the live bus.
- `backend=["false"]` = the always-failing backend (every `upload_json`/`list_files` returns falsy/empty). Tests that need success patch `fulcra_coord.cli.remote.upload_json` / `.download_json` / `.list_files` with `unittest.mock.patch` (see `TestReconcilePreservesMarkersOnFailure`, `cli.py` test at `test_fulcra_coord.py:1622`).
- `tests/fake_fulcra_backend.py` is the stateful fake bus (`_FAKE = str(Path(__file__).parent / "fake_fulcra_backend.py")`, used as `backend=[sys.executable, _FAKE]` in `test_retention.py:150`).
- Version pins to update in Task 6: `tests/test_operator_digest.py:424` (`test_version_is_083` → asserts `"0.8.3"`) and `tests/test_fulcra_coord.py:7053` (`test_version_is_0_8_3` → asserts `"0.8.3"`). Rename + retarget both to `0.9.0`.

---

## File Structure

| File | Responsibility | Change |
| --- | --- | --- |
| `fulcra_coord/views.py` | Pure health judgment + env-knob readers | **+** `assess_infra_health()`, `_health_degraded_seconds()`, `_health_outage_seconds()`, `is_prunable_health()`; **~** `build_operator_digest()` gains `infra` key (Task 4) |
| `fulcra_coord/remote.py` | Bus path helpers | **+** `health_remote_path(slug)`, `health_prefix()` |
| `fulcra_coord/cli.py` | Reconcile health write, `health` cmd, doctor fold, digest render, retention prune | **~** `cmd_reconcile` (Task 2 write); **+** `_build_health_record()`, `cmd_health()`, `_prune_dead_health()`; **~** `cmd_doctor` (fold), `_render_digest` (infra line), `_run_retention` (health branch) |
| `fulcra_coord/entry.py` | CLI wiring | **+** `health` subparser + `--format`; `"health"` in `COMMAND_MAP` |
| `fulcra_coord/__init__.py` | Version | **~** `__version__` 0.8.3 → 0.9.0 |
| `CHANGELOG.md` | Release notes | **+** `[0.9.0]` section |
| `tests/test_health.py` | New test module for §2/§3/§5 | **+** new file |
| `tests/test_fulcra_coord.py` | Reconcile health-write tests + version pin | **+** tests; **~** version-pin test |
| `tests/test_operator_digest.py` | Digest infra-line tests + version pin | **+** tests; **~** version-pin test |

---

### Task 1 — `views.assess_infra_health` (pure judgment) + env-knob readers

Pure, no I/O, most tests — do FIRST. Lives in `views.py` next to the other pure
judges (after `is_prunable_presence`, ~`views.py:320`).

**Step 1.1 — env-knob readers (test first)**

- [ ] In `tests/test_health.py` add `class TestHealthKnobs(unittest.TestCase)`:
```python
import os
import unittest
from fulcra_coord import views, heartbeat


class TestHealthKnobs(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FULCRA_COORD_HEALTH_DEGRADED_SECONDS", None)
        os.environ.pop("FULCRA_COORD_HEALTH_OUTAGE_SECONDS", None)

    def test_degraded_default_is_interval_times_three(self):
        os.environ.pop("FULCRA_COORD_HEALTH_DEGRADED_SECONDS", None)
        self.assertEqual(views._health_degraded_seconds(),
                         heartbeat.INTERVAL_MIN_DEFAULT * 60 * 3)

    def test_degraded_env_override(self):
        os.environ["FULCRA_COORD_HEALTH_DEGRADED_SECONDS"] = "300"
        self.assertEqual(views._health_degraded_seconds(), 300.0)

    def test_degraded_garbage_env_falls_back(self):
        os.environ["FULCRA_COORD_HEALTH_DEGRADED_SECONDS"] = "not-a-number"
        self.assertEqual(views._health_degraded_seconds(),
                         heartbeat.INTERVAL_MIN_DEFAULT * 60 * 3)

    def test_outage_default_is_three_hours(self):
        os.environ.pop("FULCRA_COORD_HEALTH_OUTAGE_SECONDS", None)
        self.assertEqual(views._health_outage_seconds(), 3 * 3600.0)

    def test_outage_env_override(self):
        os.environ["FULCRA_COORD_HEALTH_OUTAGE_SECONDS"] = "7200"
        self.assertEqual(views._health_outage_seconds(), 7200.0)
```
- [ ] Run the module: `uv run --extra dev python -m pytest -q tests/test_health.py` — RED (no readers yet).
- [ ] Implement in `views.py` (mirror `_presence_retention_days` at 246):
```python
HEALTH_OUTAGE_SECONDS_DEFAULT = 3 * 3600  # ~3h


def _health_degraded_seconds(seconds=None):
    """Age (s) past which a host's newest reconcile_at is 'degraded'.

    Default ties to the heartbeat interval (interval x 3) — not bare wall-clock —
    so one slow or skipped tick can't flap a host to degraded. interval has no env
    override; INTERVAL_MIN_DEFAULT (minutes) is the only source. Env
    FULCRA_COORD_HEALTH_DEGRADED_SECONDS overrides; non-numeric -> default."""
    if seconds is not None:
        return float(seconds)
    raw = os.environ.get("FULCRA_COORD_HEALTH_DEGRADED_SECONDS", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    from . import heartbeat  # lazy: keep views import-light, avoid any cycle
    return float(heartbeat.INTERVAL_MIN_DEFAULT * 60 * 3)


def _health_outage_seconds(seconds=None):
    """Age (s) past which a host is 'outage' (default ~3h). Env
    FULCRA_COORD_HEALTH_OUTAGE_SECONDS overrides; non-numeric -> default."""
    if seconds is not None:
        return float(seconds)
    raw = os.environ.get("FULCRA_COORD_HEALTH_OUTAGE_SECONDS", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(HEALTH_OUTAGE_SECONDS_DEFAULT)
```
- [ ] Re-run: GREEN.

**Step 1.2 — `assess_infra_health` (test first)**

- [ ] Add `class TestAssessInfraHealth(unittest.TestCase)` to `tests/test_health.py`. Build records with `_parse_dt`-parseable timestamps and a fixed `now`:
```python
from datetime import datetime, timedelta, timezone


def _rec(host, slug, ago_s, now):
    return {
        "schema": "fulcra.coordination.health.v1",
        "host": host, "agent": f"claude-code:{host}:repo", "version": "0.9.0",
        "reconcile_at": (now - timedelta(seconds=ago_s)).isoformat(
            timespec="microseconds").replace("+00:00", "Z"),
        "duration_s": 1.2, "tasks_loaded": 5, "views_refreshed": 7,
        "repair_backlog": 0, "retention_last_run": None,
        "listener_last_fire": None, "bus_task_count": 5,
    }


class TestAssessInfraHealth(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

    def test_fresh_record_is_healthy(self):
        recs = [_rec("mac", "claude-code-mac-repo", 60, self.now)]
        out = views.assess_infra_health(recs, now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        self.assertEqual(out["worst_status"], "healthy")
        self.assertEqual(out["hosts"][0]["status"], "healthy")

    def test_record_past_degraded_is_degraded(self):
        recs = [_rec("mac", "claude-code-mac-repo", 4000, self.now)]  # >3600, <10800
        out = views.assess_infra_health(recs, now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        self.assertEqual(out["hosts"][0]["status"], "degraded")
        self.assertEqual(out["worst_status"], "degraded")
        self.assertTrue(any("stale" in r for r in out["hosts"][0]["reasons"]))

    def test_record_past_outage_is_outage(self):
        recs = [_rec("mac", "claude-code-mac-repo", 20000, self.now)]  # >10800
        out = views.assess_infra_health(recs, now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        self.assertEqual(out["hosts"][0]["status"], "outage")
        self.assertEqual(out["worst_status"], "outage")

    def test_no_health_records_is_not_a_degraded_status(self):
        out = views.assess_infra_health([], now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        self.assertEqual(out["hosts"], [])
        self.assertEqual(out["worst_status"], "healthy")  # nothing reporting != degraded

    def test_undatable_reconcile_at_is_not_reporting(self):
        bad = _rec("mac", "claude-code-mac-repo", 60, self.now)
        bad["reconcile_at"] = "not-a-timestamp"
        out = views.assess_infra_health([bad], now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        self.assertEqual(out["hosts"][0]["status"], "not_reporting")
        # not_reporting is informational — never escalates worst_status
        self.assertEqual(out["worst_status"], "healthy")

    def test_metrics_surfaced_but_not_gated(self):
        rec = _rec("mac", "claude-code-mac-repo", 60, self.now)
        rec["duration_s"] = 88.0  # absurd duration must NOT change status
        rec["repair_backlog"] = 50
        out = views.assess_infra_health([rec], now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        self.assertEqual(out["hosts"][0]["status"], "healthy")
        self.assertEqual(out["hosts"][0]["metrics"]["duration_s"], 88.0)
        self.assertEqual(out["hosts"][0]["metrics"]["repair_backlog"], 50)

    def test_worst_status_is_the_worst_of_many(self):
        recs = [
            _rec("a", "a", 60, self.now),      # healthy
            _rec("b", "b", 4000, self.now),    # degraded
            _rec("c", "c", 20000, self.now),   # outage
        ]
        out = views.assess_infra_health(recs, now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        self.assertEqual(out["worst_status"], "outage")

    def test_bus_missed_digest_only_on_true_miss(self):
        recs = [_rec("a", "a", 60, self.now)]
        # last emit 9h ago -> a normal overnight gap, NOT a miss
        recent = (self.now - timedelta(hours=9)).strftime("%Y-%m-%d")
        out = views.assess_infra_health(
            recs, now=self.now, degraded_after_s=3600, outage_after_s=10800,
            digest_last_emit=recent)
        self.assertFalse(out["bus"]["missed_digest_window"])
        # last emit 30h ago -> beyond the ~20h max inter-window gap -> a true miss
        old = (self.now - timedelta(hours=30)).strftime("%Y-%m-%d")
        out2 = views.assess_infra_health(
            recs, now=self.now, degraded_after_s=3600, outage_after_s=10800,
            digest_last_emit=old)
        self.assertTrue(out2["bus"]["missed_digest_window"])

    def test_bus_no_digest_marker_is_missed(self):
        recs = [_rec("a", "a", 60, self.now)]
        out = views.assess_infra_health(
            recs, now=self.now, degraded_after_s=3600, outage_after_s=10800,
            digest_last_emit=None)
        self.assertTrue(out["bus"]["missed_digest_window"])
```
- [ ] Run: RED.
- [ ] Implement `assess_infra_health` in `views.py`:
```python
# The max inter-window gap (evening->morning) is ~14h; add slack so a normal
# overnight gap is never read as a miss. ~20h: a true miss means BOTH a morning
# and an evening window elapsed with no marker.
HEALTH_DIGEST_MISS_HOURS = 20


def assess_infra_health(health_records, *, now=None, degraded_after_s=None,
                        outage_after_s=None, digest_last_emit=None,
                        retention_last_run=None, task_count=None):
    """Judge fleet infra health from per-host health records (PURE, no I/O).

    Status gates on RECONCILE-STALENESS ONLY (v1): newest reconcile_at within
    degraded_after_s -> healthy; older -> degraded; older than outage_after_s ->
    outage. A record whose reconcile_at can't be parsed is 'not_reporting' —
    informational, never escalates worst_status (an un-upgraded / heartbeat-less
    host must not raise a false alarm). Duration / repair_backlog / bus size are
    surfaced as METRICS, never gated (no baselined thresholds in v1).

    Bus block: missed_digest_window is True only on a TRUE miss — no marker, or a
    last emit older than HEALTH_DIGEST_MISS_HOURS (~20h, > the max inter-window
    gap + slack) so a normal overnight gap is healthy. digest_last_emit /
    retention_last_run are bus-GLOBAL (any-agent, dedup'd) so they live here, not
    in the per-host record. All datetime gates use _parse_dt; never lexical."""
    if now is None:
        now = _now()
    deg = _health_degraded_seconds(degraded_after_s)
    out = _health_outage_seconds(outage_after_s)

    hosts = []
    worst_rank = 0  # 0 healthy, 1 degraded, 2 outage; not_reporting does NOT raise it
    rank = {"healthy": 0, "degraded": 1, "outage": 2}
    for rec in health_records:
        dt = _parse_dt(rec.get("reconcile_at") or "")
        metrics = {
            "duration_s": rec.get("duration_s"),
            "tasks_loaded": rec.get("tasks_loaded"),
            "views_refreshed": rec.get("views_refreshed"),
            "repair_backlog": rec.get("repair_backlog"),
            "bus_task_count": rec.get("bus_task_count"),
            "retention_last_run": rec.get("retention_last_run"),
            "listener_last_fire": rec.get("listener_last_fire"),
            "reconcile_at": rec.get("reconcile_at"),
        }
        if dt is None:
            hosts.append({"host": rec.get("host") or rec.get("agent") or "?",
                          "agent": rec.get("agent"), "status": "not_reporting",
                          "reasons": ["no parseable reconcile_at"], "metrics": metrics})
            continue
        age = (now - dt).total_seconds()
        if age >= out:
            status, reasons = "outage", [f"reconcile stale {int(age // 60)}m (outage)"]
        elif age >= deg:
            status, reasons = "degraded", [f"reconcile stale {int(age // 60)}m"]
        else:
            status, reasons = "healthy", []
        worst_rank = max(worst_rank, rank[status])
        hosts.append({"host": rec.get("host") or rec.get("agent") or "?",
                      "agent": rec.get("agent"), "status": status,
                      "reasons": reasons, "metrics": metrics})

    # Bus-level digest miss: True if no marker OR last emit older than the slack
    # window. Datetime parse via _parse_dt (digest_last_emit is a YYYY-MM-DD date
    # string from the freshest digest/markers/ path, normalized to midnight UTC).
    missed = True
    if digest_last_emit:
        dt = _parse_dt(digest_last_emit) or _parse_dt(f"{digest_last_emit}T00:00:00Z")
        if dt is not None:
            missed = (now - dt).total_seconds() >= HEALTH_DIGEST_MISS_HOURS * 3600

    worst = {0: "healthy", 1: "degraded", 2: "outage"}[worst_rank]
    return {
        "hosts": hosts,
        "bus": {
            "digest_last_emit": digest_last_emit,
            "retention_last_run": retention_last_run,
            "task_count": task_count,
            "missed_digest_window": missed,
        },
        "worst_status": worst,
    }
```
- [ ] Re-run: GREEN. **Note** `test_bus_missed_digest_only_on_true_miss` uses a date-only `digest_last_emit`; 9h-ago on the same date parses to that date's midnight, which is <20h from `now=12:00` only if the date is today — adjust the test's expected boundary if the fixture date math drifts (the implementer should verify the 9h case yields a same-day-or-prior date whose midnight is <20h before noon; if the overnight case needs an explicit datetime, pass a full ISO string — `_parse_dt` handles both).

---

### Task 2 — per-host health record write in `cmd_reconcile` (success path) + `_build_health_record()`

Depends on Task 1 (none of its judgment, but conceptually the record feeds it).
Two parts: the path helpers (remote.py) + the builder + the placed write (cli.py).

**Step 2.1 — remote path helpers (test first)**

- [ ] In `tests/test_health.py` add:
```python
from fulcra_coord import remote


class TestHealthPaths(unittest.TestCase):
    def test_health_remote_path(self):
        p = remote.health_remote_path("claude-code-mac-repo")
        self.assertTrue(p.endswith("/health/claude-code-mac-repo.json"))
        self.assertIn(remote.remote_root(), p)

    def test_health_prefix(self):
        self.assertTrue(remote.health_prefix().endswith("/health/"))
```
- [ ] Implement in `remote.py` next to `presence_prefix` (~398), mirroring it exactly:
```python
def health_remote_path(host_slug: str) -> str:
    """Per-host self-reported health record path. Takes an ALREADY-SLUGGED id
    (views.agent_slug). Only that host writes its own file -> zero cross-host
    write contention (the no-CAS-safe per-file pattern, same as presence)."""
    return f"{remote_root()}/health/{host_slug}.json"


def health_prefix() -> str:
    """List prefix for per-host health records (health command + retention prune)."""
    return f"{remote_root()}/health/"
```
- [ ] Run: GREEN.

**Step 2.2 — `_build_health_record()` (test first)**

- [ ] In `tests/test_fulcra_coord.py` (it already imports cli, cache, etc.) add a focused class:
```python
class TestBuildHealthRecord(unittest.TestCase):
    def test_record_shape_from_locals(self):
        from fulcra_coord import cli
        from datetime import datetime, timezone
        now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        rec = cli._build_health_record(
            now=now, duration_s=1.5, tasks_loaded=5, views_refreshed=7,
            repair_backlog=2, retention_last_run="2026-06-05",
            listener_last_fire=None, bus_task_count=5)
        self.assertEqual(rec["schema"], "fulcra.coordination.health.v1")
        self.assertEqual(rec["tasks_loaded"], 5)
        self.assertEqual(rec["views_refreshed"], 7)
        self.assertEqual(rec["repair_backlog"], 2)
        self.assertEqual(rec["bus_task_count"], 5)
        self.assertTrue(rec["reconcile_at"].endswith("Z"))
        self.assertIn("host", rec)
        self.assertIn("agent", rec)
        self.assertIn("version", rec)
```
- [ ] Run: RED.
- [ ] Implement in `cli.py` (near `_inbox_surface_path`, ~2044). It is pure-ish: takes the runtime values as args (so it is trivially testable) and reads identity/version itself:
```python
def _build_health_record(*, now, duration_s, tasks_loaded, views_refreshed,
                         repair_backlog, retention_last_run, listener_last_fire,
                         bus_task_count) -> dict:
    """Assemble the per-host health record from a SUCCESSFUL reconcile's locals
    plus cheap reads. Pure given its args; identity/version read here so the
    caller stays a one-liner. host = short hostname (matches identity.derived_agent);
    agent = resolve_agent(). reconcile_at is the success instant."""
    import socket
    from . import __version__
    try:
        host = socket.gethostname().split(".")[0]
    except Exception:
        host = "host"
    return {
        "schema": "fulcra.coordination.health.v1",
        "host": host,
        "agent": identity.resolve_agent(),
        "version": __version__,
        "reconcile_at": now.astimezone(timezone.utc).isoformat(
            timespec="microseconds").replace("+00:00", "Z"),
        "duration_s": duration_s,
        "tasks_loaded": tasks_loaded,
        "views_refreshed": views_refreshed,
        "repair_backlog": repair_backlog,
        "retention_last_run": retention_last_run,
        "listener_last_fire": listener_last_fire,
        "bus_task_count": bus_task_count,
    }
```
- [ ] Run: GREEN.

**Step 2.3 — place the write in `cmd_reconcile` (test first — the contract tests)**

- [ ] Add to `tests/test_fulcra_coord.py` a class that captures *which paths* get uploaded, so we can assert the health file lands on success and NOT on failure:
```python
class TestReconcileHealthWrite(unittest.TestCase):
    """The success contract: health/<slug>.json is written on the failures==[]
    path, NOT when a view upload fails (return 1 first), and NOT suppressed by a
    raising best-effort sub-pass."""

    def setUp(self):
        import types
        self.types = types

    def _run_capturing_uploads(self, upload_side_effect):
        from fulcra_coord.cli import cmd_reconcile
        uploaded = []

        def _capture(data, path, **kw):
            uploaded.append(path)
            return upload_side_effect(data, path, **kw)

        with patch("fulcra_coord.cli.remote.upload_json", side_effect=_capture), \
             patch("fulcra_coord.cli.remote.download_json", return_value=None), \
             patch("fulcra_coord.cli.remote.list_files", return_value=[]):
            rc = cmd_reconcile(self.types.SimpleNamespace(), backend=["false"])
        return rc, uploaded

    def test_health_written_on_success(self):
        rc, uploaded = self._run_capturing_uploads(lambda *a, **k: True)
        self.assertEqual(rc, 0)
        self.assertTrue(any("/health/" in p for p in uploaded),
                        f"health record must be uploaded on success; got {uploaded}")

    def test_health_not_written_when_view_upload_fails(self):
        # Views fail -> failures != [] -> return 1 BEFORE the health write.
        def _side(data, path, **kw):
            return "/health/" not in path and False  # all uploads fail
        rc, uploaded = self._run_capturing_uploads(_side)
        self.assertEqual(rc, 1)
        self.assertFalse(any("/health/" in p for p in uploaded),
                         "a failing reconcile must NOT write a fresh health record")

    def test_health_write_failure_does_not_fail_the_tick(self):
        # Views succeed; the health upload itself raises -> still return 0.
        from fulcra_coord.cli import cmd_reconcile

        def _side(data, path, **kw):
            if "/health/" in path:
                raise RuntimeError("boom")
            return True

        with patch("fulcra_coord.cli.remote.upload_json", side_effect=_side), \
             patch("fulcra_coord.cli.remote.download_json", return_value=None), \
             patch("fulcra_coord.cli.remote.list_files", return_value=[]):
            rc = cmd_reconcile(self.types.SimpleNamespace(), backend=["false"])
        self.assertEqual(rc, 0, "a health-write failure must never fail the tick")
```
- [ ] Run: RED (`test_health_written_on_success` fails — nothing writes health yet).
- [ ] Implement: insert between `cli.py:3744` (the `return 1` guard) and `cli.py:3746` (`for m in needs_repair:`):
```python
    # --- Self-reported per-host health record (spec v2 §1) -------------------
    # SUCCESS POINT: we are PAST the `if failures: return 1` guard above, so
    # failures == [] here. The health write is its OWN failure-isolated upload —
    # NOT a member of the parallel view-upload batch (which completes BEFORE the
    # failure verdict, so a batched health file would upload even on a FAILING
    # reconcile and falsely read healthy). It is also NOT gated on the best-effort
    # sub-passes (_sweep_review_routes / _run_retention ran above and never fail
    # the tick); gating on their flakiness would suppress a healthy heartbeat. A
    # health-write failure logs and NEVER changes this tick's return code.
    try:
        retention_last_run = None
        try:
            rmark = remote.download_json(remote.retention_marker_path(now), backend=backend)
            if isinstance(rmark, dict):
                retention_last_run = rmark.get("at") or rmark.get("date")
        except Exception:
            retention_last_run = None
        listener_last_fire = None
        try:
            surface = _inbox_surface_path(identity.resolve_agent())
            if surface.exists():
                listener_last_fire = datetime.fromtimestamp(
                    surface.stat().st_mtime, tz=timezone.utc).isoformat(
                    timespec="microseconds").replace("+00:00", "Z")
        except Exception:
            listener_last_fire = None
        record = _build_health_record(
            now=now,
            duration_s=round(time.monotonic() - t0, 3),
            tasks_loaded=len(all_tasks),
            views_refreshed=len(all_views),
            repair_backlog=len(needs_repair),
            retention_last_run=retention_last_run,
            listener_last_fire=listener_last_fire,
            bus_task_count=len(all_tasks),
        )
        slug = views.agent_slug(identity.resolve_agent())
        if not remote.upload_json(record, remote.health_remote_path(slug), backend=backend):
            _warn("  Health record upload failed (best-effort; tick unaffected).")
    except Exception as e:
        _warn(f"  Health record write error (skipped): {e}")
    # ------------------------------------------------------------------------
```
- [ ] Re-run the class: GREEN. Confirm `time` is in scope — `cmd_reconcile` does `import time` at 3630, and `datetime`/`timezone` are module-level imports already used at 3649.

---

### Task 3 — `health` command + `--format` + COMMAND_MAP wiring + `doctor` fold

Depends on Tasks 1–2.

**Step 3.1 — `cmd_health` (test first)**

- [ ] In `tests/test_health.py` add (uses `unittest.mock.patch` on `cli.remote.*` and captures stdout):
```python
import io
import types
from contextlib import redirect_stdout
from unittest import mock
from fulcra_coord import cli


class TestCmdHealth(unittest.TestCase):
    def _records(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        fresh = {"schema": "fulcra.coordination.health.v1", "host": "mac",
                 "agent": "claude-code:mac:repo", "version": "0.9.0",
                 "reconcile_at": now.isoformat().replace("+00:00", "Z"),
                 "duration_s": 1.0, "tasks_loaded": 3, "views_refreshed": 5,
                 "repair_backlog": 0, "retention_last_run": None,
                 "listener_last_fire": None, "bus_task_count": 3}
        return fresh

    def test_health_json_format(self):
        rec = self._records()
        with mock.patch("fulcra_coord.cli.remote.list_files",
                        return_value=["/coordination/health/claude-code-mac-repo.json"]), \
             mock.patch("fulcra_coord.cli.remote.download_json", return_value=rec):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.cmd_health(types.SimpleNamespace(format="json"), backend=["false"])
        self.assertEqual(rc, 0)
        import json
        out = json.loads(buf.getvalue())
        self.assertEqual(out["worst_status"], "healthy")
        self.assertEqual(len(out["hosts"]), 1)

    def test_health_table_format_runs(self):
        rec = self._records()
        with mock.patch("fulcra_coord.cli.remote.list_files",
                        return_value=["/coordination/health/claude-code-mac-repo.json"]), \
             mock.patch("fulcra_coord.cli.remote.download_json", return_value=rec):
            rc = cli.cmd_health(types.SimpleNamespace(format="table"), backend=["false"])
        self.assertEqual(rc, 0)

    def test_health_tolerates_missing_and_garbage(self):
        # One path lists but download returns None (garbage/missing) -> no crash.
        with mock.patch("fulcra_coord.cli.remote.list_files",
                        return_value=["/coordination/health/x.json", "/coordination/health/dir-not-json"]), \
             mock.patch("fulcra_coord.cli.remote.download_json", return_value=None):
            rc = cli.cmd_health(types.SimpleNamespace(format="json"), backend=["false"])
        self.assertEqual(rc, 0)

    def test_health_empty_bus_is_healthy(self):
        with mock.patch("fulcra_coord.cli.remote.list_files", return_value=[]), \
             mock.patch("fulcra_coord.cli.remote.download_json", return_value=None):
            rc = cli.cmd_health(types.SimpleNamespace(format="json"), backend=["false"])
        self.assertEqual(rc, 0)
```
- [ ] Run: RED.
- [ ] Implement a shared loader + `cmd_health` in `cli.py` (the loader is reused by the doctor fold and could be reused by the digest, though the digest computes its own — keep the loader in cli.py since it does bus I/O):
```python
def _load_health_records(*, backend=None) -> list[dict]:
    """List health/*.json and download each, tolerating a missing/garbage file
    (None is skipped) and a non-json listing entry. Best-effort: a failed list
    yields []. Per the 0.8.x hardening, never raises into a caller."""
    recs = []
    try:
        for path in remote.list_files(remote.health_prefix(), backend=backend):
            if not path.endswith(".json"):
                continue
            try:
                rec = remote.download_json(path, backend=backend)
            except Exception:
                rec = None
            if isinstance(rec, dict):
                recs.append(rec)
    except Exception:
        pass
    return recs


def _freshest_digest_emit(*, backend=None):
    """The bus-GLOBAL digest_last_emit: the freshest YYYY-MM-DD embedded in a
    digest/markers/<date>-<window>.json path. None if no marker. Dated from the
    PATH (no download) via views._MARKER_DATE_RE — the same date model the marker
    prune uses. Best-effort."""
    best = None
    try:
        for path in remote.list_files(remote.digest_markers_prefix(), backend=backend):
            m = views._MARKER_DATE_RE.search(path)
            if m and (best is None or m.group(1) > best):
                best = m.group(1)
    except Exception:
        pass
    return best


def _assess_fleet(*, now, backend=None):
    """Load all health inputs (records + bus markers) and run the pure judgment.
    Shared by cmd_health, the doctor fold, and (its own copy of) the digest."""
    recs = _load_health_records(backend=backend)
    digest_emit = _freshest_digest_emit(backend=backend)
    retention_last_run = None
    try:
        rmark = remote.download_json(remote.retention_marker_path(now), backend=backend)
        if isinstance(rmark, dict):
            retention_last_run = rmark.get("at") or rmark.get("date")
    except Exception:
        retention_last_run = None
    return views.assess_infra_health(
        recs, now=now, digest_last_emit=digest_emit,
        retention_last_run=retention_last_run, task_count=len(recs) or None)


def cmd_health(args: Any, backend: Optional[list[str]] = None) -> int:
    """Fleet coordination-health dashboard: load health/*.json, judge via
    views.assess_infra_health (reconcile-staleness gating only, v1), print per
    host status + reasons + metrics and the bus block. --format json for tooling.
    Read-only; tolerant of a missing/garbage record (the 0.8.x hardening)."""
    out_format = getattr(args, "format", "table")
    now = datetime.now(timezone.utc)
    result = _assess_fleet(now=now, backend=backend)

    if out_format == "json":
        _print_json(result)
        return 0

    worst = result["worst_status"]
    _info(f"\nfleet health: {worst}")
    if not result["hosts"]:
        _info("  (no hosts reporting health records yet)")
    for h in result["hosts"]:
        reasons = ("; ".join(h["reasons"])) if h["reasons"] else "ok"
        _info(f"  [{h['status']}] {h['host']} — {reasons}")
        m = h["metrics"]
        _info(f"      reconcile_at={m.get('reconcile_at')} "
              f"duration_s={m.get('duration_s')} tasks={m.get('tasks_loaded')} "
              f"views={m.get('views_refreshed')} backlog={m.get('repair_backlog')}")
    b = result["bus"]
    miss = " (MISSED window)" if b["missed_digest_window"] else ""
    _info(f"  bus: digest_last_emit={b['digest_last_emit']}{miss} "
          f"retention_last_run={b['retention_last_run']} task_count={b['task_count']}")
    return 0
```
- [ ] Run: GREEN.

**Step 3.2 — entry.py wiring (test first)**

- [ ] In `tests/test_health.py`:
```python
class TestHealthWiring(unittest.TestCase):
    def test_health_in_command_map(self):
        from fulcra_coord.entry import COMMAND_MAP
        from fulcra_coord import cli
        self.assertIs(COMMAND_MAP["health"], cli.cmd_health)

    def test_health_parses_format(self):
        from fulcra_coord.entry import build_parser
        args = build_parser().parse_args(["health", "--format", "json"])
        self.assertEqual(args.command, "health")
        self.assertEqual(args.format, "json")
```
- [ ] Run: RED.
- [ ] In `entry.py`, after the `capabilities` block (~271) add:
```python
    sp = sub.add_parser("health",
                        help="Fleet coordination-system health dashboard")
    sp.add_argument("--format", choices=["table", "json"], default="table")
```
- [ ] In `COMMAND_MAP` (after `"capabilities": _cli.cmd_capabilities,` at 472) add:
```python
    "health": _cli.cmd_health,
```
- [ ] Run: GREEN.

**Step 3.3 — doctor fold (test first)**

- [ ] In `tests/test_health.py`:
```python
class TestDoctorHealthFold(unittest.TestCase):
    def test_doctor_includes_fleet_health(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        rec = {"schema": "fulcra.coordination.health.v1", "host": "mac",
               "agent": "claude-code:mac:repo", "version": "0.9.0",
               "reconcile_at": now.isoformat().replace("+00:00", "Z"),
               "duration_s": 1.0, "tasks_loaded": 1, "views_refreshed": 1,
               "repair_backlog": 0, "retention_last_run": None,
               "listener_last_fire": None, "bus_task_count": 1}
        buf = io.StringIO()
        with mock.patch("fulcra_coord.cli.remote.list_files", return_value=[]), \
             mock.patch("fulcra_coord.cli.remote.download_json", return_value=None), \
             mock.patch("fulcra_coord.cli._assess_fleet",
                        return_value={"hosts": [{"host": "mac", "status": "healthy",
                                                 "reasons": [], "metrics": {}}],
                                      "bus": {"missed_digest_window": False,
                                              "digest_last_emit": None,
                                              "retention_last_run": None,
                                              "task_count": 1},
                                      "worst_status": "healthy"}), \
             redirect_stdout(buf):
            cli.cmd_doctor(types.SimpleNamespace(), backend=["false"])
        self.assertIn("Fleet health", buf.getvalue())

    def test_doctor_fleet_health_never_crashes_doctor(self):
        buf = io.StringIO()
        with mock.patch("fulcra_coord.cli._assess_fleet",
                        side_effect=RuntimeError("boom")), \
             mock.patch("fulcra_coord.cli.remote.check_cli_available",
                        return_value=(True, "ok")), \
             mock.patch("fulcra_coord.cli.remote.check_file_commands",
                        return_value=(True, "ok")), \
             mock.patch("fulcra_coord.cli.remote.check_remote_access",
                        return_value=(True, "ok")), \
             redirect_stdout(buf):
            rc = cli.cmd_doctor(types.SimpleNamespace(), backend=["false"])
        # doctor must still return its own verdict; a fleet-health error degrades
        # to a noted line, never a crash.
        self.assertIn(rc, (0, 1))
```
- [ ] Run: RED.
- [ ] In `cmd_doctor`, insert a block just before `cli.py:3999` (`_info(f"\n{'='*50}")`):
```python
    # Fleet health (the per-host coordination-machinery self-reports). Local
    # on-host checks above + fleet health here = the full picture. Wrapped
    # defensively: a fleet-health read error must degrade to a noted line, never
    # crash doctor (mirrors the file-probe guard above).
    _info(f"\n[Fleet health]")
    try:
        result = _assess_fleet(now=datetime.now(timezone.utc), backend=backend)
        _info(f"  Worst status: {result['worst_status']}")
        for h in result["hosts"]:
            reasons = ("; ".join(h["reasons"])) if h["reasons"] else "ok"
            _info(f"  [{h['status']}] {h['host']} — {reasons}")
        if not result["hosts"]:
            _info("  (no hosts reporting health records yet)")
        if result["bus"]["missed_digest_window"]:
            _info("  -> digest window appears MISSED (no recent digest marker)")
    except Exception as e:
        _info(f"  Fleet health: unavailable ({e})")
```
- [ ] Run: GREEN.

---

### Task 4 — digest infra line in `build_operator_digest` / `_render_digest`

Depends on Tasks 1, 3 (reuses the assessment; the digest computes its own copy
via `_assess_fleet` so the pure builder stays I/O-free — the assessment dict is
passed IN to `build_operator_digest`).

**Step 4.1 — builder gains an `infra` key (test first)**

- [ ] In `tests/test_operator_digest.py` add:
```python
class TestDigestInfraLine(unittest.TestCase):
    def test_infra_key_present_when_assessment_given(self):
        from fulcra_coord import views
        assessment = {"hosts": [{"host": "mac", "status": "healthy",
                                 "reasons": [], "metrics": {}}],
                      "bus": {"missed_digest_window": False},
                      "worst_status": "healthy"}
        d = views.build_operator_digest([], [], human="ash",
                                        infra=assessment)
        self.assertEqual(d["infra"], assessment)

    def test_infra_defaults_none_when_absent(self):
        from fulcra_coord import views
        d = views.build_operator_digest([], [], human="ash")
        self.assertIsNone(d.get("infra"))
```
- [ ] Run: RED.
- [ ] In `views.py`, add an `infra=None` kwarg to `build_operator_digest` (signature at 1196) and include it in the returned dict (after `"stale": stale,` at 1268):
```python
def build_operator_digest(summaries, presence, *, human, now=None, since=None,
                          infra=None):
```
and in the return dict:
```python
        "stale": stale,
        "infra": infra,  # pre-computed assess_infra_health dict, or None (v1 push surface)
    }
```
Update the docstring's block list to note the optional fifth surface: "* ``infra`` — a pre-computed ``assess_infra_health`` dict (passed in; pure builder does no I/O), rendered as one compact line by ``_render_digest``."
- [ ] Run: GREEN.

**Step 4.2 — renderer emits one infra line (test first)**

- [ ] In `tests/test_operator_digest.py`:
```python
class TestRenderInfraLine(unittest.TestCase):
    def test_degraded_infra_renders_a_warning_line(self):
        from fulcra_coord import cli
        digest = {"blocked_on_you": [], "upcoming": [], "per_agent": [],
                  "stale": [],
                  "infra": {"hosts": [{"host": "mac", "status": "degraded",
                                       "reasons": ["reconcile stale 120m"],
                                       "metrics": {}}],
                            "bus": {"missed_digest_window": False},
                            "worst_status": "degraded"}}
        name, note = cli._render_digest(digest, window="evening")
        self.assertIn("infra", note)
        self.assertIn("mac", note)

    def test_all_healthy_infra_is_affirmative_or_brief(self):
        from fulcra_coord import cli
        digest = {"blocked_on_you": [], "upcoming": [], "per_agent": [],
                  "stale": [],
                  "infra": {"hosts": [{"host": "a", "status": "healthy",
                                       "reasons": [], "metrics": {}},
                                      {"host": "b", "status": "healthy",
                                       "reasons": [], "metrics": {}}],
                            "bus": {"missed_digest_window": False},
                            "worst_status": "healthy"}}
        name, note = cli._render_digest(digest, window="evening")
        self.assertIn("2 hosts healthy", note)

    def test_no_infra_renders_nothing_extra(self):
        from fulcra_coord import cli
        digest = {"blocked_on_you": [], "upcoming": [], "per_agent": [],
                  "stale": [], "infra": None}
        name, note = cli._render_digest(digest, window="evening")
        self.assertNotIn("infra", note)

    def test_single_host_reconcile_down_still_reports(self):
        # The v1 push surface: a single-host box with reconcile down but the
        # digest scheduler alive still emits this line.
        from fulcra_coord import cli
        digest = {"blocked_on_you": [], "upcoming": [], "per_agent": [],
                  "stale": [],
                  "infra": {"hosts": [{"host": "solo", "status": "outage",
                                       "reasons": ["reconcile stale 400m (outage)"],
                                       "metrics": {}}],
                            "bus": {"missed_digest_window": False},
                            "worst_status": "outage"}}
        name, note = cli._render_digest(digest, window="morning")
        self.assertIn("solo", note)
        self.assertIn("infra", note)
```
- [ ] Run: RED.
- [ ] In `cli.py` `_render_digest` (2200), after the `stale` block (before `note = "\n".join(sections).strip()` at 2259), append:
```python
    infra = digest.get("infra")
    if infra:
        hosts = infra.get("hosts") or []
        worst = infra.get("worst_status", "healthy")
        if worst == "healthy" and not infra.get("bus", {}).get("missed_digest_window"):
            healthy_n = sum(1 for h in hosts if h.get("status") == "healthy")
            if healthy_n:
                sections.append("")
                sections.append(f"infra: {healthy_n} hosts healthy")
        else:
            # Surface the unhealthy hosts compactly (one host:reason each), plus a
            # missed-digest note. This is v1's PUSH surface — the digest scheduler
            # is independent of reconcile, so it reports a broken reconcile even on
            # a single-host box.
            bad = [h for h in hosts if h.get("status") in ("degraded", "outage", "not_reporting")]
            parts = []
            for h in bad:
                reason = (h.get("reasons") or ["?"])[0]
                parts.append(f"{h.get('host', '?')} {reason}")
            if infra.get("bus", {}).get("missed_digest_window"):
                parts.append("digest window missed")
            sections.append("")
            sections.append("infra: ⚠ " + " · ".join(parts) if parts
                            else f"infra: {worst}")
```
- [ ] Wire the assessment into `cmd_digest` (2493): compute it once and pass to the builder. After the `presence = ...` line (2516) and before `digest = views.build_operator_digest(...)` (2518), add:
```python
    # v1 push surface: compute the fleet assessment once (best-effort; a read
    # failure leaves infra=None and the digest renders without the line) and pass
    # it into the pure builder so the builder stays I/O-free.
    try:
        infra = _assess_fleet(now=now, backend=backend)
    except Exception:
        infra = None
```
and change the builder call to pass `infra=infra`:
```python
    digest = views.build_operator_digest(
        summaries, presence, human=human, now=now, since=since, infra=infra)
```
- [ ] Run: GREEN. Re-run the whole digest module to confirm no regression in existing render tests (empty-block skipping is preserved — the infra line is only appended when `infra` is truthy).

---

### Task 5 — retention prunes `health/` on the dead-presence window

Depends on Task 2 (the records exist). Mirrors `_prune_dead_presence` (2412).

**Step 5.1 — `is_prunable_health` pure judge (test first)**

- [ ] In `tests/test_health.py`:
```python
class TestIsPrunableHealth(unittest.TestCase):
    def setUp(self):
        from datetime import datetime, timezone
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

    def test_aged_record_is_prunable(self):
        from datetime import timedelta
        rec = {"reconcile_at": (self.now - timedelta(days=40)).isoformat().replace("+00:00", "Z")}
        self.assertTrue(views.is_prunable_health(rec, self.now))  # > 30d default

    def test_fresh_record_is_kept(self):
        from datetime import timedelta
        rec = {"reconcile_at": (self.now - timedelta(days=1)).isoformat().replace("+00:00", "Z")}
        self.assertFalse(views.is_prunable_health(rec, self.now))

    def test_undatable_record_is_kept(self):
        self.assertFalse(views.is_prunable_health({"reconcile_at": "nope"}, self.now))
        self.assertFalse(views.is_prunable_health({}, self.now))
```
- [ ] Run: RED.
- [ ] Implement in `views.py` next to `is_prunable_presence` (306), reusing the SAME window helper:
```python
def is_prunable_health(record, now=None, presence_days=None):
    """True when a health record's reconcile_at is older than the dead-presence
    retention window — a decommissioned host's record that would otherwise linger
    stale-forever. Reuses _presence_retention_days so health and dead presence
    prune in LOCKSTEP. reconcile_at parsed via _parse_dt (never lexical); a
    missing/unparseable reconcile_at is KEPT (fail-safe: never delete what we
    can't date)."""
    if now is None:
        now = _now()
    dt = _parse_dt(record.get("reconcile_at", ""))
    if dt is None:
        return False
    return (now - dt).total_seconds() / 86400.0 >= _presence_retention_days(presence_days)
```
- [ ] Run: GREEN.

**Step 5.2 — `_prune_dead_health` + wire into `_run_retention` (test first)**

- [ ] In `tests/test_health.py` (mirror `TestPrunePresence`-style, patching cli.remote):
```python
class TestPruneDeadHealth(unittest.TestCase):
    def setUp(self):
        from datetime import datetime, timezone
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

    def _rec(self, days_ago):
        from datetime import timedelta
        return {"reconcile_at": (self.now - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")}

    def test_prunes_aged_keeps_fresh_keeps_undatable(self):
        paths = ["/coordination/health/old.json",
                 "/coordination/health/fresh.json",
                 "/coordination/health/bad.json"]
        bodies = {"/coordination/health/old.json": self._rec(40),
                  "/coordination/health/fresh.json": self._rec(1),
                  "/coordination/health/bad.json": {"reconcile_at": "nope"}}
        deleted = []
        with mock.patch("fulcra_coord.cli.remote.list_files", return_value=paths), \
             mock.patch("fulcra_coord.cli.remote.download_json",
                        side_effect=lambda p, **k: bodies.get(p)), \
             mock.patch("fulcra_coord.cli.remote.delete",
                        side_effect=lambda p, **k: deleted.append(p) or True):
            n = cli._prune_dead_health(self.now, backend=["false"])
        self.assertEqual(n, 1)
        self.assertEqual(deleted, ["/coordination/health/old.json"])

    def test_failsafe_on_list_error(self):
        with mock.patch("fulcra_coord.cli.remote.list_files",
                        side_effect=RuntimeError("boom")):
            n = cli._prune_dead_health(self.now, backend=["false"])
        self.assertEqual(n, 0)
```
- [ ] Run: RED.
- [ ] Implement `_prune_dead_health` in `cli.py` right after `_prune_dead_presence` (ends 2437), an exact structural mirror:
```python
def _prune_dead_health(now: datetime, *, backend: Optional[list[str]] = None) -> int:
    """Delete per-host health records for long-departed hosts — in lockstep with
    _prune_dead_presence (same window), so a decommissioned host's presence AND
    health records disappear together. views.is_prunable_health FAILS SAFE: an
    undatable record is KEPT, never pruned. Best-effort, per-item isolated;
    platform soft-delete keeps a pruned record restorable. Returns count deleted."""
    n = 0
    try:
        for path in remote.list_files(remote.health_prefix(), backend=backend):
            if not path.endswith(".json"):
                continue
            try:
                rec = remote.download_json(path, backend=backend)
                if rec and views.is_prunable_health(rec, now):
                    if remote.delete(path, backend=backend):
                        n += 1
            except Exception:
                continue
    except Exception:
        pass
    return n
```
- [ ] Wire into `_run_retention` (2440): after `pruned_presence = _prune_dead_presence(now, backend=backend)` (2488), add `pruned_health = _prune_dead_health(now, backend=backend)` and include it in the returned dict:
```python
    pruned_presence = _prune_dead_presence(now, backend=backend)
    pruned_health = _prune_dead_health(now, backend=backend)
    return {"archived": archived, "deferred": deferred,
            "pruned_markers": pruned_markers, "pruned_presence": pruned_presence,
            "pruned_health": pruned_health}
```
- [ ] Update the `_run_retention` caller log line in `cmd_reconcile` (3734–3736) to mention health pruning (optional but keeps the tally honest):
```python
            _info(f"  Retention: archived {ret['archived']} task(s) "
                  f"(deferred {ret['deferred']}), pruned {ret['pruned_markers']} marker(s), "
                  f"{ret['pruned_presence']} dead presence, {ret.get('pruned_health', 0)} health.")
```
- [ ] Confirm no existing retention test asserts the exact dict keys without `.get` — `test_retention.py` constructs results via the fake bus; the new key is additive. Run `uv run --extra dev python -m pytest -q tests/test_retention.py`: GREEN.

---

### Task 6 — version bump 0.8.3 → 0.9.0 + CHANGELOG + version-pinned tests

Depends on Tasks 1–5 (do last).

- [ ] In `fulcra_coord/__init__.py:15` change `__version__ = "0.8.3"` → `__version__ = "0.9.0"`.
- [ ] In `tests/test_operator_digest.py:424` rename `test_version_is_083` → `test_version_is_090` and change the assertion to `self.assertEqual(__version__, "0.9.0")`.
- [ ] In `tests/test_fulcra_coord.py:7053` rename `test_version_is_0_8_3` → `test_version_is_0_9_0`, update its comment block to describe the health surface, and change the assertion to `self.assertEqual(__version__, "0.9.0")`.
- [ ] Prepend a new section to `CHANGELOG.md` above the `## [0.8.3]` header (matching the existing "Why:" prose style — human-readable, explains the problem it solves):
```markdown
## [0.9.0] — Coordination-system health surface: a silently-failing reconcile becomes visible

**Why:** the operator had rich awareness of *task* state but ZERO awareness of
whether the coordination machinery itself was healthy. A real incident: reconcile
failed on every heartbeat (a `KeyError` on a malformed bus task), views silently
went stale, retention never ran — and nothing surfaced it. It was caught only by
a manual live smoke. This release makes a degraded bus visible.

- **Self-reported per-host health record.** On a SUCCESSFUL reconcile (views
  rebuilt + uploaded with `failures == []`), each host writes
  `health/<slug>.json`. Staleness of that self-report IS the degradation signal
  (the same mechanism as presence liveness). The write is a separate,
  failure-isolated upload placed AFTER reconcile's `if failures: return 1` guard —
  so a failed reconcile leaves its record stale (the contract), and a flaky
  best-effort sub-pass can't suppress a healthy heartbeat.
- **`views.assess_infra_health` (pure).** Judges newest `reconcile_at` per host:
  healthy / degraded (default = heartbeat interval × 3) / outage (default ~3h).
  A host with no record = "not reporting" (informational, never a false alarm).
  Duration / repair-backlog / bus-size are surfaced as metrics, NOT gated.
- **`fulcra-coord health [--format table|json]`** dashboard + a fleet-health fold
  into `doctor` + a one-line infra summary in the twice-daily operator digest
  (the digest scheduler is independent of reconcile, so it reports a broken
  reconcile even on a single-host box — v1's push surface).
- **Retention prunes `health/`** on the dead-presence window, so a decommissioned
  host's records disappear in lockstep with its presence record.
- **Knobs:** `FULCRA_COORD_HEALTH_DEGRADED_SECONDS`,
  `FULCRA_COORD_HEALTH_OUTAGE_SECONDS`.

---
```
- [ ] Full suite: `uv run --extra dev python -m pytest -q` — all GREEN. Do NOT commit `uv.lock`.

---

## Self-review

### Spec coverage (every v2 §/decision → a task)

| Spec v2 item | Task | Notes |
| --- | --- | --- |
| §1 per-host health record, written on success | Task 2 | `_build_health_record` + the placed write |
| §1 success = views rebuilt + `failures == []`, write AFTER `if failures: return 1`, NOT in batch, NOT gated on sub-passes | **Task 2 (load-bearing)** | Insertion at cli.py 3744→3746; contract tests `test_health_written_on_success`, `test_health_not_written_when_view_upload_fails`, `test_health_write_failure_does_not_fail_the_tick` |
| §1 record fields (host…bus_task_count); retention_last_run from marker; listener_last_fire from inbox-pending mtime | Task 2 | marker read + `_inbox_surface_path(...).stat().st_mtime` |
| §2 `assess_infra_health` healthy/degraded/outage by threshold | Task 1 | |
| §2 degraded default = interval×3 (env knob); outage ~3h (env knob) | Task 1 | `_health_degraded_seconds` uses `heartbeat.INTERVAL_MIN_DEFAULT*60*3` (no interval env exists — grounded); `_health_outage_seconds` |
| §2 no-health-file = not-reporting, never degraded/outage | Task 1 | `test_no_health_records_is_not_a_degraded_status`, `test_undatable_reconcile_at_is_not_reporting` |
| §2 bus block + missed_digest_window only on a true miss (~20h, never overnight) | Task 1 | `test_bus_missed_digest_only_on_true_miss` |
| §2 metrics surfaced not gated; `_parse_dt` everywhere | Task 1 | `test_metrics_surfaced_but_not_gated` |
| §2 no presence cross-signal | Tasks 1–2 | judgment is self-contained on `reconcile_at`; presence never read by `assess_infra_health` |
| §3 `health` command + `--format` + doctor fold; tolerant of garbage | Task 3 | |
| §3 scheduler signals from bus markers (retention/last-run, digest/markers freshest, bus-global) | Task 3 | `_freshest_digest_emit`, `_assess_fleet` retention read |
| §4 digest infra line; affirmative/omitted when healthy; single-host reconcile-down still reports | Task 4 | `_render_digest` block; `test_single_host_reconcile_down_still_reports` |
| §5 retention prunes `health/` on dead-presence window; fail-safe (undatable kept) | Task 5 | `is_prunable_health` reuses `_presence_retention_days`; `_prune_dead_health` mirrors `_prune_dead_presence` |
| §Scope knobs `FULCRA_COORD_HEALTH_{DEGRADED,OUTAGE}_SECONDS` | Tasks 1, 6 | |
| §Out: plate-alert escalation CUT | (absent by design) | No escalation/plate-alert code anywhere in this plan — verified below |
| version bump + CHANGELOG | Task 6 | |

**Cut-escalation check (explicit):** the v2 spec cut the peer-escalation
plate-alert (inert single-host, not idempotent). This plan adds NO plate-alert,
NO peer escalation, NO recovery-self-escalation, NO open/close hysteresis. The
only push surface is the digest line (Task 4). Confirmed absent.

### Placeholder scan

No `TODO`, no `pass  # implement`, no `...`, no `<FILL>` in any code block. Every
test has concrete fixtures and assertions; every impl is real code grounded in
the cited signatures. The one soft note is in Task 1 Step 1.2's final checkbox
(date-only `digest_last_emit` boundary) — that is a verification reminder, not a
placeholder; the impl handles both date and full-ISO via `_parse_dt`.

### Name / type consistency

- Slug: `views.agent_slug(identity.resolve_agent())` everywhere a `health/<slug>.json`
  path is built (Task 2 write, retention prune lists by `health_prefix`).
- `assess_infra_health` returns `{hosts:[{host,agent,status,reasons,metrics}], bus:{digest_last_emit,retention_last_run,task_count,missed_digest_window}, worst_status}` — the same shape consumed by `cmd_health`, the doctor fold, and `_render_digest`. Statuses: `healthy|degraded|outage|not_reporting`; `worst_status ∈ {healthy,degraded,outage}` (not_reporting never escalates).
- Record schema string `"fulcra.coordination.health.v1"` used in `_build_health_record`, all test fixtures, and the prune tests.
- `_health_degraded_seconds`/`_health_outage_seconds` accept an explicit override arg (mirrors `_presence_retention_days`), enabling deterministic tests that pass `degraded_after_s=`/`outage_after_s=` straight into `assess_infra_health`.
- Retention dict gains `pruned_health`; all readers use `.get("pruned_health", 0)` so older callers/tests are unaffected.

### Best-effort invariants (each verified in a test)

- Health write never raises into the tick: `test_health_write_failure_does_not_fail_the_tick`.
- `assess_infra_health` is pure + `.get`-defensive: all Task 1 tests.
- `_load_health_records` / `_assess_fleet` tolerate garbage: `test_health_tolerates_missing_and_garbage`.
- Doctor fold degrades, never crashes: `test_doctor_fleet_health_never_crashes_doctor`.
- Prune fail-safe: `test_failsafe_on_list_error`, `test_prunes_aged_keeps_fresh_keeps_undatable`.
