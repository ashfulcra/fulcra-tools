# fulcra-coord Reconcile Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `fulcra-coord reconcile` back under its default 90s deadline at steady-state task counts so the launchd heartbeat completes every pass without a budget bump.

**Architecture:** Reconcile is the authoritative view-repair path: it loads every task **body** from the durable `tasks/` listing (can't trust derived views it exists to repair), rebuilds ~33–40 views, re-asserts them all, then runs a tail of best-effort sub-passes (presence rebuild, retention, parity, undelivered, role-health, review-sweep). This plan attacks the two measured cost centers — the body-load and the sub-pass tail — **without** raising transport concurrency (which has a documented gateway-saturation incident). It is **instrumentation-first**: every optimization is gated on before/after numbers from a built-in phase timer, because two intuitive hypotheses (view-upload cost; body-load cost) were already empirically wrong.

**Tech Stack:** Python 3.10+ stdlib only (fulcra-coord is stdlib-only by constraint), `concurrent.futures` thread pools, the `remote`/`io` transport layer over Fulcra Files (a CLI-subprocess-per-file backend).

## Global Constraints

- **stdlib-only:** fulcra-coord ships no third-party runtime deps. No new imports outside the Python stdlib.
- **Never raise into a tick:** reconcile and every sub-pass are best-effort; a failure logs and degrades, never crashes the heartbeat. All new code keeps this contract.
- **Do NOT raise transport concurrency past the current cap** (`max_workers = min(16, …)` in `io.py`) without an explicit, separately-authorized live gateway benchmark: ~15–18 concurrent `fulcra-api` subprocesses saturated the API gateway in a prior incident (see `io.py` ~line 456). Concurrency increases are out of scope for this plan.
- **Preserve reconcile's authoritativeness:** the task set MUST come from the raw `tasks/` listing (durable bodies), never the derived summaries aggregate. Optimizations may not substitute summaries for bodies in the rebuild source.
- **Version bump + CHANGELOG:** every user-visible change bumps `fulcra_coord/__init__.py::__version__` and adds a CHANGELOG entry (and updates the two version-pin tests: `test_fulcra_coord.py::TestVersionFlag` and `test_operator_digest.py::TestVersion`).
- **Benchmark target:** at ~300 hot tasks, total `reconcile` wall-time < 90s (the default `FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS`) with NO best-effort sub-pass reporting "deadline budget spent".

---

## Measured Baseline (2026-06-22, ~295 hot tasks, this operator's box)

Captured ad-hoc; Task 1 makes this reproducible and durable. Numbers are the
problem statement — do not delete them, the plan is calibrated to them.

| Phase | Wall-time |
|---|---|
| `_load_all_tasks_by_listing` (295 bodies) | **40s** |
| `build_all_views` (33 views, in-memory) | ~0s |
| Upload 33 views (8-worker pool) | **8s** |
| Best-effort sub-pass tail (presence rebuild, retention, parity, undelivered, role-health, review-sweep) | **~75s** (inferred: total ~123s − ~48s core) |
| **Total** | **~123s** (exceeds the 90s default → 2+ best-effort passes skipped) |

Supporting facts:
- A **single** task-body fetch (`remote.download_json`) = **~0.9s**. So 295 bodies at the 16-worker cap *should* be ~17–25s; the measured 40s means effective parallelism is only ~6–7×. **The gap — not the per-fetch cost — is the Task 2 target.**
- There is **no bulk-fetch primitive**: `remote.list_json` is itself `list_files` + parallel per-file `download_json` (one subprocess each). O(N) subprocesses is structural.
- The earlier "view-upload is the bottleneck" hypothesis was **wrong** (uploads = 8s). Do not re-litigate it.

---

## File Structure

- **Modify `fulcra_coord/cli.py`** (`cmd_reconcile`, ~line 1676): add a phase-timing harness that records per-phase wall-time into the existing per-host health record and logs it at `info`. One responsibility: make reconcile self-measuring.
- **Modify `fulcra_coord/io.py`** (`_load_all_tasks_by_listing` ~line 767, `_cache_remote_task`): remove redundant per-body round-trips found by Task 2's investigation; the load stays at the same worker cap.
- **Modify `fulcra_coord/cli.py`** (the sub-pass tail, ~lines 2266–2570): widen the E4 tick-scoped snapshot sharing so sub-passes reuse already-downloaded snapshots instead of re-fetching.
- **Modify `packages/fulcra-coord/CHANGELOG.md`** and `fulcra_coord/__init__.py`: version + changelog per phase that ships a user-visible change.
- **Test files:** `tests/test_reconcile_timing.py` (new, Task 1), `tests/test_io_load.py` (extend, Task 2), `tests/test_reconcile_snapshot_sharing.py` (new, Task 3).

Phase 4 (transport batch/persistent backend) is **scoped but not specified to task-level** here — it touches `fulcra-api` and warrants its own spec after Tasks 1–3 quantify the residual.

---

## Task 1: Reconcile phase-timing instrumentation (do this FIRST)

**Why first:** every later task is gated on before/after numbers, and two hypotheses already proved wrong by guessing. Make reconcile measure itself so the optimization is data-driven and regressions are visible in the health record.

**Files:**
- Modify: `fulcra_coord/cli.py` (`cmd_reconcile`, ~1676; the health-record write ~2356)
- Test: `tests/test_reconcile_timing.py` (create)

**Interfaces:**
- Produces: a `_PhaseTimer` helper with `.mark(label)` and `.summary() -> dict[str, float]`; reconcile's health record gains a `phase_timings_ms: dict[str,float]` field.

- [ ] **Step 1: Write the failing test** — `_PhaseTimer` records monotonic deltas between marks and emits a label→ms dict.

```python
# tests/test_reconcile_timing.py
import time
from fulcra_coord.cli import _PhaseTimer

def test_phase_timer_records_labelled_deltas(monkeypatch):
    t = [100.0]
    monkeypatch.setattr("fulcra_coord.cli.time.monotonic", lambda: t[0])
    pt = _PhaseTimer()
    t[0] = 100.5; pt.mark("load")
    t[0] = 100.9; pt.mark("build")
    s = pt.summary()
    assert s["load"] == 500.0      # 0.5s -> 500ms
    assert s["build"] == 400.0     # 0.4s -> 400ms
```

- [ ] **Step 2: Run it, verify it fails** — `pytest tests/test_reconcile_timing.py -v` → FAIL (`_PhaseTimer` undefined).

- [ ] **Step 3: Implement `_PhaseTimer`** in `cli.py` (near the top helpers).

```python
class _PhaseTimer:
    """Monotonic phase stopwatch for cmd_reconcile. mark(label) records the
    elapsed ms since the previous mark under `label`. Never raises."""
    def __init__(self) -> None:
        self._last = time.monotonic()
        self._timings: dict[str, float] = {}
    def mark(self, label: str) -> None:
        now = time.monotonic()
        self._timings[label] = round((now - self._last) * 1000.0, 1)
        self._last = now
    def summary(self) -> dict[str, float]:
        return dict(self._timings)
```

- [ ] **Step 4: Run it, verify it passes.**

- [ ] **Step 5: Wire into `cmd_reconcile`** — instantiate `pt = _PhaseTimer()` after `t0`, call `pt.mark("load")` after the `all_tasks` load, `pt.mark("views")` after the view upload + summaries verification block, and `pt.mark("subpasses")` only after the whole best-effort tail has run (presence rebuild, review-route sweep, retention, event parity, dual-write health, loop-record load, directive parity, loop health, role health, verdict adoption, and undelivered-directive check). Add `record["phase_timings_ms"] = pt.summary()` where the health record is assembled (~2356), after the final `subpasses` mark. Add one `_info(f"  Phase timings (ms): {pt.summary()}")` line before the success return. All inside existing try/except — a timer failure must never affect the tick.

- [ ] **Step 6: Manual verification** — `fulcra-coord reconcile` prints a `Phase timings (ms): {...}` line; numbers roughly match the baseline table (load ≫ views).

- [ ] **Step 7: Bump version + CHANGELOG + version-pin tests, run full suite, commit.**

```bash
cd packages/fulcra-coord && uv run --extra dev pytest -q
git add -A && git commit -m "feat(coord): reconcile phase-timing instrumentation (health record + log)"
```

---

## Task 2: Cut the body-load round-trips (no concurrency increase)

**Hypothesis to verify, then fix:** the 295-body load takes 40s but a single fetch is 0.9s and the cap is 16 workers (ideal ~17–25s). The ~2× gap is most likely a **redundant per-body remote round-trip** — `_cache_remote_task` doing a `remote.stat()` before the download when local cached metadata/body exist (an optimistic-concurrency check that the read-only reconcile load doesn't need). Removing the pre-stat on this path halves remote round-trips without touching `max_workers` (so no gateway-saturation risk). Note: `cache.read_meta()` is a local cache sidecar read, not a remote transport call.

**Files:**
- Modify: `fulcra_coord/io.py` (`_cache_remote_task` and/or `_load_all_tasks_by_listing` ~767)
- Test: `tests/test_io_load.py` (extend or create)

**Interfaces:**
- Consumes: `_PhaseTimer` numbers from Task 1 (the `load` figure is the before/after metric).
- Produces: a read-only fetch path used by the reconcile load that issues exactly ONE remote round-trip per task body (no pre-stat), preserving the cache-merge + per-body best-effort guards.

- [ ] **Step 1: Investigate + record the round-trip count.** Read `_cache_remote_task` fully; count remote calls per body (`remote.stat` + `remote.download_json`). Add a temporary counter in a scratch test that patches `remote.download_json`/`remote.stat` to tally calls for a 1-body cached steady-state load. WRITE THE FINDING into the task's PR description. If there is exactly one remote round-trip already, STOP — the gap is parallelism scheduling, not redundant I/O; escalate to re-scope (do NOT raise workers).

- [ ] **Step 2: Write the failing test** — the reconcile body-load path issues no pre-download stat per body.

```python
# tests/test_io_load.py
from unittest import mock
from fulcra_coord import cache, io, remote

def test_listing_load_does_one_roundtrip_per_body(coord_backend):
    # seed two task bodies via the fake backend, then seed cached bodies + meta
    # so the current stat-gated path would try a remote.stat before download.
    ...  # create tasks/<id>.json x2 through the fixture
    ...  # cache.write_cached_task(...) and cache.write_meta(...) for both paths
    calls = {"download": 0, "stat": 0, "cache_meta": 0}
    real_dl = remote.download_json
    real_stat = remote.stat
    real_read_meta = cache.read_meta
    def dl(p, **k): calls["download"] += 1; return real_dl(p, **k)
    def stat(p, **k): calls["stat"] += 1; return real_stat(p, **k)
    def read_meta(p): calls["cache_meta"] += 1; return real_read_meta(p)
    with mock.patch.object(remote, "download_json", dl), \
         mock.patch.object(remote, "stat", stat), \
         mock.patch.object(cache, "read_meta", read_meta):
        io._load_all_tasks_by_listing(backend=coord_backend)
    assert calls["stat"] == 0, "reconcile load must not remote-stat bodies before downloading them"
    assert calls["download"] == 2
```

- [ ] **Step 3: Run it, verify it fails** with the current pre-stat behavior (`stat > 0`), confirming the redundant remote round-trip.

- [ ] **Step 4: Implement** — give `_load_all_tasks_by_listing` a read-only fetch that calls `remote.download_json` directly (or a `_cache_remote_task(..., skip_meta=True)` flag), skipping the optimistic-concurrency stat. Keep the cache overlay + id-less-body guard (A2) + per-body try/except exactly as-is. Do not change `max_workers`.

- [ ] **Step 5: Run it, verify it passes.**

- [ ] **Step 6: Run the full suite** — especially `test_write_path_read_errors.py` and any `_cache_remote_task` consumers; the write path's optimistic-concurrency stat MUST be untouched (only the reconcile read-load skips it).

- [ ] **Step 7: Live benchmark** — `fulcra-coord reconcile`, read the Task-1 `phase_timings_ms.load`. Expected: load drops from ~40s toward ~20s at ~300 tasks. Record before/after in the PR.

- [ ] **Step 8: Bump version + CHANGELOG + version tests, commit.**

---

## Task 3: Widen tick-scoped snapshot sharing across the sub-pass tail

**Why:** the ~75s sub-pass tail is many best-effort passes each doing their own I/O. The E4 sharing (cli.py ~2058) already loads `summaries_view` and the presence aggregate once and threads them through *some* passes. Audit the tail for sub-passes that STILL self-load a snapshot this tick already holds (presence, summaries, the directives prefix) and thread the shared copy in.

**Files:**
- Modify: `fulcra_coord/cli.py` (sub-pass calls ~2266–2570: `_sweep_review_routes`, `_adopt_orphaned_verdicts`, `_undelivered_directive_check`, `_role_health_check`, parity checks)
- Test: `tests/test_reconcile_snapshot_sharing.py` (create)

**Interfaces:**
- Consumes: the `summaries_view` / `presence_agents` snapshots already loaded in `cmd_reconcile`.
- Produces: each audited sub-pass accepts and uses an injected snapshot (keeping its `_UNSET`/`None` self-loading fallback for direct callers + tests).

- [ ] **Step 1: Audit + list** — for each sub-pass in the tail, grep whether it calls `_load_presence_agents` / `download_json("summaries")` / lists the directives prefix internally despite `cmd_reconcile` already holding that snapshot. Write the list of redundant re-loads into the PR.

- [ ] **Step 2: Write the failing test** — with presence + summaries already loaded, a reconcile tick calls each tail sub-pass with the shared snapshot and performs NO additional presence/summaries download.

```python
# tests/test_reconcile_snapshot_sharing.py
from unittest import mock
from fulcra_coord import cli, remote

def test_subpasses_reuse_shared_snapshots(coord_backend, monkeypatch):
    dl = mock.Mock(side_effect=remote.download_json)
    monkeypatch.setattr(remote, "download_json", dl)
    cli.cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)
    summary_loads = [c for c in dl.call_args_list if "summaries" in str(c)]
    assert len(summary_loads) <= 1, "summaries downloaded more than once per tick"
```

- [ ] **Step 3: Run it, verify it fails** (summaries/presence loaded >1×).

- [ ] **Step 4: Implement** — thread `summaries_view=` / `presence=` into each audited sub-pass call that lacks it; rely on the existing injected-snapshot params (most already have them from E4). Preserve every self-loading fallback.

- [ ] **Step 5: Run it, verify it passes; run the full suite.**

- [ ] **Step 6: Live benchmark** — read `phase_timings_ms.subpasses`; record the drop.

- [ ] **Step 7: Bump version + CHANGELOG + version tests, commit.**

---

## Task 4 (scoped, NOT task-level specified): transport batch / persistent backend

After Tasks 1–3, re-measure. If reconcile is still over 90s at steady-state, the residual is structural: one `fulcra-api` **subprocess per file**. Options to spec in a *separate* plan (each needs its own design + live gateway benchmark):

- A **persistent backend process** (long-lived `fulcra-api` serving many file ops over one stdio/socket connection) to amortize subprocess-spawn cost — the likely biggest structural win, but a `fulcra-api` change and a new transport contract.
- A **server-side bulk-get** endpoint (`GET many paths` in one call) if the Fulcra Files API can add one.
- An **in-process httpx backend** for reads (the session already keeps reads on httpx in places) to skip subprocess spawn entirely on the hot read path.

Do NOT start Task 4 until Tasks 1–3 numbers prove it's needed and quantify the gap. Capture the post-Task-3 `phase_timings_ms` in the Task 4 spec as its baseline.

---

## Risks & Methodology

- **Gateway saturation (highest risk):** do not touch `max_workers`. Tasks 2–3 cut *round-trips* and *redundant loads*, not concurrency. If a task's only available lever turns out to be concurrency, STOP and escalate for a separately-authorized live benchmark.
- **Authoritativeness regression:** Task 2/3 must not substitute summaries for bodies in the rebuild source. The `test_write_path_read_errors.py` + `TestBuildAllViewsEquivalence` suites are the guardrails — keep them green.
- **Measure on the real transport:** unit tests prove correctness; only a live `fulcra-coord reconcile` on a ~300-task bus proves speed. Record `phase_timings_ms` before/after each task in its PR.
- **Operational fallback stays:** the heartbeat budget bump (`FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS=300`) and the bus cleanup (`FULCRA_COORD_RETENTION_DAYS`) remain the safety net while this lands; only remove the bump once Task 1's numbers show <90s at steady-state.

## Self-Review

- **Coverage:** body-load (Task 2) and sub-pass tail (Task 3) — the two measured cost centers — each have a task; instrumentation (Task 1) gates them; the structural residual (Task 4) is scoped, not hand-waved. ✓
- **Placeholders:** Task 4 is intentionally not task-level — it's flagged as needing its own spec after measurement, not as a "TODO". Tasks 1–3 carry concrete tests + code. ✓
- **Type consistency:** `_PhaseTimer.summary() -> dict[str,float]` and `record["phase_timings_ms"]` are used consistently across Tasks 1/2/3. ✓
- **Known gap:** Task 2's exact fix is hypothesis-gated (Step 1 verifies the redundant stat before changing code); if the hypothesis is wrong the task escalates rather than guessing — deliberate, given two prior wrong hypotheses.
