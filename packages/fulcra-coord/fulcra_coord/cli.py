"""CLI command implementations for fulcra-coord.

Each command accepts parsed argparse namespace and an optional backend=
override for testing without live Fulcra access.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from . import cache, remote, schema, views, log as ops_log, session_link, claude_code, openclaw, heartbeat, codex, listener, identity
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

    for tid in remote_ids:
        t = _cache_remote_task(tid, backend=backend)
        if t:
            task_map[tid] = t

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
    To avoid dropping tasks THIS agent already knows about, we union in our local
    cached task summaries, preferring the FRESHER record per id by ``updated_at``
    (so we never resurrect a stale local copy over a newer remote one). Tasks
    only another agent knows about and that raced out of the aggregate still heal
    on the next ``reconcile`` (which rebuilds from the full index+search+next
    union) — that residual transient is accepted by design.

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

    # S2: union local cached task summaries, freshest-by-updated_at wins, so a
    # task this agent knows about that a raced aggregate dropped is recovered —
    # without ever overwriting a newer remote record with a stale local one.
    for t in cache.list_cached_tasks():
        s = schema.task_summary(t)
        prev = by_id.get(s["id"])
        if prev is None or s.get("updated_at", "") > prev.get("updated_at", ""):
            by_id[s["id"]] = s

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
        raise schema.NeedsReconcile(
            f"Task {task_id} written, but view upload partial. "
            f"Run 'fulcra-coord reconcile' to repair views."
        )

    op_marker["status"] = "done"
    cache.write_op_marker(op_id, op_marker)
    cache.clear_op_marker(op_id)
    ops_log.log_op(command, task_id, status="ok")

    # Best-effort lifecycle annotation on the operator's Fulcra timeline. Placed
    # AFTER the task+views write fully succeeds so we never annotate a write that
    # didn't land, and intentionally last so a (gated, normally no-op) annotation
    # can never affect the task op's outcome. emit_lifecycle_annotation is itself
    # best-effort and never raises, but we still guard the call site so even a
    # programming error in the hook cannot break a successful task write.
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

    return True


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

    import copy

    if local_status != remote_status:
        remote_event_times = {e["at"] for e in remote_task.get("events", [])}

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
            # Only remote changed status: remote's authoritative state wins.
            # Merge local's non-status-change events on top and apply local's
            # field updates (current_summary, next_action) if they are more
            # recent than remote's last update.
            merged = copy.deepcopy(remote_task)
            for ev in local.get("events", []):
                if ev["at"] not in remote_event_times:
                    merged.setdefault("events", []).append(ev)
            if local.get("updated_at", "") > remote_task.get("updated_at", ""):
                if local.get("current_summary") is not None:
                    merged["current_summary"] = local["current_summary"]
                if local.get("next_action") is not None:
                    merged["next_action"] = local["next_action"]
                # A concurrent `assign` sets assignee/owner_agent with a
                # non-status event. If local is the more recent side, carry those
                # field edits too — otherwise a reassignment racing a remote
                # status change is silently dropped (the merge base wins).
                if local.get("assignee") is not None:
                    merged["assignee"] = local["assignee"]
                if local.get("owner_agent") is not None:
                    merged["owner_agent"] = local["owner_agent"]
            merged["events"] = sorted(merged["events"], key=lambda e: e["at"])
            merged["events"] = merged["events"][-schema.MAX_EVENTS_INLINE:]
            return merged

    # Same status, or only local changed status: local is the base.
    merged = copy.deepcopy(local)
    for ev in remote_task.get("events", []):
        if ev["at"] not in local_event_times:
            merged.setdefault("events", []).append(ev)
    merged["events"] = sorted(merged["events"], key=lambda e: e["at"])
    merged["events"] = merged["events"][-schema.MAX_EVENTS_INLINE:]

    # Symmetric with the remote-only-status-change path: if remote's non-status
    # fields are more recent (concurrent field update by another agent), apply them.
    if remote_task.get("updated_at", "") > local.get("updated_at", ""):
        if remote_task.get("current_summary") is not None:
            merged["current_summary"] = remote_task["current_summary"]
        if remote_task.get("next_action") is not None:
            merged["next_action"] = remote_task["next_action"]
        # Symmetric with the local-newer branch above: a concurrent remote
        # `assign` (reassignment) racing this side's status change must not be
        # lost. Carry assignee/owner_agent from the more-recent remote side.
        if remote_task.get("assignee") is not None:
            merged["assignee"] = remote_task["assignee"]
        if remote_task.get("owner_agent") is not None:
            merged["owner_agent"] = remote_task["owner_agent"]

    return merged


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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

    if out_format == "json":
        _print_json({"agents": agent_blocks, "mine": mine})
        return 0

    if not agent_blocks:
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
        for t in blk["tasks"]:
            mark = " ⚠" if t["stale"] else ""
            print(f"    [{t['status'].upper()}] [{t['priority']}] "
                  f"{t['id'][:28]}{mark}  {t['title'][:50]}")
            if t["next_action"]:
                print(f"          next: {t['next_action'][:70]}")
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

    items = _load_inbox(me, backend=backend)

    if out_format == "json":
        _print_json({"agent": me, "count": len(items), "inbox": items})
        return 0

    if not items:
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
    print()
    return 0


def _load_inbox(me: str, backend: Optional[list[str]] = None) -> list[dict[str, Any]]:
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
    return views.inbox_for(me, all_tasks)


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
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def cmd_needs_me(args: Any, backend: Optional[list[str]] = None) -> int:
    """THE "what's blocked on ME" view (situational awareness piece 3).

    Lists every OPEN task (proposed/waiting/blocked) assigned to / blocked on the
    human, across all agents — showing WHO is waiting (owner_agent), WHAT they
    need (blocked_on / next_action), and HOW LONG it's been. This is the human's
    glance of "what's on my plate from my agents." Read-only.

    The human is resolved via ``--human`` > ``resolve_human()`` (env > config >
    default ``human``); matching is prefix-aware so ``human`` and ``ash`` both
    work. ``--format json`` for tooling (the SessionStart banner + the listener).
    """
    human = getattr(args, "human", None) or identity.resolve_human()
    out_format = getattr(args, "format", "table")

    # needs_human reads status/assignee/tags — all on a summary; no body fetch.
    all_tasks = _load_task_summaries(backend=backend)
    items = views.needs_human(all_tasks, human)

    if out_format == "json":
        _print_json({"human": human, "count": len(items), "items": items})
        return 0

    if not items:
        _info(f"Nothing blocked on you ({human}).")
        return 0

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

    if out_format == "json":
        _print_json({
            "agent": me,
            "human": human,
            "active": active,
            "blocked_on_me": blocked_on_me,
            "owed_to_others": owed_to_others,
            "blocked_on_human": blocked_on_human,
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
    print()
    return 0


def cmd_start(args: Any, backend: Optional[list[str]] = None) -> int:
    """Create a new task and upload it."""
    title = args.title
    workstream = args.workstream
    agent = args.agent
    kind = getattr(args, "kind", "ops") or "ops"
    priority = getattr(args, "priority", "P2") or "P2"
    summary = getattr(args, "summary", "") or ""
    next_action = getattr(args, "next", "") or ""
    surface = getattr(args, "surface", None)

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


def cmd_reconcile(args: Any, backend: Optional[list[str]] = None) -> int:
    """Repair views and resolve pending operation markers."""
    import time
    _info("Reconciling coordination views...")
    t0 = time.monotonic()
    timeout = int(os.environ.get("FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS", "90"))

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
    failures = []
    for view_name, view_data in all_views.items():
        cache.write_cached_view(view_name, view_data)
        vpath = _view_name_to_remote(view_name)
        ok = remote.upload_json(view_data, vpath, backend=backend,
                                timeout=remote._reconcile_timeout())
        if not ok:
            failures.append(view_name)

        if time.monotonic() - t0 > timeout:
            _err("Reconcile timeout exceeded mid-upload.")
            ops_log.log_op("reconcile", status="timeout")
            return 1

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
