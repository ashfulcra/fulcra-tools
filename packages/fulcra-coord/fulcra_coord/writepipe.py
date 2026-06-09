"""Task write pipeline for fulcra-coord — the single mutation path onto the bus.

Every command that changes a task (start / update / block / pause / done / abandon /
tell / broadcast / assign / inbox-ack / request-review) converges here:
``_write_task_and_views`` uploads the task body under optimistic concurrency, merges
a concurrent peer's write when the pre-stat detects one (``_try_merge`` and its
field-carry / event-union / tag-repair helpers), rebuilds + uploads all views from
the summaries aggregate, keeps this session's task pointer in sync
(``_stamp_session_pointer``), and emits the best-effort lifecycle annotation
(``_emit_lifecycle``).

Extracted from cli.py behind stable re-exports; depends only on lower layers
(cache / remote / schema / views / identity / session_link + the io loader, the
timeutil stamp, the annotations and op-log siblings) and never imports cli, so the
split introduces no cycle. The crash-safety / last-writer-wins-with-merge contract
is load-bearing — the bodies are moved verbatim.
"""

from __future__ import annotations

import concurrent.futures
import copy
import uuid
from typing import Any, Optional

from . import cache, remote, schema, views, identity, session_link
from . import annotations as lifecycle_annotations
from . import eventlog, events as _events
from . import log as ops_log
from .io import _load_summaries_for_rebuild, _updated_at_key
from .timeutil import now_iso as _now_iso


# Event-only / acked_by-only keys are reconciled by the union helper, never by
# the wholesale field carry — copying them would clobber the union.
_MERGE_EVENT_KEYS = {"events", "acked_by"}


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

    # Strangler-fig dual-write: also append an immutable event mirroring this
    # mutation. BEST-EFFORT — never fail the task write on an event-log error
    # (Phase 1: the mutable file is still authoritative). A later reconcile
    # parity pass surfaces any event-vs-file drift as health debt.
    #
    # Reaches here ONLY on the fully-clean normal-completion path: the
    # conflict branch raised ConflictError before any upload (no mutation to
    # mirror), and the partial-view-failure branch raised NeedsReconcile above.
    # So an event is appended exactly when — and only when — the task body and
    # all views actually landed.
    try:
        ev = _events.make_event(
            family="tasks", task_id=task["id"], kind=command,
            actor=task.get("owner_agent") or task.get("assignee") or "unknown",
            # Phase 2a: the payload IS the full task snapshot — the entire task
            # dict, not a field subset — so ``fold_task`` can reconstruct a
            # complete, schema-valid task from the latest snapshot (including the
            # nested ``source{}``/``claim{}``/``done{}`` and ``tags[]``). It is
            # deep-copied so a later in-place mutation of ``task`` (this same
            # object can be re-touched downstream) can't retro-alter the
            # already-emitted, immutable event.
            payload=copy.deepcopy(task),
            idempotency_key=op_id,
        )
        ok = eventlog.append_event(ev, backend=backend)
        if not ok:
            try:
                ops_log.log_op(command, task["id"], status="event_append_failed",
                               error="Event append returned false")
            except Exception:
                pass
    except Exception as exc:
        # Best-effort; the mutable write already succeeded. But DO record the
        # failure in the ops log — Phase 1's whole job is to validate the
        # dual-write, so a silent miss is exactly what we must not have. The
        # logging is itself guarded so even it cannot break the task write.
        try:
            ops_log.log_op(command, task["id"], status="event_append_failed",
                           error=str(exc))
        except Exception:
            pass

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


# Fields the per-field 3-way decision must NOT touch: events/acked_by are
# reconciled by the union helper, the derived ``tags`` are rebuilt by
# _repair_merged_tags from the merged scalar fields, and updated_at /
# last_touched_* legitimately differ between a point-in-time fold and the live
# file on every write (the same set the parity check ignores), so comparing them
# would manufacture spurious "both changed differently" conflicts. status is
# handled by its own transition policy below, so it is excluded from the generic
# scalar loop too. _applied_event_count is fold bookkeeping that should never
# reach a clean body, excluded defensively.
_THREE_WAY_DERIVED_OR_VOLATILE = {
    "events", "acked_by", "tags", "status",
    "updated_at", "last_touched_by", "last_touched_in",
    "_applied_event_count",
}


