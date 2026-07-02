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

from . import aggregate, okf, query, roles, tasks
from . import reconcile as rec
from .transport import FulcraFileTransport, TransportError

__all__ = ["main"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _host() -> str:
    return os.environ.get("FULCRA_COORD_AGENT") or f"coord-reconcile:{socket.gethostname()}"


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
        transport, args.team, now=_iso(dt), today=dt.strftime("%Y-%m-%d"), host=_host()
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
            priority=args.priority,
        )
    except tasks.TaskError as e:
        print(f"task update failed: {e}", file=sys.stderr)
        return 1
    transport.write(path, out)
    print(f"updated {args.name}" + (f" → {args.status}" if args.status else ""))
    return 0


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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coord-engine", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def add_json(sp):
        sp.add_argument("--json", action="store_true", help="emit JSON")

    r = sub.add_parser("reconcile", help="scan + heal a team's task views")
    r.add_argument("team")
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
    sc.set_defaults(func=cmd_search)

    rl = sub.add_parser("roles", help="role status fold (fulcra-agent-roles)")
    rlsub = rl.add_subparsers(dest="roles_command", required=True)
    rst = rlsub.add_parser("status", help="HELD/VACANT/CONTESTED + escalation-due")
    rst.add_argument("team"); rst.add_argument("role"); add_json(rst)
    rst.set_defaults(func=cmd_roles_status)

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
    tup.add_argument("--blocked-on", dest="blocked_on")
    tup.set_defaults(func=cmd_task_update)
    tdn = tksub.add_parser("done", help="mark done (requires evidence)")
    tdn.add_argument("team"); tdn.add_argument("name"); tdn.add_argument("--evidence", "-e", required=True)
    tdn.set_defaults(func=cmd_task_done)
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
