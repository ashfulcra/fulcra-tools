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
import pathlib
import secrets
import socket
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from . import aggregate, atc, atc_dash, continuity, continuity_audit, digest as digest_mod, directives, forge as forge_mod, health as health_mod, migrate as migrate_mod, okf, presence, query, review, roles, tasks
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
        + (" [fast-path: no fold-relevant changes in store feed]" if res.get("fast_path") else "")
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
    got += _pending_reviews_for(transport, args.team, args.agent)
    if args.json:
        print(json.dumps(got, indent=2))
    else:
        print(f"{len(got)} item(s) need {args.agent}:")
        for r in got:
            if r.get("type") == "review-pending":
                print(f"  [REVIEW] pending verdict: {r['name']} "
                      f"(required: {', '.join(r['pending_required'])})")
            else:
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


def _nonce_state_path(team: str, role: str, key: str) -> pathlib.Path:
    base = pathlib.Path(os.environ.get("COORD_ENGINE_STATE_DIR")
                        or pathlib.Path.home() / ".local" / "state" / "coord-engine")
    # agent_key over the (team, role) pair keeps the filename injective — raw
    # f"{team}-{role}" would collide ("a-b"/"c" vs "a"/"b-c"), the exact defect
    # agent_key exists to prevent for agent ids.
    return base / f"lease-nonce-{tasks.agent_key(f'{team}/{role}')}-{key}.txt"


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


def _review_tally(transport: Any, team: str, slug: str) -> dict[str, Any]:
    """Shared review fold: doc + verdict shards -> tally dict."""
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
    return review.tally(verdicts, required=required)


def _role_fresh_holders(transport: Any, team: str, name: str, *, now: str) -> list[str]:
    """Fresh lease holders of role name per the CANONICAL fold: the role
    doc's own sla_hours (falling back to the default) fed to
    roles.fresh_holders — the same fold roles status uses, so the two
    can never disagree about a lease. A name that is not a role (no role doc)
    or contains a path separator returns []."""
    if "/" in name:
        return []  # a role name is a single path segment; anything else is not a role
    reg = okf.parse_frontmatter(transport.read(_role_doc_path(team, name)))
    if reg is None:
        return []
    try:
        sla = float(reg.get("sla_hours") or roles.DEFAULT_SLA_HOURS)
    except (TypeError, ValueError):
        sla = roles.DEFAULT_SLA_HOURS
    leases: list[dict[str, Any]] = []
    try:
        for f in transport.list_dir(_leases_prefix(team, name)):
            fn = f.get("name") or ""
            if f.get("is_dir") or not fn.endswith(".md"):
                continue
            fm = okf.parse_frontmatter(transport.read(_leases_prefix(team, name) + fn)) or {}
            leases.append({"agent": fm.get("agent") or fn[:-3],
                           "timestamp": fm.get("timestamp")})
    except TransportError:
        return []
    return [str(l.get("agent")) for l in roles.fresh_holders(leases, now=now, sla_hours=sla)]


def _pending_reviews_for(transport: Any, team: str, agent: str) -> list[dict[str, Any]]:
    """Reviews whose pending_required names the agent — directly or via a role
    it holds a fresh lease on. Best-effort: any listing failure yields []
    (needs-me/briefing must not fail because the review add-on is absent).

    COST: one listing of review/ plus, per unsettled review, the verdict fold —
    and per unique role-shaped pending name, one role-doc read + lease fold.
    Fine for inbox polling on teams with tens of reviews; do NOT call in a tight
    loop. If review counts grow, the right home for this is the reconcile
    pre-fold (like task rows) — tracked on the bus."""
    out: list[dict[str, Any]] = []
    now = _iso(_now())
    role_holders: dict[str, list[str]] = {}
    try:
        entries = transport.list_dir(f"team/{team}/review/")
    except TransportError:
        return []
    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md"):
            continue
        slug = n[:-3]
        tally = _review_tally(transport, team, slug)
        pending = tally.get("pending_required") or []
        if tally.get("state") != "PENDING" or not pending:
            continue
        if agent not in pending:  # direct hit needs no role folding at all
            for r in pending:
                if r not in role_holders:
                    role_holders[r] = _role_fresh_holders(transport, team, r, now=now)
        if review.is_pending_for(pending, agent, role_holders):
            out.append({"type": "review-pending", "name": slug,
                        "state": "PENDING", "pending_required": pending})
    return out


def cmd_review_request(args: argparse.Namespace, transport: Any) -> int:
    """Open a review with named REQUIRED reviewers, making the obligation
    structurally durable: the doc lands at the SAME path `_review_tally` reads
    (`_review_doc_path`), so each required reviewer's `pending_required` marker
    surfaces in `needs-me` and stays there until their verdict file exists.

    Requesters SHOULD name roles, not identities (role-routing doctrine) — a
    role name is resolved to its fresh lease holders by the needs-me fold."""
    team = args.team
    # A title slugs like `tell` slugs titles; an already-slug-like arg round-trips
    # through the same helper unchanged (single path segment).
    slug = tasks.slugify(args.name)
    required = [r.strip() for r in (args.reviewer or []) if r and r.strip()]
    if not required:
        # An empty/whitespace-only --reviewer list would gate on nothing: the
        # tally has no pending_required marker, so any stray verdict flips the
        # review to APPROVED and no reviewer ever sees it in needs-me. Refuse,
        # writing no doc, rather than open a review that gates on nothing.
        print("review request needs at least one non-empty --reviewer",
              file=sys.stderr)
        return 2
    path = _review_doc_path(team, slug)
    if transport.read(path) is not None:
        print(f"review {slug} already exists", file=sys.stderr)
        return 1
    fm = {
        "type": "Review",
        "schema": "review-request/v1",
        "requested_by": getattr(args, "sender", None) or _host(),
        "of": args.of,
        "required": required,
        "ts": _iso(_now()),
    }
    body = f"\nReview requested: {args.of}\n"
    transport.write(path, okf.render_frontmatter(fm) + body)
    print(f"review {slug} requested (required: {', '.join(required)})")
    for r in required:
        print(f"  reviewer {r} -> file verdict at {_verdicts_prefix(team, slug)}{r}.md")
    return 0


