"""Liveness-aware reviewer routing for fulcra-coord.

The review-directive lifecycle: resolve the configured reviewer SEED ids for an
author (_review_seeds), build the live-reviewer candidate pool from presence
(_review_pool), the `request-review` command, and the reconcile-time sweep
(_sweep_review_routes) that reroutes a stalled review to a live reviewer or
escalates it to the human (_escalate_review_to_human / _force_block_for_human),
with the reroute/accept timing knobs. Sits on the pure routing.py policy module
(imported as `routing`) and the write pipeline (_write_task_and_views).

Extracted from cli.py behind stable re-exports; depends only on lower layers and
never imports cli, so the split has no cycle.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import cache, remote, schema, views, identity, routing, env_float
from .io import _cache_remote_task, _load_all_tasks
from .output import info as _info, print_json as _print_json, warn as _warn
from .timeutil import iso_z as _iso_z
from .writepipe import _write_task_and_views

_SWEEP_DEADLINE_HEADROOM_SECONDS = 5.0


def _review_routing_config_path() -> Optional[str]:
    """Optional per-fleet review-routing policy file. Adopters drop their own
    seed/overrides here; absent by default (pure capability-driven)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    p = Path(base) / "fulcra-coord" / "review-routing.json"
    return str(p) if p.exists() else None


def _review_routing_config() -> dict:
    """Load the optional policy file. Best-effort: any error -> {} (no policy)."""
    path = _review_routing_config_path()
    if not path:
        return {}
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _review_seed_list(value: Any) -> list[str]:
    """Return only explicit non-empty string seed entries from config."""
    if not isinstance(value, list):
        return []
    return [s for s in value if isinstance(s, str) and s]


def _review_seeds(author: str) -> list[str]:
    """Ordered preferred-reviewer SEED ids for `author`, from config. Empty by
    default. NO fleet ids are hard-coded here — policy is per-adopter config.

    Precedence: env FULCRA_COORD_REVIEW_SEED (comma-sep) > file author_overrides
    (first author_prefix match) > file top-level seed > []. The seed is a day-one
    preference/tie-break only; a live capability:review agent still wins in
    _review_pool, and an empty seed degrades gracefully to capability-driven."""
    env = os.environ.get("FULCRA_COORD_REVIEW_SEED", "").strip()
    if env:
        return [s.strip() for s in env.split(",") if s.strip()]
    cfg = _review_routing_config()
    overrides = cfg.get("author_overrides") or []
    if not isinstance(overrides, list):
        overrides = []
    for ov in overrides:
        if not isinstance(ov, dict):
            continue
        pref = ov.get("author_prefix")
        if not isinstance(pref, str):
            continue
        if pref and (author or "").startswith(pref):
            return _review_seed_list(ov.get("seed"))
    return _review_seed_list(cfg.get("seed"))


def _review_pool(author: str, presence: list[dict[str, Any]]) -> list[str]:
    """Preference-ordered candidate pool: configured seed ids first (may be
    empty — see _review_seeds), then every review-capable agent in presence
    order. De-duplicated, seeds kept first. An empty seed degrades to a purely
    capability-driven pool; cmd_request_review escalates via block --on-user when
    the pool has no live reviewer."""
    pool = list(_review_seeds(author))
    for rec in presence:
        agent = rec.get("agent")
        if not agent:
            continue
        if "review" in (rec.get("capabilities") or []):
            pool.append(agent)
    seen: set[str] = set()
    ordered: list[str] = []
    for a in pool:
        if a and a not in seen:
            seen.add(a)
            ordered.append(a)
    return ordered


def _append_route_event_and_assignee(task, *, kind, to, by, attempt, reason,
                                     candidate_snapshot, observed_updated_at,
                                     dt=None):
    """Append a routing event AND sync task.assignee to its `to`, so the event
    log (audit + sweep input) and the assignee (inbox/tell machinery) never
    disagree. Mutates + returns a deep copy of the task.

    The durable directive routing sub-log is mirrored AFTER the authoritative
    task write confirms the body landed. Keeping this helper pure avoids a
    phantom route shard when the later task upload returns False."""
    import copy
    from . import routing
    task = copy.deepcopy(task)
    at = _iso_z(dt or datetime.now(timezone.utc))
    ev = routing.make_route_event(kind=kind, to=to, by=by, attempt=attempt,
                                  reason=reason, candidate_snapshot=candidate_snapshot,
                                  observed_updated_at=observed_updated_at, at=at)
    task.setdefault("events", []).append(ev)
    task["events"] = task["events"][-schema.MAX_EVENTS_INLINE:]
    task["assignee"] = to
    task["updated_at"] = at
    task["last_touched_by"] = by
    return task