def _try_merge_from_base(
    base: dict[str, Any],
    mine: dict[str, Any],
    theirs: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """3-way merge for a FOLD-sourced write. Returns merged task or None if unsafe.

    ``base``   — the fold body at read time (the merge base).
    ``mine``   — the command's edited body (read-modify-write result).
    ``theirs`` — the fresh mutable ``tasks/<id>.json`` body.

    WHY a 3-way merge and not the 2-way ``_try_merge``: in events-mode the body a
    command edited was reconstructed from a FOLD that may LAG the file (a missed
    best-effort event append, an old-CLI writer, a mixed fleet). The 2-way merge
    treats the ENTIRE local body as intentional, so an unchanged-but-stale fold
    field with a newer ``updated_at`` would clobber a newer file field — silent
    data loss (root cause A2). With the fold as base we can tell an unchanged
    field (stale read state → recover ``theirs``) from a real edit (keep
    ``mine``).

    Per non-event / non-acked / non-derived-tag / non-status scalar-or-dict
    field, over the UNION of base/mine/theirs keys minus the derived/volatile
    set:
      * mine == base, theirs != base  → take theirs (recover the newer file
        field; my unchanged copy was just stale read state).
      * mine != base, theirs == base  → take mine (my real edit).
      * both changed to the SAME value → that value.
      * both changed DIFFERENTLY      → conflict (None).

    ``status`` uses a transition policy evaluated against BASE: a remote-only
    status change (theirs != base, mine == base) must survive — a stale fold must
    never overwrite it; a local-only change wins; both changing away from base is
    a conflict. ``events`` and ``acked_by`` are UNIONed (acked_by never shrinks —
    a file ack the fold lacked is preserved). Derived ``tags`` are rebuilt from
    the merged fields afterward.
    """
    merged = copy.deepcopy(base)

    # --- status: transition policy evaluated against base ---
    base_status = base.get("status")
    mine_status = mine.get("status")
    theirs_status = theirs.get("status")
    mine_changed_status = mine_status != base_status
    theirs_changed_status = theirs_status != base_status
    if mine_changed_status and theirs_changed_status:
        if mine_status != theirs_status:
            return None  # both moved status away from base, differently → unsafe
        merged["status"] = mine_status  # both agreed on the same new status
    elif mine_changed_status:
        merged["status"] = mine_status      # my real transition
    elif theirs_changed_status:
        merged["status"] = theirs_status    # remote transition — must not be clobbered
    else:
        merged["status"] = base_status      # neither moved status

    # --- generic per-field 3-way over the key universe ---
    keys = (set(base) | set(mine) | set(theirs)) - _THREE_WAY_DERIVED_OR_VOLATILE
    for k in keys:
        b = base.get(k)
        m = mine.get(k)
        t = theirs.get(k)
        mine_changed = m != b
        theirs_changed = t != b
        if mine_changed and theirs_changed:
            if m == t:
                merged[k] = m          # both changed to the same value
            else:
                return None            # both changed differently → conflict
        elif mine_changed:
            merged[k] = m              # my real edit
        elif theirs_changed:
            merged[k] = t              # recover newer file field (mine was stale)
        else:
            merged[k] = b              # unchanged on both sides

    # --- events + acked_by union, then derived-tag repair ---
    # Reuse the 2-way helper: it unions events (dedup-by-`at`, sort, truncate)
    # and unions acked_by across the two dicts it is given. Folding the base in
    # too keeps any base-only event/ack that neither side carried. acked_by can
    # only GROW (set union), so a file ack the fold lacked is never dropped.
    base_event_times = {e["at"] for e in base.get("events", []) if "at" in e}
    mine_event_times = {e["at"] for e in mine.get("events", []) if "at" in e}
    theirs_event_times = {e["at"] for e in theirs.get("events", []) if "at" in e}
    # Two passes so all three sources contribute (the helper takes two dicts):
    # first union base into theirs-shaped merged, then union mine on top.
    _union_events_and_acked(merged, base, theirs,
                            base_event_times, theirs_event_times)
    _union_events_and_acked(merged, merged, mine,
                            {e["at"] for e in merged.get("events", []) if "at" in e},
                            mine_event_times)

    _repair_merged_tags(merged, mine, theirs)
    return merged


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
    def is_standard_tag(tag: str) -> bool:
        if tag.startswith("kind:"):
            return tag[5:] in schema.VALID_KINDS
        return tag.startswith(("workstream:", "agent:", "status:", "priority:"))

    extra = [
        tag
        for task in (local, remote_task, merged)
        for tag in (task.get("tags") or [])
        if not is_standard_tag(tag)
    ]
    merged["tags"] = schema.build_tags(
        status=merged.get("status", ""),
        workstream=merged.get("workstream", ""),
        agent=merged.get("owner_agent", ""),
        kind=schema._extract_kind_from_tags(merged.get("tags") or []),
        priority=merged.get("priority", ""),
        extra=extra or None,
    )