def cmd_review_status(args: argparse.Namespace, transport: Any) -> int:
    team, slug = args.team, args.slug
    result = _review_tally(transport, team, slug)
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


def _agent_snapshots(transport: Any, team: str, agent: str) -> list[dict[str, Any]]:
    """All of one agent's latest-per-task continuity snapshots.

    Same transport mechanism ``cmd_continuity_resume`` uses to find an agent's
    single latest snapshot — here every task's ``latest.json`` is collected so
    the health audit can fold across agents.
    """
    snaps: list[dict[str, Any]] = []
    try:
        for e in transport.list_dir(_continuity_prefix(team, agent)):
            n = (e.get("name") or "").rstrip("/")
            if not e.get("is_dir") or not n:
                continue
            raw = transport.read(_continuity_path(team, agent, n))
            if raw:
                try:
                    snaps.append(json.loads(raw))
                except Exception:
                    pass
    except TransportError:
        pass
    return snaps


def cmd_continuity_resume(args: argparse.Namespace, transport: Any) -> int:
    if args.task:
        raw = transport.read(_continuity_path(args.team, args.agent, tasks.slugify(args.task)))
        try:
            snap = json.loads(raw) if raw else None
        except Exception:
            snap = None
    else:
        snap = continuity.latest(_agent_snapshots(transport, args.team, args.agent))
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


# --- continuity completion (A6): role checkpoints, park, briefing ---

def _set_role_field(transport: Any, team: str, role: str, key: str, value: str) -> bool:
    """Read-modify-write one frontmatter field on a role doc, preserving the rest."""
    path = _role_doc_path(team, role)
    doc = transport.read(path)
    fm = okf.parse_frontmatter(doc)
    if fm is None:
        return False
    split = okf.split_frontmatter(doc or "")
    body = split[1] if split else ""
    fm[key] = value
    return transport.write(path, okf.render_frontmatter(fm) + "\n" + body.lstrip("\n"))


def cmd_continuity_checkpoint(args: argparse.Namespace, transport: Any) -> int:
    if args.ref:
        if not _set_role_field(transport, args.team, args.role, "checkpoint_ref", args.ref):
            print(f"checkpoint failed: role {args.role} not found/parseable", file=sys.stderr)
            return 1
        print(f"checkpoint_ref for role {args.role} -> {args.ref}")
        return 0
    reg = okf.parse_frontmatter(transport.read(_role_doc_path(args.team, args.role))) or {}
    ref = reg.get("checkpoint_ref")
    if not ref:
        print(f"role {args.role}: no checkpoint_ref set")
        return 0
    print(f"role {args.role}: checkpoint_ref = {ref}")
    if "/continuity/" in str(ref):
        raw = transport.read(str(ref))
        try:
            snap = json.loads(raw) if raw else None
        except Exception:
            snap = None
        if snap:
            print(continuity.render_resume(snap))
    return 0


def _held_roles(transport: Any, team: str, agent: str) -> list[str]:
    """Roles where ``agent`` holds a FRESH lease (same freshness fold as roles status)."""
    held: list[str] = []
    now = _iso(_now())
    try:
        entries = transport.list_dir(f"team/{team}/roles/")
    except TransportError:
        return held
    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md") or n == "index.md":
            continue
        role = n[:-3]
        reg = okf.parse_frontmatter(transport.read(_role_doc_path(team, role))) or {}
        try:
            sla = float(reg.get("sla_hours") or roles.DEFAULT_SLA_HOURS)
        except (TypeError, ValueError):
            sla = roles.DEFAULT_SLA_HOURS
        lease = okf.parse_frontmatter(
            transport.read(f"{_leases_prefix(team, role)}{tasks.agent_key(agent)}.md")) or {}
        if lease and roles.age_hours(lease.get("timestamp"), now) <= sla:
            held.append(role)
    return held


def cmd_continuity_park(args: argparse.Namespace, transport: Any) -> int:
    """Session-exit checkpoint: snapshot every role the agent holds and point
    each role's checkpoint_ref at it. The incumbent's `park`."""
    agent = args.agent or _host()
    now = _iso(_now())
    held = _held_roles(transport, args.team, agent)
    if not held:
        print(f"park: {agent} holds no fresh roles in team/{args.team} — nothing to park")
        return 0
    for role in held:
        task_slug = f"role-{tasks.slugify(role)}"
        snap = continuity.build_snapshot(
            agent=agent, task=task_slug,
            objective=args.objective or f"parked role {role} at session exit",
            now=now, next_actions=args.next or [],
            open_questions=args.open_question or [],
        )
        path = _continuity_path(args.team, agent, task_slug)
        if not transport.write(path, json.dumps(snap, indent=2)):
            print(f"park: snapshot write FAILED for {role}; checkpoint_ref left unchanged",
                  file=sys.stderr)
            continue
        if not _set_role_field(transport, args.team, role, "checkpoint_ref", path):
            print(f"park: checkpoint_ref update FAILED for {role}", file=sys.stderr)
            continue
        print(f"parked {role} -> {path}")
    return 0


