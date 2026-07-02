"""CLI for coord-engine — the shared coord2 engine.

    coord-engine reconcile <team>
    coord-engine status    <team> [--json]
    coord-engine board     <team> [--json]
    coord-engine needs-me  <team> --agent <id> [--json]
    coord-engine search    <team> <query> [--json]
    coord-engine roles status <team> <role> [--json]

Command functions take an injected ``transport`` so they're testable without the
network; ``main`` builds the real ``FulcraFileTransport``.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from . import aggregate, continuity, directives, okf, presence, query, review, roles, tasks
from . import reconcile as rec
from .transport import FulcraFileTransport, TransportError

__all__ = ["main"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _host() -> str:
    return os.environ.get("FULCRA_COORD_AGENT") or f"coord-reconcile:{socket.gethostname()}"


def _human() -> str:
    return os.environ.get("FULCRA_COORD_HUMAN") or "human"


def _load_rows(transport: Any, team: str) -> list[dict[str, Any]]:
    raw = transport.read(rec.summaries_path(team))
    if not raw:
        return []
    try:
        return aggregate.aggregate_rows(json.loads(raw))
    except Exception:
        return []


def _line(row: dict[str, Any]) -> str:
    return (
        f"  [{row.get('priority', '?'):>2}] {str(row.get('status', '?')):8} "
        f"{row.get('title') or row.get('name')}"
        + (f"  ({row.get('assignee')})" if row.get("assignee") else "")
    )


def cmd_reconcile(args: argparse.Namespace, transport: Any) -> int:
    dt = _now()
    res = rec.reconcile(
        transport, args.team, now=_iso(dt), today=dt.strftime("%Y-%m-%d"), host=_host(),
        retention_days=getattr(args, "retention_days", None),
    )
    if res.get("degraded"):
        print(f"reconcile degraded (no writes): {res.get('reason')}", file=sys.stderr)
        return 1
    print(
        f"reconciled team/{args.team}: {res['tasks']} tasks "
        f"({res['parsed']} parsed, {res['reused']} reused), "
        f"{res['transitions']} log entries, {len(res['warnings'])} warnings"
    )
    for w in res["warnings"]:
        print(f"  warn: {w}", file=sys.stderr)
    return 0


def cmd_status(args: argparse.Namespace, transport: Any) -> int:
    rows = _load_rows(transport, args.team)
    counts = query.status_counts(rows)
    if args.json:
        print(json.dumps(counts, indent=2))
    else:
        if not rows:
            print(f"(no aggregate for team/{args.team} — run `reconcile` first)")
        print(f"team/{args.team}: {len(rows)} tasks — " + ", ".join(
            f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


def cmd_board(args: argparse.Namespace, transport: Any) -> int:
    rows = _load_rows(transport, args.team)
    groups = query.board(rows)
    if args.json:
        print(json.dumps(groups, indent=2))
        return 0
    for section in ("active", "waiting", "blocked", "proposed"):
        items = groups.get(section, [])
        if items:
            print(f"{section.upper()} ({len(items)})")
            for r in items:
                print(_line(r))
    return 0


def cmd_needs_me(args: argparse.Namespace, transport: Any) -> int:
    rows = _load_rows(transport, args.team)
    got = query.needs_me(rows, args.agent, now=_iso(_now()))
    if args.json:
        print(json.dumps(got, indent=2))
    else:
        print(f"{len(got)} item(s) need {args.agent}:")
        for r in got:
            print(_line(r))
    return 0


def cmd_search(args: argparse.Namespace, transport: Any) -> int:
    rows = _load_rows(transport, args.team)
    if getattr(args, "archived", False):
        # cold path: read archived task docs directly (archives are small + rare)
        from . import model as _model
        for month in _archive_months(transport, args.team):
            pfx = f"{rec.archive_prefix(args.team)}{month}/"
            try:
                for e in transport.list_dir(pfx):
                    n = e.get("name") or ""
                    if e.get("is_dir") or not n.endswith(".md"):
                        continue
                    fm = okf.parse_frontmatter(transport.read(pfx + n))
                    if fm is not None and _model.is_task(fm):
                        row = _model.row_from_frontmatter(fm, name=n[:-3],
                                                          path=f"task/archive/{month}/{n}")
                        row["archived"] = month
                        rows.append(row)
            except TransportError:
                pass
    got = query.search(rows, args.query)
    if args.json:
        print(json.dumps(got, indent=2))
    else:
        print(f"{len(got)} match(es) for {args.query!r}:")
        for r in got:
            print(_line(r))
    return 0


# --- roles (fulcra-agent-roles fold) ---

def _role_doc_path(team: str, role: str) -> str:
    return f"team/{team}/roles/{role}.md"


def _leases_prefix(team: str, role: str) -> str:
    return f"team/{team}/roles/{role}/leases/"


def _escalation_marker_path(team: str, role: str, date: str) -> str:
    return f"team/{team}/roles/{role}/escalations/{date}.md"


def cmd_roles_status(args: argparse.Namespace, transport: Any) -> int:
    team, role = args.team, args.role
    now = _iso(_now())
    reg = okf.parse_frontmatter(transport.read(_role_doc_path(team, role))) or {}
    policy = reg.get("policy") or "shared"
    try:
        sla = float(reg.get("sla_hours") or roles.DEFAULT_SLA_HOURS)
    except (TypeError, ValueError):
        sla = roles.DEFAULT_SLA_HOURS
    try:
        entries = transport.list_dir(_leases_prefix(team, role))
        leases: Optional[list[dict[str, Any]]] = []
        for e in entries:
            n = e.get("name") or ""
            if e.get("is_dir") or not n.endswith(".md"):
                continue
            fm = okf.parse_frontmatter(transport.read(_leases_prefix(team, role) + n)) or {}
            leases.append({"agent": fm.get("agent") or n[:-3], "timestamp": fm.get("timestamp")})
    except TransportError:
        leases = None  # unreadable -> UNKNOWN
    status = roles.classify(leases, now=now, sla_hours=sla, policy=policy)
    today = _now().strftime("%Y-%m-%d")
    marker_exists = transport.read(_escalation_marker_path(team, role, today)) is not None
    esc = roles.escalation_due(leases, now=now, sla_hours=sla, marker_exists_today=marker_exists)
    fresh = roles.fresh_holders(leases, now=now, sla_hours=sla) if leases else []
    result = {
        "team": team, "role": role, "status": status, "policy": policy, "sla_hours": sla,
        "holders": [l.get("agent") for l in (leases or [])],
        "fresh_holders": [l.get("agent") for l in fresh],
        "escalation_due": esc,
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"role {role} in team/{team}: {status} (policy={policy}, sla={sla:g}h)")
        if fresh:
            print("  fresh holders: " + ", ".join(str(l.get("agent")) for l in fresh))
        if esc:
            print("  ESCALATION DUE — vacant past SLA, no marker today")
    return 0


# --- tasks (fulcra-agent-tasks lifecycle) ---

def _task_path(team: str, name: str) -> str:
    return f"team/{team}/task/{name}.md"


def cmd_task_start(args: argparse.Namespace, transport: Any) -> int:
    try:
        slug, content = tasks.new_task_doc(
            args.title, now=_iso(_now()), workstream=args.workstream, status=args.status,
            priority=args.priority, owner=_host(), assignee=args.assignee,
            summary=args.summary or "", next_action=args.next, kind=args.kind,
        )
    except tasks.TaskError as e:
        print(f"task start failed: {e}", file=sys.stderr)
        return 1
    path = _task_path(args.team, slug)
    if not args.force and transport.read(path) is not None:
        print(f"task {slug} already exists (use --force)", file=sys.stderr)
        return 1
    transport.write(path, content)
    print(f"created team/{args.team}/task/{slug}.md ({args.status})")
    return 0


def cmd_task_update(args: argparse.Namespace, transport: Any) -> int:
    path = _task_path(args.team, args.name)
    try:
        out = tasks.apply_update(
            transport.read(path), now=_iso(_now()), status=args.status, summary=args.summary,
            next_action=args.next, assignee=args.assignee, blocked_on=args.blocked_on,
            priority=args.priority, evidence=args.evidence,
        )
    except tasks.TaskError as e:
        print(f"task update failed: {e}", file=sys.stderr)
        return 1
    transport.write(path, out)
    print(f"updated {args.name}" + (f" → {args.status}" if args.status else ""))
    return 0


def _task_apply(args, transport, **kw) -> int:
    """Shared read-modify-write for the dedicated lifecycle verbs."""
    path = _task_path(args.team, args.name)
    try:
        out = tasks.apply_update(transport.read(path), now=_iso(_now()), **kw)
    except tasks.TaskError as e:
        verb = getattr(args, "verb", getattr(args, "task_command", "update"))
        print(f"task {verb} failed: {e}", file=sys.stderr)
        return 1
    transport.write(path, out)
    print(f"{getattr(args, 'verb', 'updated')} {args.name}")
    return 0


def cmd_task_block(args: argparse.Namespace, transport: Any) -> int:
    if not args.blocked_on and not args.on_user:
        print("task block failed: requires --blocked-on or --on-user", file=sys.stderr)
        return 1
    if args.blocked_on and args.on_user:
        print("task block failed: pass --blocked-on OR --on-user, not both", file=sys.stderr)
        return 1
    kw = {"status": "blocked", "blocked_on": args.on_user or args.blocked_on}
    if args.on_user:
        kw["assignee"] = _human()
        kw["add_tags"] = ["needs:human"]
    return _task_apply(args, transport, **kw)


def cmd_task_pause(args: argparse.Namespace, transport: Any) -> int:
    return _task_apply(args, transport, status="waiting", next_action=args.next)


def cmd_task_abandon(args: argparse.Namespace, transport: Any) -> int:
    return _task_apply(args, transport, status="abandoned", evidence=args.reason)


def cmd_task_assign(args: argparse.Namespace, transport: Any) -> int:
    kw = {"assignee": args.assignee}
    if args.assignee != _human():
        kw["remove_tags"] = ["needs:human"]
    return _task_apply(args, transport, **kw)


def _archive_months(transport: Any, team: str) -> list[str]:
    try:
        return [e["name"].rstrip("/") for e in transport.list_dir(rec.archive_prefix(team))
                if e.get("is_dir")]
    except TransportError:
        return []


def cmd_task_restore(args: argparse.Namespace, transport: Any) -> int:
    """Move an archived task back into the hot path (verified move)."""
    for month in sorted(_archive_months(transport, args.team), reverse=True):
        src = f"{rec.archive_prefix(args.team)}{month}/{args.name}.md"
        if transport.read(src) is None:
            continue
        dst = _task_path(args.team, args.name)
        if transport.read(dst) is not None:
            print(f"restore failed: {args.name} already exists in the hot path", file=sys.stderr)
            return 1
        if rec._crash_safe_move(transport, src, dst):
            print(f"restored {args.name} from archive/{month}/ (run reconcile to reindex)")
            return 0
        print(f"restore failed: verified move from archive/{month}/ failed", file=sys.stderr)
        return 1
    print(f"restore failed: {args.name} not found in the archive", file=sys.stderr)
    return 1


def cmd_task_done(args: argparse.Namespace, transport: Any) -> int:
    path = _task_path(args.team, args.name)
    try:
        out = tasks.mark_done(transport.read(path), now=_iso(_now()), evidence=args.evidence)
    except tasks.TaskError as e:
        print(f"task done failed: {e}", file=sys.stderr)
        return 1
    transport.write(path, out)
    print(f"done {args.name}")
    return 0


# --- review (fulcra-agent-review verdict tally) ---

def _review_doc_path(team: str, slug: str) -> str:
    return f"team/{team}/review/{slug}.md"


def _verdicts_prefix(team: str, slug: str) -> str:
    return f"team/{team}/review/{slug}/verdicts/"


def cmd_review_status(args: argparse.Namespace, transport: Any) -> int:
    team, slug = args.team, args.slug
    req_doc = okf.parse_frontmatter(transport.read(_review_doc_path(team, slug))) or {}
    required = req_doc.get("required")
    if isinstance(required, str):
        required = [r.strip() for r in required.split(",") if r.strip()]
    elif isinstance(required, list):
        required = [str(r).strip() for r in required if str(r).strip()]
    verdicts: list[dict[str, Any]] = []
    try:
        for e in transport.list_dir(_verdicts_prefix(team, slug)):
            n = e.get("name") or ""
            if e.get("is_dir") or not n.endswith(".md"):
                continue
            fm = okf.parse_frontmatter(transport.read(_verdicts_prefix(team, slug) + n)) or {}
            # Key by the FILENAME stem (ACL-controlled path), not the frontmatter
            # `reviewer:` — otherwise a file `mallory.md` claiming `reviewer: alice`
            # could shadow alice's real verdict. One verdict file per reviewer.
            verdicts.append({"reviewer": n[:-3], "verdict": fm.get("verdict")})
    except TransportError:
        pass
    result = review.tally(verdicts, required=required)
    result.update({"team": team, "slug": slug})
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"review {slug} in team/{team}: {result['state']}")
        if result["approvals"]:
            print("  approvals: " + ", ".join(result["approvals"]))
        if result["changes"]:
            print("  changes requested: " + ", ".join(result["changes"]))
        if result["pending_required"]:
            print("  awaiting required: " + ", ".join(result["pending_required"]))
    return 0


# --- continuity (fulcra-agent-continuity snapshots) ---

def _continuity_path(team: str, agent: str, task: str) -> str:
    return f"team/{team}/member/{agent}/continuity/{task}/latest.json"


def _continuity_prefix(team: str, agent: str) -> str:
    return f"team/{team}/member/{agent}/continuity/"


def cmd_continuity_snapshot(args: argparse.Namespace, transport: Any) -> int:
    task = tasks.slugify(args.task)  # single path segment; a slash breaks the no-task fold
    snap = continuity.build_snapshot(
        agent=args.agent, task=task, objective=args.objective, now=_iso(_now()),
        decisions=args.decision, next_actions=args.next, open_questions=args.open_question,
        artifacts=args.artifact, context_used_percent=args.context_percent,
        transcript_path=args.transcript,
    )
    transport.write(_continuity_path(args.team, args.agent, task), json.dumps(snap, indent=2))
    print(f"snapshot {snap['checkpoint_id']}")
    return 0


def cmd_continuity_resume(args: argparse.Namespace, transport: Any) -> int:
    if args.task:
        raw = transport.read(_continuity_path(args.team, args.agent, tasks.slugify(args.task)))
        try:
            snap = json.loads(raw) if raw else None
        except Exception:
            snap = None
    else:
        snaps: list[dict[str, Any]] = []
        try:
            for e in transport.list_dir(_continuity_prefix(args.team, args.agent)):
                n = (e.get("name") or "").rstrip("/")
                if not e.get("is_dir") or not n:
                    continue
                raw = transport.read(_continuity_path(args.team, args.agent, n))
                if raw:
                    try:
                        snaps.append(json.loads(raw))
                    except Exception:
                        pass
        except TransportError:
            pass
        snap = continuity.latest(snaps)
    if args.json:
        print(json.dumps(snap, indent=2))
    else:
        print(continuity.render_resume(snap))
    return 0


# --- directives (fulcra-agent-directives) ---

def _ack_path(team: str, slug: str, agent: str) -> str:
    return f"team/{team}/_coord/acks/{slug}/{tasks.agent_key(agent)}.md"


def _response_path(team: str, slug: str, stamp: str) -> str:
    return f"team/{team}/_coord/responses/{slug}/{stamp}.md"


def _stamp_for_path(now: str, agent: str) -> str:
    safe_time = now.replace(":", "").replace("-", "").replace(".", "")
    return f"{safe_time}-{tasks.agent_key(agent)}"


def _create_directive(args: argparse.Namespace, transport: Any, *, assignee: str,
                      not_before: Optional[str] = None) -> int:
    try:
        slug, content = tasks.new_task_doc(
            args.title, now=_iso(_now()), workstream=args.workstream,
            status="proposed", priority=args.priority,
            owner=getattr(args, "sender", None) or _host(), assignee=assignee,
            summary=args.summary or "", next_action=args.next, kind="directive",
            not_before=not_before,
        )
    except tasks.TaskError as e:
        print(f"directive failed: {e}", file=sys.stderr)
        return 1
    path = _task_path(args.team, slug)
    if transport.read(path) is not None:
        print(f"directive {slug} already exists", file=sys.stderr)
        return 1
    transport.write(path, content)
    print(f"directive {slug} -> {assignee}" + (f" (visible {not_before})" if not_before else ""))
    return 0


def cmd_tell(args: argparse.Namespace, transport: Any) -> int:
    return _create_directive(args, transport, assignee=args.assignee)


def cmd_broadcast(args: argparse.Namespace, transport: Any) -> int:
    return _create_directive(args, transport, assignee="*")


def cmd_remind(args: argparse.Namespace, transport: Any) -> int:
    when = directives.parse_when(args.when, now=_iso(_now()))
    if when is None:
        print(f"remind failed: cannot parse WHEN {args.when!r} (ISO or 5d/36h/10m)", file=sys.stderr)
        return 1
    return _create_directive(args, transport, assignee=args.assignee, not_before=when)


def cmd_later(args: argparse.Namespace, transport: Any) -> int:
    return _create_directive(args, transport, assignee=directives.BACKLOG)


def cmd_handoff(args: argparse.Namespace, transport: Any) -> int:
    """Atomic handoff: checkpoint ref + assignee land in ONE task write."""
    path = _task_path(args.team, args.name)
    try:
        out = tasks.apply_update(
            transport.read(path), now=_iso(_now()), assignee=args.to,
            checkpoint_ref=args.checkpoint, next_action=args.next,
        )
    except tasks.TaskError as e:
        print(f"handoff failed: {e}", file=sys.stderr)
        return 1
    transport.write(path, out)
    print(f"handed off {args.name} -> {args.to}"
          + (f" (checkpoint {args.checkpoint})" if args.checkpoint else ""))
    return 0


def cmd_inbox(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    if args.ack:
        fm = {"type": "Ack", "agent": agent, "timestamp": _iso(_now())}
        transport.write(_ack_path(args.team, args.ack, agent),
                        okf.render_frontmatter(fm) + "\nacked\n")
        print(f"acked {args.ack}")
        return 0
    rows = _load_rows(transport, args.team)
    acks = {str(r.get("name")): (r.get("acked_by") or []) for r in rows}
    stale_visible = directives.inbox(rows, acks, agent, now=_iso(_now()),
                                     include_backlog=args.all)
    for r in stale_visible:
        slug = str(r.get("name") or "")
        if agent not in (acks.get(slug) or []) and transport.read(_ack_path(args.team, slug, agent)):
            acks.setdefault(slug, []).append(agent)
    got = directives.inbox(rows, acks, agent, now=_iso(_now()),
                           include_backlog=args.all)
    # read-your-write: an ack written since the last reconcile hides the item
    # for the acking agent immediately (live shard check, only for shown items).
    got = [r for r in got
           if transport.read(_ack_path(args.team, str(r.get("name")), agent)) is None]
    if args.json:
        print(json.dumps(got, indent=2))
        return 0
    print(f"inbox — {agent}: {len(got)} item(s)")
    for r in got:
        print(_line(r))
    return 0


def cmd_respond(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    now = _iso(_now())
    stamp = _stamp_for_path(now, agent)
    fm = {"type": "Response", "agent": agent, "outcome": args.outcome, "timestamp": now}
    transport.write(_response_path(args.team, args.name, stamp),
                    okf.render_frontmatter(fm) + f"\n{args.evidence or args.outcome}\n")
    path = _task_path(args.team, args.name)
    try:
        out = tasks.apply_update(transport.read(path), now=now, status="done",
                                 evidence=f"{args.outcome} (respond by {agent})")
        transport.write(path, out)
        print(f"responded {args.name}: {args.outcome} (closed)")
    except tasks.TaskError as e:
        print(f"responded {args.name}: {args.outcome} (response recorded; not closed: {e})")
    return 0


# --- presence (fulcra-agent-presence) ---

def _presence_prefix(team: str) -> str:
    return f"team/{team}/presence/"


def _presence_shards(transport: Any, team: str) -> list[dict[str, Any]]:
    shards: list[dict[str, Any]] = []
    try:
        for e in transport.list_dir(_presence_prefix(team)):
            n = e.get("name") or ""
            if e.get("is_dir") or not n.endswith(".md"):
                continue
            fm = okf.parse_frontmatter(transport.read(_presence_prefix(team) + n)) or {}
            fm.setdefault("agent", n[:-3])
            shards.append(fm)
    except TransportError:
        pass
    return shards


def cmd_presence_beat(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    fm = {
        "type": "Presence", "title": f"presence — {agent}", "agent": agent,
        "workstreams": args.workstream or [], "summary": args.summary or "",
        "timestamp": _iso(_now()),
    }
    body = f"\n# Presence: {agent}\n"
    slug = tasks.agent_key(agent)
    transport.write(f"{_presence_prefix(args.team)}{slug}.md", okf.render_frontmatter(fm) + body)
    print(f"beat {agent} ({slug}.md)")
    return 0


def cmd_presence_show(args: argparse.Namespace, transport: Any) -> int:
    ros = presence.roster(_presence_shards(transport, args.team), now=_iso(_now()))
    if args.json:
        print(json.dumps(ros, indent=2))
        return 0
    print(f"presence — team/{args.team}: {len(ros)} agent(s)")
    for r in ros:
        ws = ", ".join(r["workstreams"])
        print(f"  [{r['liveness']:5}] {r['agent']}" + (f"  ({ws})" if ws else "")
              + (f" — {r['summary']}" if r["summary"] else ""))
    return 0


def cmd_agents(args: argparse.Namespace, transport: Any) -> int:
    rows = _load_rows(transport, args.team)
    digest = presence.agents_digest(rows, _presence_shards(transport, args.team), now=_iso(_now()))
    if args.json:
        print(json.dumps(digest, indent=2))
        return 0
    for a in digest:
        counts = ", ".join(f"{k}={v}" for k, v in sorted(a["open"].items())) or "no open work"
        print(f"  [{a['liveness']:7}] {a['agent']} — {counts}"
              + (f" — {a['summary']}" if a["summary"] else ""))
    return 0


def cmd_roles_claim(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    slug = tasks.agent_key(agent)
    fm = {"type": "Lease", "title": f"{args.role} lease — {agent}", "agent": agent,
          "timestamp": _iso(_now())}
    transport.write(f"{_leases_prefix(args.team, args.role)}{slug}.md",
                    okf.render_frontmatter(fm) + f"\nHolding {args.role}.\n")
    print(f"claimed {args.role} as {agent} (refresh by re-running)")
    return 0


def cmd_roles_release(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    slug = tasks.agent_key(agent)
    path = f"{_leases_prefix(args.team, args.role)}{slug}.md"
    if transport.read(path) is None:
        print(f"no lease for {agent} on {args.role}", file=sys.stderr)
        return 1
    ok = transport.delete(path) if hasattr(transport, "delete") else False
    print(f"released {args.role} ({agent})" if ok else f"release failed for {path}",
          file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coord-engine", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def add_json(sp):
        sp.add_argument("--json", action="store_true", help="emit JSON")

    r = sub.add_parser("reconcile", help="scan + heal a team's task views")
    r.add_argument("team")
    r.add_argument("--retention-days", dest="retention_days",
                   help="archive terminal tasks older than N days (or env COORD_RETENTION_DAYS)")
    r.set_defaults(func=cmd_reconcile)

    s = sub.add_parser("status", help="counts by status")
    s.add_argument("team"); add_json(s); s.set_defaults(func=cmd_status)

    b = sub.add_parser("board", help="open work grouped by status")
    b.add_argument("team"); add_json(b); b.set_defaults(func=cmd_board)

    nm = sub.add_parser("needs-me", help="open work assigned to / blocking an agent")
    nm.add_argument("team"); nm.add_argument("--agent", required=True); add_json(nm)
    nm.set_defaults(func=cmd_needs_me)

    sc = sub.add_parser("search", help="substring search over tasks")
    sc.add_argument("team"); sc.add_argument("query"); add_json(sc)
    sc.add_argument("--archived", action="store_true", help="also search the cold archive")
    sc.set_defaults(func=cmd_search)

    rl = sub.add_parser("roles", help="role status fold (fulcra-agent-roles)")
    rlsub = rl.add_subparsers(dest="roles_command", required=True)
    rst = rlsub.add_parser("status", help="HELD/VACANT/CONTESTED + escalation-due")
    rst.add_argument("team"); rst.add_argument("role"); add_json(rst)
    rst.set_defaults(func=cmd_roles_status)
    rcl = rlsub.add_parser("claim", help="claim/refresh a lease on a role")
    rcl.add_argument("team"); rcl.add_argument("role"); rcl.add_argument("--agent", "-a")
    rcl.set_defaults(func=cmd_roles_claim)
    rre = rlsub.add_parser("release", help="release your lease on a role")
    rre.add_argument("team"); rre.add_argument("role"); rre.add_argument("--agent", "-a")
    rre.set_defaults(func=cmd_roles_release)

    pr = sub.add_parser("presence", help="presence beats + roster (fulcra-agent-presence)")
    prsub = pr.add_subparsers(dest="presence_command", required=True)
    prb = prsub.add_parser("beat", help="write/refresh your presence shard")
    prb.add_argument("team"); prb.add_argument("--agent", "-a")
    prb.add_argument("--workstream", "-w", action="append")
    prb.add_argument("--summary", "-s")
    prb.set_defaults(func=cmd_presence_beat)
    prs = prsub.add_parser("show", help="roster with live/idle/stale liveness")
    prs.add_argument("team"); add_json(prs)
    prs.set_defaults(func=cmd_presence_show)

    ag = sub.add_parser("agents", help="cross-agent digest (open work by agent + liveness)")
    ag.add_argument("team"); add_json(ag)
    ag.set_defaults(func=cmd_agents)

    def add_directive_flags(sp):
        sp.add_argument("--priority", "-p", default="P2"); sp.add_argument("--workstream", "-w")
        sp.add_argument("--summary", "-s"); sp.add_argument("--next", "-n")
        sp.add_argument("--from", dest="sender")

    tl = sub.add_parser("tell", help="direct work at an agent (directive = task w/ assignee)")
    tl.add_argument("team"); tl.add_argument("assignee"); tl.add_argument("title")
    add_directive_flags(tl); tl.set_defaults(func=cmd_tell)
    bc = sub.add_parser("broadcast", help="direct work at every agent (*)")
    bc.add_argument("team"); bc.add_argument("title")
    add_directive_flags(bc); bc.set_defaults(func=cmd_broadcast)
    rm = sub.add_parser("remind", help="scheduled directive, hidden until WHEN (ISO or 5d/36h/10m)")
    rm.add_argument("team"); rm.add_argument("assignee"); rm.add_argument("when"); rm.add_argument("title")
    add_directive_flags(rm); rm.set_defaults(func=cmd_remind)
    lt = sub.add_parser("later", help="capture a backlog idea (@backlog)")
    lt.add_argument("team"); lt.add_argument("title")
    add_directive_flags(lt); lt.set_defaults(func=cmd_later)
    ho = sub.add_parser("handoff", help="atomic handoff: assignee + checkpoint ref in one write")
    ho.add_argument("team"); ho.add_argument("name"); ho.add_argument("--to", required=True)
    ho.add_argument("--checkpoint"); ho.add_argument("--next", "-n")
    ho.set_defaults(func=cmd_handoff)
    ib = sub.add_parser("inbox", help="open directives for an agent (--ack <slug> to ack)")
    ib.add_argument("team"); ib.add_argument("--agent", "-a"); ib.add_argument("--ack")
    ib.add_argument("--all", action="store_true", help="include @backlog"); add_json(ib)
    ib.set_defaults(func=cmd_inbox)
    rp = sub.add_parser("respond", help="answer + close a directive with an outcome")
    rp.add_argument("team"); rp.add_argument("name"); rp.add_argument("--outcome", "-o", required=True)
    rp.add_argument("--evidence", "-e"); rp.add_argument("--agent", "-a")
    rp.set_defaults(func=cmd_respond)

    tk = sub.add_parser("task", help="typed task lifecycle (fulcra-agent-tasks)")
    tksub = tk.add_subparsers(dest="task_command", required=True)
    tst = tksub.add_parser("start", help="create a task doc")
    tst.add_argument("team"); tst.add_argument("title")
    tst.add_argument("--workstream", "-w"); tst.add_argument("--status", default="proposed")
    tst.add_argument("--priority", "-p", default="P2"); tst.add_argument("--assignee")
    tst.add_argument("--summary", "-s"); tst.add_argument("--next", "-n")
    tst.add_argument("--kind", "-k"); tst.add_argument("--force", action="store_true")
    tst.set_defaults(func=cmd_task_start)
    tup = tksub.add_parser("update", help="update a task (enforces the status machine)")
    tup.add_argument("team"); tup.add_argument("name")
    tup.add_argument("--status"); tup.add_argument("--priority", "-p"); tup.add_argument("--assignee")
    tup.add_argument("--summary", "-s"); tup.add_argument("--next", "-n")
    tup.add_argument("--blocked-on", dest="blocked_on"); tup.add_argument("--evidence", "-e")
    tup.set_defaults(func=cmd_task_update)
    tdn = tksub.add_parser("done", help="mark done (requires evidence)")
    tdn.add_argument("team"); tdn.add_argument("name"); tdn.add_argument("--evidence", "-e", required=True)
    tdn.set_defaults(func=cmd_task_done)
    tbl = tksub.add_parser("block", help="mark blocked (sets blocked_on; --on-user routes to a human)")
    tbl.add_argument("team"); tbl.add_argument("name")
    tbl.add_argument("--blocked-on", dest="blocked_on")
    tbl.add_argument("--on-user", dest="on_user", help="human-facing ask; assigns to FULCRA_COORD_HUMAN/human + tags needs:human")
    tbl.set_defaults(func=cmd_task_block, verb="block")
    tpa = tksub.add_parser("pause", help="pause to waiting (requires --next)")
    tpa.add_argument("team"); tpa.add_argument("name"); tpa.add_argument("--next", "-n", required=True)
    tpa.set_defaults(func=cmd_task_pause, verb="pause")
    tab = tksub.add_parser("abandon", help="abandon (requires --reason)")
    tab.add_argument("team"); tab.add_argument("name"); tab.add_argument("--reason", "-r", required=True)
    tab.set_defaults(func=cmd_task_abandon, verb="abandon")
    trs = tksub.add_parser("restore", help="move an archived task back to the hot path")
    trs.add_argument("team"); trs.add_argument("name")
    trs.set_defaults(func=cmd_task_restore, verb="restore")
    tas = tksub.add_parser("assign", help="set/redirect assignee")
    tas.add_argument("team"); tas.add_argument("name"); tas.add_argument("assignee")
    tas.set_defaults(func=cmd_task_assign, verb="assign")

    rv = sub.add_parser("review", help="review verdict tally (fulcra-agent-review)")
    rvsub = rv.add_subparsers(dest="review_command", required=True)
    rvs = rvsub.add_parser("status", help="APPROVED/CHANGES/PENDING from reviewers' verdicts")
    rvs.add_argument("team"); rvs.add_argument("slug"); add_json(rvs)
    rvs.set_defaults(func=cmd_review_status)

    ct = sub.add_parser("continuity", help="structured resumable snapshots (fulcra-agent-continuity)")
    ctsub = ct.add_subparsers(dest="continuity_command", required=True)
    cts = ctsub.add_parser("snapshot", help="write a structured resume snapshot")
    cts.add_argument("team"); cts.add_argument("agent"); cts.add_argument("task")
    cts.add_argument("--objective", required=True)
    cts.add_argument("--next", action="append", dest="next")
    cts.add_argument("--decision", action="append", dest="decision")
    cts.add_argument("--open-question", action="append", dest="open_question")
    cts.add_argument("--artifact", action="append", dest="artifact")
    cts.add_argument("--context-percent", type=float, dest="context_percent")
    cts.add_argument("--transcript", dest="transcript")
    cts.set_defaults(func=cmd_continuity_snapshot)
    ctr = ctsub.add_parser("resume", help="print a resume brief from the latest snapshot")
    ctr.add_argument("team"); ctr.add_argument("agent"); ctr.add_argument("task", nargs="?")
    ctr.add_argument("--json", action="store_true")
    ctr.set_defaults(func=cmd_continuity_resume)
    return p


def main(argv: Optional[list[str]] = None, transport: Any = None) -> int:
    args = build_parser().parse_args(argv)
    transport = transport if transport is not None else FulcraFileTransport()
    try:
        return args.func(args, transport)
    except Exception as e:  # never dump a traceback at the user
        print(f"coord-engine: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