def _mirror_route_to_directive_sublog(
    task: dict[str, Any], *, backend: Optional[list[str]] = None
) -> None:
    """Best-effort mirror of the task's latest route event into the directive sub-log.

    This is called only after the task body has landed (True or NeedsReconcile
    path from _write_task_and_views). Writing the shard before the task upload
    would let a failed upload leave durable directive routing metadata for a
    route that never became authoritative.
    """
    try:
        task_id = task.get("id")
        route_event = routing.current_route(task)
        if task_id and route_event:
            from . import directives
            directives.append_directive_route(
                directives.stable_directive_id(task_id), route_event, backend=backend)
    except Exception:
        pass


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

    Forge-agnostic: `pr` is the OPAQUE artifact ref (PR#/MR#/branch/SHA/URL),
    rendered the SAME way cmd_request_review does — "#<n>" for an all-digit ref,
    verbatim otherwise — with no hardcoded "PR " prefix. `repo` is optional;
    when absent we fall back to "general" everywhere (workstream, ask, marker)
    so a repo-less ref never emits the literal "None".

    Idempotent by caller: the sweep passes `existing` (the review task) to
    update IT in place (so the escalation lands on the same task the agents are
    already tracking, not a duplicate); a fresh request-review miss passes None
    and creates a dedicated escalation task. Best-effort: never raises into
    request-review / reconcile — a failure is warned and reported False."""
    try:
        human = identity.resolve_human()
        me = identity.resolve_agent(None)
        artifact_display = (f"#{pr}" if str(pr).isdigit() else str(pr))
        repo_label = repo or "general"
        repo_clause = f" in {repo}" if repo else ""
        ask = (f"{artifact_display}{repo_clause} needs review; no reviewer is "
               f"live/idle (tried: {', '.join(tried) or 'none'}). Assign a "
               f"reviewer manually.")
        marker = f"review-escalation:{repo_label}#{pr}"
        task = existing
        if task is None:
            task = schema.make_task(
                title=f"{artifact_display}{repo_clause} needs a reviewer",
                workstream=repo_label, agent=me, owner_agent=me, assignee=human,
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
    """Route a review of an artifact to a live/idle reviewer, or escalate to the
    human.

    Builds a preference-ordered pool (canonical reviewer seed + capability:review
    agents), resolves the best live/idle recipient via the liveness-aware
    resolver, and either tells them a kind:review-tagged directive (appending a
    `routed` event + syncing assignee) or escalates via block --on-user. --dry-run
    prints the ranked pool / tiers / excluded / winner / reason and writes
    nothing. Best-effort: a presence/resolve failure escalates rather than
    crashing (a review must never silently vanish)."""
    from . import routing
    # The artifact is an OPAQUE review ref (PR#/MR#/branch/SHA/URL/patch id), not
    # specifically a GitHub PR. args.pr keeps the historical dest name (lower
    # churn); `artifact` is the clear local. --repo is now optional.
    artifact = args.pr
    repo = getattr(args, "repo", None)
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
    # Display form of the artifact: bare-number refs keep the "#<n>" convention
    # (backward compat with the old "Review PR #<n>", minus the hardcoded "PR ");
    # everything else (branch/SHA/URL/patch id) is shown verbatim.
    artifact_display = (f"#{artifact}" if str(artifact).isdigit() else str(artifact))
    if dry_run:
        report = {"artifact": artifact, "repo": repo, "pool": pool, "snapshot": snapshot,
                  "excluded": [e["agent"] for e in excluded], "winner": winner,
                  "reason": "live/idle reviewer found" if winner
                            else "no live reviewer — would escalate"}
        if out_format == "json":
            _print_json(report)
        else:
            _info(f"[dry-run] pool={pool} winner={winner}")
        return 0
    if winner is None:
        escalated = _escalate_review_to_human(
            pr=artifact, repo=repo, tried=[s["agent"] for s in snapshot],
            backend=backend)
        if escalated:
            _info(f"Review {artifact_display}: no reviewer live — escalated to human.")
            return 0
        _warn(f"Review {artifact_display}: no reviewer live and escalation failed.")
        return 1
    # HIT: build the directive, tag kind:review, append routed event + assignee.
    title = f"Review {artifact_display} — assume bugs, claim the review before working"
    # --repo is optional now; when absent the directive has no repo workstream, so
    # fall back to "general" and omit the "in <repo>" clause from the summary.
    workstream = repo or "general"
    repo_clause = f" in {repo}" if repo else ""
    task = schema.make_task(
        title=title, workstream=workstream, agent=author,
        owner_agent=author, assignee=winner, priority="P1",
        summary=(f"Review {artifact_display}{repo_clause} needs review. Claim it "
                 f"(transition active / emit review-accepted) before working."))
    task["tags"] = sorted(set(task.get("tags", []) + [routing.REVIEW_TAG]))
    task["pr"] = artifact
    if repo is not None:
        task["repo"] = repo  # carried for the sweep + audit (only when supplied)
    tier = next((s["tier"] for s in snapshot if s["agent"] == winner), "idle")
    task = _append_route_event_and_assignee(
        task, kind="routed", to=winner, by=author, attempt=1,
        reason=f"live/idle reviewer ({tier})", candidate_snapshot=snapshot,
        observed_updated_at=task.get("updated_at", ""))
    cache.write_cached_task(task)
    try:
        ok = _write_task_and_views(task, backend=backend, command="request-review")
    except (schema.ConflictError, schema.NeedsReconcile):
        # The task BODY landed (these are raised only after the body uploaded),
        # so the dual-write mirror should still fire — same posture as cmd_tell.
        ok = True
    if ok:
        _mirror_route_to_directive_sublog(task, backend=backend)
        # Phase 3b dual-write: mirror the routed review into a `review` directive
        # (type detected by the kind:review tag; artifact_ref from pr/repo). Uses
        # the shared low-layer writer in directives — see the WHY note there for
        # the lifecycle↔routing_ops import-cycle reason it lives there, not here.
        from . import directives
        directives.dual_write(task, command="request-review", backend=backend)
    _info(f"Review {artifact_display} routed to {winner} ({tier}).")
    return 0 if ok else 1


REVIEW_VERDICT_TAG = "kind:review-verdict"


def _resolve_review_request(artifact: str, *, backend=None) -> Optional[dict[str, Any]]:
    """Find the open kind:review task for this artifact, or None.

    request-review records the author as ``owner_agent`` on the routed
    kind:review directive and stores the opaque artifact ref verbatim in
    ``task["pr"]`` (routing_ops ~:207). We match on that EXACT stored field —
    ``str(t["pr"]) == str(artifact)`` — never on a substring of the directive
    title. Title-substring matching was a confident-misroute hazard: it sent
    `review-done 10`'s verdict to the author of a `Review #101 …` directive
    (and `feat/x` to `feat/xyz`), which is worse than a clean error. Returns
    None when no exact match exists — the caller then requires --to rather than
    guess. Best-effort: a load failure resolves to None (caller falls back to
    --to)."""
    try:
        tasks = _load_all_tasks(backend=backend)
    except Exception:  # noqa: BLE001 — never crash the verdict path on a load error
        return None
    target = str(artifact)
    for t in tasks:
        if not routing.is_review_directive(t):
            continue
        if t.get("status") in ("done", "abandoned"):
            continue
        if str(t.get("pr")) != target:  # EXACT stored-ref match, no substrings
            continue
        return t
    return None


def _resolve_review_author(artifact: str, *, backend=None) -> Optional[str]:
    """Find the AUTHOR who requested review of this artifact, or None."""
    request = _resolve_review_request(artifact, backend=backend)
    if not request:
        return None
    author = request.get("owner_agent")
    return author if author else None


def cmd_review_done(args: Any, backend: Optional[list[str]] = None) -> int:
    """Land a reviewer's verdict as a BUS directive to the artifact's author.

    The durable fix for review verdicts getting lost when a reviewer posts only
    to a forge (GitHub comment): the verdict ALWAYS rides the bus to the author's
    inbox (listener/SessionStart catch it), regardless of any forge mirror.
    coord NEVER calls gh / a forge — the directive is the source of truth; a
    forge mirror is the reviewer's separate manual step.

    Author resolution: --to wins if given; else the open kind:review directive
    whose title references this artifact supplies its owner_agent (the author who
    requested the review). If neither resolves, error cleanly (do NOT guess /
    broadcast). The directive is a proposed task assigned to the author, tagged
    kind:review-verdict, carrying the verdict + note. Best-effort + fail-safe:
    a write failure warns and returns non-zero rather than crashing."""
    artifact = args.artifact
    verdict = args.verdict
    note = getattr(args, "note", None) or ""
    repo = getattr(args, "repo", None)
    to = getattr(args, "to", None)
    reviewer = getattr(args, "from", None)
    dry_run = getattr(args, "dry_run", False)

    reviewer = identity.resolve_agent(reviewer)
    artifact_display = (f"#{artifact}" if str(artifact).isdigit() else str(artifact))

    # Resolve the author the verdict is directed at. --to is the explicit override;
    # otherwise derive it from the original review request. Never guess. Keep the
    # original request too so review-done can close its mirrored review loop via a
    # bus response shard; the verdict directive below remains for mixed-fleet
    # readers that do not fold loops yet.
    review_request = None if to else _resolve_review_request(artifact, backend=backend)
    author = to or ((review_request or {}).get("owner_agent") if review_request else None)

    # --dry-run must NEVER hard-fail: it's a planning/preview mode, so an
    # unresolved author prints the plan with a "<unresolved — pass --to>"
    # placeholder and returns 0. Only the real (non-dry-run) path treats an
    # unresolved author as a clean error.
    if dry_run:
        to_display = author or "<unresolved — pass --to>"
        report = {"artifact": artifact, "verdict": verdict, "to": to_display,
                  "from": reviewer, "note": note, "repo": repo,
                  "tag": REVIEW_VERDICT_TAG}
        if getattr(args, "format", "table") == "json":
            _print_json(report)
        else:
            _info(f"[dry-run] verdict {verdict} on {artifact_display} -> {to_display}")
        return 0

    if not author:
        _warn(f"could not resolve the author of {artifact_display}; "
              f"pass --to <agent>")
        return 1

    title = f"Review verdict ({verdict}) on {artifact_display}"
    summary = f"Reviewer {reviewer} verdict: {verdict}."
    if note:
        summary += f" Note: {note}"
    # next_action nudges the author toward the obvious follow-up per verdict.
    next_action = ("Address the requested changes." if verdict == "changes"
                   else "Approved — proceed to land.")

    try:
        workstream = repo or "general"
        task = schema.make_task(
            title=title, workstream=workstream, agent=reviewer,
            owner_agent=reviewer, assignee=author, priority="P1",
            summary=summary, next_action=next_action)
        task["tags"] = sorted(set(task.get("tags", []) + [REVIEW_VERDICT_TAG]))
        task["pr"] = artifact
        if repo is not None:
            task["repo"] = repo
        cache.write_cached_task(task)
        try:
            ok = _write_task_and_views(task, backend=backend, command="review-done")
        except (schema.ConflictError, schema.NeedsReconcile):
            ok = True
    except Exception as e:  # noqa: BLE001 — best-effort; a write blowup warns, never crashes
        _warn(f"review-done directive failed (non-fatal): {e}")
        return 1
    if ok:
        # Phase 3b dual-write: mirror the verdict into a `verdict` directive
        # addressed to the author (type detected by the kind:review-verdict tag).
        # Shared low-layer writer; never fails the authoritative task write.
        from . import directives
        directives.dual_write(task, command="review-done", backend=backend)
        if review_request and review_request.get("id"):
            from . import loop_ops
            loop_id = directives.stable_directive_id(review_request["id"])
            loop_record = remote.download_json(remote.directive_remote_path(loop_id),
                                               backend=backend)
            if isinstance(loop_record, dict):
                outcome: dict[str, Any] = {"verdict": verdict}
                if note:
                    outcome["note"] = note
                response_ok = loop_ops.append_loop_response(
                    loop_id, {"by": reviewer, "outcome": outcome}, backend=backend)
                if not response_ok:
                    _warn(f"review-done response write failed for {artifact_display}; "
                          "review loop may still be open")
                    try:
                        ops_log.log_op("review-done", loop_id,
                                       status="response_write_failed")
                    except Exception:
                        pass
                    return 1
                try:
                    folded = loop_ops.fold_loop(loop_record, backend=backend)
                    remote.upload_json(folded, remote.directive_remote_path(loop_id),
                                       backend=backend)
                except Exception:
                    pass
    _info(f"Review verdict ({verdict}) on {artifact_display} sent to {author}.")
    return 0 if ok else 1


def _reroute_minutes(priority: str) -> float:
    """Minutes a never-acted review may sit on a below-floor assignee before the
    sweep reroutes it. P1 is more urgent (15m) than P2/P3 (30m); both are
    wall-clock durations (bus-global, machine-agnostic) and env-overridable."""
    if (priority or "P2") == "P1":
        return env_float("FULCRA_COORD_REVIEW_REROUTE_MINUTES_P1", 15.0)
    return env_float("FULCRA_COORD_REVIEW_REROUTE_MINUTES_P2", 30.0)


def _reroute_max() -> int:
    """Max route attempts (the initial route + reroutes) before the sweep gives
    up and escalates to the human instead of cycling reviewers forever."""
    # int(env_float(...)) — NOT env_int — to preserve float-parse-then-truncate:
    # a configured "2.9" must read as 2, not fall back to the default.
    return int(env_float("FULCRA_COORD_REVIEW_REROUTE_MAX", 2.0))


def _accepted_stall_hours() -> float:
    """Hours an ACCEPTED-then-silent review may stall before the sweep escalates
    it to the human (it is never rerouted once accepted — we don't yank work out
    from under a reviewer mid-flight; we only nudge the human after a long stall)."""
    return env_float("FULCRA_COORD_ACCEPTED_STALL_HOURS", 2.0)


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
    if task.get("status") == "blocked" and "needs:human" in (task.get("tags") or []):
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


def _sweep_review_routes(all_tasks, *, backend=None, now=None, deadline=None):
    """Authoritative reconcile-time reroute sweep. Considers ONLY kind:review
    directives. For each: classify; reroute a never-acted below-floor past-
    threshold review (excluding already-tried agents, minting a new route_id),
    escalate on cap/miss, freeze an accepted-then-stalled one (escalate after
    ACCEPTED_STALL_HOURS).

    Runs once per reconcile cycle; whichever machine reconciles first wins and
    the others converge via the stale-observation re-read (Files has no CAS) plus
    the optimistic write. Best-effort: one bad task — or the whole presence
    download — never raises into a reconcile tick (a failure skips, never crashes).

    DEADLINE-BOUNDED (B1): each directive can cost a network re-read plus a full
    view-rebuild write, and this sweep runs BEFORE the retention pass in
    cmd_reconcile. With no time check an O(review-directives) backlog could run
    past reconcile's ~90s ceiling and STARVE retention (which gates on the same
    deadline). ``deadline`` is reconcile's monotonic deadline; once the budget
    (minus a small headroom for retention) is spent we stop processing further
    directives. Best-effort: deferred directives drain on the next tick.
    ``deadline=None`` keeps the old unbounded behavior for direct callers/tests."""
    import time
    from . import routing
    if now is None:
        now = datetime.now(timezone.utc)
    budget_floor = (deadline - _SWEEP_DEADLINE_HEADROOM_SECONDS
                    if deadline is not None else None)
    try:
        agg = remote.download_json(remote.presence_view_path(), backend=backend)
        presence = (agg or {}).get("agents", []) if agg else []
    except Exception:
        presence = []
    deferred = 0
    for task in all_tasks:
        try:
            # Stop early if reconcile's budget is (nearly) spent — leave room for
            # the retention pass that runs after this sweep. Count, don't crash.
            if budget_floor is not None and time.monotonic() >= budget_floor:
                deferred += 1
                continue
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
            fresh = _cache_remote_task(task["id"], backend=backend)
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
                if _write_task_and_views(updated, backend=backend, command="reroute-review"):
                    _mirror_route_to_directive_sublog(updated, backend=backend)
            except schema.NeedsReconcile:
                _mirror_route_to_directive_sublog(updated, backend=backend)
            except schema.ConflictError:
                pass  # optimistic write is the second line of defence; reconverges next cycle
        except Exception:
            continue  # one bad task must never break the sweep / reconcile tick
    if deferred:
        _info(f"  Review sweep deferred {deferred} directive(s) "
              f"(deadline budget spent — drains next tick).")