def cmd_briefing(args: argparse.Namespace, transport: Any) -> int:
    """One-call session-start bundle. Every section tolerates absent add-ons."""
    agent = args.agent or _host()
    now = _iso(_now())
    out: dict[str, Any] = {"schema": "coord.teams.briefing.v1", "team": args.team,
                           "agent": agent, "at": now}
    rows = _load_rows(transport, args.team)
    try:
        out["presence"] = presence.roster(_presence_shards(transport, args.team), now=now)
    except Exception as e:
        print(f"briefing: presence section unavailable ({type(e).__name__})", file=sys.stderr)
        out["presence"] = []
    try:
        out["board"] = query.board(rows)
    except Exception as e:
        print(f"briefing: board section unavailable ({type(e).__name__})", file=sys.stderr)
        out["board"] = {}
    try:
        acks = {str(r.get("name")): list(r.get("acked_by") or []) for r in rows}
        stale_visible = directives.inbox(rows, acks, agent, now=now)
        for r in stale_visible:
            slug = str(r.get("name") or "")
            if agent not in (acks.get(slug) or []) and transport.read(_ack_path(args.team, slug, agent)):
                acks.setdefault(slug, []).append(agent)
        out["inbox"] = directives.inbox(rows, acks, agent, now=now)
        out["inbox"] = [
            r for r in out["inbox"]
            if transport.read(_ack_path(args.team, str(r.get("name")), agent)) is None
        ]
    except Exception as e:
        print(f"briefing: inbox section unavailable ({type(e).__name__})", file=sys.stderr)
        out["inbox"] = []
    try:
        out["needs_me"] = query.needs_me(rows, agent, now=now)
    except Exception as e:
        print(f"briefing: needs_me section unavailable ({type(e).__name__})", file=sys.stderr)
        out["needs_me"] = []
    try:
        out["pending_reviews"] = _pending_reviews_for(transport, args.team, agent)
    except Exception as e:
        print(f"briefing: pending_reviews section unavailable ({type(e).__name__})", file=sys.stderr)
        out["pending_reviews"] = []
    try:
        snaps = []
        for e in transport.list_dir(_continuity_prefix(args.team, agent)):
            n = (e.get("name") or "").rstrip("/")
            if e.get("is_dir") and n:
                raw = transport.read(_continuity_path(args.team, agent, n))
                if raw:
                    try:
                        snaps.append(json.loads(raw))
                    except Exception:
                        pass
        out["resume"] = continuity.latest(snaps)
    except Exception as e:
        print(f"briefing: resume section unavailable ({type(e).__name__})", file=sys.stderr)
        out["resume"] = None
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"briefing — {agent} in team/{args.team}")
    live = [p["agent"] for p in out["presence"] if p.get("liveness") == "live"]
    print(f"  live now: {', '.join(live) if live else '(nobody)'}")
    open_counts = {k: len(v) for k, v in (out["board"] or {}).items() if v}
    print("  board: " + (", ".join(f"{k}={v}" for k, v in open_counts.items()) or "empty"))
    print(f"  inbox: {len(out['inbox'])} item(s)")
    for r in out["inbox"][:5]:
        print(_line(r))
    print(f"  needs-me: {len(out['needs_me'])} item(s)")
    print(f"  pending reviews: {len(out['pending_reviews'])} item(s)")
    for r in out["pending_reviews"][:5]:
        print(_line(r))
    print(continuity.render_resume(out["resume"]))
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


# --- ATC: cross-subscription cap ledger (fulcra-agent-atc) -------------------


def _atc_accounts_path(team: str) -> str:
    return f"team/{team}/atc/accounts.json"


def _atc_usage_prefix(team: str) -> str:
    return f"team/{team}/atc/usage/"


def _atc_usage_shards(transport: Any, team: str) -> list[dict[str, Any]]:
    """Read usage shards into the row shape ``atc.headroom`` folds.

    Malformed shards (bad frontmatter, unparseable/absent ``ts``, no account)
    are skipped rather than raising — one corrupt shard cannot break the fold.
    """
    rows: list[dict[str, Any]] = []
    pfx = _atc_usage_prefix(team)
    try:
        entries = transport.list_dir(pfx)
    except TransportError:
        return rows
    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md"):
            continue
        try:
            fm = okf.parse_frontmatter(transport.read(pfx + n)) or {}
            ts = continuity._parse_created_at(fm.get("ts"))
            if ts is None or not fm.get("account"):
                continue
            row = {"account": fm["account"], "ts": ts,
                   "units": int(fm.get("units") or 0),
                   "throttled": bool(fm.get("throttled"))}
            # Task-3 outcome fields flow through only when present; v1 shards
            # (no model/task_class/outcome) reach the demotions fold untouched
            # and are ignored there.
            for k in ("model", "task_class", "outcome"):
                if fm.get(k) is not None:
                    row[k] = fm[k]
            rows.append(row)
        except Exception:
            continue
    return rows


