"""Task lifecycle + directive commands for fulcra-coord.

The bus-mutation command surface: the directive creators (``tell`` / ``broadcast``
/ ``assign``) and the task state-machine transitions (``start`` / ``update`` /
``block`` / ``pause`` / ``done`` / ``abandon``). Each validates its transition via
schema, then writes through the single ``_write_task_and_views`` pipeline.

Extracted from cli.py behind stable re-exports; depends only on lower layers (the
write pipeline, the io loader, the routing escalation, the presence onboarding hint,
plus cache/remote/schema/views/identity/annotations and the output leaf utils) and
never imports cli, so the split has no cycle. ``_derive_agent`` is a thin stateless
alias over ``identity.resolve_agent``; cli keeps its own identical copy for the
commands that remain there, so duplicating the one-liner avoids a back-import.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from . import cache, remote, schema, views, identity, continuity
from . import annotations as lifecycle_annotations
from .io import _load_task
from .output import info as _info, warn as _warn, err as _err
from .writepipe import _write_task_and_views
from .routing_ops import _escalate_review_to_human
from .presence import _maybe_warn_legacy_identity


def _derive_agent() -> str:
    return identity.resolve_agent()

#: A title that LOOKS like a task id (``TASK-YYYYMMDD-…``). When ``start`` is
#: handed one of these the operator almost certainly meant to CLAIM/activate the
#: existing task, not create a new one named after an id. Only the prefix is
#: matched (date-stamped ``TASK-<8 digits>-``) so a genuine title that merely
#: mentions a date can't trip it.
_TASK_ID_TITLE_RE = re.compile(r"^TASK-\d{8}-")


def cmd_tell(args: Any, backend: Optional[list[str]] = None) -> int:
    """Create a directive task addressed at another agent (sugar over `start`).

    A directive is a `proposed` task with assignee=<the target agent> and
    owner_agent = --from (the directing agent) or unset. It lands in the target's
    inbox until they ack or claim it.
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
            agg = remote.download_json(remote.presence_view_path(), backend=backend)
            presence = (agg or {}).get("agents", []) if agg else []
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

    def _artifact(value: str) -> dict[str, str]:
        if "=" not in value:
            return {"path": value, "note": ""}
        path, note = value.split("=", 1)
        return {"path": path.strip(), "note": note.strip()}

    next_action = getattr(args, "next", None)
    next_actions = [next_action] if next_action else None
    session_context = {
        "overall_goal": getattr(args, "session_goal", "") or "",
        "why_continuity_matters": getattr(args, "why_continuity", "") or "",
        "current_state": getattr(args, "session_state", "") or "",
        "immediate_followup": getattr(args, "session_followup", "") or "",
    }
    session_context = session_context if any(session_context.values()) else None
    artifacts = [_artifact(item) for item in (getattr(args, "artifact", None) or [])]
    checkpoint = continuity.make_checkpoint(
        task,
        agent=agent,
        reason=getattr(args, "reason", None) or "manual",
        transcript_path=getattr(args, "transcript_path", None) or "",
        decisions=getattr(args, "decision", None) or None,
        open_questions=getattr(args, "open_question", None) or None,
        next_actions=next_actions,
        artifacts=artifacts or None,
        memory_writes=getattr(args, "memory", None) or None,
        session_context=session_context,
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
