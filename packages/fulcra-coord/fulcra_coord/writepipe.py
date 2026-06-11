"""Task write pipeline for fulcra-coord — the single mutation path onto the bus.

Every command that changes a task (start / update / block / pause / done / abandon /
tell / broadcast / assign / inbox-ack / request-review) converges here:
``_write_task_and_views`` uploads the task body under optimistic concurrency, merges
a concurrent peer's write when the pre-stat detects one (``_try_merge`` and its
field-carry / event-union / tag-repair helpers), rebuilds all views from the
summaries aggregate and uploads the ones whose content actually changed since
their last confirmed upload (the success-only fingerprint skip — see the
SKIP-UNCHANGED comment in ``_write_task_and_views``), keeps this session's
task pointer in sync
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
import hashlib
import json
import random
import uuid
from typing import Any, Optional

from . import cache, remote, schema, views, identity, session_link, env_int
from . import annotations as lifecycle_annotations
from . import eventlog, events as _events
from . import log as ops_log
from .io import (
    SUMMARIES_READ_ERROR,
    _confirmed_absent,
    _load_summaries_for_rebuild,
    _updated_at_key,
)
from .output import warn as _warn
from .timeutil import now_iso as _now_iso


# Event-only / acked_by-only keys are reconciled by the union helper, never by
# the wholesale field carry — copying them would clobber the union.
_MERGE_EVENT_KEYS = {"events", "acked_by"}


def _retry_sleep(seconds: float) -> None:
    """Jitter sleep before a task-body upload retry. A module-level wrapper (not
    an inline ``time.sleep``) ONLY so tests can patch it out and assert on the
    jitter — the same patch-point idiom as cli._retry_sleep (#141)."""
    import time
    time.sleep(seconds)


def _upload_task_body(
    task: dict[str, Any], task_path: str, *, backend: Optional[list[str]] = None
) -> bool:
    """Upload the AUTHORITATIVE task body, retrying ONCE on failure.

    WHY (2026-06-10, four losses in one evening): under backend write-throttling
    the single-write path (tell/later/done) intermittently failed its one upload
    attempt; the body never reached the bus while the sender saw success-shaped
    output, and recipients only got the message at a much-later reconcile. The
    reconcile-pool retry (#141 / FULCRA_COORD_UPLOAD_RETRY) covers VIEW uploads
    only — this is its single-write sibling for the task body, the one write
    that actually delivers the message.

    A RAISING upload is treated as a failed attempt, not an escape hatch: if it
    propagated, the caller's cached-locally / needs-reconcile contract would be
    bypassed entirely (same rationale as the view pool's S3 guard). The retry
    sleeps a 0.5–2.0s jitter first to de-sync from the burst that got us
    throttled. ``FULCRA_COORD_WRITE_RETRY`` (default 1) set to ``0`` restores
    the single attempt. A second failure is final — the caller falls through to
    today's unchanged cached-locally path.
    """
    try:
        ok = remote.upload_json(task, task_path, backend=backend)
    except Exception:
        ok = False
    if not ok and env_int("FULCRA_COORD_WRITE_RETRY", 1) != 0:
        _retry_sleep(random.uniform(0.5, 2.0))
        try:
            ok = remote.upload_json(task, task_path, backend=backend)
        except Exception:
            ok = False
    return ok


def _fail_write_pre_read_error(
    task: dict[str, Any], op_id: str, command: str
) -> bool:
    """The pre-write read FAILED and absence could not be confirmed: refuse to
    write. Returns False (the caller's existing cached-locally contract).

    WHY (2026-06-11 write-path read-error audit, F1): writing anyway is the
    blind-LWW disaster — agent A holds a stale body, agent B's ``done`` is on
    the bus, A's pre-read 504s, and A's upload reverts B's transition with no
    signal anywhere. Failing the write is strictly safer: the body is cached
    locally and the failed/needs_reconcile marker hands it to cmd_reconcile's
    body-repair pass, which replays it MERGE-AWARE (the C2 fix: download the
    current remote, _try_merge, never clobber a newer transition). So the edit
    is deferred-and-merged instead of delivered-by-clobbering.

    Deliberately ``return False`` and NOT ``raise NeedsReconcile``: every
    NeedsReconcile handler in lifecycle.py treats the exception as "the TASK
    BODY landed, only views need repair" (some return success and fire the
    directive dual-write on it). Raising it BEFORE any upload would make a
    failed write report as delivered — the exact lie this fix exists to stop.
    """
    cache.write_cached_task(task)  # the self-heal source for the marker replay
    op_marker = {
        "op_id": op_id,
        "command": command,
        "task_id": task["id"],
        "status": "failed",
        "needs_reconcile": True,
        "started_at": _now_iso(),
    }
    cache.write_op_marker(op_id, op_marker)
    ops_log.log_op(command, task["id"], status="error",
                   error="Pre-write read failed and absence is unconfirmed — "
                         "refusing to write blind; cached for reconcile")
    _warn(
        f"WRITE DEFERRED: {task['id']} — the remote body could not be read and "
        f"the bus could not confirm it is absent. Writing blind could revert a "
        f"concurrent agent's transition, so the body is cached locally; run "
        f"'fulcra-coord reconcile' to merge-and-deliver it."
    )
    return False


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
    """Upload task + the views whose content changed. Returns True on full success.

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

    # Provenance hand-off from the read (root cause A2). When the body this write
    # carries was reconstructed from a COMPLETE fold (events-mode), the fold may
    # LAG the file: an unchanged-but-stale fold field would otherwise clobber a
    # newer file field with no stat-change signal (stat is the FILE's, which the
    # fold body's read also cached, so stat_changed is False — the silent
    # data-loss path). For a fold-sourced write we therefore ALWAYS download the
    # fresh file and 3-way-merge it against the fold-at-read base, regardless of
    # stat. A file-sourced (or provenance-absent) write keeps the EXISTING
    # 2-way stat-change merge check unchanged.
    prov = cache.read_provenance(task_id)
    if prov and prov.get("source") == "fold" and prov.get("fold_complete"):
        fresh = remote.download_json(task_path, backend=backend)
        if fresh:
            merged = _try_merge_from_base(prov.get("fold_base") or {}, task, fresh)
            if merged is None:
                ops_log.log_op(command, task_id, status="conflict",
                               error="Unsafe 3-way merge — fold-sourced write vs newer file")
                raise schema.ConflictError(
                    f"Remote task {task_id} changed and fold-sourced merge is "
                    f"unsafe. Run 'fulcra-coord reconcile' to repair."
                )
            task = merged
        elif pre_stat is not None or not _confirmed_absent(task_path, backend=backend):
            # 2026-06-11 write-path read-error audit (F1, fold branch): a None
            # download used to read as "file gone, nothing to merge against" —
            # but None is ALSO what a failed read returns, and a fold-sourced
            # body is exactly the kind that may LAG the file. A visible
            # pre-stat proves the file EXISTS (the read failed); an
            # unreachable bus proves nothing. Either way, uploading the fold
            # body as-is would blind-LWW over whatever we couldn't read.
            return _fail_write_pre_read_error(task, op_id, command)
        # else: pre_stat missed, the download missed, AND a follow-up stat
        # missed while the bus probes reachable — absence CONFIRMED (the file
        # really is gone). Keep `task` as-is: nothing to merge against.
    else:
        # Pre-read disambiguation (2026-06-11 write-path read-error audit, F1):
        # stat -> None means EITHER "the file does not exist yet" (new task)
        # OR "the stat failed after retry" (504 weather) — and the no-CAS
        # transport cannot tell us which. The old code assumed the former and
        # skipped the merge check, producing this failure sequence: agent A
        # holds a stale task; agent B lands `done`; A runs `update`; A's
        # pre-stat 504s -> merge check skipped -> A's upload silently reverts
        # B's transition (blind LWW). So a None stat now costs one body
        # download: a readable body forces the merge check; a missing body is
        # trusted as ABSENT only when probe_reachable() confirms the bus is
        # answering (the role_ops C1 idiom — the probe spawn is spent only on
        # this rare path, never on the happy path).
        fresh: Optional[dict[str, Any]] = None
        if pre_stat is None:
            fresh = remote.download_json(task_path, backend=backend)
            if fresh:
                needs_merge_check = True   # the file exists; stat lied/failed
            elif _confirmed_absent(task_path, backend=backend):
                needs_merge_check = False  # absence CONFIRMED: genuinely new
            else:
                return _fail_write_pre_read_error(task, op_id, command)
        else:
            # Trigger merge/conflict check when:
            # - we have a cached baseline and it differs from the current remote (normal case), OR
            # - we have NO cached baseline but the file already exists remotely (fresh machine
            #   that loaded the task via _load_task or _load_all_tasks but never previously wrote
            #   it — unknown whether another agent updated it since we loaded it).
            # Skipping this check when cached_meta is None would silently overwrite concurrent
            # remote changes from other agents on cross-machine sessions.
            needs_merge_check = (
                cached_meta is None or remote.stat_changed(cached_meta, pre_stat)
            )
        if needs_merge_check:
            if fresh is None:
                fresh = remote.download_json(task_path, backend=backend)
            if not fresh:
                # The pre-stat SAW the file (this branch is unreachable for the
                # stat-None path, which always carries a body here) but its
                # body could not be read: READ_ERROR, never "nothing to merge
                # against" — proceeding would be the same blind overwrite (F1).
                return _fail_write_pre_read_error(task, op_id, command)
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

    # Cache the body BEFORE the upload attempt (2026-06-11 live find, the
    # 15 unreplayable "no cached body to replay" zombie markers): this write
    # used to happen only AFTER a successful upload, so the failed-upload
    # return below left a failed/needs_reconcile marker with NO replay asset —
    # cmd_reconcile's body-repair pass had nothing to upload and the marker
    # re-failed every tick. Bit hardest on callers that create the task
    # straight through this pipeline with no pre-caching of their own
    # (request-review's escalated-to-human path was the live source). Caching
    # first makes every marker replayable by construction; on success the
    # cached copy is byte-identical to what landed (``task`` is final past the
    # merge above), so the happy path's cache state is unchanged.
    cache.write_cached_task(task)

    # Upload task file — the AUTHORITATIVE write (one jittered retry inside;
    # the view/event/directive side-writes below stay best-effort/unchanged).
    task_ok = _upload_task_body(task, task_path, backend=backend)
    if not task_ok:
        op_marker["status"] = "failed"
        op_marker["needs_reconcile"] = True
        cache.write_op_marker(op_id, op_marker)
        ops_log.log_op(command, task_id, status="error", error="Task upload failed")
        return False

    # Post-stat for version tracking. This stat DOUBLES as verify-after-write:
    # the 2026-06-10 losses showed an upload can return success-shaped output
    # with nothing on the bus, so "upload returned True" is not proof of
    # delivery — a visible stat is. Reusing the version-tracking stat keeps the
    # fast path at exactly one post-upload round-trip (no extra HEAD).
    post_stat = remote.stat(task_path, backend=backend)
    unverified = False
    if post_stat is None and env_int("FULCRA_COORD_WRITE_VERIFY", 1) != 0:
        # UNVERIFIED: stat can't see the file (absent, or stat itself failed —
        # indistinguishable, and both mean we cannot claim delivery). One more
        # jittered re-upload + re-stat, then warn. Gated by
        # FULCRA_COORD_WRITE_VERIFY (default ON) so a backend whose stat is
        # flaky-by-design can opt out without losing the upload retry above.
        _retry_sleep(random.uniform(0.5, 2.0))
        try:
            remote.upload_json(task, task_path, backend=backend)
        except Exception:
            pass  # the re-stat below is the arbiter, not the upload's claim
        post_stat = remote.stat(task_path, backend=backend)
        if post_stat is None:
            unverified = True
            # UNMISSABLE by contract: the sender must SEE that the recipient
            # may not. But NEVER flip the exit code — the body is cached
            # locally and the needs_reconcile marker (kept below) lets the
            # standard reconcile self-heal repair it, exactly like the
            # cached-locally failure path.
            _warn(
                f"DELIVERY NOT CONFIRMED: {task_id} — upload reported success "
                f"but the write is not visible on the bus. Body cached "
                f"locally; recipients may not see it until the next "
                f"successful reconcile."
            )
    if post_stat:
        cache.write_meta(task_path, post_stat)

    # Drop the read->write provenance now that the body has landed: a LATER
    # file-sourced write of this task must not inherit a stale fold provenance
    # and force a spurious 3-way merge. Best-effort (clear is ignore-missing).
    try:
        cache.clear_provenance(task_id)
    except Exception:
        pass

    _stamp_session_pointer(task)
    # (The cached-task write moved ABOVE the upload — see the unreplayable-
    # marker comment there; ``task`` has not changed since, so nothing is lost.)

    # Regenerate all views from the compact summaries aggregate, NOT re-fetched
    # task bodies. build_all_views produces identical output from task_summary
    # dicts as from full bodies (guarded by the equivalence test), so the
    # authoritative ``views/summaries.json`` (one download) plus the just-written
    # task's own summary upserted in is a complete, current view source. This is
    # the write-path half of the perf refactor: it removes the per-task body
    # fetch loop (~N round-trips) that _load_all_tasks performed on every write.
    #
    # BACKWARD COMPAT: a bus that predates the aggregate has no summaries.json.
    # When that ABSENCE is confirmed, _load_summaries_for_rebuild falls back to
    # the old _load_all_tasks path (correctness over speed) — a fresh machine
    # that ran only _load_task() still pulls every remote task before building
    # views, so no task is silently dropped. The current task is already cached
    # (line above), so it is always part of the rebuilt set either way.
    rebuild_source = _load_summaries_for_rebuild(task, backend=backend)
    if rebuild_source is SUMMARIES_READ_ERROR:
        # 2026-06-11 write-path read-error audit (F2): NO trustworthy rebuild
        # source could be read (the aggregate exists but is unreadable, or the
        # fallback full load degraded to local cache because the index is
        # unreadable). Rebuilding views from whatever this host happens to have
        # cached and uploading them with a fresh generated_at would silently
        # BLANK the bus's read surface — the stale-view guard cannot catch
        # fresh-but-truncated. The task BODY already landed above (the
        # transition is real), so this is exactly a partial write: keep the
        # needs_reconcile marker, emit the lifecycle moment (BUG 10 contract),
        # and raise NeedsReconcile so reconcile — which refuses the same
        # degraded source (F3) — repairs the views once it can see the bus.
        op_marker["status"] = "partial"
        op_marker["needs_reconcile"] = True
        op_marker["views_skipped"] = "rebuild_source_read_error"
        cache.write_op_marker(op_id, op_marker)
        ops_log.log_op(command, task_id, status="partial",
                       detail="Task written; view rebuild skipped — the "
                              "summaries/index rebuild source could not be "
                              "read (would truncate views)")
        _emit_lifecycle(command, task, lifecycle, backend=backend)
        raise schema.NeedsReconcile(
            f"Task {task_id} written, but the view-rebuild source could not "
            f"be read — views were left untouched instead of being rebuilt "
            f"from a partial set. Run 'fulcra-coord reconcile' to repair."
        )
    all_views = views.build_all_views(rebuild_source)

    # Upload views CONCURRENTLY (P1): remote.upload_json is thread-safe (each
    # call writes a unique tempfile + runs an independent subprocess; remote.py
    # holds no shared mutable state), so a small thread pool collapses the
    # sequential view uploads into one round-trip's wall-time. Semantics are
    # preserved exactly: per-view success is collected, any failure lands in
    # view_failures, and the partial-upload handling below is unchanged. Local
    # cache writes happen in the main thread after the futures resolve.
    #
    # SKIP-UNCHANGED (2026-06-10/11 incident): build_all_views regenerates the
    # WHOLE view set — on the live bus ~55 views (per-agent views for 33
    # identities, inboxes, workstreams, board), a fan-out that scales with
    # fleet size while a tell/update/done actually changes ~5 of them. Under
    # backend 504-weather (1-16s per op) those 50 redundant uploads turned
    # EVERY write into "Task written, views failed: [~50 names]" ->
    # NeedsReconcile, and reconcile's same-shaped repair burst couldn't drain
    # (backlog 67->95 in three runs). So: upload ONLY the views whose content
    # digest differs from the last CONFIRMED upload's fingerprint.
    #
    # THE TRAP that makes the fingerprint store necessary: the cached-view
    # write below runs for EVERY view regardless of upload success (so local
    # readers see the freshest build) — therefore "content == cached view"
    # does NOT mean "remote is current"; a previous failed upload poisons that
    # inference. The fingerprint is written ONLY after a confirmed upload
    # (never on failure), so a failed view keeps mismatching and is retried on
    # the next write. FULCRA_COORD_VIEW_SKIP_UNCHANGED=0 restores the old
    # upload-everything behavior.
    #
    # DIVISION OF LABOR with reconcile (2026-06-11 review finding): even a
    # success-only fingerprint proves only what THIS HOST last uploaded — it
    # can never prove the remote's CURRENT content, because the store has no
    # compare-and-swap and views are shared mutable paths another host can
    # overwrite after our digest was recorded. The write path accepts that
    # bounded staleness: it is the hot path, it skips unchanged views (the
    # ~10x fan-out cut), and it is fully correct in the single-host case.
    # Reconcile (cmd_reconcile's view pool) is the cross-host drift REPAIR: it
    # always re-uploads every rebuilt view, never honoring the skip, so a view
    # clobbered by another host is re-asserted within one reconcile cadence
    # (~20 min). Do not "optimize" reconcile back onto this skip — that would
    # make cross-host clobbers permanent (the repair path would skip too).
    view_items = list(all_views.items())
    view_failures = []
    skip_unchanged = _view_skip_enabled()
    view_digests: dict[str, str] = {}
    to_upload: list[tuple[str, dict[str, Any]]] = []
    for view_name, view_data in view_items:
        digest = _view_fingerprint(view_data)
        view_digests[view_name] = digest
        if (skip_unchanged
                and not _view_must_always_upload(view_data)
                and cache.read_view_fingerprint(view_name) == digest):
            # Confirmed on the remote and content-identical: skip the upload
            # (no subprocess). The local cache write below still runs.
            continue
        to_upload.append((view_name, view_data))

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

    max_workers = min(8, len(to_upload)) or 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for view_name, ok in pool.map(_upload_one, to_upload):
            if ok:
                # SUCCESS-ONLY fingerprint write (main thread): the next write
                # may skip this view. On failure the stale/absent fingerprint
                # is deliberately kept so the next write re-attempts even if
                # the content hasn't changed since (the poisoned-cache case).
                cache.write_view_fingerprint(view_name, view_digests[view_name])
            else:
                view_failures.append(view_name)

    # Cache every view locally regardless of upload outcome — INCLUDING the
    # skipped ones (matches prior behavior: the old loop wrote the cache for
    # every view, success or not, so local readers always see the freshest
    # build). This is exactly why the cache can't drive the skip decision.
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

    if unverified:
        # Keep the marker alive with needs_reconcile so the standard reconcile
        # pass owns the repair — the same self-heal vehicle as a failed upload,
        # because an unverified write IS possibly a failed upload that lied.
        op_marker["status"] = "unverified"
        op_marker["needs_reconcile"] = True
        cache.write_op_marker(op_id, op_marker)
        ops_log.log_op(command, task_id, status="unverified",
                       error="Upload reported success but post-write stat "
                             "could not confirm the body on the bus")
    else:
        op_marker["status"] = "done"
        cache.write_op_marker(op_id, op_marker)
        cache.clear_op_marker(op_id)
        ops_log.log_op(command, task_id, status="ok")

    _emit_lifecycle(command, task, lifecycle, backend=backend)

    # Event dual-write: also append an immutable event mirroring this
    # mutation. BEST-EFFORT — never fail the task write on an event-log error
    # (the mutable file stays authoritative). The reconcile
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
            # The payload IS the full task snapshot — the entire task
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
        # failure in the ops log — the parity pass audits the
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


# Top-level per-rebuild stamps the view builders write on EVERY rebuild
# (views.build_all_views stamps a fresh `now` each call). They change even when
# the view CONTENT is identical, so the skip-unchanged fingerprint must exclude
# them — hashing the raw upload bytes would never match across rebuilds and the
# skip would be a permanent no-op. Top-level keys only: a task's own
# `updated_at` nested inside a view's summaries is real content and is hashed.
_VIEW_VOLATILE_STAMP_KEYS = {"updated_at", "generated_at"}


def _view_fingerprint(view_data: dict[str, Any]) -> str:
    """sha256 hexdigest of a view's CONTENT as upload_json would send it.

    Uses remote.serialize_json — the exact serialization upload_json puts on
    the wire — over the view with only the top-level per-rebuild stamps
    excluded. Sharing the serializer is load-bearing: if fingerprint and
    upload serialization drifted (indent/sort_keys), skipping would silently
    break (uploads forever, or worse, a real change mis-read as unchanged).
    Guarded byte-for-byte by test_view_skip's drift test."""
    payload = {k: v for k, v in view_data.items()
               if k not in _VIEW_VOLATILE_STAMP_KEYS}
    return hashlib.sha256(
        remote.serialize_json(payload).encode("utf-8")).hexdigest()


def _view_skip_enabled() -> bool:
    """The one escape hatch: FULCRA_COORD_VIEW_SKIP_UNCHANGED=0 restores the
    upload-every-view-every-write behavior (default 1 = skip unchanged)."""
    return env_int("FULCRA_COORD_VIEW_SKIP_UNCHANGED", 1) != 0


def _view_must_always_upload(view_data: dict[str, Any]) -> bool:
    """True for views that must upload even when content is unchanged.

    A view carrying a top-level ``generated_at`` (today: the summaries
    aggregate) is a FRESHNESS BEACON: the stale-view read guard
    (views.view_staleness_minutes / FULCRA_COORD_VIEW_STALE_MIN) treats an
    aging stamp as "this view may be lying" and pushes every reader onto the
    slow direct-listing fallback. Skipping its upload on a quiet bus would age
    the stamp with perfectly-current content and trip that guard fleet-wide —
    so the beacon always re-uploads (1 upload of the ~55, a fine price)."""
    return "generated_at" in view_data


def _view_name_to_remote(name: str) -> str:
    # (The "agents/<id>" branch was removed with the 2026-06-11 retirement of
    # the per-agent views — build_all_views no longer emits those names.)
    if name == "index":
        return remote.view_remote_path("index")
    if name.startswith("workstreams/"):
        ws = name[len("workstreams/"):]
        return remote.workstream_remote_path(ws)
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
    # 2026-06-11 bug hunt S8: events are bus data and can arrive malformed —
    # hard-indexing e["at"] KeyError-ed the merge MID-WRITE on a single
    # at-less event. Skip-on-missing here; the union below keeps such events
    # with sentinel ordering (oldest) so nothing is silently lost.
    local_event_times = {e["at"] for e in local.get("events", []) if "at" in e}
    remote_event_times = {e["at"] for e in remote_task.get("events", [])
                          if "at" in e}

    if local_status != remote_status:
        # S8 sentinel choice for the transition detection: an at-less
        # status-shaped event cannot be ordered against the other side, so it
        # reads as ancient/shared — NEVER as evidence of a new transition.
        # The alternative (counting it as new) would manufacture spurious
        # both-sides-changed conflicts out of malformed data.
        local_has_new_status_change = any(
            e.get("type") in schema.VALID_STATUSES
            and "at" in e and e["at"] not in remote_event_times
            for e in local.get("events", [])
        )
        remote_has_new_status_change = any(
            e.get("type") in schema.VALID_STATUSES
            and "at" in e and e["at"] not in local_event_times
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
        newer = remote_task
    else:
        newer = local

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
        # A key absent from a side is stale-read/absent state, NOT a deletion: an
        # older-CLI / mixed-fleet writer that omits a key must not null a field the
        # other side legitimately carries (root cause A: silent data loss). Only
        # honor a change when the key actually EXISTS on that side.
        mine_changed = (k in mine) and (m != b)
        theirs_changed = (k in theirs) and (t != b)
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

    # These fields are excluded from the conflict loop because point-in-time
    # fold/file values naturally differ on every write; still, the successful
    # merged write must publish the command's fresh touch metadata, not the stale
    # fold base.
    for key in ("updated_at", "last_touched_by", "last_touched_in"):
        if key in mine:
            merged[key] = mine[key]
        elif key in theirs:
            merged[key] = theirs[key]

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
    acked_by. Idempotent regardless of which side `merged` started from.

    2026-06-11 bug hunt S8: an event missing ``at`` (malformed bus data) used
    to KeyError this union mid-write. Such an event cannot be time-deduped or
    time-ordered, so it is kept with SENTINEL ordering — treated as OLDEST
    (placed first, and therefore dropped first by the inline cap) and deduped
    by its JSON shape so the union stays idempotent and deterministic."""
    by_time: dict[str, dict[str, Any]] = {}
    for ev in local.get("events", []):
        if "at" in ev:
            by_time[ev["at"]] = ev
    for ev in remote_task.get("events", []):
        if "at" in ev:
            by_time.setdefault(ev["at"], ev)
    atless: list[dict[str, Any]] = []
    seen_atless: set[str] = set()
    for ev in list(local.get("events", [])) + list(remote_task.get("events", [])):
        if "at" not in ev:
            key = json.dumps(ev, sort_keys=True, default=str)
            if key not in seen_atless:
                seen_atless.add(key)
                atless.append(ev)
    events = atless + sorted(by_time.values(), key=lambda e: e["at"])
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
    primary_kind = schema._extract_kind_from_tags(merged.get("tags") or [])
    # 2026-06-11 bug hunt C7 (mirrors apply_transition's _secondary_kinds
    # carry): every non-primary kind: tag from either side must survive the
    # rebuild. Multi-kind membership is routing-load-bearing, and a SECOND
    # standard kind (kind:feature beside kind:ops) is excluded from `extra`
    # above precisely because it IS a standard tag — without this explicit
    # carry it vanished from the merged task. build_tags dedups via
    # sorted(set(...)), so overlap with `extra` is harmless.
    secondary_kinds = [
        tag
        for task in (local, remote_task, merged)
        for tag in (task.get("tags") or [])
        if tag.startswith("kind:") and tag != f"kind:{primary_kind}"
    ]
    merged["tags"] = schema.build_tags(
        status=merged.get("status", ""),
        workstream=merged.get("workstream", ""),
        agent=merged.get("owner_agent", ""),
        kind=primary_kind,
        priority=merged.get("priority", ""),
        extra=(extra + secondary_kinds) or None,
    )
