"""Task lifecycle + directive commands for fulcra-coord.

The bus-mutation command surface: the directive creators (``tell`` / ``broadcast``
/ ``assign``) and the task state-machine transitions (``start`` / ``update`` /
``block`` / ``pause`` / ``done`` / ``abandon``). Each validates its transition via
schema, then writes through the single ``_write_task_and_views`` pipeline.

Extracted from cli.py behind stable re-exports; depends only on lower layers (the
write pipeline, the io loader, the routing escalation, the presence onboarding hint
+ guarded roster loader, plus cache/remote/schema/views/identity/annotations and
the output leaf utils) and
never imports cli, so the split has no cycle. ``_derive_agent`` is a thin stateless
alias over ``identity.resolve_agent``; cli keeps its own identical copy for the
commands that remain there, so duplicating the one-liner avoids a back-import.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

# NB: `remote` looks unused since the raw presence-aggregate download for
# --route-capability moved behind presence._load_presence_agents, but it is a
# LOAD-BEARING test patch surface: the directive dual-write tests monkeypatch
# `lifecycle.remote.upload_json` to fail the transport under this module's
# commands. Keep it bound here.
from . import cache, remote, routing, schema, views, identity, continuity  # noqa: F401
from . import annotations as lifecycle_annotations
from .io import _load_task
from .output import info as _info, warn as _warn, err as _err
from .writepipe import _write_task_and_views
from .routing_ops import _escalate_review_to_human
from .presence import _load_presence_agents, _maybe_warn_legacy_identity


def _derive_agent() -> str:
    return identity.resolve_agent()

#: A title that LOOKS like a task id (``TASK-YYYYMMDD-…``). When ``start`` is
#: handed one of these the operator almost certainly meant to CLAIM/activate the
#: existing task, not create a new one named after an id. Only the prefix is
#: matched (date-stamped ``TASK-<8 digits>-``) so a genuine title that merely
#: mentions a date can't trip it.
_TASK_ID_TITLE_RE = re.compile(r"^TASK-\d{8}-")


def cmd_tell(args: Any, backend: Optional[list[str]] = None,
             *, marker_tag: Optional[str] = None,
             task_fields: Optional[dict[str, Any]] = None) -> int:
    """Create a directive task addressed at another agent (sugar over `start`).

    A directive is a `proposed` task with assignee=<the target agent> and
    owner_agent = --from (the directing agent) or unset. It lands in the target's
    inbox until they ack or claim it.

    ``marker_tag`` (internal, for sugar commands like ``later``): an extra
    loop-kind membership tag (e.g. ``routing.IDEA_TAG``) appended to the task's
    tags so the directive dual-write maps the right loop kind — the same
    marker-tag pattern routing_ops uses for ``REVIEW_TAG``.

    ``task_fields`` (internal, for sugar commands like ``handoff``): extra
    payload fields set on the task AFTER construction — the same post-make_task
    pattern request-review uses for ``task["pr"]``. Exists so a sugar command
    can ride this single creation/validation/upload/dual-write path instead of
    forking its own copy just to add one payload key.
    """
    assignee = getattr(args, "assignee", None)
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

    # --route-capability: resolve a LIVE recipient at send time (the general
    # route-to-live primitive that request-review is the first consumer of)
    # instead of a fixed assignee. Pool = every presence agent declaring the
    # capability; ranked by liveness via the same resolver reviews use. On a
    # miss we escalate to the human (same surface as request-review) rather than
    # parking the directive on a dead agent. Best-effort: any failure escalates.
    route_capability = getattr(args, "route_capability", None)
    if route_capability:
        try:
            # Staleness-guarded roster read (falls back to per-agent records
            # when the aggregate lags under backend throttling), so a live
            # capability-holder is never skipped for a stale last_seen.
            presence = _load_presence_agents(backend=backend)
        except Exception:
            presence = []  # treat as no live candidate -> escalate
        pool = [r["agent"] for r in presence
                if route_capability in (r.get("capabilities") or []) and r.get("agent")]
        winner = views.resolve_live_recipient(
            pool, presence, floor=getattr(args, "floor", "idle") or "idle")
        if winner is None:
            # No live agent declares the capability — land it on the human's
            # plate the same way a review-routing miss does (needs:human surface).
            _escalate_review_to_human(
                pr=title, repo=workstream, tried=sorted(pool), backend=backend)
            _info(f"No live '{route_capability}' agent — escalated to human.")
            return 0
        assignee = winner
        _info(f"Routed to live '{route_capability}' agent: {winner}")

    # Either a fixed assignee or a resolved --route-capability winner is now
    # required; without one there is no directive to create.
    if not assignee:
        _err("tell requires a recipient: pass ASSIGNEE or --route-capability CAP.")
        return 1

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

    if marker_tag:
        # Mirror routing_ops's REVIEW_TAG append: an EXTRA membership tag the
        # dual-write mapper reads — never the task's ``kind`` field.
        task["tags"] = sorted(set(task.get("tags", []) + [marker_tag]))
    if task_fields:
        # Sugar-payload fields (e.g. handoff's checkpoint_ref) — set verbatim,
        # post-construction, exactly like request-review's task["pr"].
        task.update(task_fields)
    if getattr(args, "expects_response", False):
        # --expects-response makes this an ASK, not an FYI: the dispatch marker
        # makes the dual-write mirror it as an OPEN kind=dispatch loop
        # (assigned, SLA-tracked) that only a bus `respond` closes. tell-only —
        # broadcast never grows this flag (fan-out FYI must not open N loops).
        task["tags"] = sorted(set(task.get("tags", []) + [routing.DISPATCH_TAG]))

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
        # The TASK BODY landed (NeedsReconcile is raised only after the body
        # uploaded — see writepipe), so the dual-write mirror should still fire.
        _warn(str(e))
        _dual_write_directive(task, command="tell", backend=backend)
        return 0

    if ok:
        _dual_write_directive(task, command="tell", backend=backend)
        _info(f"\nDirective created: {task['id']} -> {assignee}")
        return 0
    _warn(f"Directive cached locally but remote upload failed: {task['id']}.")
    return 1


def _dual_write_directive(
    task: dict[str, Any], *, command: str, backend: Optional[list[str]] = None
) -> None:
    """Directive dual-write: ADDITIVELY mirror a directive-creating task into
    a first-class ``directives/<id>.json`` loop record.

    BEST-EFFORT — this NEVER fails or alters the authoritative task write.
    It is called only AFTER the task body has already landed, so that write
    is committed regardless of what happens here. Any failure (a raising or
    False-returning upload, a mapping error) is swallowed and recorded in the
    ops log as ``directive_write_failed`` so the parity pass can audit misses
    — exactly mirroring the event-append best-effort posture in writepipe.

    The TASK record stays authoritative for task state; the mirrored loop
    records carry coordination state (board / digest / review-done / the
    reconcile health and parity passes read them).

    ``directives`` is lazy-imported inside the function to avoid paying its
    import cost on every lifecycle import and to keep the module-load graph flat
    (no eager cycle risk during package init).

    The upload + ops-log-on-miss logic lives in the low-layer
    ``directives.dual_write`` so tell/broadcast (here) and the routing_ops
    commands (assign/request-review/review-done) share ONE writer. This function
    is the thin lifecycle-facing entry point cmd_tell/cmd_broadcast call; it
    delegates so there is a single best-effort implementation to audit, not two
    divergent copies.
    """
    from . import directives  # lazy import: avoid import cost / cycle at module load
    directives.dual_write(task, command=command, backend=backend)


def cmd_later(args: Any, backend: Optional[list[str]] = None) -> int:
    """Capture a "do later" item as a backlog idea ON THE BUS (sugar over `tell`).

    Operator requirement (2026-06-10): when the operator hands an agent a
    deferred task, the agent must put it on the bus — the bus is where backlog
    lives, so it stays portable across sessions and agents instead of dying
    with one session's memory.

    WHY the ``@backlog`` audience: it is a ROLE audience that nobody holds —
      * the item sits DURABLY on the bus (a real proposed task), and because
        role matching delivers ``@<role>`` only to agents that DECLARED the
        role (#128), it inbox-spams NOBODY;
      * it stays VISIBLE: the board's ideas_pipeline counts it (the dual-written
        directive is a kind=idea captured loop) and `search` finds it;
      * a future backlog-groomer agent can `connect --role backlog` and receive
        the entire backlog in its inbox — no migration needed.

    Routing a backlog item to a worker later is the EXISTING
    ``assign TASK-ID <agent>`` (an audience change that already re-dual-writes);
    a concrete assignee folds the idea loop's state captured→routed.

    Implemented by pinning assignee=@backlog + the kind:idea marker tag and
    delegating to cmd_tell, so capture shares the one creation/validation/
    upload/dual-write path (the cmd_broadcast pattern). Backlog-flavored
    defaults applied here (not in the parser) so direct callers get them too:
    workstream "general", priority P3 (it's deferred work by definition).
    """
    args.assignee = routing.BACKLOG_AUDIENCE
    if not getattr(args, "workstream", None):
        args.workstream = "general"
    if not getattr(args, "priority", None):
        args.priority = "P3"
    return cmd_tell(args, backend=backend, marker_tag=routing.IDEA_TAG)


def cmd_handoff(args: Any, backend: Optional[list[str]] = None) -> int:
    """Hand work to another agent/role WITH its resume state (sugar over
    ``tell``, the way ``later`` is).

    Spec 2026-06-10 (continuity integration): a handoff is a ``kind=dispatch``
    expects_response loop whose payload carries ``checkpoint_ref`` — the
    producing session's continuity checkpoint. The recipient's pickup surfaces
    the ref (and, when the optional ``fulcra-continuity`` CLI is installed,
    the rendered resume brief); closing the loop = the work continued. This is
    the ArcBot always-on backbone: a respawned session is only useful if it
    can resume the dead one's work, and the bus — not a hand-rolled
    ``.session-resume.md`` — is where that state must travel.

    ``--checkpoint`` accepts EITHER an opaque ref (forwarded verbatim — coord
    never parses refs) OR a local checkpoint JSON file. The local-file case
    exists because the fulcra-continuity CLI writes local paths only (the
    verified storage reality): a local path is useless on another host, so we
    PUBLISH it to the remote ``continuity/...`` tree via coord's bridge and
    carry the remote ARCHIVE path as the ref. If that publish fails, the
    checkpoint body rides INLINE in the loop payload (small docs are fine
    inline) so a transport blip can't strand the handoff.

    Implemented by delegating to cmd_tell with the DISPATCH marker +
    expects_response (the ``tell --expects-response`` machinery), so handoff
    shares the one creation/validation/upload/dual-write path."""
    to = getattr(args, "to", None)
    if not to:
        _err("handoff requires a recipient: pass --to <agent|@role>.")
        return 1

    task_fields: dict[str, Any] = {}
    ref = getattr(args, "checkpoint", None)
    if ref:
        try:
            is_local = Path(ref).is_file()
        except Exception:
            is_local = False
        if is_local:
            remote_ref, body = continuity.publish_checkpoint_file(
                ref, backend=backend)
            if remote_ref:
                _info(f"Checkpoint published to bus: {remote_ref}")
                ref = remote_ref
            elif body is not None:
                # Publish failed but the body is readable: carry it inline so
                # the recipient can still resume; keep the local path as the
                # (provenance-only) ref.
                _warn("Checkpoint publish to the bus failed — carrying the "
                      "checkpoint INLINE in the loop payload.")
                task_fields["checkpoint_inline"] = body
            else:
                _warn(f"--checkpoint {ref} is not readable JSON — forwarding "
                      "the ref opaquely.")
        task_fields["checkpoint_ref"] = str(ref)

    args.assignee = to
    args.expects_response = True   # a handoff is an ASK by definition
    if not getattr(args, "workstream", None):
        args.workstream = "general"
    return cmd_tell(args, backend=backend,
                    task_fields=task_fields or None)


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
    # Directive dual-write: assignment sets/redirects the directive audience. This
    # is an LWW-snapshot OVERWRITE of directives/<id>.json (storage model A): a
    # re-assign re-writes the same record with the new audience, so the directive
    # always reflects the latest assignee. Best-effort — never fails the task write.
    _dual_write_directive(task, command="assign", backend=backend)
    _info(f"Assigned {task_id} -> {assignee}")
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

    # Continuity resume at pickup (spec 2026-06-10): a CLAIM of a handoff
    # directive is exactly the moment the recipient needs the producer's
    # where-I-left-off, so surface the checkpoint ref — and, when the optional
    # fulcra-continuity CLI is installed (probed inside the helper), the
    # rendered resume brief. ONLY on a genuine pickup (not progress notes):
    # re-printing the brief on every update would be noise. Fully best-effort
    # and guarded — a brief problem must never fail a successful claim.
    if lifecycle == "pickup" and task.get("checkpoint_ref"):
        ref = task["checkpoint_ref"]
        _info(f"  Continuity checkpoint: {ref}")
        try:
            brief = continuity.render_brief_via_cli(task["checkpoint_inline"]) \
                if task.get("checkpoint_inline") \
                else continuity.render_brief_for_ref(ref, backend=backend)
            if brief:
                _info("  Resume brief:")
                for line in brief.rstrip("\n").splitlines():
                    _info(f"    {line}")
        except Exception:
            pass  # the ref line above is the floor; the brief is best-effort

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

    if getattr(args, "snapshot", False):
        checkpoint = continuity.make_checkpoint(
            task,
            agent=agent,
            reason="pause",
            next_actions=[next_action],
        )
        snap_ok, snap_path = continuity.write_checkpoint(checkpoint, backend=backend)
        if snap_ok:
            _info(f"  Continuity snapshot: {checkpoint['checkpoint_id']}")
            _info(f"  Snapshot path: {snap_path}")
        else:
            _warn("Task paused, but continuity snapshot upload failed.")

    _info(f"Paused: {task_id}")
    _info(f"  Next: {next_action}")
    return 0


def cmd_snapshot(args: Any, backend: Optional[list[str]] = None) -> int:
    """Write a Fulcra Continuity checkpoint without changing task state."""
    task_id = args.task_id
    agent = getattr(args, "agent", None) or _derive_agent()

    task = _load_task(task_id, backend=backend)
    if task is None:
        _err(f"Task not found: {task_id}")
        return 1

    next_action = getattr(args, "next", None)
    next_actions = [next_action] if next_action else None
    checkpoint = continuity.make_checkpoint(
        task,
        agent=agent,
        reason=getattr(args, "reason", None) or "manual",
        transcript_path=getattr(args, "transcript_path", None) or "",
        next_actions=next_actions,
    )
    ok, snap_path = continuity.write_checkpoint(checkpoint, backend=backend)
    if not ok:
        _warn("Continuity snapshot upload failed.")
        return 1

    _info(f"Continuity snapshot: {checkpoint['checkpoint_id']}")
    _info(f"  Snapshot path: {snap_path}")
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
