"""Read-only situational-awareness commands for fulcra-coord.

The operator's at-a-glance surfaces: ``status`` (the board), ``agents`` (who is on
what, folding in presence), ``needs-me`` (directives + blocked-on-you addressed to
me, with first-seen tracking), and ``resume`` (what to pick up after a restart).
All four read the compact summaries aggregate (one download, no per-task body
fetch) and render via the shared relative-time formatters — they never mutate bus
state.

Extracted from cli.py behind stable re-exports; depends only on lower layers
(remote / views / schema / identity / cache / listener, the io summaries loader, and
the output / textfmt leaf utils) and never imports cli, so the split has no cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from . import remote, views, schema, identity, cache, continuity
from .io import _load_task_summaries
from .output import info as _info, print_json as _print_json
from .textfmt import age_str as _age_str, until_str as _until_str, due_str as _due_str


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
    with_continuity = bool(getattr(args, "with_continuity", False))

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
    # PRs I OWN that were never routed for review (author left "review PR #N" as
    # free text instead of running request-review, so no kind:review directive
    # exists and the review reaches no reviewer's inbox/resume — how PR #101 sat
    # unreviewed). Surfaced so I route them before going idle.
    unrouted_pr = views.unrouted_pr_reviews(all_tasks, me)
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
        continuity_snapshots = []
        if with_continuity:
            for task in active:
                checkpoint = continuity.read_latest_for_task(task, agent=me, backend=backend)
                summary = continuity.summarize_checkpoint(checkpoint)
                if summary:
                    continuity_snapshots.append(summary)
        _print_json({
            "agent": me,
            "human": human,
            "active": active,
            "blocked_on_me": blocked_on_me,
            "owed_to_others": owed_to_others,
            "blocked_on_human": blocked_on_human,
            "unrouted_pr_reviews": unrouted_pr,
            "other_agents": other_agents,
            "continuity_snapshots": continuity_snapshots,
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

    # Loud, first — an unrouted PR review is silent work-loss, so it leads the
    # briefing with the exact command to fix it.
    if unrouted_pr:
        print(f"\n  ⚠ PRs you own with NO review routed ({len(unrouted_pr)}) — "
              f"run request-review so a reviewer actually gets it:")
        for s in unrouted_pr:
            prs = ", ".join("#" + p for p in s.get("pr_mentions", []))
            print(f"    [{s.get('status','?').upper()}] {s.get('id','')}  "
                  f"{s.get('title','')[:50]}  (mentions {prs})")
            repo = s.get("workstream", "") or "<repo>"
            first_pr = (s.get("pr_mentions") or ["N"])[0]
            print(f"          fix: fulcra-coord request-review "
                  f"{first_pr} --repo {repo}")

    _section("Your active/waiting work", active)
    _section("Blocked on YOU", blocked_on_me, ask_field=True)
    _section("You owe others", owed_to_others)
    _section(f"Blocked on the human ({human})", blocked_on_human, ask_field=True)

    if with_continuity:
        snapshots = []
        for task in active:
            checkpoint = continuity.read_latest_for_task(task, agent=me, backend=backend)
            summary = continuity.summarize_checkpoint(checkpoint)
            if summary:
                snapshots.append(summary)
        print(f"\n  Continuity snapshots ({len(snapshots)})")
        for s in snapshots:
            print(f"    {s.get('checkpoint_id','')}  {s.get('title','')[:50]}")
            when = s.get("created_at") or ""
            path = s.get("path") or ""
            if when:
                print(f"          at: {when}")
            if path:
                print(f"          path: {path}")
            nexts = s.get("next_actions") or []
            if nexts:
                print(f"          next: {str(nexts[0])[:70]}")

    # Concise team-state footer so a resuming agent sees what the others are on.
    if other_agents:
        print(f"\n  Other agents (presence) ({len(other_agents)})")
        for a in other_agents:
            ws = ", ".join(a.get("workstreams", [])) or "(none)"
            age = _age_str(a.get("last_seen", ""))
            print(f"    {a['agent']}  [{a.get('liveness','')}] (seen {age}): {ws}")
    print()
    return 0
