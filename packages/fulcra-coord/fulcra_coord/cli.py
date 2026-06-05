"""CLI command implementations for fulcra-coord.

Each command accepts parsed argparse namespace and an optional backend=
override for testing without live Fulcra access.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import cache, remote, schema, views, log as ops_log, session_link, claude_code, openclaw, heartbeat, codex, listener, identity, digest_schedule
# Imported under an alias because ``from __future__ import annotations`` above
# binds the bare name ``annotations`` to the __future__ feature, which would
# otherwise shadow this module on the cli namespace.
from . import annotations as lifecycle_annotations


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2))


def _err(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)


def _warn(msg: str) -> None:
    print(f"WARN: {msg}", file=sys.stderr)


def _info(msg: str) -> None:
    print(msg)


#: A title that LOOKS like a task id (``TASK-YYYYMMDD-…``). When ``start`` is
#: handed one of these the operator almost certainly meant to CLAIM/activate the
#: existing task, not create a new one named after an id. Only the prefix is
#: matched (date-stamped ``TASK-<8 digits>-``) so a genuine title that merely
#: mentions a date can't trip it.
_TASK_ID_TITLE_RE = re.compile(r"^TASK-\d{8}-")


def _maybe_warn_legacy_identity(explicit: Optional[str]) -> None:
    """Print a one-line migration hint (to STDERR) iff the resolved identity is
    purely DERIVED (nothing explicit/env/per-cwd) AND a legacy global
    ``identity.json`` exists.

    Rationale: an operator who set the pre-split global identity gets a derived
    id in every repo now (the global is no longer resolved automatically, I-1),
    so their agent shows up under an unexpected ``claude-code:<host>:<repo>``.
    This nudges them to declare a per-cwd id. STDERR so the backgrounded
    ``connect`` hook discards it; one line; only when BOTH conditions hold so it
    never nags a correctly-configured session."""
    _agent, source = identity.resolve_agent_source(explicit)
    if source != "derived":
        return
    legacy = identity.read_legacy_identity()
    if not legacy:
        return
    _warn("legacy identity.json found but per-cwd identity isn't set here — run "
          "'fulcra-coord identity migrate' (or 'identity set <vendor>:<host>:<purpose>').")


# ---------------------------------------------------------------------------
# Session pointer
# ---------------------------------------------------------------------------

def _stamp_session_pointer(task: dict[str, Any]) -> None:
    """Keep this session's current-task pointer in sync so PreCompact/SessionEnd
    hooks find the right task.

    Non-terminal (active/waiting/blocked) → write/refresh the pointer.
    Terminal (done/abandoned) → CLEAR any pointer to this task, so the hooks don't
    later checkpoint a finished task. No-op outside a session (write_pointer
    returns False; clear scans by task id regardless of session env).
    """
    status = task.get("status")
    try:
        if status in ("active", "waiting", "blocked"):
            session_link.write_pointer(
                task["id"],
                agent=task.get("owner_agent", "claude-code"),
                root=remote.remote_root(),
            )
        elif status in ("done", "abandoned"):
            session_link.clear_for_task(task["id"])
    except Exception:
        pass  # pointer is best-effort; never break a write


# ---------------------------------------------------------------------------
# Remote I/O helpers
# ---------------------------------------------------------------------------

def _cache_remote_task(task_id: str, backend: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
    """Download a remote task, cache its body and current stat metadata."""
    task_path = remote.task_remote_path(task_id)
    task = remote.download_json(task_path, backend=backend)
    if not task:
        return None
    cache.write_cached_task(task)
    task_stat = remote.stat(task_path, backend=backend)
    if task_stat:
        cache.write_meta(task_path, task_stat)
    return task


def _load_all_tasks(backend: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Load tasks from cache, refreshing remote-indexed tasks when available."""
    cached = cache.list_cached_tasks()
    idx = remote.download_json(remote.view_remote_path("index"), backend=backend)
    if idx is None:
        return cached

    remote_ids = {s["id"] for s in idx.get("active", []) + idx.get("recent_done", [])}
    search_idx = remote.download_json(remote.view_remote_path("search-index"), backend=backend)
    if search_idx:
        # Cache the fresh remote search-index so cmd_search doesn't use a stale local copy.
        # Without this, status+search would show stale results for remotely-updated tasks.
        cache.write_cached_view("search-index", search_idx)
        remote_ids.update(r["id"] for r in search_idx.get("records", []) if r.get("id"))

    # The index seeds only active + recent_done ids; PROPOSED (and waiting) tasks
    # ride only on the search-index. If the search-index fetch fails or is absent,
    # a remote-only proposed directive would be invisible here — so it would be
    # silently dropped from every rebuilt view (recompute could lose a pending
    # directive). The `next` view contains exactly the proposed+waiting set, so
    # fold its ids in as a second, independent source for those statuses.
    next_view = remote.download_json(remote.view_remote_path("next"), backend=backend)
    if next_view:
        remote_ids.update(t["id"] for t in next_view.get("tasks", []) if t.get("id"))
    task_map: dict[str, dict[str, Any]] = {t["id"]: t for t in cached}

    # Fetch each remote task body CONCURRENTLY (PERF). Each fetch is one
    # independent `fulcra file download` subprocess (~1.3s) writing to a
    # distinct per-id cache file — there is no shared mutable state, so a thread
    # pool is safe and collapses N serial round-trips into a single batch's
    # wall-time. This is the root-cause fix for the reconcile heartbeat blowing
    # past its 90s timeout (76 sequential fetches measured at ~96s). Semantics
    # are preserved exactly: a None result (404/error) is skipped, order is
    # irrelevant (results dedup into task_map by id), and the local-cache base
    # already seeded above survives any id the index doesn't name.
    if remote_ids:
        max_workers = min(16, max(4, len(remote_ids)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_cache_remote_task, tid, backend=backend): tid
                for tid in remote_ids
            }
            for fut in concurrent.futures.as_completed(futures):
                # Mirror the old loop's best-effort guard: a single failed fetch
                # must not abort the whole load. _cache_remote_task already
                # returns None on a missing/empty body; catching here covers an
                # unexpected raise (network blowup) so it can't escape the pool.
                try:
                    t = fut.result()
                except Exception:
                    t = None
                if t:
                    task_map[t["id"]] = t

    return list(task_map.values())