def cmd_usage_log(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    task_class = getattr(args, "task_class", None)
    # --task-class is taxonomy-validated (exit 2 on unknown, matching route's
    # unknown-need contract) — validate BEFORE any write so a rejected
    # invocation leaves no shard behind. --outcome is argparse-choices gated.
    if task_class is not None and task_class not in atc.TAXONOMY:
        print(f"usage log — unknown task-class: {task_class} (must be one of: "
              f"{','.join(sorted(atc.TAXONOMY))})", file=sys.stderr)
        return 2
    ts = _iso(_now())
    fm = {"schema": "atc-usage/v1", "agent": agent, "ts": ts,
          "account": args.account, "tier": args.tier,
          "units": int(args.units or 0), "throttled": bool(args.throttled)}
    # Outcome-attribution fields are written ONLY when provided, so v1 shards
    # stay v1 (the headroom + demotions folds both tolerate their absence).
    if getattr(args, "model", None):
        fm["model"] = args.model
    if task_class is not None:
        fm["task_class"] = task_class
    if getattr(args, "outcome", None) is not None:
        fm["outcome"] = args.outcome
    # Path-safe stamp (colons stripped) + agent slug, matching the repo's
    # timestamped-shard convention (_stamp_for_path); fm["ts"] keeps the real
    # ISO value the headroom fold parses.
    transport.write(_atc_usage_prefix(args.team) + _stamp_for_path(ts, agent) + ".md",
                    okf.render_frontmatter(fm) + "\n")
    extra = "".join(
        f", {k}={fm[k]}" for k in ("model", "task_class", "outcome") if k in fm)
    print(f"logged {fm['units']} units -> {args.account} ({args.tier}"
          + (", THROTTLED" if args.throttled else "") + ")" + extra)
    return 0


def cmd_headroom(args: argparse.Namespace, transport: Any) -> int:
    text = transport.read(_atc_accounts_path(args.team))
    parsed = atc.parse_accounts(text)
    if not parsed["accounts"]:
        print("headroom — no accounts declared"
              + (f" ({parsed['error']})" if parsed.get("error") else "")
              + " — see fulcra-agent-atc §setup")
        return 0
    shards = _atc_usage_shards(transport, args.team)
    rows = atc.headroom(parsed["accounts"], shards, _now())
    if args.json:
        # Contract change (task 3): headroom --json now emits an OBJECT with the
        # per-window rows under "windows" plus a "demotions" list folded from the
        # outcome shards — the array top-level could not gain a sibling key.
        demo = [{"model": m, "task_class": tc, "bad": v["bad"], "of": v["of"]}
                for (m, tc), v in sorted(atc.demotions(shards).items())]
        print(json.dumps({"windows": rows, "demotions": demo}, indent=2))
        return 0
    print(f"headroom — {args.team}")
    for r in rows:
        flags = " THROTTLED(calibrate caps)" if r["throttled"] else ""
        print(f"  {r['account']:<20} {r['window_hours']:>4}h  "
              f"{r['headroom']}/{r['cap']} ({r['pct']}%){flags}")
    return 0


def _atc_models_overlay(text: Optional[str]) -> Optional[dict[str, Any]]:
    """Extract the optional top-level ``models`` overlay from accounts.json.

    Returns the overlay dict, or ``None`` when absent/malformed (v1 accounts.json
    has no ``models`` key -> defaults-only routing). Never raises."""
    if not text:
        return None
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return None
    m = d.get("models") if isinstance(d, dict) else None
    return m if isinstance(m, dict) else None


def cmd_route(args: argparse.Namespace, transport: Any) -> int:
    text = transport.read(_atc_accounts_path(args.team))
    parsed = atc.parse_accounts(text)
    merged, merge_reports = atc.merge_models(
        atc.load_default_models(), _atc_models_overlay(text))
    needs = [n.strip() for n in (args.needs or "").split(",") if n.strip()]
    shards = _atc_usage_shards(transport, args.team)
    # Fold outcome shards -> demoted (model, task_class) pairs, then adapt to the
    # {model: [tags]} shape route consumes (task_class values are taxonomy tags).
    demo_for_route = atc._demotions_for_route(atc.demotions(shards))
    result = atc.route(parsed, merged, needs, shards,
                       demotions=demo_for_route, now=_now())
    # Surface the overlay-merge notes alongside the fold's own coercion notes.
    result["dropped_unknown_tags"] = merge_reports + result.get("dropped_unknown_tags", [])
    reason = result.get("reason")
    unknown_need = bool(reason) and reason.startswith("unknown need:")
    if args.json:
        print(json.dumps(result, indent=2))
        return 2 if unknown_need else 0
    if unknown_need:
        print(f"route — {reason} (needs must be one of: "
              f"{','.join(sorted(atc.TAXONOMY))})")
        return 2
    if not result["candidates"]:
        print(f"no candidates: {reason}")
        return 0
    print(f"route — {args.team} — needs {','.join(needs)} "
          f"(map {result['map_version']})")
    for i, c in enumerate(result["candidates"], 1):
        pct = f"{c['headroom_pct']:g}"
        tags = ",".join(c["tags"])
        demo = f" [demoted: {', '.join(c['demoted'])}]" if c["demoted"] else ""
        print(f"{i}. {c['model']} — ({c['account']}) — {pct}% — {tags}{demo}")
    return 0


def cmd_atc_report(args: argparse.Namespace, transport: Any) -> int:
    """Team dispatch/tier/calibration report over the trailing --days window.

    Reads the same accounts.json + usage shards the other ATC verbs use, folds
    the demotions (calibration) and merged model map alongside, and renders the
    estimate-labelled text block. Never crashes on an empty/corrupt ledger."""
    text = transport.read(_atc_accounts_path(args.team))
    parsed = atc.parse_accounts(text)
    shards = _atc_usage_shards(transport, args.team)
    merged, _ = atc.merge_models(atc.load_default_models(),
                                 _atc_models_overlay(text))
    rep = atc.report_fold(parsed, shards, team=args.team,
                          demotions=atc.demotions(shards), models=merged,
                          days=args.days, now=_now())
    if args.json:
        print(json.dumps(rep, indent=2))
        return 0
    print(atc.render_report(rep))
    return 0


def cmd_dash(args: argparse.Namespace, transport: Any) -> int:
    """Serve the localhost ATC dashboard in the foreground (127.0.0.1 only).

    ``data_fn`` recomputes ``dash_data`` from the live ledger on every
    ``/data.json`` request, so the page's 30s poll reflects fresh headroom
    without restarting the server. Bind host is never operator-controllable —
    there is deliberately no ``--host`` flag."""
    def data_fn() -> dict[str, Any]:
        text = transport.read(_atc_accounts_path(args.team))
        parsed = atc.parse_accounts(text)
        shards = _atc_usage_shards(transport, args.team)
        merged, _ = atc.merge_models(atc.load_default_models(),
                                     _atc_models_overlay(text))
        return atc_dash.dash_data(parsed, shards, team=args.team,
                                  models=merged, now=_now())

    atc_dash.serve(args.team, port=args.port, data_fn=data_fn)
    return 0


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
    if okf.parse_frontmatter(transport.read(_role_doc_path(args.team, args.role))) is None:
        print(f"note: role {args.role!r} has no registered role doc — status folds fall back "
              f"to defaults and review role-routing will NOT match this role's holders; "
              f"create team/{args.team}/roles/{args.role}.md", file=sys.stderr)
    shard_path = f"{_leases_prefix(args.team, args.role)}{slug}.md"
    state = _nonce_state_path(args.team, args.role, slug)
    # Same-id double-acting check: leases can't distinguish two sessions sharing one
    # id (same shard file), so compare the shard's nonce to the one THIS session wrote.
    existing = okf.parse_frontmatter(transport.read(shard_path)) or {}
    try:
        stored = state.read_text().strip() if state.exists() else None
    except OSError:
        stored = None
    shard_nonce = existing.get("nonce")  # absent for pre-nonce shards: overwrites by
    # old-engine sessions are undetectable by design — nothing to compare against.
    if stored and shard_nonce and shard_nonce != stored:
        print(f"WARNING: nonce mismatch on {slug}.md — another session has been acting "
              f"as {agent} since your last claim (same-id double-acting). Give each "
              f"session its own FULCRA_COORD_AGENT identity, or stop one.", file=sys.stderr)
    elif stored is None and shard_nonce:
        print(f"note: taking over an existing lease shard for {agent} written by another "
              f"session (no local nonce state to compare)", file=sys.stderr)
    nonce = secrets.token_hex(8)
    fm = {"type": "Lease", "title": f"{args.role} lease — {agent}", "agent": agent,
          "timestamp": _iso(_now()), "nonce": nonce}
    transport.write(shard_path, okf.render_frontmatter(fm) + f"\nHolding {args.role}.\n")
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(nonce + "\n")
    except OSError as e:
        print(f"note: could not persist nonce state (double-acting check disabled "
              f"until it can be written): {e}", file=sys.stderr)
    print(f"claimed {args.role} as {agent} ({slug}.md; refresh by re-running)")
    return 0


def cmd_roles_release(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    slug = tasks.agent_key(agent)
    path = f"{_leases_prefix(args.team, args.role)}{slug}.md"
    state = _nonce_state_path(args.team, args.role, slug)
    if transport.read(path) is None:
        try:
            state.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"no lease for {agent} on {args.role}", file=sys.stderr)
        return 1
    ok = transport.delete(path) if hasattr(transport, "delete") else False
    if ok:
        try:
            state.unlink(missing_ok=True)
        except OSError:
            pass
    print(f"released {args.role} ({agent})" if ok else f"release failed for {path}",
          file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


# --- health / doctor (fulcra-agent-health) ---

def cmd_health(args: argparse.Namespace, transport: Any) -> int:
    shards = []
    try:
        for e in transport.list_dir(health_mod.health_prefix(args.team)):
            n = e.get("name") or ""
            if not e.get("is_dir") and n.endswith(".json"):
                sh = health_mod.parse_shard(transport.read(health_mod.health_prefix(args.team) + n))
                if sh:
                    shards.append(sh)
    except TransportError:
        pass
    view = health_mod.fold(shards, now=_iso(_now()))
    code = 0 if view["healthy"] else 1
    # Tier-1 continuity audit: an agent beating presence but with no fresh
    # snapshot is working without a recoverable trail. Compute it here so both
    # the JSON payload and the text output surface it; it does not move health's
    # exit code — that stays reconciler-driven.
    now_dt = _now()
    pres_rows: list[dict[str, Any]] = []
    snap_rows: list[dict[str, Any]] = []
    for r in presence.roster(_presence_shards(transport, args.team), now=_iso(now_dt)):
        pts = roles._parse(r.get("last_seen"))
        if pts is None:
            continue
        pres_rows.append({"agent": r["agent"], "ts": pts})
        for snap in _agent_snapshots(transport, args.team, r["agent"]):
            sts = continuity._parse_created_at(snap.get("created_at"))
            if sts is not None:
                snap_rows.append({"agent": r["agent"], "ts": sts})
    flagged_agents = continuity_audit.stale_agents(pres_rows, snap_rows, now=now_dt)
    # Same row fields stale_agents returns: agent/presence_age_h/snapshot_age_h.
    view["continuity_stale"] = flagged_agents
    if args.json:
        print(json.dumps(view, indent=2))
        return code
    print(f"health — team/{args.team}: {view['fresh']}/{view['total']} host(s) fresh"
          + ("" if view["healthy"] else "  [NO FRESH RECONCILER]"))
    if view["total"] == 0:
        print("  (no health shards at all — nobody has ever reconciled this team)")
    for h in view["hosts"]:
        age = "?" if h["age_hours"] is None else f"{h['age_hours']:g}h"
        flag = "STALE" if h["stale"] else "ok"
        print(f"  [{flag:5}] {h['host']} — last reconcile {age} ago"
              f" (v{h.get('engine_version')}, {h.get('tasks')} tasks, {h.get('warnings')} warn)")
    # Tier-1 continuity audit (computed above): an agent beating presence but
    # with no fresh snapshot is working without a recoverable trail.
    for flagged in flagged_agents:
        y = flagged["snapshot_age_h"]
        snap_desc = "missing" if y is None else f"stale ({y}h)"
        print(f"  continuity-stale: {flagged['agent']}"
              f" presence-fresh ({flagged['presence_age_h']}h)"
              f" but snapshot {snap_desc} — see fulcra-agent-continuity contract")
    # empty fleet reads UNHEALTHY: "nobody ever reconciled" is the primary
    # cold-start failure a monitor probe exists to catch (review finding).
    return code


def cmd_doctor(args: argparse.Namespace, transport: Any) -> int:
    """Local preflight: tooling on PATH + store reachable. Exit 0 = healthy."""
    import shutil
    ok = True
    from .transport import _split_command
    full_cmd = " ".join(_split_command())
    launcher = _split_command()[0]
    if shutil.which(launcher):
        print(f"  ✓ storage command launcher on PATH ({launcher}; full: {full_cmd!r})")
    else:
        print(f"  ✗ storage command launcher NOT found ({launcher}; full: {full_cmd!r}) — "
              f"install fulcra-api + auth login", file=sys.stderr)
        ok = False
    try:
        transport.list_dir(f"team/{args.team}/" if args.team else "team/")
        print("  ✓ File Store reachable")
    except Exception as e:
        print(f"  ✗ File Store unreachable: {type(e).__name__}: {e}", file=sys.stderr)
        ok = False
    from . import __version__ as _v
    print(f"  ✓ coord-engine v{_v}")
    print("doctor: healthy" if ok else "doctor: PROBLEMS FOUND")
    return 0 if ok else 1


# --- digest + escalate (fulcra-agent-health, A5b) ---

def cmd_digest(args: argparse.Namespace, transport: Any) -> int:
    now = _iso(_now())
    rows = _load_rows(transport, args.team)
    d = digest_mod.build(rows, _presence_shards(transport, args.team),
                         now=now, human=args.human or _human())
    if args.json:
        print(json.dumps(d, indent=2))
    else:
        print(digest_mod.render(d), end="")
        try:
            text = transport.read(_atc_accounts_path(args.team))
            parsed = atc.parse_accounts(text)
            if parsed["accounts"]:
                rows = atc.headroom(parsed["accounts"],
                                    _atc_usage_shards(transport, args.team), _now())
                low = [r for r in rows if r["pct"] < 15.0]
                for r in low:
                    print(f"  headroom LOW: {r['account']} {r['window_hours']}h "
                          f"at {r['pct']}%" + (" THROTTLED" if r["throttled"] else ""))
        except Exception:
            pass
    if args.store:
        day = now[:10]
        window = digest_mod.window_for(now)
        marker = f"team/{args.team}/_coord/digests/{day}-{window}.md"
        if transport.read(marker) is not None:
            print(f"(digest for {day} {window} already stored — skipped)", file=sys.stderr)
        else:
            transport.write(marker, digest_mod.render(d))
            print(f"stored digest -> _coord/digests/{day}-{window}.md", file=sys.stderr)
    return 0


def cmd_escalate(args: argparse.Namespace, transport: Any) -> int:
    """Role-vacancy sweep: for every role doc, if vacancy past SLA and no marker
    today, write the marker + a P1 directive to the role's maintainer.
    Heartbeat-safe (idempotent per day)."""
    now = _iso(_now()); today = _now().strftime("%Y-%m-%d")
    escalated = checked = 0
    try:
        entries = transport.list_dir(f"team/{args.team}/roles/")
    except TransportError:
        print("escalate: roles dir unreadable", file=sys.stderr)
        return 1
    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md") or n == "index.md":
            continue
        role = n[:-3]; checked += 1
        reg = okf.parse_frontmatter(transport.read(_role_doc_path(args.team, role))) or {}
        try:
            sla = float(reg.get("sla_hours") or roles.DEFAULT_SLA_HOURS)
        except (TypeError, ValueError):
            sla = roles.DEFAULT_SLA_HOURS
        leases: Optional[list[dict[str, Any]]] = []
        try:
            for f in transport.list_dir(_leases_prefix(args.team, role)):
                fn = f.get("name") or ""
                if not f.get("is_dir") and fn.endswith(".md"):
                    fm = okf.parse_frontmatter(
                        transport.read(_leases_prefix(args.team, role) + fn)) or {}
                    leases.append({"agent": fm.get("agent") or fn[:-3],
                                   "timestamp": fm.get("timestamp")})
        except TransportError:
            leases = None
        marker_path = _escalation_marker_path(args.team, role, today)
        marker_exists = transport.read(marker_path) is not None
        if not roles.escalation_due(leases, now=now, sla_hours=sla,
                                    marker_exists_today=marker_exists):
            continue
        maintainer = str(reg.get("maintainer") or _human())
        transport.write(marker_path, okf.render_frontmatter(
            {"type": "Escalation", "role": role, "timestamp": now}) + "\nescalated\n")
        slug, content = tasks.new_task_doc(
            f"ROLE VACANT {today}: {role} unattended past {sla:g}h SLA",
            now=now, status="proposed", priority="P1", owner=_host(),
            assignee=maintainer, kind="directive",
            summary=f"Role {role} in team/{args.team} has no fresh lease past its SLA. "
                    f"Claim it (coord-engine roles claim {args.team} {role}) or reassign.",
        )
        dst = _task_path(args.team, slug)
        if transport.read(dst) is None:
            transport.write(dst, content)
            escalated += 1
            print(f"escalated {role} -> {maintainer}")
        else:
            print(f"re-escalation suppressed for {role} (today's directive already exists)")
    print(f"escalate: {checked} role(s) checked, {escalated} escalated")
    return 0


# --- forge (fulcra-agent-forge) ---

def cmd_forge_mirror(args: argparse.Namespace, transport: Any) -> int:
    import shutil as _sh
    if not _sh.which("gh") and args.runner is None:
        print("forge mirror: gh CLI not found — nothing mirrored (install GitHub CLI to enable)",
              file=sys.stderr)
        return 0  # degradation, not an error
    res = forge_mod.mirror(transport, args.team, now=_iso(_now()),
                           runner=args.runner or forge_mod.default_runner,
                           repo=args.repo)
    if res.get("error"):
        print(f"forge mirror: {res['error']}", file=sys.stderr)
        return 1
    print(f"forge mirror: {res['checked']} PR review(s) checked, "
          f"{res['mirrored']} evidence shard(s) written, {res['verdicts']} auto-verdict(s)")
    return 0


# --- migrate (incumbent fulcra-coord -> coord2, docs 06 approach C) ---

def cmd_migrate(args: argparse.Namespace, transport: Any) -> int:
    res = migrate_mod.migrate(
        transport, args.team, now=_iso(_now()), source=args.source,
        dry_run=args.dry_run, mark=not args.no_mark,
        include_terminal=args.include_terminal, limit=args.limit,
    )
    if args.dry_run:
        print(f"DRY RUN — {len(res['planned'])} task(s) would migrate "
              f"({res['skipped']} already migrated/skipped):")
        for line in res["planned"]:
            print(f"  {line}")
    else:
        print(f"migrated {res['migrated']} task(s) to team/{args.team} "
              f"({res['skipped']} skipped as already-migrated, {res['marked']} marked on the incumbent)")
    for err in res["errors"]:
        print(f"  ERROR: {err}", file=sys.stderr)
    if res["errors"]:
        return 1
    print("(run `coord-engine reconcile` on the team to index the migrated tasks)")
    return 0


# --- operator loop (fulcra-agent-operator): asks + answer ---

def cmd_asks(args: argparse.Namespace, transport: Any) -> int:
    rows = _load_rows(transport, args.team)
    got = query.asks(rows, now=_iso(_now()), human=args.human or _human())
    if args.json:
        print(json.dumps(got, indent=2))
        return 0
    print(f"asks — {len(got)} waiting on {args.human or _human()} (oldest first)")
    for r in got:
        age = "?" if r.get("age_hours") is None else f"{r['age_hours']:g}h"
        print(f"  [{age:>6}] [{r.get('priority')}] {r.get('title')}")
        ask = str(r.get('blocked_on') or r.get('next_action') or '').strip()
        if ask:
            print(f"           ask: {ask[:140]}")
        print(f"           slug: {r.get('name')}  owner: {r.get('owner')}")
    return 0


def cmd_answer(args: argparse.Namespace, transport: Any) -> int:
    path = _task_path(args.team, args.name)
    try:
        doc, owner = tasks.apply_answer(transport.read(path), now=_iso(_now()),
                                        answer=args.with_text, relayer=_host(),
                                        human=args.human or _human())
    except tasks.TaskError as e:
        print(f"answer failed: {e}", file=sys.stderr)
        return 1
    if not transport.write(path, doc):
        print("answer failed: write did not land", file=sys.stderr)
        return 1
    print(f"answered {args.name} -> handed back to {owner} (unblocked; will surface in their inbox)")
    return 0


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
    hl = sub.add_parser("health", help="fleet health: which hosts reconcile this team (fulcra-agent-health)")
    hl.add_argument("team"); add_json(hl)
    hl.set_defaults(func=cmd_health)

    us = sub.add_parser("usage", help="ATC cap ledger (fulcra-agent-atc)")
    ussub = us.add_subparsers(dest="usage_command", required=True)
    ul = ussub.add_parser("log", help="record spend against an account after a dispatch")
    ul.add_argument("team"); ul.add_argument("--account", required=True)
    ul.add_argument("--tier", required=True); ul.add_argument("--units", type=int, default=0)
    ul.add_argument("--throttled", action="store_true"); ul.add_argument("--agent")
    ul.add_argument("--model", help="model id this spend attributes to (for outcome routing)")
    ul.add_argument("--task-class", dest="task_class",
                    help="capability tag the work exercised (taxonomy-validated)")
    ul.add_argument("--outcome", choices=["clean", "rework", "escalated"],
                    help="how the dispatched work turned out (feeds the demotion fold)")
    ul.set_defaults(func=cmd_usage_log)

    hr = sub.add_parser("headroom", help="per-account cap headroom fold (fulcra-agent-atc)")
    hr.add_argument("team"); hr.add_argument("--json", action="store_true")
    hr.set_defaults(func=cmd_headroom)

    rt = sub.add_parser("route", help="rank models covering needs by cost + headroom (fulcra-agent-atc)")
    rt.add_argument("team")
    rt.add_argument("--needs", required=True,
                    help="comma-separated capability tags (e.g. code,long-context)")
    rt.add_argument("--json", action="store_true")
    rt.set_defaults(func=cmd_route)

    at = sub.add_parser("atc", help="ATC reports (fulcra-agent-atc)")
    atsub = at.add_subparsers(dest="atc_command", required=True)
    atr = atsub.add_parser("report",
                           help="team dispatch/tier/calibration report over the last N days")
    atr.add_argument("team")
    atr.add_argument("--days", type=int, default=7,
                     help="trailing window in days (default 7)")
    atr.add_argument("--json", action="store_true")
    atr.set_defaults(func=cmd_atc_report)

    dsh = sub.add_parser("dash",
                         help="serve the localhost ATC gauge dashboard (127.0.0.1 only)")
    dsh.add_argument("team")
    dsh.add_argument("--port", type=int, default=8787,
                     help="loopback port to bind (default 8787)")
    dsh.set_defaults(func=cmd_dash)

    dr = sub.add_parser("doctor", help="local preflight: tooling + store reachability")
    dr.add_argument("team", nargs="?")
    dr.set_defaults(func=cmd_doctor)

    ak = sub.add_parser("asks", help="waiting-for-operator asks, oldest first (orchestrator pull)")
    ak.add_argument("team"); ak.add_argument("--human"); add_json(ak)
    ak.set_defaults(func=cmd_asks)
    aw = sub.add_parser("answer", help="operator return-leg: unblock + answer + hand back to owner")
    aw.add_argument("team"); aw.add_argument("name")
    aw.add_argument("--with", dest="with_text", required=True, help="the answer text")
    aw.add_argument("--human", help="operator handle (default $FULCRA_COORD_HUMAN or 'human') — must match the handle used with `asks`")
    aw.set_defaults(func=cmd_answer)

    bf = sub.add_parser("briefing", help="one-call session-start bundle (tolerates absent add-ons)")
    bf.add_argument("team"); bf.add_argument("--agent", "-a"); add_json(bf)
    bf.set_defaults(func=cmd_briefing)

    dg = sub.add_parser("digest", help="operator digest: blocked-on-you / upcoming / agents / stale")
    dg.add_argument("team"); dg.add_argument("--human"); add_json(dg)
    dg.add_argument("--store", action="store_true",
                    help="persist to _coord/digests/<date>-<window>.md (deduped per day+window)")
    dg.set_defaults(func=cmd_digest)
    es = sub.add_parser("escalate", help="role-vacancy sweep -> daily marker + P1 directive to maintainer")
    es.add_argument("team")
    es.set_defaults(func=cmd_escalate)

    fg = sub.add_parser("forge", help="mirror GitHub PR signals into review evidence (fulcra-agent-forge)")
    fgsub = fg.add_subparsers(dest="forge_command", required=True)
    fgm = fgsub.add_parser("mirror", help="one pass: PR state -> evidence shards + auto-verdict on merge")
    fgm.add_argument("team")
    fgm.add_argument("--repo", help="owner/name allowlist: mirror ONLY PR urls of this repo")
    fgm.set_defaults(func=cmd_forge_mirror, runner=None)

    mg = sub.add_parser("migrate", help="one-shot exporter: incumbent fulcra-coord tasks -> this team (docs 06)")
    mg.add_argument("team")
    mg.add_argument("--source", default="/coordination")
    mg.add_argument("--dry-run", action="store_true")
    mg.add_argument("--no-mark", dest="no_mark", action="store_true",
                    help="rehearsal: don't tag incumbent tasks migrated:coord2")
    mg.add_argument("--include-terminal", action="store_true")
    mg.add_argument("--limit", type=int)
    mg.set_defaults(func=cmd_migrate)

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
    rvq = rvsub.add_parser("request", help="open a review with required reviewers (durable obligation)")
    rvq.add_argument("team"); rvq.add_argument("name", help="slug or title")
    rvq.add_argument("--of", required=True, help="artifact under review (PR url or description)")
    rvq.add_argument("--reviewer", action="append", required=True,
                     help="required reviewer (role preferred); repeat for many")
    rvq.add_argument("--from", dest="sender", help="requesting agent (defaults to host)")
    rvq.set_defaults(func=cmd_review_request)
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
    ctc = ctsub.add_parser("checkpoint", help="get/set a role's durable checkpoint_ref")
    ctc.add_argument("team"); ctc.add_argument("--role", required=True); ctc.add_argument("--ref")
    ctc.set_defaults(func=cmd_continuity_checkpoint)
    ctp = ctsub.add_parser("park", help="session-exit: snapshot every held role + set checkpoint_refs")
    ctp.add_argument("team"); ctp.add_argument("--agent", "-a"); ctp.add_argument("--objective")
    ctp.add_argument("--next", action="append"); ctp.add_argument("--open-question", action="append", dest="open_question")
    ctp.set_defaults(func=cmd_continuity_park)

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