def _load_task_summaries(backend: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Load the compact task-summary list WITHOUT fetching task bodies.

    The performance fast-path for reads (status/agents/needs-me/resume/search/
    inbox): one download of ``views/summaries.json`` replaces ``_load_all_tasks``'
    N+3 round-trips (index + search-index + next, then one body fetch per task).
    Every read command and the view builders operate on summary dicts, which now
    carry every field they read (schema.task_summary was enriched with
    ``last_touched_by`` and a flattened ``done_at`` for exactly this).

    BACKWARD COMPAT: a bus that predates this aggregate has no
    ``views/summaries.json``. When the download is absent/None we FALL BACK to the
    full ``_load_all_tasks`` path and summarize locally — correctness over speed —
    so an older bus keeps working (just without the speedup) until its next write
    materializes the aggregate."""
    summaries_view = remote.download_json(
        remote.view_remote_path("summaries"), backend=backend)
    if summaries_view and summaries_view.get("summaries") is not None:
        return summaries_view["summaries"]
    # Older bus: no aggregate yet — fall back to the authoritative full load.
    return [schema.task_summary(t) for t in _load_all_tasks(backend=backend)]


def _load_summaries_for_rebuild(
    task: dict[str, Any], *, backend: Optional[list[str]] = None
) -> list[dict[str, Any]]:
    """The write-path view-rebuild source: the summaries aggregate with the
    just-written task's summary upserted in.

    Downloads the authoritative ``views/summaries.json`` (NOT the local cache —
    another agent may have written since we loaded), replaces the entry for this
    task by id (or appends it if new), and returns the merged summary list. Since
    build_all_views gives identical output from summaries as from full bodies,
    this list is a complete view source without re-fetching any task body.

    BACKWARD COMPAT: when the aggregate is absent (older bus that never wrote it),
    fall back to the full ``_load_all_tasks`` path and summarize locally —
    correctness over speed — so views still rebuild from the complete task set.
    The fallback returns summaries too, so build_all_views sees a uniform shape.

    ROBUSTNESS (S2): the single aggregate is one file under last-writer-wins, so
    a concurrent peer's write could leave the copy we downloaded missing a task.
    Two layers recover it: (1) we union in our local cached task summaries,
    freshest-per-id by ``updated_at`` (never resurrecting a stale local copy over
    a newer remote one); and (2) the SELF-HEAL below — we enumerate the durable
    per-task files and re-include any whose file exists but whom the aggregate
    dropped. Because the task files are the un-clobberable source of truth, a
    dropped task is recovered on the very NEXT write by any agent, not only on a
    full ``reconcile``.

    ACK PRESERVATION (B1): ``acked_by`` on the just-written task is recomputed
    from its event log, which is truncated to the last MAX_EVENTS_INLINE events.
    A heavily-acked broadcast can scroll an ``inbox_ack`` out of that window, so
    the body-derived acks may be INCOMPLETE. The durable aggregate entry holds
    the previously-recorded acks, so we UNION them into the written task's summary
    rather than letting a recompute silently drop an ack (which would re-surface a
    directive an agent already cleared)."""
    summaries_view = remote.download_json(
        remote.view_remote_path("summaries"), backend=backend)
    if not (summaries_view and summaries_view.get("summaries") is not None):
        # Older bus: no aggregate. Rebuild from the authoritative full task set so
        # a fresh machine doesn't truncate views; the just-written task is already
        # cached and thus present in _load_all_tasks' result.
        return [schema.task_summary(t) for t in _load_all_tasks(backend=backend)]

    # Start from the downloaded aggregate, keyed by id.
    by_id: dict[str, dict[str, Any]] = {
        s["id"]: s for s in summaries_view["summaries"] if s.get("id")
    }

    # S2 layer 1: union local cached task summaries, freshest-by-updated_at wins,
    # so a task this agent knows about that a raced aggregate dropped is recovered
    # — without ever overwriting a newer remote record with a stale local one.
    for t in cache.list_cached_tasks():
        # task_summary is now defensive (renders a partial body with "" defaults
        # rather than KeyError-ing), so a corrupt cached body is SURFACED, not
        # dropped. This try/except is kept as belt-and-suspenders: any OTHER
        # unexpected failure summarizing one entry must not crash the whole
        # rebuild AFTER the task body uploaded (which would leave stale views with
        # no needs_reconcile marker) — skip the offending entry, keep the rest.
        try:
            s = schema.task_summary(t)
        except Exception:
            continue
        if not s.get("id"):
            continue  # a body with no id can't key the aggregate; skip safely
        prev = by_id.get(s["id"])
        if prev is None or _updated_at_key(s) > _updated_at_key(prev):
            by_id[s["id"]] = s

    # S2 layer 2 — SELF-HEAL: the per-task FILES are the durable, un-clobberable
    # truth (each owned by one agent). Enumerate them and recover any id whose
    # file exists but is absent from the aggregate AND our cache — i.e. a task a
    # concurrent write dropped from summaries.json. This heals the drop on THIS
    # write (seconds) instead of leaving it invisible until a 90s reconcile. One
    # `list` call; a body is fetched ONLY for an id nothing else already covers,
    # so steady-state cost is ~0 fetches. Best-effort and ADD-only: a failed or
    # empty listing contributes nothing and can never make the rebuild worse than
    # the aggregate alone.
    try:
        prefix = f"{remote.remote_root()}/tasks/"
        for path in remote.list_files(prefix, backend=backend):
            if not path.endswith(".json"):
                continue
            tid = path.rsplit("/", 1)[-1][: -len(".json")]
            if not tid or tid in by_id:
                continue
            try:
                body = _cache_remote_task(tid, backend=backend)
                if body and body.get("id"):
                    by_id[body["id"]] = schema.task_summary(body)
            except Exception:
                continue
    except Exception:
        pass  # listing is best-effort; never break a task write over it

    # Upsert the just-written task. B1: preserve any acks the truncated event log
    # can no longer prove by unioning the prior known acked_by set.
    this_summary = schema.task_summary(task)
    prior = by_id.get(task["id"])
    if prior:
        this_summary["acked_by"] = sorted(
            set(this_summary.get("acked_by", []) or [])
            | set(prior.get("acked_by", []) or [])
        )
    by_id[task["id"]] = this_summary

    return list(by_id.values())


def _load_task(task_id: str, *, backend: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
    """Load a specific task from cache or remote."""
    t = cache.read_cached_task(task_id)
    if t is not None:
        return t
    return _cache_remote_task(task_id, backend=backend)


def _write_task_and_views(
    task: dict[str, Any],
    *,
    backend: Optional[list[str]] = None,
    command: str = "write",
    lifecycle: Optional[str] = None,
) -> bool:
    """Upload task + all views. Returns True on full success.

    ``lifecycle`` lets a caller override the command->lifecycle mapping for the
    best-effort annotation. This matters for ``update``, where the same command
    can be either a 'pickup' (a real transition INTO active) or a plain
    'update' (a progress note on an already-active task). Only the caller knows
    whether THIS call transitioned the task, so it passes the resolved tag in;
    ``_lifecycle_for`` is the fallback for callers that don't (I2)."""
    task_id = task["id"]
    task_path = remote.task_remote_path(task_id)
    op_id = uuid.uuid4().hex[:12]

    # Pre-stat for optimistic concurrency
    pre_stat = remote.stat(task_path, backend=backend)
    cached_meta = cache.read_meta(task_path)

    # Trigger merge/conflict check when:
    # - we have a cached baseline and it differs from the current remote (normal case), OR
    # - we have NO cached baseline but the file already exists remotely (fresh machine
    #   that loaded the task via _load_task or _load_all_tasks but never previously wrote
    #   it — unknown whether another agent updated it since we loaded it).
    # Skipping this check when cached_meta is None would silently overwrite concurrent
    # remote changes from other agents on cross-machine sessions.
    needs_merge_check = pre_stat is not None and (
        cached_meta is None or remote.stat_changed(cached_meta, pre_stat)
    )
    if needs_merge_check:
        fresh = remote.download_json(task_path, backend=backend)
        if fresh:
            merged = _try_merge(task, fresh)
            if merged is None:
                ops_log.log_op(command, task_id, status="conflict",
                               error="Unsafe merge — remote version changed")
                raise schema.ConflictError(
                    f"Remote task {task_id} changed and merge is unsafe. "
                    f"Run 'fulcra-coord reconcile' to repair."
                )
            task = merged

    # Write operation marker before fan-out
    op_marker = {
        "op_id": op_id,
        "command": command,
        "task_id": task_id,
        "status": "in_progress",
        "needs_reconcile": False,
        "started_at": _now_iso(),
    }
    cache.write_op_marker(op_id, op_marker)

    # Upload task file
    task_ok = remote.upload_json(task, task_path, backend=backend)
    if not task_ok:
        op_marker["status"] = "failed"
        op_marker["needs_reconcile"] = True
        cache.write_op_marker(op_id, op_marker)
        ops_log.log_op(command, task_id, status="error", error="Task upload failed")
        return False

    # Post-stat for version tracking
    post_stat = remote.stat(task_path, backend=backend)
    if post_stat:
        cache.write_meta(task_path, post_stat)

    _stamp_session_pointer(task)

    cache.write_cached_task(task)

    # Regenerate all views from the compact summaries aggregate, NOT re-fetched
    # task bodies. build_all_views produces identical output from task_summary
    # dicts as from full bodies (guarded by the equivalence test), so the
    # authoritative ``views/summaries.json`` (one download) plus the just-written
    # task's own summary upserted in is a complete, current view source. This is
    # the write-path half of the perf refactor: it removes the per-task body
    # fetch loop (~N round-trips) that _load_all_tasks performed on every write.
    #
    # BACKWARD COMPAT: a bus that predates the aggregate has no summaries.json. In
    # that case _load_summaries_for_rebuild returns None and we fall back to the
    # old _load_all_tasks path (correctness over speed) — a fresh machine that ran
    # only _load_task() still pulls every remote task before building views, so no
    # task is silently dropped. The current task is already cached (line above),
    # so it is always part of the rebuilt set either way.
    rebuild_source = _load_summaries_for_rebuild(task, backend=backend)
    all_views = views.build_all_views(rebuild_source)

    # Upload views CONCURRENTLY (P1): remote.upload_json is thread-safe (each
    # call writes a unique tempfile + runs an independent subprocess; remote.py
    # holds no shared mutable state), so a small thread pool collapses the ~8-15
    # sequential view uploads into one round-trip's wall-time. Semantics are
    # preserved exactly: per-view success is collected, any failure lands in
    # view_failures, and the partial-upload handling below is unchanged. Local
    # cache writes happen in the main thread after the futures resolve.
    view_items = list(all_views.items())
    view_failures = []

    def _upload_one(item):
        view_name, view_data = item
        vpath = _view_name_to_remote(view_name)
        # S3: treat a RAISING upload as a failed view, not an escape hatch. If
        # upload_json ever raises (rather than returning False), an unguarded
        # pool.map would re-raise out of _write_task_and_views, bypassing the
        # view_failures -> NeedsReconcile path and leaving a half-written op with
        # an in_progress marker. Catching keeps the contract: any failure (False
        # OR exception) lands in view_failures.
        try:
            ok = remote.upload_json(view_data, vpath, backend=backend)
        except Exception:
            ok = False
        return view_name, ok

    max_workers = min(8, len(view_items)) or 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for view_name, ok in pool.map(_upload_one, view_items):
            if not ok:
                view_failures.append(view_name)

    # Cache every view locally regardless of upload outcome (matches prior
    # behavior: the old loop wrote the cache for every view, success or not).
    for view_name, view_data in view_items:
        cache.write_cached_view(view_name, view_data)

    if view_failures:
        op_marker["status"] = "partial"
        op_marker["needs_reconcile"] = True
        op_marker["failed_views"] = view_failures
        cache.write_op_marker(op_id, op_marker)
        ops_log.log_op(command, task_id, status="partial",
                       detail=f"Task written, views failed: {view_failures}")
        # BUG 10: the TASK BODY uploaded successfully — the lifecycle transition
        # is REAL — so record the annotation BEFORE raising. Previously the emit
        # was only after a fully-clean write, so a partial view failure dropped
        # the lifecycle moment forever (reconcile repairs views but never emits).
        # The emit is best-effort/guarded and its own idempotency marker keeps a
        # later retry from double-emitting, so it can't change the NeedsReconcile
        # outcome.
        _emit_lifecycle(command, task, lifecycle, backend=backend)
        raise schema.NeedsReconcile(
            f"Task {task_id} written, but view upload partial. "
            f"Run 'fulcra-coord reconcile' to repair views."
        )

    op_marker["status"] = "done"
    cache.write_op_marker(op_id, op_marker)
    cache.clear_op_marker(op_id)
    ops_log.log_op(command, task_id, status="ok")

    _emit_lifecycle(command, task, lifecycle, backend=backend)

    return True


def _emit_lifecycle(
    command: str,
    task: dict[str, Any],
    lifecycle: Optional[str],
    *,
    backend: Optional[list[str]] = None,
) -> None:
    """Best-effort lifecycle annotation on the operator's Fulcra timeline.

    Called once the TASK BODY has landed (success path, or before raising
    NeedsReconcile on a partial view failure — see BUG 10): the transition is
    real either way. emit_lifecycle_annotation is itself best-effort and never
    raises, but the call site is guarded too so even a programming error in the
    hook cannot break a task write or change a NeedsReconcile outcome. The
    annotation carries its own idempotency marker, so calling this on a retry of
    the same transition does not double-emit."""
    try:
        lc = lifecycle if lifecycle is not None else _lifecycle_for(command, task)
        if lc is not None:
            lifecycle_annotations.emit_lifecycle_annotation(
                lifecycle=lc,
                task=task,
                agent=identity.resolve_agent(),
                backend=backend,
            )
    except Exception:
        pass


def _lifecycle_for(command: str, task: dict[str, Any]) -> Optional[str]:
    """Map a write command (+ resulting task state) onto an annotation lifecycle.

    The four lifecycle tags arc specced are create / pickup / update / complete:

      * create   — a task came into existence: ``start``, ``tell``, ``broadcast``
                   (broadcast delegates to ``tell`` so it arrives as "tell").
      * pickup   — an agent claimed/started the work: an ``update`` that
                   ACTUALLY TRANSITIONED the task INTO ``active`` this call. This
                   pickup-vs-update distinction is a transition EVENT, not a
                   resulting state — a progress note on an already-active task is
                   NOT a pickup. Because only the caller (cmd_update) knows
                   whether this call transitioned, it passes the resolved tag to
                   ``_write_task_and_views(lifecycle=...)``; this fallback maps a
                   bare ``update`` (no transition signal) to plain ``update`` so
                   it can never mis-tag an already-active task as pickup (I2).
      * update   — any other touch that doesn't create/claim/complete: a plain
                   ``update``, an ``assign`` (reassignment), ``block``, ``pause``.
      * complete — the task finished: ``done``.

    Commands with no timeline meaning (e.g. ``abandon``, internal ``inbox-ack``,
    bare ``write``/``reconcile``) return None so no annotation is emitted."""
    if command in ("start", "tell", "broadcast"):
        return "create"
    if command == "done":
        return "complete"
    if command == "update":
        return "update"
    if command in ("assign", "block", "pause"):
        return "update"
    return None


def _view_name_to_remote(name: str) -> str:
    if name == "index":
        return remote.view_remote_path("index")
    if name.startswith("workstreams/"):
        ws = name[len("workstreams/"):]
        return remote.workstream_remote_path(ws)
    if name.startswith("agents/"):
        agent = name[len("agents/"):]
        return remote.agent_remote_path(agent)
    return remote.view_remote_path(name)


def _updated_at_key(task: dict[str, Any]) -> datetime:
    """Parsed, tz-aware ``updated_at`` for newer/older comparison, epoch on miss.

    BUG 1 (mixed-precision data loss): a raw STRING compare of two ``updated_at``
    values mis-orders timestamps in the same second when one was emitted with
    microsecond=0 (``...:45Z``) and the other with microseconds>0
    (``...:45.000001Z``) — lexically ``.`` < ``Z`` so the truly-newer fractional
    timestamp wrongly sorts BEFORE the whole-second one, and the merge would
    silently drop the newer side's field edits. The emission fix gives all NEW
    timestamps fixed-width microseconds, but mixed-precision data already on the
    bus must still compare correctly, so the merge compares PARSED datetimes via
    the shared ``views._parse_dt`` (which coerces naive->UTC). A missing or
    unparseable timestamp sorts oldest (epoch) so a clock-less side never wins."""
    dt = views._parse_dt(task.get("updated_at", ""))
    return dt if dt is not None else datetime.min.replace(tzinfo=timezone.utc)


def _try_merge(
    local: dict[str, Any], remote_task: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Attempt a safe merge. Returns merged task or None if unsafe.

    Status transition events have type == the target status name (e.g. "active",
    "done") — NOT "status_change".

    A conflict only exists when BOTH sides have independently created new
    status-transition events (i.e. both agents changed status from an agreed
    base).  If only REMOTE changed status, its new state is authoritative and
    local's non-status updates (summary, next_action, events) are merged in on
    top.  Checking for remote-only status changes as a conflict caused spurious
    ConflictErrors when one agent updated task fields while another concurrently
    changed status — a normal cross-environment workflow.
    """
    local_status = local.get("status")
    remote_status = remote_task.get("status")
    local_event_times = {e["at"] for e in local.get("events", [])}
    remote_event_times = {e["at"] for e in remote_task.get("events", [])}

    import copy

    if local_status != remote_status:
        local_has_new_status_change = any(
            e.get("type") in schema.VALID_STATUSES and e["at"] not in remote_event_times
            for e in local.get("events", [])
        )
        remote_has_new_status_change = any(
            e.get("type") in schema.VALID_STATUSES and e["at"] not in local_event_times
            for e in remote_task.get("events", [])
        )

        if local_has_new_status_change and remote_has_new_status_change:
            return None  # Both sides independently changed status → unsafe

        if remote_has_new_status_change and not local_has_new_status_change:
            # Only remote changed status: remote's status transition is the
            # authoritative state, so it MUST win regardless of which side has
            # the more recent updated_at. Use remote as the field base; layer
            # local's newer non-status field edits on top only when local is
            # more recent (the symmetric case below covers the same logic for
            # the local-only-status-change path).
            merged = copy.deepcopy(remote_task)
            if _updated_at_key(local) > _updated_at_key(remote_task):
                # Carry ALL non-event scalar/dict fields from the more-recent
                # local side EXCEPT status (remote's transition is authoritative)
                # — a hardcoded allowlist silently dropped not_before/due/
                # blocked_on/priority/title/etc. (BUG 1, data-loss).
                _carry_fields(merged, local, skip_status=True)
            _union_events_and_acked(merged, local, remote_task,
                                    local_event_times, remote_event_times)
            _repair_merged_tags(merged, local, remote_task)
            return merged

    # Same status, or only local changed status: pick the more-recent side as
    # the field base so EVERY non-event field follows the newer write. Status
    # is taken from the side that changed it: when only local changed status,
    # local must keep its status even if remote's updated_at is newer.
    if _updated_at_key(remote_task) > _updated_at_key(local):
        newer, older = remote_task, local
    else:
        newer, older = local, remote_task

    merged = copy.deepcopy(newer)

    if local_status != remote_status:
        # Reaches here only when local changed status and remote did not
        # (the remote-only branch returned above). Local's status is
        # authoritative — restore it if remote happened to be the newer base.
        merged["status"] = local_status

    _union_events_and_acked(merged, local, remote_task,
                            local_event_times, remote_event_times)
    _repair_merged_tags(merged, local, remote_task)
    return merged


# Event-only / acked_by-only keys are reconciled by the union helper, never by
# the wholesale field carry — copying them would clobber the union.
_MERGE_EVENT_KEYS = {"events", "acked_by"}


def _carry_fields(
    dst: dict[str, Any], src: dict[str, Any], *, skip_status: bool = False
) -> None:
    """Copy every non-event scalar/dict field from src onto dst (BUG 1).

    Replaces the old hardcoded allowlist (current_summary/next_action/assignee/
    owner_agent). Any field the more-recent side carries — not_before, due,
    blocked_on, priority, title, collaborators, links, etc. — now survives the
    merge instead of being lost to the merge base.

    skip_status keeps the destination's status intact: used on the
    remote-only-status-change path, where remote's transition is authoritative
    and must not be clobbered by the (status-unchanged) newer local side.
    """
    for key, value in src.items():
        if key in _MERGE_EVENT_KEYS:
            continue
        if skip_status and key == "status":
            continue
        dst[key] = value


def _union_events_and_acked(
    merged: dict[str, Any],
    local: dict[str, Any],
    remote_task: dict[str, Any],
    local_event_times: set,
    remote_event_times: set,
) -> None:
    """Union events from both sides (dedup by `at`, sort, truncate) and union
    acked_by. Idempotent regardless of which side `merged` started from."""
    by_time: dict[str, dict[str, Any]] = {}
    for ev in local.get("events", []):
        by_time[ev["at"]] = ev
    for ev in remote_task.get("events", []):
        by_time.setdefault(ev["at"], ev)
    events = sorted(by_time.values(), key=lambda e: e["at"])
    merged["events"] = events[-schema.MAX_EVENTS_INLINE:]

    acked = set(local.get("acked_by") or []) | set(remote_task.get("acked_by") or [])
    if acked or "acked_by" in local or "acked_by" in remote_task:
        merged["acked_by"] = sorted(acked)


def _repair_merged_tags(
    merged: dict[str, Any],
    local: dict[str, Any],
    remote_task: dict[str, Any],
) -> None:
    """Rebuild standard tags after merging fields from different sides.

    A safe merge can intentionally combine local status with newer remote fields.
    Copying a whole task as the field base then restoring just ``status`` leaves
    stale derived tags such as ``status:proposed`` on an ``active`` task. Rebuild
    the standard tags from the merged fields and keep non-standard tags from both
    sides, so membership markers like ``needs:human`` survive too.
    """
    standard_prefixes = ("workstream:", "agent:", "kind:", "status:", "priority:")
    extra = [
        tag
        for task in (local, remote_task, merged)
        for tag in (task.get("tags") or [])
        if not any(tag.startswith(prefix) for prefix in standard_prefixes)
    ]
    merged["tags"] = schema.build_tags(
        status=merged.get("status", ""),
        workstream=merged.get("workstream", ""),
        agent=merged.get("owner_agent", ""),
        kind=schema._extract_kind_from_tags(merged.get("tags") or []),
        priority=merged.get("priority", ""),
        extra=extra or None,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_session_task(args: Any, backend: Optional[list[str]] = None) -> int:
    """Print the task id for a session id (used by hooks). Hidden command."""
    ptr = session_link.read_pointer(args.session_id)
    if not ptr or not ptr.get("task_id"):
        return 1
    print(ptr["task_id"])
    return 0


def cmd_identity(args: Any, backend: Optional[list[str]] = None) -> int:
    """Show, set, or clear this host's declared agent identity (the handshake).

    - `identity`            → show the resolved id and its source (explicit/env/
                              config/derived) so an operator can see who they are
                              and *why*.
    - `identity set <id>`   → persist <id> for the CURRENT cwd; an existing
                              long-running session declares its stable id once and
                              every subsequent bus op in that repo reuses it.
                              Per-cwd, so a sibling session in another repo is
                              never clobbered.
    - `identity clear`      → remove the persisted id for the current cwd (fall
                              back to env/derived; the legacy global is NOT used).
    - `identity migrate`    → copy the legacy global identity (if any) into this
                              cwd's per-cwd entry, so a pre-split setup keeps its
                              declared id without the silent global fallback (I-1).
    """
    action = getattr(args, "identity_action", None)
    out_format = getattr(args, "format", "table")

    if action == "set":
        agent_id = args.agent_id
        identity.set_identity(agent_id)
        if out_format == "json":
            _print_json({"agent": agent_id, "source": "config", "action": "set"})
        else:
            _info(f"Identity set: {agent_id}")
            _info(f"  Persisted to: {identity.identity_path()}")
        return 0

    if action == "migrate":
        # I-1 migration helper: the legacy global is no longer resolved silently,
        # so an operator who relied on it copies it into this repo's per-cwd entry
        # once. No-op (with a note) when there's nothing to migrate.
        legacy = identity.read_legacy_identity()
        if legacy:
            identity.set_identity(legacy)
        agent, source = identity.resolve_agent_source()
        if out_format == "json":
            _print_json({"agent": agent, "source": source, "action": "migrate",
                         "migrated": bool(legacy)})
        else:
            if legacy:
                _info(f"Migrated legacy global identity '{legacy}' into this repo.")
                _info(f"  Persisted to: {identity.identity_path()}")
            else:
                _info("No legacy global identity to migrate.")
            _info(f"Now resolving as: {agent}  (source: {source})")
        return 0

    if action == "clear":
        removed = identity.clear_identity()
        agent, source = identity.resolve_agent_source()
        if out_format == "json":
            _print_json({"agent": agent, "source": source, "action": "clear",
                         "removed": removed})
        else:
            if removed:
                _info("Identity cleared.")
            else:
                _info("No persisted identity to clear.")
            _info(f"Now resolving as: {agent}  (source: {source})")
        return 0

    # show (default)
    agent, source = identity.resolve_agent_source()
    # I-1: surface a one-line hint when a legacy global exists AND this cwd has no
    # per-cwd entry, so an operator who set the old global learns it no longer
    # resolves automatically and how to re-declare it for this repo.
    legacy = identity.read_legacy_identity()
    show_legacy_hint = bool(legacy) and identity.read_identity() is None
    if out_format == "json":
        _print_json({"agent": agent, "source": source,
                     "identity_file": str(identity.identity_path()),
                     "legacy_global": legacy})
    else:
        _info(f"Agent:  {agent}")
        _info(f"Source: {source}")
        if show_legacy_hint:
            _info(f"  Note: legacy global identity '{legacy}' found; it is no longer "
                  f"used automatically —")
            _info(f"        run `fulcra-coord identity set <id>` to set this repo's "
                  f"identity (or `identity migrate`).")
        elif source != "config":
            _info(f"  (declare a stable id with: fulcra-coord identity set <agent-id>)")
    return 0


def cmd_human(args: Any, backend: Optional[list[str]] = None) -> int:
    """Show, set, or clear the human operator's handle (situational awareness).

    The human is an addressable identity on the bus — the one tasks are
    "blocked on ME" against. Defaults to the neutral ``human`` so the public repo
    carries no name; this operator runs ``fulcra-coord human set ash``.

    - `human`              → show the resolved handle + its source (env/config/
                             default).
    - `human set <handle>` → persist <handle> globally for this machine.
    - `human clear`        → remove the persisted handle (fall back to env/default).
    """
    action = getattr(args, "human_action", None)
    out_format = getattr(args, "format", "table")

    if action == "set":
        handle = args.handle
        identity.set_human(handle)
        if out_format == "json":
            _print_json({"human": handle, "source": "config", "action": "set"})
        else:
            _info(f"Human handle set: {handle}")
            _info(f"  Persisted to: {identity.human_path()}")
        return 0

    if action == "clear":
        removed = identity.clear_human()
        handle, source = identity.resolve_human_source()
        if out_format == "json":
            _print_json({"human": handle, "source": source, "action": "clear",
                         "removed": removed})
        else:
            _info("Human handle cleared." if removed
                  else "No persisted human handle to clear.")
            _info(f"Now resolving as: {handle}  (source: {source})")
        return 0

    # show (default)
    handle, source = identity.resolve_human_source()
    if out_format == "json":
        _print_json({"human": handle, "source": source,
                     "human_file": str(identity.human_path())})
    else:
        _info(f"Human:  {handle}")
        _info(f"Source: {source}")
        if source == "default":
            _info("  (personalize with: fulcra-coord human set <handle>)")
    return 0


def cmd_annotations(args: Any, backend: Optional[list[str]] = None) -> int:
    """Enable, disable, or inspect the Agent-Tasks timeline annotations writer.

    Annotations drop a durable breadcrumb on the operator's Fulcra timeline every
    time an agent creates/picks-up/updates/completes a task. Historically they
    only fired if ``FULCRA_COORD_ANNOTATIONS=http`` was exported in each shell, so
    the timeline rarely filled. This command PERSISTS the enablement once
    (machine-wide) so every agent emits without a per-session export.

    - ``annotations on``     → persist ``http`` to the config file.
    - ``annotations off``    → remove the config file (resolves to off unless the
                               env var is set — env always wins).
    - ``annotations`` / ``status`` → report the resolved mode, its SOURCE
                               (env/config/default), and whether a bearer token
                               resolves (the token VALUE is never printed).
    """
    action = getattr(args, "annotations_action", None)
    out_format = getattr(args, "format", "table")

    if action == "on":
        path = lifecycle_annotations.set_persisted_mode("http")
        if out_format == "json":
            _print_json({"mode": "http", "source": "config", "action": "on"})
        else:
            _info("Annotations enabled (mode: http).")
            _info(f"  Persisted to: {path}")
            _info("  Every agent on this machine will now emit Agent-Tasks "
                  "timeline annotations.")
        return 0

    if action == "off":
        removed = lifecycle_annotations.clear_persisted_mode()
        mode, source = lifecycle_annotations.resolve_mode_source()
        if out_format == "json":
            _print_json({"mode": mode, "source": source, "action": "off",
                         "removed": removed})
        else:
            _info("Annotations disabled." if removed
                  else "No persisted annotation mode to clear.")
            if source == "env":
                _info(f"  Note: FULCRA_COORD_ANNOTATIONS is set in this shell — "
                      f"still resolving as {mode} (env overrides config).")
            else:
                _info(f"  Now resolving as: {mode}  (source: {source})")
        return 0

    # status (default / bare)
    mode, source = lifecycle_annotations.resolve_mode_source()
    # Reuse the doctor's token check so `status` and `[Annotations]` agree on
    # whether a write could actually authenticate. NEVER print the token value.
    token_ok = bool(lifecycle_annotations._resolve_token())
    if out_format == "json":
        _print_json({"mode": mode, "source": source, "token_ok": token_ok,
                     "config_file": str(lifecycle_annotations._annotations_config_path())})
    else:
        _info(f"Annotations: {mode}")
        _info(f"Source:      {source}")
        _info(f"Token:       {'OK' if token_ok else 'not available'}")
        if mode == "off":
            _info("  (enable for every agent with: fulcra-coord annotations on)")
    return 0


def _report_resolved_cli(plan: dict[str, Any]) -> None:
    """Print the CLI invocation baked into the just-installed hooks, and warn if
    it had to fall back to `python -m` (Gap 1) — that works, but signals the
    `fulcra-coord` entry point is not on PATH, which the operator may want to fix
    (e.g. with `fulcra-coord install-shim`)."""
    from . import cli_invocation
    resolved = plan.get("resolved_cli")
    if resolved:
        _info(f"  Hooks will call: {resolved}")
    if cli_invocation.used_python_m_fallback():
        _warn("fulcra-coord is not on PATH; hooks use the `python -m fulcra_coord` "
              "fallback. To put it on PATH, run: fulcra-coord install-shim")


def cmd_install_claude_code(args: Any, backend: Optional[list[str]] = None) -> int:
    """Install/uninstall Claude Code lifecycle hooks for coordination."""
    scope = "project" if getattr(args, "scope", "global") == "project" else "global"
    plan = claude_code.install_claude_code(
        scope=scope, uninstall=args.uninstall, dry_run=args.dry_run)
    if args.dry_run:
        _info("[dry-run] Would write to: " + plan["settings"])
        _info("[dry-run] Hook scripts: " + plan["hooks_dir"])
        for e in plan.get("events", []):
            _info(f"  + {e}")
        import json as _json
        if plan.get("would_write") is not None:
            _info("[dry-run] Resulting settings.json:")
            _info(_json.dumps(plan["would_write"], indent=2))
        return 0
    if args.uninstall:
        _info(f"Removed fulcra-coord hooks from {plan['settings']}")
        return 0
    _info(f"Installed Claude Code hooks ({scope}) -> {plan['settings']}")
    for e in plan["events"]:
        _info(f"  + {e}")
    _report_resolved_cli(plan)
    _info("New Claude Code sessions will now surface in-flight work and checkpoint automatically.")
    _info("Verify auth/connectivity with: fulcra-coord doctor")
    return 0


def cmd_install_openclaw(args: Any, backend: Optional[list[str]] = None) -> int:
    """Install/uninstall OpenClaw Track A coordination artifacts."""
    hooks_root = getattr(args, "hooks_root", None)
    plan = openclaw.install_openclaw(
        hooks_root=hooks_root, uninstall=args.uninstall, dry_run=args.dry_run)
    if args.dry_run:
        _info("[dry-run] OpenClaw hooks root: " + plan["hooks_root"])
        for w in plan.get("writes", []):
            _info(f"  + would write {w}")
        for r in plan.get("removes", []):
            _info(f"  - would remove {r}")
        return 0
    if args.uninstall:
        _info(f"Removed fulcra-coord OpenClaw artifacts from {plan['hooks_root']}")
        return 0
    _info(f"Installed OpenClaw Track A artifacts -> {plan['hooks_root']}")
    for d in plan.get("hook_dirs", []):
        _info(f"  + hook {d}")
    for f in plan.get("prompt_files", []):
        _info(f"  + prompt {f}")
    _report_resolved_cli(plan)
    _info("New OpenClaw sessions will surface in-flight work at boot and park "
          "active tasks on gateway shutdown.")
    _info("The handler.ts templates are written to the real OpenClaw "
          "automation-hook API (verified against the SDK source); they still "
          "can't be run in this repo.")

    # Track B add-on: materialize the Plugin-SDK plugin if requested. This is a
    # source drop only — building + registering needs npm/tsc, which the CLI
    # can't do, so we print the manual finish-the-install steps.
    if getattr(args, "with_plugin", False):
        from . import openclaw_plugin
        pplan = openclaw_plugin.install_openclaw_plugin(
            plugin_dir=getattr(args, "plugin_dir", None),
            uninstall=args.uninstall, dry_run=args.dry_run)
        if args.dry_run:
            _info("[dry-run] Track B plugin dir: " + pplan["plugin_dir"])
            for w in pplan.get("writes", []):
                _info(f"  + would write {w}")
            for r in pplan.get("removes", []):
                _info(f"  - would remove {r}")
        elif args.uninstall:
            _info(f"Removed Track B plugin sources from {pplan['plugin_dir']}")
        else:
            _info(f"Materialized Track B plugin sources -> {pplan['plugin_dir']}")
            _info("Build and register the plugin (needs npm; the CLI can't):")
            for step in pplan["build_steps"]:
                _info(f"    {step}")

    _info("Verify auth/connectivity with: fulcra-coord doctor")
    return 0


def cmd_install_codex(args: Any, backend: Optional[list[str]] = None) -> int:
    """Install/uninstall Codex lifecycle hooks for coordination (Gap 4)."""
    plan = codex.install_codex(
        uninstall=args.uninstall, dry_run=args.dry_run,
        target_dir=getattr(args, "target_dir", None))
    if args.dry_run:
        _info("[dry-run] Would write to: " + plan["hooks_file"])
        _info("[dry-run] Hook scripts: " + plan["hooks_dir"])
        for e in plan.get("events", []):
            _info(f"  + {e}")
        if plan.get("would_write") is not None:
            import json as _json
            _info("[dry-run] Resulting hooks.json:")
            _info(_json.dumps(plan["would_write"], indent=2))
        return 0
    if args.uninstall:
        _info(f"Removed fulcra-coord hooks from {plan['hooks_file']}")
        return 0
    _info(f"Installed Codex hooks -> {plan['hooks_file']}")
    for e in plan["events"]:
        _info(f"  + {e}")
    _report_resolved_cli(plan)
    _info("Codex SessionStart surfaces in-flight work; PreCompact checkpoints "
          "before context loss.")
    _info("No Stop hook by design — Codex Stop fires every turn; end-parking is "
          "delegated to the heartbeat. Install it with: fulcra-coord install-heartbeat")
    _info("Verify auth/connectivity with: fulcra-coord doctor")
    return 0


def cmd_install_heartbeat(args: Any, backend: Optional[list[str]] = None) -> int:
    """Install/uninstall a scheduled `fulcra-coord reconcile` heartbeat (Gap 2).

    The heartbeat is the safety net for crashed agents and end-hook-less surfaces
    (ChatGPT, and Codex whose Stop fires every turn): it re-runs reconcile on a
    cadence to sweep stale `active` tasks and rebuild needs-attention.json.
    """
    plan = heartbeat.install_heartbeat(
        interval_min=getattr(args, "interval_min", heartbeat.INTERVAL_MIN_DEFAULT),
        uninstall=args.uninstall,
        dry_run=args.dry_run,
        target_dir=getattr(args, "target_dir", None),
        logs_dir=getattr(args, "logs_dir", None),
    )
    if args.dry_run:
        _info(f"[dry-run] Heartbeat mechanism: {plan['mechanism']}")
        _info(f"[dry-run] Scheduled command: {plan['cli_command']} reconcile "
              f"(every {plan['interval_min']} min)")
        for w in plan.get("writes", []):
            _info(f"  + would write {w}")
        for r in plan.get("removes", []):
            _info(f"  - would remove {r}")
        return 0
    if args.uninstall:
        _info(f"Removed fulcra-coord heartbeat ({plan['mechanism']}).")
        return 0
    _info(f"Installed fulcra-coord heartbeat ({plan['mechanism']}) — "
          f"reconcile every {plan['interval_min']} min.")
    for w in plan.get("writes", []):
        _info(f"  + {w}")
    if plan["mechanism"] == "launchd":
        _info("Load it now (or it loads at next login): "
              f"launchctl load -w {plan['writes'][0]}")
    else:
        _info("Apply it now: crontab " + plan["writes"][0])
    _info(f"Scheduled command: {plan['cli_command']} reconcile")
    return 0


def cmd_install_listener(args: Any, backend: Optional[list[str]] = None) -> int:
    """Install/uninstall a scheduled `fulcra-coord notify-inbox` listener (Part 3).

    The durable, per-agent inbox listener: it polls for directives addressed to
    this agent on a cadence (default 10 min) and surfaces + notifies — so an
    idle agent notices directed work without a session open. launchd on macOS,
    crontab elsewhere. The Claude Code "scheduled remote agent" is the preferred
    mechanism (see adapters/claude-code/LISTENER.md); this is the harness-free
    fallback.
    """
    agent = getattr(args, "agent", None) or _derive_agent()
    plan = listener.install_listener(
        agent=agent,
        interval_min=getattr(args, "interval_min", listener.INTERVAL_MIN_DEFAULT),
        uninstall=args.uninstall,
        dry_run=args.dry_run,
        target_dir=getattr(args, "target_dir", None),
        logs_dir=getattr(args, "logs_dir", None),
    )
    if args.dry_run:
        _info(f"[dry-run] Listener mechanism: {plan['mechanism']}")
        _info(f"[dry-run] Scheduled command: {plan['cli_command']} "
              f"notify-inbox --agent {agent} (every {plan['interval_min']} min)")
        if plan.get("supersedes_legacy"):
            _info("[dry-run] Would supersede the legacy machine-global listener "
                  f"job watching {agent} (it migrates to a per-agent job).")
        for w in plan.get("writes", []):
            _info(f"  + would write {w}")
        for r in plan.get("removes", []):
            _info(f"  - would remove {r}")
        return 0
    if args.uninstall:
        _info(f"Removed fulcra-coord listener ({plan['mechanism']}).")
        return 0
    _info(f"Installed fulcra-coord listener ({plan['mechanism']}) for {agent} — "
          f"notify-inbox every {plan['interval_min']} min.")
    for w in plan.get("writes", []):
        _info(f"  + {w}")
    if plan["mechanism"] == "launchd":
        _info("Load it now (or it loads at next login): "
              f"launchctl load -w {plan['writes'][0]}")
    else:
        _info("Apply it now: crontab " + plan["writes"][0])
    _info(f"Scheduled command: {plan['cli_command']} notify-inbox --agent {agent}")
    return 0


def cmd_status(args: Any, backend: Optional[list[str]] = None) -> int:
    """Show current coordination status.

    Reads the compact summaries aggregate (one download) rather than fetching
    every task body — every field this command and build_index read is present
    on a summary. Falls back to a full load on an older bus (see
    _load_task_summaries)."""
    all_tasks = _load_task_summaries(backend=backend)

    workstream_filter = getattr(args, "workstream", None)
    agent_filter = getattr(args, "agent", None)

    if workstream_filter:
        all_tasks = [t for t in all_tasks if t.get("workstream") == workstream_filter]
    if agent_filter:
        all_tasks = [t for t in all_tasks if t.get("owner_agent") == agent_filter]

    out_format = getattr(args, "format", "table")

    if out_format == "json":
        idx = views.build_index(all_tasks)
        _print_json(idx)
        return 0

    by_status: dict[str, list] = {}
    for t in all_tasks:
        s = t.get("status", "unknown")
        by_status.setdefault(s, []).append(t)

    total = len(all_tasks)
    print(f"\n{'='*60}")
    print(f"  Fulcra Coordination Status")
    if workstream_filter:
        print(f"  Workstream: {workstream_filter}")
    if agent_filter:
        print(f"  Agent: {agent_filter}")
    print(f"  Total tasks: {total}")
    print(f"{'='*60}")

    for status_name in ("active", "blocked", "waiting", "proposed", "done", "abandoned"):
        tasks_in_status = by_status.get(status_name, [])
        if not tasks_in_status:
            continue
        print(f"\n  [{status_name.upper()}] ({len(tasks_in_status)})")
        for t in sorted(tasks_in_status, key=lambda x: x.get("priority", "P9")):
            priority = t.get("priority", "??")
            title = t.get("title", "")[:60]
            task_id = t.get("id", "")
            print(f"    [{priority}] {task_id[:28]}  {title}")
            summary = t.get("current_summary", "").strip()
            if summary:
                print(f"           {summary[:80]}")
            blocked_on = t.get("blocked_on")
            if blocked_on:
                print(f"           Blocked: {blocked_on[:70]}")
            next_action = t.get("next_action", "").strip()
            if next_action and status_name in ("waiting", "blocked"):
                print(f"           Next: {next_action[:70]}")

    markers = [m for m in cache.list_op_markers() if m.get("needs_reconcile")]
    if markers:
        print(f"\n  WARN: {len(markers)} operation(s) need reconcile.")
        for m in markers:
            print(f"    OP-{m['op_id']}: {m.get('task_id', '?')} — {m.get('status')}")

    print()
    return 0


def cmd_agents(args: Any, backend: Optional[list[str]] = None) -> int:
    """Cross-agent digest (Gap 3): what every agent is currently working on.

    Groups active/waiting/blocked tasks by owner_agent and shows, per agent, the
    per-status counts and each task's title + next_action, marking stale tasks
    with a ⚠. This is the original "what are all my agents doing / what was I
    working on" recall surface — `status` lists tasks but isn't shaped for it.

    Pure read over the existing task set; no new remote state. The stale flag is
    read from the materialized active view when present (so the heartbeat's
    judgment is authoritative) and computed on the fly otherwise.
    """
    out_format = getattr(args, "format", "table")
    mine = getattr(args, "mine", None)

    # Summaries fast-path: cmd_agents reads only status/owner_agent/id/title/
    # priority/next_action/updated_at — all present on a summary — and is_stale
    # reads status + updated_at. No task body is needed.
    all_tasks = _load_task_summaries(backend=backend)
    open_tasks = [t for t in all_tasks if t.get("status") in ("active", "waiting", "blocked")]
    if mine:
        open_tasks = [t for t in open_tasks if t.get("owner_agent") == mine]

    # Prefer the stale flags already materialized in the active view (the
    # heartbeat reconciler owns that judgment); fall back to computing per task.
    stale_by_id: dict[str, bool] = {}
    av = cache.read_cached_view("active")
    if av:
        for s in av.get("tasks", []):
            if "stale" in s:
                stale_by_id[s.get("id")] = bool(s.get("stale"))
    now = datetime.now(timezone.utc)

    def _stale(t: dict[str, Any]) -> bool:
        tid = t.get("id")
        if tid in stale_by_id:
            return stale_by_id[tid]
        return views.is_stale(t, now)

    # Group by owner_agent. Within an agent, most-recent activity first so
    # `--mine` answers "what was I most recently working on".
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in open_tasks:
        groups.setdefault(t.get("owner_agent", "unknown"), []).append(t)

    agent_blocks = []
    for agent in sorted(groups):
        tasks = sorted(groups[agent], key=lambda x: x.get("updated_at", ""), reverse=True)
        counts = {"active": 0, "waiting": 0, "blocked": 0}
        task_entries = []
        for t in tasks:
            st = t.get("status", "")
            if st in counts:
                counts[st] += 1
            task_entries.append({
                "id": t.get("id"),
                "title": t.get("title", ""),
                "status": st,
                "priority": t.get("priority", ""),
                "next_action": t.get("next_action", ""),
                "stale": _stale(t),
            })
        agent_blocks.append({"agent": agent, "counts": counts, "tasks": task_entries})

    # Fold in presence (situational awareness): annotate each task-derived agent
    # with its declared workstreams + liveness, AND surface agents that have a
    # presence record but NO active task — the whole point of presence. One read
    # of the aggregate roster (no task re-fetch). Best-effort: a missing roster
    # leaves `agents` behaving exactly as before (backward compatible).
    presence_by_agent: dict[str, dict[str, Any]] = {}
    try:
        agg = remote.download_json(remote.presence_view_path(), backend=backend)
        if agg:
            roster = views.build_presence([
                {k: v for k, v in a.items() if k != "liveness"}
                for a in agg.get("agents", [])
            ])
            for a in roster["agents"]:
                if mine and a.get("agent") != mine:
                    continue
                presence_by_agent[a["agent"]] = a
    except Exception:
        presence_by_agent = {}

    # Annotate task blocks with presence (where present).
    task_agents = {b["agent"] for b in agent_blocks}
    for blk in agent_blocks:
        p = presence_by_agent.get(blk["agent"])
        if p:
            blk["presence"] = {
                "workstreams": p.get("workstreams", []),
                "summary": p.get("summary", ""),
                "last_seen": p.get("last_seen", ""),
                "liveness": p.get("liveness", ""),
            }

    # Presence-only agents: have a record but no active/waiting/blocked task.
    presence_only = [
        p for agent, p in sorted(presence_by_agent.items())
        if agent not in task_agents
    ]

    if out_format == "json":
        _print_json({"agents": agent_blocks, "presence_only": presence_only,
                     "mine": mine})
        return 0

    if not agent_blocks and not presence_only:
        scope = f" for {mine}" if mine else ""
        _info(f"No active/waiting/blocked work{scope} on the coordination bus.")
        return 0

    print(f"\n{'='*60}")
    print("  Fulcra Coordination — Agents")
    if mine:
        print(f"  Filter: {mine}")
    print(f"{'='*60}")
    for blk in agent_blocks:
        c = blk["counts"]
        print(f"\n  {blk['agent']}  "
              f"(active {c['active']} / waiting {c['waiting']} / blocked {c['blocked']})")
        p = blk.get("presence")
        if p:
            ws = ", ".join(p.get("workstreams", [])) or "(none)"
            age = _age_str(p.get("last_seen", ""))
            print(f"    presence: {ws}  [{p.get('liveness','')}] (seen {age})")
        for t in blk["tasks"]:
            mark = " ⚠" if t["stale"] else ""
            print(f"    [{t['status'].upper()}] [{t['priority']}] "
                  f"{t['id'][:28]}{mark}  {t['title'][:50]}")
            if t["next_action"]:
                print(f"          next: {t['next_action'][:70]}")

    if presence_only:
        print(f"\n  --- Present (no active task) ---")
        for p in presence_only:
            ws = ", ".join(p.get("workstreams", [])) or "(none)"
            age = _age_str(p.get("last_seen", ""))
            print(f"\n  {p['agent']}  [{p.get('liveness','')}] (seen {age})")
            print(f"    workstreams: {ws}")
            if p.get("summary"):
                print(f"    on: {p['summary'][:80]}")
    print()
    return 0


def _derive_agent() -> str:
    """Resolve the caller's agent id when not given explicitly.

    Thin wrapper over identity.resolve_agent() — the single "who am I" entry
    point. Kept as a local alias so the (many) callsites read naturally; the
    resolution order (explicit > env > persisted identity > derived) now lives in
    fulcra_coord.identity so the CLI, listener, and `identity` command agree.
    """
    return identity.resolve_agent()


def cmd_tell(args: Any, backend: Optional[list[str]] = None) -> int:
    """Create a directive task addressed at another agent (sugar over `start`).

    A directive is a `proposed` task with assignee=<the target agent> and
    owner_agent = --from (the directing agent) or unset. It lands in the target's
    inbox until they ack or claim it.
    """
    assignee = args.assignee
    title = args.title
    workstream = getattr(args, "workstream", "general") or "general"
    priority = getattr(args, "priority", "P2") or "P2"
    summary = getattr(args, "summary", "") or ""
    next_action = getattr(args, "next", "") or ""
    from_agent = getattr(args, "from", None)
    # owner_agent is the directing agent (who created it); if --from is omitted
    # we fall back to make_task's default (agent==assignee would make it self-
    # owned and thus NOT a directive), so we pass the resolved caller as agent.
    caller = from_agent or _derive_agent()

    try:
        task = schema.make_task(
            title=title,
            workstream=workstream,
            agent=caller,
            owner_agent=caller,
            assignee=assignee,
            priority=priority,
            summary=summary,
            next_action=next_action,
        )
    except schema.SchemaError as e:
        _err(str(e))
        return 1

    errs = schema.validate_task(task)
    if errs:
        _err("Task schema errors:\n  " + "\n  ".join(errs))
        return 1

    _info(f"Directing task to {assignee}: {task['id']}")
    _info(f"  Title: {task['title']}")
    _info(f"  From:  {caller}")
    cache.write_cached_task(task)

    try:
        ok = _write_task_and_views(task, backend=backend, command="tell")
    except schema.ConflictError as e:
        _err(str(e))
        return 2
    except schema.NeedsReconcile as e:
        _warn(str(e))
        return 0

    if ok:
        _info(f"\nDirective created: {task['id']} -> {assignee}")
        return 0
    _warn(f"Directive cached locally but remote upload failed: {task['id']}.")
    return 1


def cmd_broadcast(args: Any, backend: Optional[list[str]] = None) -> int:
    """Create a directive addressed at ALL agents (sugar over `tell` with the
    wildcard assignee).

    A broadcast is a `proposed` task whose assignee is the BROADCAST sentinel
    (``*``), owned by the directing agent (--from / resolved identity). Because
    views.agent_matches treats ``*`` as matching every agent, it lands in every
    agent's inbox; because acks are per-`by`, each agent acknowledges it
    independently (one agent's inbox_ack never clears it for the others). This is
    the durable "tell every agent X" primitive — e.g. "update fulcra-coord when
    main changes." Use `tell` for a single agent; `broadcast` for all.

    Implemented by setting assignee="*" and delegating to cmd_tell so the two
    share one creation/validation/upload path (no divergence to maintain).
    """
    args.assignee = views.BROADCAST
    return cmd_tell(args, backend=backend)


def cmd_assign(args: Any, backend: Optional[list[str]] = None) -> int:
    """Set or redirect the assignee on an existing task."""
    task_id = args.task_id
    assignee = args.assignee
    agent = getattr(args, "agent", None) or _derive_agent()

    task = _load_task(task_id, backend=backend)
    if task is None:
        _err(f"Task not found: {task_id}")
        return 1

    # Assignment is a field edit, not a status change. Route through apply_update
    # so it carries an event + bumps updated_at/last_touched_by; set the field
    # on the returned copy so the event log records the reassignment too.
    task = schema.apply_update(
        task, by=agent,
        summary=f"Assigned to {assignee} by {agent}.",
    )
    task["assignee"] = assignee
    # BUG 3: a `block --on-user` task carries a ``needs:human`` tag so it shows on
    # the human's plate (views.needs_human counts that tag, not just the assignee).
    # When it is REASSIGNED to a non-human agent, the assignee changes but the
    # stale tag persisted — so the human kept seeing it as "blocked on you" forever.
    # Strip the tag whenever we reassign AWAY from the human; keep it when the new
    # assignee IS the human (a no-op / re-park must not drop the marker). Resolve
    # the human handle the same way cmd_block / needs-me do (identity.resolve_human).
    if not views.agent_matches(identity.resolve_human(), assignee):
        task["tags"] = [t for t in task.get("tags", []) if t != "needs:human"]
    cache.write_cached_task(task)

    ok = False
    try:
        ok = _write_task_and_views(task, backend=backend, command="assign")
    except schema.ConflictError as e:
        _err(str(e))
        return 2
    except schema.NeedsReconcile as e:
        _warn(str(e))
        ok = True

    if not ok:
        _warn(f"Task cached locally but remote upload failed: {task_id}.")
        return 1
    _info(f"Assigned {task_id} -> {assignee}")
    return 0


def cmd_inbox(args: Any, backend: Optional[list[str]] = None) -> int:
    """List (or ack) open directives addressed to the calling agent.

    Read path recomputes authoritatively from the full task set (see
    _load_inbox) — mirroring cmd_agents — rather than trusting a materialized
    inbox view, which can go stale once an inbox empties. `--ack <id>` records an
    inbox_ack event so the listener stops re-notifying, without claiming the task.
    """
    me = getattr(args, "agent", None) or _derive_agent()
    out_format = getattr(args, "format", "table")
    ack_id = getattr(args, "ack", None)
    show_all = bool(getattr(args, "all", False))

    if ack_id:
        task = _load_task(ack_id, backend=backend)
        if task is None:
            _err(f"Task not found: {ack_id}")
            return 1
        task = schema.apply_event(task, "inbox_ack", by=me,
                                  summary=f"Inbox acknowledged by {me}.")
        cache.write_cached_task(task)
        ok = False
        try:
            ok = _write_task_and_views(task, backend=backend, command="inbox-ack")
        except schema.ConflictError as e:
            _err(str(e))
            return 2
        except schema.NeedsReconcile as e:
            _warn(str(e))
            ok = True
        if not ok:
            _warn(f"Ack cached locally but remote upload failed: {ack_id}.")
            return 1
        _info(f"Acknowledged: {ack_id}")
        return 0

    # Load the task set ONCE, then derive both the shown items and the aged-out
    # count from it — no second backend round-trip. With --all the age-out filter
    # is bypassed and aged-out broadcasts are included; otherwise stale
    # informational broadcasts are hidden and only counted for the note below.
    all_tasks = _load_task_summaries(backend=backend)
    # BUG 14: pin a single `now` for the whole command. inbox_for and
    # aged_out_inbox_count each resolve _now() independently (3+ reads per
    # cmd_inbox), so at the age-out boundary the same broadcast could be SHOWN by
    # one read and COUNTED HIDDEN by a later one. One timestamp keeps them
    # consistent — an id is either shown or counted hidden, never both.
    now = views._now()
    items = views.inbox_for(me, all_tasks, now=now, include_aged=show_all)
    hidden = 0 if show_all else views.aged_out_inbox_count(me, all_tasks, now=now)

    if out_format == "json":
        _print_json({"agent": me, "count": len(items), "hidden_aged": hidden,
                     "inbox": items})
        return 0

    if not items:
        if hidden:
            _info(f"Inbox empty for {me} "
                  f"({hidden} older broadcast{'s' if hidden != 1 else ''} "
                  f"hidden — --all to show).")
        else:
            _info(f"Inbox empty for {me}.")
        return 0

    print(f"\n{'='*60}")
    print(f"  Inbox — directives for {me}")
    print(f"{'='*60}")
    for s in items:
        frm = s.get("owner_agent", "?")
        print(f"  [{s.get('priority','??')}] {s.get('id','')}  {s.get('title','')[:50]}")
        print(f"        from: {frm}")
        if s.get("next_action"):
            print(f"        next: {s['next_action'][:70]}")
    if hidden:
        print(f"\n  ({hidden} older broadcast{'s' if hidden != 1 else ''} "
              f"hidden — --all to show)")
    print()
    return 0


def _load_inbox(me: str, backend: Optional[list[str]] = None,
                include_aged: bool = False) -> list[dict[str, Any]]:
    """Open directives for `me`, recomputed authoritatively from the full task set.

    Mirrors cmd_agents: inbox_for over the live tasks is the single source of
    truth. We deliberately do NOT prefer a materialized inbox/<slug> view here.

    Membership uses prefix-aware matching (views.inbox_for / agent_matches): a
    directive addressed to a short id like `claude-code` reaches the full-id
    agent `claude-code:<host>:<repo>` it prefixes. This is the correctness fix
    for the original bug — strict slug equality silently dropped short-id
    directives.

    Why recompute (not read a materialized view): build_all_views only emits an
    inbox/<slug> view for assignees who still have at least one open directive.
    When an inbox empties — the last directive is acked or claimed — the stale
    inbox/<slug>.json (local cache AND remote) is never overwritten, so preferring
    it returned a phantom directive forever (`inbox` re-listed handled work, the
    listener re-notified, SessionStart re-injected). Recomputing from the task set
    always reflects the current truth, at the cost of one task-set load — the same
    cost cmd_agents pays.
    """
    # Summaries fast-path: inbox_for reads assignee/status/owner_agent and the
    # ack set, which the summary now carries (acked_by) — no event log / body
    # fetch needed. Falls back to a full load on an older bus.
    all_tasks = _load_task_summaries(backend=backend)
    # include_aged bypasses the broadcast age-out filter (the `inbox --all` path);
    # the default read hides stale informational broadcasts so they stop
    # cluttering the inbox / SessionStart, without touching any task.
    return views.inbox_for(me, all_tasks, include_aged=include_aged)


# ---------------------------------------------------------------------------
# Presence (workstream-on-connect) — best-effort, never raises into a task op
# ---------------------------------------------------------------------------

def _derive_workstreams_from_open_tasks(
    me: str, backend: Optional[list[str]] = None) -> list[str]:
    """The distinct ``workstream`` of this agent's OPEN tasks (proposed/active/
    waiting/blocked) that it OWNS.

    Read via the summaries fast-path (one download), so deriving presence
    workstreams costs the same single round-trip the other read commands pay —
    no per-task body fetch. Best-effort: any failure yields an empty list rather
    than raising into the connect path."""
    open_statuses = ("proposed", "active", "waiting", "blocked")
    try:
        summaries = _load_task_summaries(backend=backend)
    except Exception:
        return []
    return sorted({
        t.get("workstream") for t in summaries
        if t.get("owner_agent") == me
        and t.get("status") in open_statuses
        and t.get("workstream")
    })


def _upsert_presence_aggregate(
    record: dict[str, Any], backend: Optional[list[str]] = None) -> None:
    """Opportunistically merge this agent's presence record into the aggregate
    roster (``views/presence.json``) so ``presence`` / ``agents`` see it
    immediately, without waiting for a reconcile.

    BUG 4 (S2-class self-heal): the per-agent ``presence/<slug>.json`` files are
    the durable, un-clobberable truth — each owned by one agent. ``_write_presence``
    has already uploaded THIS agent's durable file before calling here, so we
    rebuild the aggregate by LISTING ``presence/*.json`` (the same authoritative
    enumeration ``_reconcile_presence`` uses) and upserting self on top. This
    recovers any peer that a concurrent last-writer-wins upload dropped from the
    aggregate — on the very next connect, instead of leaving it invisible until a
    90s reconcile (the task views already self-heal this way).

    FALLBACK: if the listing fails or is empty (a backend without a working
    ``list``), fall back to the old download-aggregate + upsert-self path so we
    never regress below the prior single-file behaviour. Whole thing is
    BEST-EFFORT: the durable per-agent record is already written and reconcile is
    the eventual-consistency backstop, so a transient aggregate write must never
    surface as an error to connect."""
    def _without_liveness(a: dict[str, Any]) -> dict[str, Any]:
        # build_presence re-derives liveness; strip any stale annotation so the
        # rebuilt entry carries a fresh one alongside the others.
        return {k: v for k, v in a.items() if k != "liveness"}

    try:
        records: list[dict[str, Any]] = []
        try:
            prefix = f"{remote.remote_root()}/presence/"
            for path in remote.list_files(prefix, backend=backend):
                if not path.endswith(".json"):
                    continue
                rec = remote.download_json(path, backend=backend)
                if rec and rec.get("agent") and rec.get("agent") != record["agent"]:
                    records.append(_without_liveness(rec))
        except Exception:
            records = []  # listing best-effort; fall through to the download path

        if not records:
            # Fallback: no usable listing → recover peers from the current
            # aggregate instead, so behaviour never regresses below pre-BUG-4.
            agg = remote.download_json(remote.presence_view_path(), backend=backend)
            existing = (agg or {}).get("agents", []) if agg else []
            records = [
                _without_liveness(a)
                for a in existing if a.get("agent") != record["agent"]
            ]

        records.append(record)
        view = views.build_presence(records)
        remote.upload_json(view, remote.presence_view_path(), backend=backend)
        cache.write_cached_view("presence", view)
    except Exception:
        pass  # aggregate is opportunistic; reconcile heals it


def _write_presence(
    record: dict[str, Any], backend: Optional[list[str]] = None) -> bool:
    """Write a presence record to its per-agent file + upsert the aggregate.

    Returns True when the durable per-agent record uploaded. The aggregate upsert
    is best-effort on top. Whole thing is guarded so a presence write can never
    raise into a caller (mirrors _stamp_session_pointer's contract)."""
    try:
        slug = views.agent_slug(record["agent"])
        ok = remote.upload_json(
            record, remote.presence_remote_path(slug), backend=backend)
        _upsert_presence_aggregate(record, backend=backend)
        return ok
    except Exception:
        return False


def _load_own_presence(
    me: str, backend: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
    """Download this agent's own presence record (``presence/<slug>.json``), or
    None if it has never connected. Used by `workstream` to mutate the existing
    record rather than clobber declared streams/summary."""
    slug = views.agent_slug(me)
    return remote.download_json(remote.presence_remote_path(slug), backend=backend)


def cmd_connect(args: Any, backend: Optional[list[str]] = None) -> int:
    """Record this agent's presence on connect (workstream-on-connect).

    The SessionStart/Codex hooks call this so the human sees what each agent is
    working on even when it owns no active task — the north star. Workstreams are
    the UNION of explicit ``--workstream`` values and the distinct ``workstream``
    of this agent's open tasks, so the common case needs no extra typing. Writes
    the durable per-agent record and opportunistically refreshes the aggregate.
    Best-effort: a presence write never fails the session boot."""
    explicit_agent = getattr(args, "agent", None)
    me = identity.resolve_agent(explicit_agent)
    out_format = getattr(args, "format", "table")
    summary = getattr(args, "summary", "") or ""

    # Non-blocking onboarding nudge (Task C): a derived identity + a lingering
    # legacy global identity.json means the operator's old declared id is being
    # silently ignored here. Hint to migrate. STDERR so the backgrounded connect
    # hook discards it (it only matters in an interactive run).
    _maybe_warn_legacy_identity(explicit_agent)

    explicit = _split_workstreams(getattr(args, "workstream", None))
    derived = _derive_workstreams_from_open_tasks(me, backend=backend)
    workstreams = sorted(set(explicit) | set(derived))

    # Declared capabilities (Task 2): --can-review is sugar for --role review.
    # These drive liveness-aware reviewer routing's candidate pool. Undeclared
    # agents stay [] (backward compatible).
    roles = list(getattr(args, "role", None) or [])
    if getattr(args, "can_review", False):
        roles.append("review")
    record = schema.make_presence(me, workstreams=workstreams, summary=summary,
                                  capabilities=roles or None,
                                  session=os.environ.get("FULCRA_COORD_SESSION") or None)
    _write_presence(record, backend=backend)

    if out_format == "json":
        _print_json(record)
        return 0
    ws = ", ".join(record["workstreams"]) or "(none)"
    _info(f"Connected: {me} — workstreams: {ws}")
    if summary:
        _info(f"  on: {summary}")
    return 0


def _split_workstreams(raw: Optional[str]) -> list[str]:
    """Split a comma-separated ``--workstream`` value into a clean list. Empty
    tokens (e.g. a trailing comma) are dropped; make_presence normalizes the rest."""
    if not raw:
        return []
    return [w.strip() for w in raw.split(",") if w.strip()]


def cmd_workstream(args: Any, backend: Optional[list[str]] = None) -> int:
    """Declare/update THIS agent's presence workstreams (manual path).

    Subcommands mutate the agent's own presence record:
      * ``set <ws>[,…]`` — REPLACE the workstream list.
      * ``add <ws>``     — APPEND to the existing list.
      * ``clear``        — empty the list.
    A bare ``workstream`` (no subcommand) just SHOWS the current presence. A
    ``--summary`` updates the one-line "what I'm on" on any mutating action.
    Reads the agent's own ``presence/<slug>.json``, mutates, and rewrites +
    upserts the aggregate (same writer as connect → no contention)."""
    me = identity.resolve_agent(getattr(args, "agent", None))
    out_format = getattr(args, "format", "table")
    action = getattr(args, "ws_action", None)
    summary_arg = getattr(args, "summary", None)

    current = _load_own_presence(me, backend=backend)
    cur_workstreams = list((current or {}).get("workstreams", []))
    cur_summary = (current or {}).get("summary", "")

    if action is None:
        # Show current presence (no mutation).
        rec = current or schema.make_presence(me, workstreams=[], summary="")
        if out_format == "json":
            _print_json(rec)
            return 0
        ws = ", ".join(rec.get("workstreams", [])) or "(none)"
        _info(f"{me} — workstreams: {ws}")
        if rec.get("summary"):
            _info(f"  on: {rec['summary']}")
        return 0

    if action == "set":
        new_workstreams = _split_workstreams(getattr(args, "workstreams", None))
    elif action == "add":
        new_workstreams = cur_workstreams + _split_workstreams(
            getattr(args, "workstreams", None))
    elif action == "clear":
        new_workstreams = []
    else:
        _err(f"Unknown workstream action: {action}")
        return 1

    new_summary = summary_arg if summary_arg is not None else cur_summary
    record = schema.make_presence(me, workstreams=new_workstreams,
                                  summary=new_summary,
                                  session=(current or {}).get("session"))
    _write_presence(record, backend=backend)

    if out_format == "json":
        _print_json(record)
        return 0
    ws = ", ".join(record["workstreams"]) or "(none)"
    _info(f"Workstreams for {me}: {ws}")
    return 0


def cmd_presence(args: Any, backend: Optional[list[str]] = None) -> int:
    """Show the agent presence roster — who is working on what, right now.

    Reads the aggregate ``views/presence.json`` (one download) and renders, per
    agent: workstreams · summary · last-seen age · liveness. This is the surface
    that answers "what is every agent on" even for agents with no active task.
    Empty/missing roster → a clear "nothing recorded yet" message."""
    out_format = getattr(args, "format", "table")
    agg = remote.download_json(remote.presence_view_path(), backend=backend)
    # Re-derive liveness at read time so the age reflects NOW, not the moment the
    # aggregate was last written (the stored liveness can have drifted to stale).
    records = [
        {k: v for k, v in a.items() if k != "liveness"}
        for a in (agg or {}).get("agents", [])
    ] if agg else []
    view = views.build_presence(records)

    if out_format == "json":
        _print_json(view)
        return 0

    if not view["agents"]:
        _info("No agent presence recorded yet.")
        return 0

    print(f"\n{'='*60}")
    print("  Fulcra Coordination — Presence")
    print(f"{'='*60}")
    for a in view["agents"]:
        ws = ", ".join(a.get("workstreams", [])) or "(none)"
        age = _age_str(a.get("last_seen", ""))
        print(f"\n  {a['agent']}  [{a['liveness']}]  (seen {age})")
        print(f"    workstreams: {ws}")
        if a.get("summary"):
            print(f"    on: {a['summary'][:80]}")
    print()
    return 0


def _inbox_surface_path(agent: str):
    """Where the listener drops pending directives for the next SessionStart to
    read. Root-scoped via cache_root() and suffixed by the agent slug so two
    agents on one machine don't clobber each other's surface file."""
    return cache.cache_root() / f"inbox-pending-{listener.agent_slug(agent)}.json"


def _needs_me_seen_path(human: str):
    """Seen-set surface for blocked-on-you notifications, keyed by the HUMAN
    handle (not the polling agent): the "has the operator already been alerted
    about this item" marker. Like the inbox-pending surface but a set of task
    ids, so the listener notifies ONCE per new needs-me item and never re-fires
    for one it already announced. Slugged via the same agent_slug so a handle
    with odd characters maps to a safe filename."""
    return cache.cache_root() / f"needs-me-seen-{listener.agent_slug(human)}.json"


def _notify_new_needs_me(backend: Optional[list[str]] = None) -> None:
    """Fire a desktop notification for each NEW item blocked on the human.

    Polled alongside the inbox by the listener (Part 5). Resolves the human via
    resolve_human(), loads what's blocked on them, and for every item not yet in
    the per-human seen-set emits "⛔ <agent> needs you: <ask>" once. Idempotent:
    the seen-set (a task-id list persisted next to the inbox surface) means a
    repeat tick over the same item does not re-notify, while a genuinely new
    blocked-on-you item alerts. Best-effort — wrapped by the caller's try/except
    so it can never crash a polling tick. No-op when nothing is blocked."""
    human = identity.resolve_human()
    # needs_human reads status/assignee/tags — all on a summary; no body fetch.
    items = views.needs_human(_load_task_summaries(backend=backend), human)
    seen_path = _needs_me_seen_path(human)
    seen: set[str] = set()
    if seen_path.exists():
        try:
            seen = set(json.loads(seen_path.read_text()))
        except (json.JSONDecodeError, OSError, TypeError):
            seen = set()

    current_ids = {i["id"] for i in items}
    for it in items:
        if it["id"] in seen:
            continue
        ask = (it.get("blocked_on") or it.get("next_action") or "").strip()
        frm = it.get("owner_agent", "?")
        listener.emit_message(f"⛔ {frm} needs you: {ask}" if ask
                              else f"⛔ {frm} needs you: {it.get('title','')}")

    # Persist the seen-set as the CURRENT item ids: newly-notified items are now
    # seen, and items that have since cleared (resolved) drop out so that if the
    # SAME task is blocked-on-you again later it re-notifies (a fresh ask).
    cache.cache_root().mkdir(parents=True, exist_ok=True)
    seen_path.write_text(json.dumps(sorted(current_ids)))


def cmd_notify_inbox(args: Any, backend: Optional[list[str]] = None) -> int:
    """Poll the inbox for an agent; on non-empty, surface + notify (Part 3).

    The single call the scheduled listener (launchd/cron/heartbeat/scheduled
    remote agent) runs each tick. Notify-only: it writes the open directives to
    a local surface file the next SessionStart injects AND emits a best-effort
    desktop notification. No-op (no notification; surface file cleared to an
    empty inbox so a stale one doesn't linger) when the inbox is empty.
    Fail-safe — never raises out; a polling tick must not crash the scheduler.
    """
    me = getattr(args, "agent", None) or _derive_agent()
    try:
        items = _load_inbox(me, backend=backend)
        surface = _inbox_surface_path(me)
        cache.cache_root().mkdir(parents=True, exist_ok=True)
        payload = {"agent": me, "count": len(items), "inbox": items}
        surface.write_text(json.dumps(payload, indent=2))
        if items:
            listener.emit_notification(me, len(items))
        # ALSO notice anything newly blocked on the human (Part 5). Independent
        # of the agent's own inbox: a tick with an empty inbox can still alert on
        # a new blocked-on-you item. Best-effort within the same fail-safe guard.
        _notify_new_needs_me(backend=backend)
    except Exception as e:
        # A polling tick that fails must not bring down the scheduler; report to
        # stderr and exit clean (fail-safe contract).
        _warn(f"notify-inbox failed (non-fatal): {e}")
        return 0
    return 0


def _age_str(updated_at: str) -> str:
    """Human-legible age of a timestamp, e.g. "3h" / "2d" / "12m" / "just now".

    Used by needs-me / resume to show "how long it's been" — the third thing the
    human wants at a glance (who, what, how long). Best-effort: an unparseable
    timestamp renders "?" rather than crashing the read-only view."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "?"
    # BUG 6a: a tz-less stored timestamp parses NAIVE, and subtracting it from the
    # AWARE now raised TypeError (not caught above) — crashing a read-only view.
    # Coerce a naive parse to UTC, matching views._parse_dt, so any stored shape
    # yields a sane age instead of a crash.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def _until_str(when: str) -> str:
    """Time-until-actionable, e.g. "in 4d" / "in 18h" / "now". Best-effort:
    an unparseable/empty value renders "soon" so the upcoming line never breaks
    on a bad not_before."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "soon"
    secs = (dt - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return "now"
    if secs < 3600:
        return f"in {int(secs // 60)}m"
    if secs < 86400:
        return f"in {int(secs // 3600)}h"
    return f"in {int(secs // 86400)}d"


def _due_str(due: str) -> str:
    """A compact calendar date for a deadline, e.g. "Jun 8". Empty/unparseable
    -> "" so the caller can drop the "(due ...)" clause entirely."""
    from datetime import datetime
    if not due:
        return ""
    try:
        dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    return f"{dt:%b} {dt.day}"


#: Max items rendered per digest block before collapsing the tail into "+N more".
#: Keeps the timeline note bounded (a 284-event-in-two-days bus could otherwise
#: produce a wall of text) while always showing the most-salient head of each list.
_DIGEST_BLOCK_CAP = 8


def _digest_lines(items: list[dict[str, Any]], fmt) -> list[str]:
    """Render up to _DIGEST_BLOCK_CAP items via ``fmt`` (item -> str), appending a
    '+N more' tail when the list is longer. Bounds every block identically."""
    head = items[:_DIGEST_BLOCK_CAP]
    lines = [fmt(s) for s in head]
    extra = len(items) - len(head)
    if extra > 0:
        lines.append(f"  …and {extra} more")
    return lines


def _render_digest(digest: dict[str, Any], *, window: str) -> tuple[str, str]:
    """Render the structured digest into a timeline (name, note). Pure, no I/O.

    ``name`` is the concise timeline label carrying the headline counts
    (``Agent digest — <window> (N on you, M upcoming)``); ``note`` is the body —
    compact markdown-ish text, one block per non-empty section, each line who /
    what / when. Empty blocks are SKIPPED entirely (no empty headers). Long lists
    are capped via ``_digest_lines`` ('+N more'). Every field is read with
    ``.get`` defaults so a summary missing an optional key renders instead of
    raising — this feeds a best-effort scheduled writer that must never crash."""
    blocked = digest.get("blocked_on_you") or []
    upcoming = digest.get("upcoming") or []
    per_agent = digest.get("per_agent") or []
    stale = digest.get("stale") or []

    name = (f"Agent digest — {window} "
            f"({len(blocked)} on you, {len(upcoming)} upcoming)")

    sections: list[str] = []

    if blocked:
        def _b(s):
            ask = (s.get("blocked_on") or s.get("next_action") or "").strip()
            who = s.get("owner_agent", "?")
            tail = f" — {ask}" if ask else ""
            return (f"  • [{(s.get('status') or '?').upper()}] "
                    f"{(s.get('title') or '')[:60]} (from {who}){tail}")
        sections.append("⛔ Blocked on you (" + str(len(blocked)) + "):")
        sections.extend(_digest_lines(blocked, _b))

    if upcoming:
        def _u(s):
            when = (s.get("not_before") or "").strip()
            return f"  • {(s.get('title') or '')[:60]}" + (f" (not before {when})" if when else "")
        sections.append("")
        sections.append("Upcoming (next 7d) (" + str(len(upcoming)) + "):")
        sections.extend(_digest_lines(upcoming, _u))

    if per_agent:
        sections.append("")
        sections.append("Per agent:")
        for a in per_agent:
            ws = ", ".join(a.get("workstreams", [])) or "(none)"
            sections.append(f"  {a.get('agent', '?')} [{a.get('liveness', '?')}] — {ws}")
            if a.get("summary"):
                sections.append(f"    on: {a['summary'][:80]}")
            done = a.get("finished_since") or []
            for s in done[:_DIGEST_BLOCK_CAP]:
                sections.append(f"    ✓ {(s.get('title') or '')[:60]}")
            if len(done) > _DIGEST_BLOCK_CAP:
                sections.append(f"    …and {len(done) - _DIGEST_BLOCK_CAP} more done")

    if stale:
        def _s(s):
            return f"  • {(s.get('title') or '')[:60]} (from {s.get('owner_agent', '?')})"
        sections.append("")
        sections.append("Stale (no update past threshold) (" + str(len(stale)) + "):")
        sections.extend(_digest_lines(stale, _s))

    note = "\n".join(sections).strip()
    return name, note


def _digest_window_since(window: str, now: datetime) -> datetime:
    """The lookback boundary for a digest window (returns a tz-aware UTC datetime).

    morning → since the previous evening (~last 14h, so an overnight run still
    reports yesterday-evening's work); evening → since this morning (~last 10h);
    any other value (on-demand) → last 12h. Approximations on purpose: the digest
    is a human-paced glance, not an exact ledger, and the per_agent completion
    filter is a >= compare against this instant. Always parsed datetimes."""
    hours = {"morning": 14, "evening": 10}.get(window, 12)
    return now - timedelta(hours=hours)


def _digest_marker_path(window: str, now: datetime) -> str:
    """Files-bus path of the per-window digest dedup marker:
    ``<remote_root>/digest/markers/<YYYY-MM-DD>-<window>.json``. Keyed by the UTC
    DATE + window so morning and evening each get one marker per day, and any
    agent on any machine claims the SAME path (the whole point of the any-agent
    guard). ``now`` is injected for deterministic tests."""
    from . import remote_root
    day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
    return f"{remote_root()}/digest/markers/{day}-{window}.json"


def _claim_digest_marker(window: str, now: datetime, *,
                         backend: Optional[list[str]] = None) -> bool:
    """Any-agent first-writer-wins claim for one window's digest. Returns True
    when THIS caller won the claim and should write the digest; False to skip.

    Protocol (spec §5): download the marker — if it exists, another agent already
    wrote this window, so NO-OP (return False). If absent, upload a marker
    stamping this agent + timestamp; on a successful upload, grant the claim.

    RACE (accepted): Fulcra Files has no compare-and-swap, so two agents firing
    in the same ~second can both see 'absent' and both write → a rare double
    digest. Harmless on a timeline; logged, not prevented (a single-owner schedule
    would remove the race but add a single point of failure — rejected per the
    any-agent decision). MARKER-CLAIM FAILURE (download or upload error) → return
    False (skip) so a transient bus error never risks a double; the next window
    retries. Never raises — best-effort like the rest of the digest path."""
    try:
        path = _digest_marker_path(window, now)
        existing = remote.download_json(path, backend=backend)
        if existing is not None:
            return False  # already claimed this window
        marker = {
            "schema": "fulcra.coordination.digest_marker.v1",
            "window": window,
            "date": now.astimezone(timezone.utc).strftime("%Y-%m-%d"),
            "by": identity.resolve_agent(),
            "claimed_at": now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        }
        return bool(remote.upload_json(marker, path, backend=backend))
    except Exception:
        # Best-effort: a marker error must never raise into a scheduled tick, and
        # must skip (not write) so we never risk a double on an uncertain claim.
        return False


def cmd_digest(args: Any, backend: Optional[list[str]] = None) -> int:
    """Write the operator's situational-awareness digest to the Fulcra timeline.

    Loads the compact summaries aggregate + the presence roster (the same reads
    needs-me / presence use — one download each, no body fetch), computes the
    window's ``since``/``now``, builds the four-block digest, and renders it to a
    timeline (name, note). ``--dry-run`` prints the rendered text and writes
    NOTHING. ``--format json`` prints the structured digest (for tooling/tests).
    Otherwise it claims the per-window dedup marker (first writer wins; others
    no-op) and emits the moment on the ``Agent Tasks — Digest`` track.

    BEST-EFFORT end to end: a failed marker claim or a failed emit is logged and
    returns 0 — a scheduled tick must never error out."""
    window = getattr(args, "window", None) or "ondemand"
    out_format = getattr(args, "format", "table")
    dry_run = getattr(args, "dry_run", False)
    human = getattr(args, "human", None) or identity.resolve_human()

    now = datetime.now(timezone.utc)
    since = _digest_window_since(window, now)

    summaries = _load_task_summaries(backend=backend)
    agg = remote.download_json(remote.presence_view_path(), backend=backend)
    presence = (agg or {}).get("agents", []) if agg else []

    digest = views.build_operator_digest(
        summaries, presence, human=human, now=now, since=since)

    if out_format == "json":
        _print_json(digest)
        return 0

    name, note = _render_digest(digest, window=window)

    if dry_run:
        _info(f"[dry-run] {name}")
        _info(note or "(nothing to report)")
        return 0

    # Any-agent dedup: claim the per-window marker first; if another agent
    # already wrote this window (or the claim errored), skip — never risk a
    # double, and never raise into a scheduled tick.
    if not _claim_digest_marker(window, now, backend=backend):
        _info(f"Digest for {window} already written (or marker claim failed) — skipping.")
        return 0

    wrote = False
    try:
        wrote = lifecycle_annotations.emit_digest_annotation(
            name=name, note=note, window=window,
            agent=identity.resolve_agent(), backend=backend)
    except Exception:
        wrote = False
    _info(f"Digest ({window}): {'written' if wrote else 'not written (annotations off or error)'}.")
    return 0


def cmd_install_digest(args: Any, backend: Optional[list[str]] = None) -> int:
    """Install/uninstall the twice-daily scheduled ``fulcra-coord digest`` jobs.

    Calendar-scheduled (08:00 morning + 18:00 evening), unlike the interval
    heartbeat/listener: launchd StartCalendarInterval on macOS, fixed cron lines
    elsewhere. Installable on every machine — the any-agent dedup marker collapses
    concurrent ticks to one digest per window. Mirrors install-heartbeat's CLI
    contract (dry-run prints the plan, surgical uninstall)."""
    plan = digest_schedule.install_digest(
        uninstall=args.uninstall,
        dry_run=args.dry_run,
        target_dir=getattr(args, "target_dir", None),
        logs_dir=getattr(args, "logs_dir", None),
    )
    if args.dry_run:
        _info(f"[dry-run] Digest mechanism: {plan['mechanism']}")
        _info(f"[dry-run] Scheduled command: {plan['cli_command']} digest "
              f"--window {{morning@08:00, evening@18:00}}")
        for w in plan.get("writes", []):
            _info(f"  + would write {w}")
        for r in plan.get("removes", []):
            _info(f"  - would remove {r}")
        return 0
    if args.uninstall:
        _info(f"Removed fulcra-coord digest schedule ({plan['mechanism']}).")
        return 0
    _info(f"Installed fulcra-coord digest schedule ({plan['mechanism']}) — "
          f"morning 08:00 + evening 18:00.")
    for w in plan.get("writes", []):
        _info(f"  + {w}")
    if plan["mechanism"] == "launchd":
        for w in plan.get("writes", []):
            _info(f"Load it now (or at next login): launchctl load -w {w}")
    else:
        _info("Apply it now: crontab " + (plan["writes"][0] if plan["writes"] else ""))
    return 0


def cmd_needs_me(args: Any, backend: Optional[list[str]] = None) -> int:
    """THE "what's blocked on ME" view (situational awareness piece 3).

    Lists every OPEN task (proposed/waiting/blocked) assigned to / blocked on the
    human, across all agents — showing WHO is waiting (owner_agent), WHAT they
    need (blocked_on / next_action), and HOW LONG it's been. This is the human's
    glance of "what's on my plate from my agents." Read-only.

    The human is resolved via ``--human`` > ``resolve_human()`` (env > config >
    default ``human``); matching is prefix-aware so ``human`` and ``ash`` both
    work. ``--format json`` for tooling (the SessionStart banner + the listener).

    SCHEDULING: the DUE-NOW section (``items``) lists only asks actionable now;
    asks with a FUTURE ``not_before`` are split into a compact ``upcoming``
    section so a task the human can't act on yet (e.g. a re-auth that opens next
    week) doesn't clutter the plate. JSON returns
    ``{human, count, items, upcoming}`` — ``count`` reflects DUE-NOW only.
    """
    human = getattr(args, "human", None) or identity.resolve_human()
    out_format = getattr(args, "format", "table")
    show_all = getattr(args, "all", False)

    # needs_human / upcoming_for_human read status/assignee/tags/not_before/due —
    # all on a summary; no body fetch. now=None -> wall-clock.
    all_tasks = _load_task_summaries(backend=backend)
    items = views.needs_human(all_tasks, human)
    upcoming = views.upcoming_for_human(all_tasks, human)

    if out_format == "json":
        _print_json({"human": human, "count": len(items), "items": items,
                     "upcoming": upcoming})
        return 0

    if not items and not upcoming:
        _info(f"Nothing blocked on you ({human}).")
        return 0

    if items:
        print(f"\n{'='*60}")
        print(f"  ⛔ BLOCKED ON YOU ({len(items)}) — {human}")
        print(f"{'='*60}")
        for s in items:
            ask = (s.get("blocked_on") or s.get("next_action") or "").strip()
            frm = s.get("owner_agent", "?")
            age = _age_str(s.get("updated_at", ""))
            print(f"  [{s.get('status','?').upper()}] {s.get('id','')}  "
                  f"{s.get('title','')[:50]}  ({age})")
            print(f"        from: {frm}")
            if ask:
                print(f"        needs: {ask[:80]}")
        print()

    # Upcoming: future-not_before asks within the window. Compact by default
    # (just a count line) so it never competes with the DUE-NOW plate; --all
    # expands each item inline ("[in 4d] <title> — <ask> (due Jun 8)").
    if upcoming:
        print(f"  Upcoming (next 7d): {len(upcoming)}")
        if show_all or not items:
            for s in upcoming:
                when = _until_str(s.get("not_before") or "")
                ask = (s.get("blocked_on") or s.get("next_action") or "").strip()
                due = _due_str(s.get("due") or "")
                due_clause = f" (due {due})" if due else ""
                ask_clause = f" — {ask[:60]}" if ask else ""
                print(f"    [{when}] {s.get('title','')[:50]}{ask_clause}{due_clause}")
        print()
    return 0


def cmd_resume(args: Any, backend: Optional[list[str]] = None) -> int:
    """Pick-up-where-you-left-off briefing for an agent (situational awareness
    piece 7). Read-only.

    Four sections, all built from the live task set so a fresh session (or the
    operator after a reboot) can reload context in one call:

      (a) active   — your active/waiting tasks + next_action (what you were doing)
      (b) blocked_on_me   — open tasks assigned to you but owned by someone else
                            (directives + things parked on you)
      (c) owed_to_others  — open tasks you own/created that are assigned to
                            someone ELSE (work you directed and still owe a result
                            or a nudge on)
      (d) blocked_on_human — what's blocked on the operator (needs-me), so an
                            agent acting for the user sees the human's plate too

    The agent is resolved via ``--agent`` > the normal identity resolution.
    ``--format json`` for tooling.
    """
    me = identity.resolve_agent(getattr(args, "agent", None))
    human = identity.resolve_human()
    out_format = getattr(args, "format", "table")

    # Summaries fast-path: resume reads owner_agent/status/assignee and re-wraps
    # entries with task_summary (now idempotent, so summarizing a summary is a
    # no-op). No task body is needed; falls back to a full load on an older bus.
    all_tasks = _load_task_summaries(backend=backend)
    open_statuses = ("proposed", "active", "waiting", "blocked")

    active = [
        schema.task_summary(t) for t in all_tasks
        if t.get("owner_agent") == me and t.get("status") in ("active", "waiting")
    ]
    # Broadcast exclusion (parity with views.needs_human): a broadcast ("*")
    # reaches every agent's inbox, but an all-agent announcement is ambient
    # context, not work PARKED on me. Including it floods the resume briefing
    # with join-announcement noise. "Blocked on me" = directives addressed to
    # me CONCRETELY (or via my id prefix); broadcasts stay visible via `inbox`.
    blocked_on_me = [
        schema.task_summary(t) for t in all_tasks
        if t.get("assignee") and t.get("assignee") != views.BROADCAST
        and views.agent_matches(me, t.get("assignee"))
        and t.get("owner_agent") != me
        and t.get("status") in ("proposed", "waiting", "blocked")
    ]
    blocked_on_human = views.needs_human(all_tasks, human)
    # M-2: a task I own that is assigned to the human is already surfaced under
    # "blocked on human"; exclude it from "owed to others" so a self-filed
    # on-user task is listed once, not double-counted across both sections.
    _on_human_ids = {s.get("id") for s in blocked_on_human}
    owed_to_others = [
        schema.task_summary(t) for t in all_tasks
        if t.get("owner_agent") == me
        and t.get("assignee") and not views.agent_matches(me, t.get("assignee"))
        and t.get("status") in open_statuses
        and t.get("id") not in _on_human_ids
    ]

    def _sort(items):
        return sorted(items, key=lambda x: (x.get("priority", "P9"),
                                            x.get("updated_at", "")))

    active = _sort(active)
    blocked_on_me = _sort(blocked_on_me)
    owed_to_others = _sort(owed_to_others)

    # Team state (presence): what OTHER agents are currently on, so an agent
    # resuming sees the room — including agents with no active task. One read of
    # the aggregate roster; best-effort (a missing roster yields an empty list,
    # so resume behaves exactly as before on an older bus).
    other_agents = []
    try:
        agg = remote.download_json(remote.presence_view_path(), backend=backend)
        if agg:
            roster = views.build_presence([
                {k: v for k, v in a.items() if k != "liveness"}
                for a in agg.get("agents", [])
            ])
            other_agents = [a for a in roster["agents"] if a.get("agent") != me]
    except Exception:
        other_agents = []

    if out_format == "json":
        _print_json({
            "agent": me,
            "human": human,
            "active": active,
            "blocked_on_me": blocked_on_me,
            "owed_to_others": owed_to_others,
            "blocked_on_human": blocked_on_human,
            "other_agents": other_agents,
        })
        return 0

    print(f"\n{'='*60}")
    print(f"  Resume briefing — {me}")
    print(f"{'='*60}")

    def _section(label, items, ask_field=None):
        print(f"\n  {label} ({len(items)})")
        for s in items:
            print(f"    [{s.get('status','?').upper()}] [{s.get('priority','??')}] "
                  f"{s.get('id','')}  {s.get('title','')[:50]}")
            na = (s.get("next_action") or "").strip()
            if na:
                print(f"          next: {na[:70]}")
            if ask_field:
                ask = (s.get("blocked_on") or "").strip()
                if ask:
                    print(f"          needs: {ask[:70]}")

    _section("Your active/waiting work", active)
    _section("Blocked on YOU", blocked_on_me, ask_field=True)
    _section("You owe others", owed_to_others)
    _section(f"Blocked on the human ({human})", blocked_on_human, ask_field=True)

    # Concise team-state footer so a resuming agent sees what the others are on.
    if other_agents:
        print(f"\n  Other agents (presence) ({len(other_agents)})")
        for a in other_agents:
            ws = ", ".join(a.get("workstreams", [])) or "(none)"
            age = _age_str(a.get("last_seen", ""))
            print(f"    {a['agent']}  [{a.get('liveness','')}] (seen {age}): {ws}")
    print()
    return 0


def cmd_start(args: Any, backend: Optional[list[str]] = None) -> int:
    """Create a new task and upload it."""
    title = args.title
    workstream = args.workstream
    # --agent is now OPTIONAL (parity with update/block/done/etc., which all
    # auto-resolve): fall back to the normal identity resolution when omitted so
    # `start` no longer uniquely requires it. --agent stays as an explicit override.
    explicit_agent = getattr(args, "agent", None)
    agent = identity.resolve_agent(explicit_agent)
    kind = getattr(args, "kind", "ops") or "ops"
    priority = getattr(args, "priority", "P2") or "P2"
    summary = getattr(args, "summary", "") or ""
    next_action = getattr(args, "next", "") or ""
    surface = getattr(args, "surface", None)

    # Non-blocking onboarding nudges (Task C). A title shaped like a task id is a
    # near-certain "I meant to claim an existing task" — warn but PROCEED (start
    # always creates a NEW task, by design). And if this session is running on a
    # derived identity while a legacy global identity.json lingers, point the
    # operator at migration. Both go to STDERR, one line each.
    if title and _TASK_ID_TITLE_RE.match(title):
        _warn("'start' always creates a NEW task. To claim/activate an existing "
              "one: fulcra-coord update <id> --status active")
    _maybe_warn_legacy_identity(explicit_agent)

    if workstream not in schema.SUGGESTED_WORKSTREAMS:
        _warn(f"Workstream {workstream!r} is not in the suggested set. Proceeding anyway.")

    try:
        task = schema.make_task(
            title=title,
            workstream=workstream,
            agent=agent,
            kind=kind,
            priority=priority,
            surface=surface,
            summary=summary,
            next_action=next_action,
        )
    except schema.SchemaError as e:
        _err(str(e))
        return 1

    errs = schema.validate_task(task)
    if errs:
        _err("Task schema errors:\n  " + "\n  ".join(errs))
        return 1

    _info(f"Creating task: {task['id']}")
    _info(f"  Title:      {task['title']}")
    _info(f"  Workstream: {task['workstream']}")
    _info(f"  Agent:      {task['owner_agent']}")
    _info(f"  Priority:   {task['priority']}")

    cache.write_cached_task(task)

    try:
        ok = _write_task_and_views(task, backend=backend, command="start")
    except schema.ConflictError as e:
        _err(str(e))
        return 2
    except schema.NeedsReconcile as e:
        _warn(str(e))
        _info(f"Task created (ID: {task['id']}) — views need repair.")
        return 0

    if ok:
        _info(f"\nTask created: {task['id']}")
        return 0

    _warn(
        f"Task cached locally but remote upload failed: {task['id']}. "
        "Run 'fulcra-coord reconcile' after Fulcra access recovers."
    )
    return 1


def cmd_update(args: Any, backend: Optional[list[str]] = None) -> int:
    """Update task summary / next_action, and optionally transition status via --status."""
    task_id = args.task_id
    summary = getattr(args, "summary", None)
    next_action = getattr(args, "next", None)
    blocked_on = getattr(args, "blocked_on", None)
    new_status = getattr(args, "status", None)

    task = _load_task(task_id, backend=backend)
    if task is None:
        _err(f"Task not found: {task_id}")
        return 1

    agent = getattr(args, "agent", None) or _derive_agent()

    # Capture the pre-transition status so we can tell a genuine pickup (a real
    # transition INTO active) from a progress note on an already-active task.
    # 'pickup' is a transition EVENT, not a resulting state, so an update that
    # merely re-asserts an already-active status is a plain 'update' (I2).
    prior_status = task.get("status")

    if new_status:
        try:
            task = schema.apply_transition(
                task,
                new_status,
                by=agent,
                summary=summary,
                next_action=next_action,
                blocked_on=blocked_on,
            )
        except (schema.TransitionError, schema.SchemaError) as e:
            _err(str(e))
            return 1
    else:
        task = schema.apply_update(
            task,
            by=agent,
            summary=summary,
            next_action=next_action,
            blocked_on=blocked_on,
        )

    cache.write_cached_task(task)

    # 'pickup' iff THIS call transitioned the task into active from a non-active
    # status; otherwise 'update'. Threaded explicitly because _write_task_and_views
    # only sees the final task state, which cannot distinguish the two.
    lifecycle = "pickup" if (new_status == "active" and prior_status != "active") else "update"

    ok = False
    try:
        ok = _write_task_and_views(task, backend=backend, command="update",
                                   lifecycle=lifecycle)
    except schema.ConflictError as e:
        _err(str(e))
        return 2
    except schema.NeedsReconcile as e:
        _warn(str(e))
        ok = True  # Task was written; only views need repair

    if not ok:
        _warn(
            f"Task cached locally but remote upload failed: {task_id}. "
            "Run 'fulcra-coord reconcile' after Fulcra access recovers."
        )
        return 1

    _info(f"Updated: {task_id}")
    return 0


def cmd_block(args: Any, backend: Optional[list[str]] = None) -> int:
    """Mark a task as blocked.

    Two flavours, mutually friendly:
      * ``--blocked-on "<reason>"`` — blocked on an agent / external thing (the
        original behaviour). No assignee change.
      * ``--on-user "<ask>"`` — blocked on the HUMAN (the situational-awareness
        path): sets blocked_on=<ask>, assignee=resolve_human(), and adds a
        ``needs:human`` tag, so it shows as blocked AND lands on the human's
        ``needs-me`` plate (and inbox). The ask answers "what you need me to do".

    If both are given, ``--on-user`` wins for the blocked_on text (it's the more
    specific human-facing ask); the human-assignment still applies.
    """
    task_id = args.task_id
    blocked_on = getattr(args, "blocked_on", None)
    on_user = getattr(args, "on_user", None)
    agent = getattr(args, "agent", None) or _derive_agent()

    if not blocked_on and not on_user:
        _err("block requires --blocked-on or --on-user.")
        return 1

    # The ask text: --on-user is the human-facing ask and takes precedence.
    block_reason = on_user or blocked_on
    human = identity.resolve_human() if on_user else None

    task = _load_task(task_id, backend=backend)
    if task is None:
        _err(f"Task not found: {task_id}")
        return 1

    try:
        task = schema.apply_transition(
            task,
            "blocked",
            by=agent,
            blocked_on=block_reason,
        )
    except (schema.TransitionError, schema.SchemaError) as e:
        _err(str(e))
        return 1

    if on_user:
        # Land it on the human: assign + tag. apply_transition rebuilds standard
        # tags but preserves non-standard ones, so adding needs:human AFTER the
        # transition keeps it through any later transition's tag rebuild.
        task["assignee"] = human
        if "needs:human" not in task.get("tags", []):
            task["tags"] = sorted(set(task.get("tags", []) + ["needs:human"]))
        # Scheduling: --not-before gates when this surfaces as DUE-NOW on the
        # human's plate; --due is the informational deadline. Both parsed via
        # schema.parse_when (ISO date/datetime or relative Nd/Nh/Nm); an
        # unparseable value resolves to None (treated as unset) so a typo never
        # blocks the op. Only set when provided so an existing value isn't
        # clobbered with None on a re-block without the flag.
        not_before_raw = getattr(args, "not_before", None)
        due_raw = getattr(args, "due", None)
        if not_before_raw is not None:
            task["not_before"] = schema.parse_when(not_before_raw)
        if due_raw is not None:
            task["due"] = schema.parse_when(due_raw)

    cache.write_cached_task(task)

    ok = False
    try:
        ok = _write_task_and_views(task, backend=backend, command="block")
    except schema.ConflictError as e:
        _err(str(e))
        return 2
    except schema.NeedsReconcile as e:
        _warn(str(e))
        ok = True  # Task was written; only views need repair

    if not ok:
        _warn(
            f"Task cached locally but remote upload failed: {task_id}. "
            "Run 'fulcra-coord reconcile' after Fulcra access recovers."
        )
        return 1

    if on_user:
        # Best-effort needs-user timeline annotation (situational awareness piece
        # 6). Gated by FULCRA_COORD_ANNOTATIONS (off by default -> no-op); never
        # raises into the task op. Emitted AFTER the write fully succeeds so we
        # never annotate a block that didn't land, and guarded so even a bug in
        # the hook can't break a successful block.
        try:
            lifecycle_annotations.emit_needs_user_annotation(
                task=task, agent=agent, backend=backend)
        except Exception:
            pass
        _info(f"Blocked on {human}: {task_id}")
        _info(f"  Needs: {block_reason}")
    else:
        _info(f"Blocked: {task_id}")
        _info(f"  Blocked on: {block_reason}")
    return 0


# ---------------------------------------------------------------------------
# Liveness-aware reviewer routing (request-review + reconcile reroute sweep)
# ---------------------------------------------------------------------------

# Canonical-reviewer identities are bus-global IDENTITIES, not locations — a
# reviewer may run on any machine (machine-agnostic invariant). Arc sessions
# route to the Arc reviewer; everyone else to the codex main reviewer.
ARC_REVIEWER = "claude-code:ArcBot:Arc-Code-Review"
DEFAULT_REVIEWER = "codex:Mac.localdomain:main"


def _canonical_reviewer(author: str) -> str:
    """The seeded, preference-first reviewer for an author. Seeded even if it
    never declared --can-review (day-one works before agents update). #devops/
    openclaw is deliberately NOT canonical — it qualifies only if actually
    live/idle AND review-capable."""
    if (author or "").startswith("claude-code:ArcBot:"):
        return ARC_REVIEWER
    return DEFAULT_REVIEWER


def _review_pool(author: str, presence: list[dict[str, Any]]) -> list[str]:
    """Preference-ordered candidate pool: canonical reviewer first (seeded,
    tie-break only — a live non-canonical reviewer still wins), then every
    review-capable agent in presence order. De-duplicated, canonical kept first."""
    canonical = _canonical_reviewer(author)
    pool = [canonical]
    for rec in presence:
        agent = rec.get("agent")
        if not agent or agent == canonical:
            continue
        if "review" in (rec.get("capabilities") or []):
            pool.append(agent)
    # de-dup preserving first occurrence (canonical stays index 0)
    seen: set[str] = set()
    ordered: list[str] = []
    for a in pool:
        if a not in seen:
            seen.add(a)
            ordered.append(a)
    return ordered


def _append_route_event_and_assignee(task, *, kind, to, by, attempt, reason,
                                     candidate_snapshot, observed_updated_at,
                                     dt=None):
    """Append a routing event AND sync task.assignee to its `to`, so the event
    log (audit + sweep input) and the assignee (inbox/tell machinery) never
    disagree. Mutates + returns a deep copy of the task."""
    import copy
    from . import routing
    task = copy.deepcopy(task)
    at = (dt or datetime.now(timezone.utc)).isoformat(
        timespec="microseconds").replace("+00:00", "Z")
    ev = routing.make_route_event(kind=kind, to=to, by=by, attempt=attempt,
                                  reason=reason, candidate_snapshot=candidate_snapshot,
                                  observed_updated_at=observed_updated_at, at=at)
    task.setdefault("events", []).append(ev)
    task["events"] = task["events"][-schema.MAX_EVENTS_INLINE:]
    task["assignee"] = to
    task["updated_at"] = at
    task["last_touched_by"] = by
    return task


def _force_block_for_human(task, *, by, ask, human):
    """Transition a task to `blocked` on the human's plate, tolerating the
    `proposed -> blocked` gap.

    `make_task` (and an as-yet-unacted review directive) starts at `proposed`,
    and schema.STATUS_TRANSITIONS does NOT allow `proposed -> blocked` directly
    (only proposed -> {active,waiting,abandoned}). The block --on-user primitive
    assumes an already-active task. So when the task is `proposed`, first step it
    through `active` (claim it for the escalating agent) before blocking, which
    is a legal path. Returns the blocked task copy carrying needs:human."""
    if task.get("status") == "proposed":
        task = schema.apply_transition(task, "active", by=by,
                                       summary="Escalating to human for manual routing.")
    task = schema.apply_transition(task, "blocked", by=by, blocked_on=ask)
    task["assignee"] = human
    if "needs:human" not in task.get("tags", []):
        task["tags"] = sorted(set(task.get("tags", []) + ["needs:human"]))
    return task


def _escalate_review_to_human(*, pr, repo, tried, backend=None, existing=None):
    """Escalate a review with no live reviewer to the human via the existing
    block --on-user shape (needs:human -> needs-me plate + digest + banner).

    Idempotent by caller: the sweep passes `existing` (the review task) to
    update IT in place (so the escalation lands on the same task the agents are
    already tracking, not a duplicate); a fresh request-review miss passes None
    and creates a dedicated escalation task. Best-effort: never raises into
    request-review / reconcile — a failure is warned and reported False."""
    try:
        human = identity.resolve_human()
        me = identity.resolve_agent(None)
        ask = (f"PR #{pr} in {repo} needs review; no reviewer is live/idle "
               f"(tried: {', '.join(tried) or 'none'}). Assign a reviewer manually.")
        marker = f"review-escalation:{repo}#{pr}"
        task = existing
        if task is None:
            task = schema.make_task(
                title=f"PR #{pr} needs a reviewer ({repo})",
                workstream=repo, agent=me, owner_agent=me, assignee=human,
                priority="P1",
                summary=ask)
        task = _force_block_for_human(task, by=me, ask=ask, human=human)
        # Stable per-PR marker for idempotency / dedup across cycles.
        task["tags"] = sorted(set(task.get("tags", [])) | {marker})
        _write_task_and_views(task, backend=backend, command="block")
        return True
    except Exception as e:  # noqa: BLE001 — best-effort; never crash the caller
        _warn(f"review escalation failed (non-fatal): {e}")
        return False


def cmd_request_review(args: Any, backend: Optional[list[str]] = None) -> int:
    """Route a PR review to a live/idle reviewer, or escalate to the human.

    Builds a preference-ordered pool (canonical reviewer seed + capability:review
    agents), resolves the best live/idle recipient via the liveness-aware
    resolver, and either tells them a kind:review-tagged directive (appending a
    `routed` event + syncing assignee) or escalates via block --on-user. --dry-run
    prints the ranked pool / tiers / excluded / winner / reason and writes
    nothing. Best-effort: a presence/resolve failure escalates rather than
    crashing (a review must never silently vanish)."""
    from . import routing
    pr = args.pr
    repo = args.repo
    dry_run = getattr(args, "dry_run", False)
    out_format = getattr(args, "format", "table")
    author = identity.resolve_agent(getattr(args, "agent", None))
    try:
        agg = remote.download_json(remote.presence_view_path(), backend=backend)
        presence = (agg or {}).get("agents", []) if agg else []
    except Exception:
        presence = []  # treat as no live candidate -> escalate
    override = getattr(args, "candidate_list", None)
    if override:
        pool = [a.strip() for a in override.split(",") if a.strip()]
    else:
        pool = _review_pool(author, presence)
    now = datetime.now(timezone.utc)
    snapshot = [
        {"agent": a,
         "tier": views._effective_routing_liveness(
             next((r.get("last_seen", "") for r in presence if r.get("agent") == a), ""),
             now, views._presence_grace_seconds()) or "below-floor"}
        for a in pool
    ]
    winner = views.resolve_live_recipient(pool, presence, floor="idle", now=now)
    excluded = [s for s in snapshot if s["tier"] == "below-floor"]
    if dry_run:
        report = {"pr": pr, "repo": repo, "pool": pool, "snapshot": snapshot,
                  "excluded": [e["agent"] for e in excluded], "winner": winner,
                  "reason": "live/idle reviewer found" if winner
                            else "no live reviewer — would escalate"}
        if out_format == "json":
            _print_json(report)
        else:
            _info(f"[dry-run] pool={pool} winner={winner}")
        return 0
    if winner is None:
        _escalate_review_to_human(pr=pr, repo=repo,
                                  tried=[s["agent"] for s in snapshot], backend=backend)
        _info(f"PR #{pr}: no reviewer live — escalated to human.")
        return 0
    # HIT: build the directive, tag kind:review, append routed event + assignee.
    title = f"Review PR #{pr} — assume bugs, claim the review before working"
    task = schema.make_task(
        title=title, workstream=repo, agent=author,
        owner_agent=author, assignee=winner, priority="P1",
        summary=(f"PR #{pr} in {repo} needs review. Claim it (transition active / "
                 f"emit review-accepted) before working."))
    task["tags"] = sorted(set(task.get("tags", []) + [routing.REVIEW_TAG]))
    task["pr"] = pr
    task["repo"] = repo  # carried for the sweep + audit
    tier = next((s["tier"] for s in snapshot if s["agent"] == winner), "idle")
    task = _append_route_event_and_assignee(
        task, kind="routed", to=winner, by=author, attempt=1,
        reason=f"live/idle reviewer ({tier})", candidate_snapshot=snapshot,
        observed_updated_at=task.get("updated_at", ""))
    cache.write_cached_task(task)
    try:
        ok = _write_task_and_views(task, backend=backend, command="request-review")
    except (schema.ConflictError, schema.NeedsReconcile):
        ok = True
    _info(f"PR #{pr} routed to {winner} ({tier}).")
    return 0 if ok else 1


# --- reconcile reroute sweep: thresholds + classification + I/O wrapper -----

def _env_float(name: str, default: float) -> float:
    """Read an env var as a float, falling back to `default` on absent/blank/
    unparseable — mirrors views._stale_hours' tolerance so a typo never breaks
    a reconcile tick."""
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(default)


def _reroute_minutes(priority: str) -> float:
    """Minutes a never-acted review may sit on a below-floor assignee before the
    sweep reroutes it. P1 is more urgent (15m) than P2/P3 (30m); both are
    wall-clock durations (bus-global, machine-agnostic) and env-overridable."""
    if (priority or "P2") == "P1":
        return _env_float("FULCRA_COORD_REVIEW_REROUTE_MINUTES_P1", 15.0)
    return _env_float("FULCRA_COORD_REVIEW_REROUTE_MINUTES_P2", 30.0)


def _reroute_max() -> int:
    """Max route attempts (the initial route + reroutes) before the sweep gives
    up and escalates to the human instead of cycling reviewers forever."""
    return int(_env_float("FULCRA_COORD_REVIEW_REROUTE_MAX", 2.0))


def _accepted_stall_hours() -> float:
    """Hours an ACCEPTED-then-silent review may stall before the sweep escalates
    it to the human (it is never rerouted once accepted — we don't yank work out
    from under a reviewer mid-flight; we only nudge the human after a long stall)."""
    return _env_float("FULCRA_COORD_ACCEPTED_STALL_HOURS", 2.0)


def _review_accepted_by_assignee(task, assignee, routed_dt):
    """The timestamp at which `assignee` explicitly ACCEPTED this review after
    routed_dt, or None.

    Acceptance is an explicit `review-accepted` event OR a status-transition-to-
    active authored by the assignee (claiming the work). A bare `inbox_ack` is a
    READ receipt, NOT acceptance — excluded here, so a reviewer that only opened
    its inbox then went dark still gets rerouted rather than freezing the PR."""
    for e in task.get("events", []):
        if e.get("by") != assignee:
            continue
        at = views._parse_dt(e.get("at", ""))
        if at is None or routed_dt is None or at < routed_dt:
            continue
        if e.get("type") == "review-accepted":
            return at
        if e.get("type") == "active":  # claim/transition-to-active is acceptance
            return at
    return None


def _classify_review(task, presence, now):
    """Pure classifier for the reroute sweep. Returns one of reroute | escalate |
    freeze | freeze-escalate | none. Never reroutes a non-kind:review task.

    Pure + deterministic given `task` + `presence` + `now` (all injected), so it
    evaluates identically on every machine that reads the same bus snapshot."""
    from . import routing
    if not routing.is_review_directive(task):
        return "none"
    if task.get("status") in ("done", "abandoned"):
        return "none"
    route = routing.current_route(task)
    if route is None:
        return "none"
    assignee = route.get("to")
    routed_dt = views._parse_dt(route.get("at", ""))
    accepted_at = _review_accepted_by_assignee(task, assignee, routed_dt)
    if accepted_at is not None:
        # Accepted-then-stalled: FREEZE (don't yank mid-work). Escalate only
        # after a long stall measured from acceptance.
        stall_h = _accepted_stall_hours()
        if (now - accepted_at).total_seconds() / 3600.0 >= stall_h:
            return "freeze-escalate"
        return "freeze"
    # Never-acted path: only reroute if assignee is below floor AND past threshold.
    eff = views._effective_routing_liveness(
        next((r.get("last_seen", "") for r in presence if r.get("agent") == assignee), ""),
        now, views._presence_grace_seconds())
    if eff is not None:  # assignee still live/idle -> give it time, no reroute
        return "none"
    threshold_min = _reroute_minutes(task.get("priority", "P2"))
    if routed_dt is None or (now - routed_dt).total_seconds() / 60.0 < threshold_min:
        return "none"
    # Cap check uses the CURRENT route's attempt counter (cumulative attempt
    # number), not the inline event count: the events list is truncated to the
    # last MAX_EVENTS_INLINE, so counting route events would under-count attempts
    # on a long-lived task. The attempt field is the durable cumulative count.
    current_attempt = route.get("attempt") or routing.route_attempt_count(task)
    if current_attempt >= _reroute_max():
        return "escalate"  # cap reached
    return "reroute"


def _sweep_review_routes(all_tasks, *, backend=None, now=None):
    """Authoritative reconcile-time reroute sweep. Considers ONLY kind:review
    directives. For each: classify; reroute a never-acted below-floor past-
    threshold review (excluding already-tried agents, minting a new route_id),
    escalate on cap/miss, freeze an accepted-then-stalled one (escalate after
    ACCEPTED_STALL_HOURS).

    Runs once per reconcile cycle; whichever machine reconciles first wins and
    the others converge via the stale-observation re-read (Files has no CAS) plus
    the optimistic write. Best-effort: one bad task — or the whole presence
    download — never raises into a reconcile tick (a failure skips, never crashes)."""
    from . import routing
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        agg = remote.download_json(remote.presence_view_path(), backend=backend)
        presence = (agg or {}).get("agents", []) if agg else []
    except Exception:
        presence = []
    for task in all_tasks:
        try:
            if not routing.is_review_directive(task):
                continue
            verdict = _classify_review(task, presence, now)
            if verdict in ("none", "freeze"):
                continue
            if verdict in ("escalate", "freeze-escalate"):
                # Escalate IN PLACE on the review task itself (existing=task) so
                # the human's plate points at the task the agents already track,
                # not a duplicate.
                _escalate_review_to_human(
                    pr=task.get("pr", task.get("id")),
                    repo=task.get("repo", task.get("workstream", "")),
                    tried=sorted(routing.tried_agents(task)),
                    backend=backend, existing=task)
                continue
            # verdict == "reroute": stale-observation check, then write.
            route = routing.current_route(task)
            fresh = _load_task(task["id"], backend=backend)
            if fresh is None:
                continue
            fresh_route = routing.current_route(fresh)
            # Abort if the task moved since we computed the decision: another
            # sweeper or the assignee changed the latest route or updated_at.
            # Two machines racing from the same snapshot thus converge to one
            # reroute (multi-sweeper convergence without a compare-and-swap).
            if (fresh_route or {}).get("route_id") != (route or {}).get("route_id") \
               or fresh.get("updated_at") != task.get("updated_at"):
                continue
            pool = _review_pool(task.get("owner_agent", ""), presence)
            winner = views.resolve_live_recipient(
                pool, presence, floor="idle", now=now,
                exclude=tuple(routing.tried_agents(task)))
            if winner is None:
                _escalate_review_to_human(
                    pr=task.get("pr", task.get("id")),
                    repo=task.get("repo", task.get("workstream", "")),
                    tried=sorted(routing.tried_agents(task)),
                    backend=backend, existing=fresh)
                continue
            snapshot = [{"agent": a} for a in pool]
            prev_attempt = (fresh_route or {}).get("attempt") \
                or routing.route_attempt_count(fresh)
            updated = _append_route_event_and_assignee(
                fresh, kind="rerouted", to=winner, by="reconcile-sweep",
                attempt=prev_attempt + 1,
                reason="assignee below floor, never acted",
                candidate_snapshot=snapshot,
                observed_updated_at=fresh.get("updated_at", ""))
            try:
                _write_task_and_views(updated, backend=backend, command="reroute-review")
            except (schema.ConflictError, schema.NeedsReconcile):
                pass  # optimistic write is the second line of defence; reconverges next cycle
        except Exception:
            continue  # one bad task must never break the sweep / reconcile tick


def cmd_pause(args: Any, backend: Optional[list[str]] = None) -> int:
    """Pause a task (set to waiting with a next_action)."""
    task_id = args.task_id
    next_action = args.next
    agent = getattr(args, "agent", None) or _derive_agent()

    task = _load_task(task_id, backend=backend)
    if task is None:
        _err(f"Task not found: {task_id}")
        return 1

    try:
        task = schema.apply_transition(
            task,
            "waiting",
            by=agent,
            next_action=next_action,
        )
    except (schema.TransitionError, schema.SchemaError) as e:
        _err(str(e))
        return 1

    cache.write_cached_task(task)

    ok = False
    try:
        ok = _write_task_and_views(task, backend=backend, command="pause")
    except schema.ConflictError as e:
        _err(str(e))
        return 2
    except schema.NeedsReconcile as e:
        _warn(str(e))
        ok = True  # Task was written; only views need repair

    if not ok:
        _warn(
            f"Task cached locally but remote upload failed: {task_id}. "
            "Run 'fulcra-coord reconcile' after Fulcra access recovers."
        )
        return 1

    _info(f"Paused: {task_id}")
    _info(f"  Next: {next_action}")
    return 0


def cmd_done(args: Any, backend: Optional[list[str]] = None) -> int:
    """Mark a task as done. Requires evidence and verification-level."""
    task_id = args.task_id
    evidence = args.evidence
    verification_level = getattr(args, "verification_level", "agent-verified") or "agent-verified"
    confidence = getattr(args, "confidence", None)
    agent = getattr(args, "agent", None) or _derive_agent()

    task = _load_task(task_id, backend=backend)
    if task is None:
        _err(f"Task not found: {task_id}")
        return 1

    try:
        task = schema.apply_transition(
            task,
            "done",
            by=agent,
            evidence=evidence,
            verification_level=verification_level,
            confidence=confidence,
        )
    except (schema.TransitionError, schema.SchemaError) as e:
        _err(str(e))
        return 1

    cache.write_cached_task(task)

    ok = False
    try:
        ok = _write_task_and_views(task, backend=backend, command="done")
    except schema.ConflictError as e:
        _err(str(e))
        return 2
    except schema.NeedsReconcile as e:
        _warn(str(e))
        ok = True  # Task was written; only views need repair

    if not ok:
        _warn(
            f"Task cached locally but remote upload failed: {task_id}. "
            "Run 'fulcra-coord reconcile' after Fulcra access recovers."
        )
        return 1

    # Prominent user-visible statement (required by design)
    _info(f"\n>>> Marked {task_id} done: {evidence}")
    return 0


def cmd_abandon(args: Any, backend: Optional[list[str]] = None) -> int:
    """Mark a task as abandoned."""
    task_id = args.task_id
    reason = args.reason
    agent = getattr(args, "agent", None) or _derive_agent()

    task = _load_task(task_id, backend=backend)
    if task is None:
        _err(f"Task not found: {task_id}")
        return 1

    try:
        task = schema.apply_transition(
            task,
            "abandoned",
            by=agent,
            reason=reason,
        )
    except (schema.TransitionError, schema.SchemaError) as e:
        _err(str(e))
        return 1

    cache.write_cached_task(task)

    ok = False
    try:
        ok = _write_task_and_views(task, backend=backend, command="abandon")
    except schema.ConflictError as e:
        _err(str(e))
        return 2
    except schema.NeedsReconcile as e:
        _warn(str(e))
        ok = True  # Task was written; only views need repair

    if not ok:
        _warn(
            f"Task cached locally but remote upload failed: {task_id}. "
            "Run 'fulcra-coord reconcile' after Fulcra access recovers."
        )
        return 1

    _info(f"Abandoned: {task_id}  Reason: {reason}")
    return 0


def _reconcile_presence(backend: Optional[list[str]] = None) -> None:
    """Rebuild ``views/presence.json`` from the durable ``presence/*.json`` files.

    Lists ``<root>/presence/`` (remote.list_files), downloads each per-agent
    record, and rebuilds the aggregate roster — the presence analogue of the task
    view self-heal. This is what makes the opportunistic connect-time aggregate
    merge eventually-consistent: even if a connect's best-effort upsert was lost,
    reconcile reconstructs the roster from the authoritative per-agent records.

    LISTING REQUIREMENT: relies on remote.list_files being able to enumerate the
    presence dir. If listing returns nothing (empty dir, or a backend without a
    working list), no aggregate is written — the existing one is left intact
    rather than clobbered to empty. Best-effort: never raises into reconcile."""
    try:
        prefix = f"{remote.remote_root()}/presence/"
        paths = remote.list_files(prefix, backend=backend)
        records = []
        for path in paths:
            if not path.endswith(".json"):
                continue
            rec = remote.download_json(path, backend=backend)
            if rec and rec.get("agent"):
                records.append(rec)
        if not records:
            return
        view = views.build_presence(records)
        remote.upload_json(view, remote.presence_view_path(), backend=backend)
        cache.write_cached_view("presence", view)
    except Exception:
        pass  # presence rebuild is best-effort; task-view reconcile is the contract


def cmd_reconcile(args: Any, backend: Optional[list[str]] = None) -> int:
    """Repair views and resolve pending operation markers."""
    import time
    _info("Reconciling coordination views...")
    t0 = time.monotonic()
    timeout = int(os.environ.get("FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS", "90"))
    deadline = t0 + timeout

    markers = cache.list_op_markers()
    needs_repair = [m for m in markers if m.get("needs_reconcile")]
    if needs_repair:
        _info(f"  {len(needs_repair)} operation(s) need view repair.")

    try:
        all_tasks = _load_all_tasks(backend=backend)
    except Exception as e:
        _warn(f"Could not load remote index: {e}")
        all_tasks = cache.list_cached_tasks()

    _info(f"  {len(all_tasks)} task(s) loaded.")

    now = datetime.now(timezone.utc)
    stale_claims = []
    for t in all_tasks:
        claim = t.get("claim", {})
        expires = claim.get("claim_expires_at")
        if expires:
            try:
                exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if now > exp_dt and t.get("status") == "active":
                    stale_claims.append(t["id"])
            except ValueError:
                pass

    if stale_claims:
        _warn(f"  Stale claims detected: {stale_claims}")

    if time.monotonic() - t0 > timeout:
        _err("Reconcile timeout exceeded.")
        return 1

    all_views = views.build_all_views(all_tasks)
    view_items = list(all_views.items())

    # Cache every view locally regardless of upload outcome — matches the prior
    # sequential loop, which wrote the cache for each view before attempting its
    # upload. Done up front (main thread) so the cache write is never racy.
    for view_name, view_data in view_items:
        cache.write_cached_view(view_name, view_data)

    # Upload the views CONCURRENTLY (PERF), the same way _write_task_and_views
    # (P1) does: remote.upload_json is thread-safe (each call writes a unique
    # tempfile + runs an independent subprocess; remote.py holds no shared
    # mutable state), so a small pool collapses the ~50 serial uploads into one
    # round-trip's wall-time — the second half of the reconcile-timeout fix.
    # Semantics are preserved exactly: per-view success is collected, any
    # failure (False OR a raise) lands in `failures`, and the partial-upload
    # handling below is unchanged.
    failures = []

    def _upload_one(item):
        view_name, view_data = item
        remaining = deadline - time.monotonic()
        # BUG 6b: the old guard was `remaining <= 0` with `timeout=max(1, int(
        # remaining))`. With 0<remaining<1 that floored the per-view timeout UP to
        # 1s, letting an upload run up to ~1s PAST the global reconcile deadline.
        # Treat any sub-1s budget as past-deadline (skip, count as a failed view)
        # so the deadline is a hard ceiling — consistent with the `<= 0` guard.
        if remaining < 1:
            return view_name, False
        vpath = _view_name_to_remote(view_name)
        # Treat a RAISING upload as a failed view, not an escape hatch: an
        # unguarded pool.map would re-raise out of cmd_reconcile, bypassing the
        # failures -> "preserve markers, return 1" path and crashing the
        # heartbeat. Catching keeps the contract: any failure is a failed view.
        try:
            ok = remote.upload_json(view_data, vpath, backend=backend,
                                    timeout=int(remaining))
        except Exception:
            ok = False
        return view_name, ok

    max_workers = min(8, len(view_items)) or 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for view_name, ok in pool.map(_upload_one, view_items):
            if not ok:
                failures.append(view_name)

    if time.monotonic() - t0 > timeout:
        _err("Reconcile timeout exceeded mid-upload.")
        ops_log.log_op("reconcile", status="timeout")
        return 1

    # Rebuild the presence aggregate from the durable per-agent presence records,
    # mirroring how the task views self-heal here. Best-effort: a presence rebuild
    # failure must not fail a task-view reconcile, so it is reported but does not
    # count toward `failures`.
    _reconcile_presence(backend=backend)

    # Liveness-aware reroute sweep (best-effort; never fails a reconcile tick).
    # Runs AFTER the presence rebuild so it reads the freshly-reconciled
    # aggregate. Considers only kind:review directives; reroutes never-acted
    # reviews whose assignee fell below liveness floor, escalates on cap/miss,
    # freezes accepted-then-stalled ones. Whichever machine reconciles first
    # wins; others converge via the stale-observation re-read inside the sweep.
    try:
        _sweep_review_routes(all_tasks, backend=backend, now=now)
    except Exception:
        pass

    if failures:
        _warn(f"  View upload failures: {failures}")
        ops_log.log_op("reconcile", status="partial", detail=f"failed views: {failures}")
        # Do NOT clear op markers — views are still broken and need another reconcile run.
        return 1

    for m in needs_repair:
        cache.clear_op_marker(m["op_id"])

    ops_log.log_op("reconcile", status="ok", detail=f"{len(all_tasks)} tasks, {len(all_views)} views")
    _info(f"  Reconcile complete. {len(all_views)} views refreshed.")
    return 0


def cmd_search(args: Any, backend: Optional[list[str]] = None) -> int:
    """Search tasks by text across title, summary, tags."""
    query = args.query
    out_format = getattr(args, "format", "table")

    idx = cache.read_cached_view("search-index")
    if idx:
        records = idx.get("records", [])
        q = query.lower()
        results = []
        for r in records:
            text = " ".join([
                r.get("title", ""),
                r.get("summary", ""),
                r.get("workstream", ""),
                r.get("owner_agent", ""),
                " ".join(r.get("tags", [])),
            ]).lower()
            if q in text:
                results.append(r)
    else:
        # No cached search-index — search the summaries aggregate. search_tasks
        # reads title/current_summary/workstream/owner_agent/tags, all present on
        # a summary; no task body fetch. Falls back to a full load on an older bus.
        all_tasks = _load_task_summaries(backend=backend)
        results = views.search_tasks(query, all_tasks)

    if out_format == "json":
        _print_json({"query": query, "count": len(results), "results": results})
        return 0

    if not results:
        _info(f"No tasks found matching {query!r}.")
        return 0

    _info(f"\n{len(results)} task(s) matching {query!r}:\n")
    for r in results:
        status = r.get("status", "?")
        task_id = r.get("id", "?")
        title = r.get("title", "")[:60]
        priority = r.get("priority", "??")
        print(f"  [{status}] [{priority}] {task_id[:28]}  {title}")
        # Search results may come from cached search-index ("summary") or
        # from task_summary() dicts ("current_summary") — handle both.
        summary_text = (r.get("summary") or r.get("current_summary") or "").strip()
        if summary_text:
            print(f"          {summary_text[:80]}")
    print()
    return 0


def cmd_capabilities(args: Any, backend: Optional[list[str]] = None) -> int:
    """Print this build's version + the commands it supports — a capability probe.

    ArcBot-2 flagged that onboarding instructions can drift ahead of the
    installed CLI: a doc tells an agent to run a subcommand its build doesn't
    have yet. This gives onboarding a machine-readable check —
    ``capabilities --format json`` returns ``{name, version, commands}`` so a
    script can verify e.g. ``"needs-me" in commands`` before relying on it,
    instead of discovering the gap via an argparse error. The command list is
    sourced from the dispatch table (``entry.COMMAND_MAP``) — the same registry
    ``main`` routes on, so it can never claim a command that won't run. The
    hidden hook-only ``__session-task`` is excluded (not part of the public
    surface). Read-only; never touches the bus."""
    from . import __version__
    # Lazy import: entry imports this module at load, so importing entry at cli
    # module scope would be circular. Inside the function it resolves fine.
    from .entry import COMMAND_MAP

    commands = sorted(k for k in COMMAND_MAP if not k.startswith("__"))
    out_format = getattr(args, "format", "table")

    if out_format == "json":
        _print_json({"name": "fulcra-coord", "version": __version__,
                     "commands": commands})
        return 0

    print(f"fulcra-coord {__version__}")
    print(f"commands ({len(commands)}): {' '.join(commands)}")
    return 0


def cmd_doctor(args: Any, backend: Optional[list[str]] = None) -> int:
    """Check configuration, CLI availability, and remote access."""
    import shutil
    from . import __version__, remote_root as get_remote_root

    _info(f"\nfulcra-coord doctor — v{__version__}")
    _info(f"{'='*50}")

    ok_all = True

    # Config
    _info(f"\n[Config]")
    _info(f"  Remote root:  {get_remote_root()}")
    _info(f"  Cache root:   {cache.cache_root()}")

    cli_env = os.environ.get("FULCRA_CLI_COMMAND", "")
    if cli_env:
        _info(f"  CLI command:  {cli_env} (FULCRA_CLI_COMMAND)")
    elif shutil.which("fulcra-api"):
        _info(f"  CLI command:  fulcra-api (found on PATH)")
    else:
        _info(f"  CLI command:  uv tool run fulcra-api (fallback)")

    # CLI availability
    _info(f"\n[CLI]")
    cli_ok, cli_msg = remote.check_cli_available(backend=backend)
    status = "OK" if cli_ok else "FAIL"
    _info(f"  CLI reachable: {status}  ({cli_msg})")
    if not cli_ok:
        ok_all = False
        _info("  -> Install Fulcra CLI: uv tool install fulcra-api")
        _info("  -> Or set FULCRA_CLI_COMMAND to your CLI invocation")

    # File command group probe — the #1 fresh-agent onboarding failure.
    #
    # The public PyPI `fulcra-api` build lacks the `file` command group that the
    # entire coordination bus is driven by, so an agent that pip-installs it sees
    # every bus op fail silently. This probe targets the *resolved real CLI* (not
    # the injected fake backend, which speaks the `file` subcommand protocol but
    # has no top-level `file` group), so it answers "does the installed CLI have
    # `file`?". Wrapped defensively: a hung or broken probe must degrade to FAIL,
    # never crash doctor.
    try:
        file_ok, file_msg = remote.check_file_commands()
    except Exception as e:  # defensive — check_file_commands shouldn't raise
        file_ok, file_msg = False, f"file probe error: {e}"
    file_status = "OK" if file_ok else "FAIL"
    _info(f"  File commands: {file_status}  ({file_msg})")
    if not file_ok:
        ok_all = False
        _info("  -> The installed Fulcra CLI lacks the `file` command group that "
              "fulcra-coord needs to drive the bus.")
        _info("  -> Install a file-capable build (the `file-management` branch of "
              "fulcradynamics/fulcra-api-python).")
        _info("  -> See docs/fulcra-cli-branch.md for the exact install command.")

    # Remote access
    _info(f"\n[Remote]")
    if cli_ok or backend:
        remote_ok, remote_msg = remote.check_remote_access(backend=backend)
        remote_status = "OK" if remote_ok else "FAIL"
        _info(f"  Remote access: {remote_status}  ({remote_msg})")
        if not remote_ok:
            ok_all = False
            _info("  -> Run: fulcra-api auth login  (see docs/auth.md)")
            _info("  -> Or check FULCRA_COORD_REMOTE_ROOT is correct")
    else:
        _info("  Remote access: SKIP (CLI not reachable)")

    # Pending operation markers
    _info(f"\n[Cache]")
    markers = cache.list_op_markers()
    needs_repair = [m for m in markers if m.get("needs_reconcile")]
    all_tasks_cached = cache.list_cached_tasks()
    _info(f"  Cached tasks:  {len(all_tasks_cached)}")
    _info(f"  Pending ops:   {len(markers)}")
    if needs_repair:
        _info(f"  Needs reconcile: {len(needs_repair)}")
        _info("  -> Run: fulcra-coord reconcile")
    else:
        _info(f"  Needs reconcile: 0")

    # Annotations (Agent-Tasks timeline writer)
    #
    # Surfaces, at a glance, WHY a timeline write would or wouldn't happen — the
    # diagnostic that would have told the operator immediately that the feature
    # was simply disabled. Reports the resolved mode, whether a bearer token is
    # obtainable (WITHOUT ever printing it), and the API base the writer targets.
    _info(f"\n[Annotations]")
    ann_mode, ann_source = lifecycle_annotations.resolve_mode_source()
    _info(f"  Mode:          {ann_mode}  (source: {ann_source})")
    if ann_mode == "off":
        _info("  -> disabled — run `fulcra-coord annotations on` to enable for "
              "every agent (or set FULCRA_COORD_ANNOTATIONS=http for this shell)")
    else:
        _info(f"  API base:      {lifecycle_annotations._api_base()}")
        # Resolve the token only to confirm one EXISTS; never echo its value.
        token = lifecycle_annotations._resolve_token()
        if token:
            src = ("FULCRA_ACCESS_TOKEN" if os.environ.get("FULCRA_ACCESS_TOKEN")
                   else "fulcra auth print-access-token")
            _info(f"  Token:         OK (via {src})")
        else:
            ok_all = False
            _info("  Token:         FAIL (no FULCRA_ACCESS_TOKEN and "
                  "`fulcra auth print-access-token` did not yield one)")
            _info("  -> Run: fulcra auth login   (or set FULCRA_ACCESS_TOKEN)")

    _info(f"\n{'='*50}")
    _info("OK" if ok_all else "Issues detected — see above.")
    return 0 if ok_all else 1


def cmd_install_shim(args: Any, backend: Optional[list[str]] = None) -> int:
    """Install a fulcra-coord shim to PATH (~/.local/bin/fulcra-coord)."""
    import stat as stat_mod
    from pathlib import Path

    # Find the installed entry point for this package
    # Works whether installed as a package or run directly
    script_path = Path(sys.argv[0]).resolve()
    if script_path.name == "fulcra-coord" and script_path.exists():
        src = script_path
    else:
        # Derive from package location
        pkg_dir = Path(__file__).resolve().parent
        src = pkg_dir.parent / "scripts" / "fulcra-coord"

    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim_path = bin_dir / "fulcra-coord"

    # Guard against writing a shim that calls itself (infinite loop).
    # This happens when `pip install --user` places the entry point directly at
    # ~/.local/bin/fulcra-coord — the same destination as the shim.
    src_is_shim_target = src.exists() and src.resolve() == shim_path.resolve()

    if src.exists() and not src_is_shim_target:
        shim_content = f"""#!/usr/bin/env bash
# fulcra-coord shim — auto-generated by fulcra-coord install-shim
exec "{src}" "$@"
"""
    else:
        # Fallback: invoke via python3 -m (works for installed packages where
        # fulcra_coord is on PYTHONPATH, and for source-tree dev installs).
        shim_content = f"""#!/usr/bin/env bash
# fulcra-coord shim — auto-generated by fulcra-coord install-shim
exec python3 -m fulcra_coord "$@"
"""

    shim_path.write_text(shim_content)
    shim_path.chmod(shim_path.stat().st_mode | stat_mod.S_IEXEC | stat_mod.S_IXGRP | stat_mod.S_IXOTH)
    _info(f"Shim installed: {shim_path}")
    _info(f"\nAdd to PATH if needed:")
    _info(f'  export PATH="$HOME/.local/bin:$PATH"')
    return 0
