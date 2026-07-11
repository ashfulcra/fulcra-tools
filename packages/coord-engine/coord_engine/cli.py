"""CLI for coord-engine — the shared coord engine.

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
import hashlib
import json
import os
import pathlib
import secrets
import socket
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import aggregate, annotate as annotate_mod, atc, atc_dash, continuity, continuity_audit, digest as digest_mod, directives, forge as forge_mod, health as health_mod, migrate as migrate_mod, okf, presence, query, review, roles, tasks, threads as threads_mod
from . import reconcile as rec
from .log import get_logger
from .transport import FulcraFileTransport, TransportError

__all__ = ["main"]

_log = get_logger("cli")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _host() -> str:
    return os.environ.get("FULCRA_COORD_AGENT") or f"coord-reconcile:{socket.gethostname()}"


def _human() -> str:
    return os.environ.get("FULCRA_COORD_HUMAN") or "human"


def _known_sender(args: argparse.Namespace) -> Optional[str]:
    """The sender identity a reply would be addressed to, or None when only the
    anonymous host fallback is available. `_create_directive` records ownership as
    ``--from`` or ``FULCRA_COORD_AGENT`` (else ``coord-reconcile:<host>``); the
    breadcrumb points others at ``listen --agent <sender>``, so we print it only
    when the sender is a real identity someone actually listens as — never the
    bare host tag."""
    return getattr(args, "sender", None) or os.environ.get("FULCRA_COORD_AGENT")


def _replies_breadcrumb(team: str, sender: str) -> str:
    return f"replies: coord-engine listen {team} --agent {sender}"


#: Read-cap for the freshness overlay: at most this many absent-from-index docs
#: are read per row load. The overlay's normal bound is new-since-reconcile items
#: (typically zero or a handful), but under a SUSTAINED reconcile outage that set
#: grows without limit — 50 new docs would mean 50 reads per surface-read, per
#: agent, fleet-wide. A capped-but-VISIBLE overlay (the truncation degrades the
#: inbox source) beats both silent truncation and unbounded reads.
DEFAULT_OVERLAY_CAP = 16


def _overlay_cap() -> int:
    """Env override ``COORD_OVERLAY_CAP`` (same parse discipline as the budgets):
    anything unparseable or non-positive falls back to the default — a bad env
    value must never disable the bound."""
    raw = os.environ.get("COORD_OVERLAY_CAP")
    if raw is None:
        return DEFAULT_OVERLAY_CAP
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_OVERLAY_CAP
    return v if v > 0 else DEFAULT_OVERLAY_CAP


#: Time budget (seconds) for the freshness overlay's doc reads. The cap bounds
#: READ COUNT, not TIME: under partial degradation (listing succeeds, each doc
#: read runs to the transport's subprocess timeout) 16 absent names could mean
#: minutes of serial timeouts inside EVERY canonical surface read — inbox/
#: needs-me/listen have no other budget on this path (the briefing budget opens
#: only AFTER _load_rows). That latency is the hang class this branch kills;
#: the overlay carries its own deadline so a watcher's tick can never starve on
#: it. Fast failures (a doc deleted between list and read returns quickly) keep
#: the continue-and-degrade behavior — the budget only stops the SLOW bleed.
DEFAULT_OVERLAY_BUDGET = 10.0


def _overlay_budget() -> float:
    """Env override ``COORD_OVERLAY_BUDGET`` (same parse discipline as
    ``COORD_BRIEFING_BUDGET``): unparseable, non-positive, NaN, or inf falls back
    to the default — a bad env value must never disable the bound."""
    raw = os.environ.get("COORD_OVERLAY_BUDGET")
    if raw is None:
        return DEFAULT_OVERLAY_BUDGET
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_OVERLAY_BUDGET
    if not (v > 0) or v == float("inf"):  # NaN, <=0, inf -> default
        return DEFAULT_OVERLAY_BUDGET
    return v


def _fresh_overlay_rows(
    transport: Any, team: str, index_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], bool, str]:
    """Freshness overlay (Task 2.5, the PR348 false-clear).

    ``inbox``/``listen``/every canonical surface read the reconcile-built summaries
    index, so a task/directive doc written BETWEEN reconciles is invisible to all of
    them until the next heartbeat rebuild (live-repro'd: delivered 14:05:29Z, raw-
    file-visible 14:07Z, inbox-visible 14:11Z — a watcher polling the canonical
    surface misses fresh work for up to a reconcile period). When the index is
    present+readable we ALSO list the task dir once and parse ONLY docs whose slug is
    ABSENT from the index (bounded by new-since-reconcile items — typically zero or a
    handful — and hard-capped at ``COORD_OVERLAY_CAP``), unioning them into the fold.
    Rows already in the index are NOT re-read: the index row wins, so this is
    behavior-preserving for every summarized doc.

    Returns ``(overlay_rows, ok, reason)``. ``ok`` flips False — degrading the inbox
    source visibly, never silent, while the index rows are still served — when:
      * the task-dir LISTING raised (the overlay's view is unknown);
      * a LISTED absent doc could not be READ (None/raise): the listing just proved
        the doc exists, so an unreadable read is a transport problem, not a
        sanctioned skip — silently dropping it is the false-clear class this branch
        kills, at the overlay's own read step;
      * the absent set exceeded the cap (truncated — served subset is deterministic:
        absent names are read in sorted order, so every agent converges on the SAME
        served subset; the reason carries {served, absent_total});
      * the ``COORD_OVERLAY_BUDGET`` deadline expired with docs still unread (the
        cap bounds read COUNT, this bounds TIME — slow per-doc reads must not
        starve a surface read/watcher tick; checked AFTER each read, the after-op
        discipline). Everything read so far is still served. When both the budget
        and the cap trip, the budget reason wins (it is the truthful one — the cap
        wasn't what stopped us).
    Parse-garbage / not-a-Task docs remain sanctioned SILENT skips (mirrors
    reconcile's own tolerance). Cost: one extra ``list_dir`` per row load, plus one
    ``read`` per genuinely-new (unsummarized) slug, at most the cap, within the
    budget."""
    deadline = time.monotonic() + _overlay_budget()
    prefix = rec.task_prefix(team)
    try:
        listing = transport.list_dir(prefix)
    except Exception:
        # listing unknown -> degraded (caller surfaces it), never silent
        return [], False, "task-dir overlay unreadable"
    from . import model
    known = {str(r.get("name")) for r in index_rows if isinstance(r, dict)}
    absent: list[tuple[str, Any]] = []
    for entry in listing:
        name = entry.get("name") or ""
        if entry.get("is_dir") or not name.endswith(".md") or name in ("index.md", "log.md"):
            continue
        if name[:-3] in known:
            continue  # index row wins — never re-read an already-summarized doc
        absent.append((name, entry))
    absent.sort(key=lambda p: p[0])  # deterministic served subset under the cap
    cap = _overlay_cap()
    overlay: list[dict[str, Any]] = []
    ok, reason = True, ""
    served = 0
    budget_breached = False
    for name, entry in absent[:cap]:
        try:
            raw = transport.read(f"{prefix}{name}")
        except Exception:
            raw = None
        served += 1
        if raw is None:
            # LISTED but unreadable: a transport problem on a doc we know exists.
            # Degrade visibly (never a silent vanish); other overlay docs + the
            # index rows are still served. A FAST failure (doc deleted between
            # list and read) keeps this continue-and-degrade path — only the
            # budget check below stops the slow bleed.
            ok = False
            reason = f"task-dir overlay: fresh doc {name} unreadable"
        else:
            try:
                fm = okf.parse_frontmatter(raw)
                if fm is not None and model.is_task(fm):
                    overlay.append(model.row_from_frontmatter(
                        fm, name=name[:-3], path=f"task/{name}", mtime=entry.get("mtime")))
                # else: parse-garbage / not a Task -> sanctioned silent skip
            except Exception:
                pass  # malformed content is a skip, not a transport failure
        if time.monotonic() > deadline:
            # After-op discipline: the budget bounds TIME where the cap bounds
            # COUNT — stop reading, serve what we have, degrade visibly.
            budget_breached = True
            break
    if budget_breached and served < len(absent):
        ok = False
        reason = (f"task-dir overlay budget exhausted: served {served} of "
                  f"{len(absent)} fresh docs")
    elif len(absent) > cap:
        ok = False
        reason = (f"task-dir overlay truncated: served {cap} of {len(absent)} "
                  f"fresh docs (COORD_OVERLAY_CAP={cap})")
    return overlay, ok, reason


def _load_rows_status(transport: Any, team: str) -> tuple[list[dict[str, Any]], bool, str]:
    """Summaries rows plus whether the fold was fully READABLE (``ok``) and, when it
    was not, a short ``reason`` for the degraded surface to print (attribution: a
    summaries-index failure and a freshness-overlay failure are different outages
    and must not report as one another). ``ok`` is False for an index we could not
    read as intended — present-but-unparseable, or a read/listing that failed under
    a degraded transport — AND for a freshness-overlay problem (listing raised, a
    listed fresh doc unreadable, or the overlay read-cap truncated the fresh set).
    A genuinely-absent index (a fresh team, no reconcile yet) is empty-and-readable
    (``ok`` True): absence is a normal empty state, never conflated with failure.

    ``read`` returning None is ambiguous (absent vs transport-down — the T1 lesson),
    so a None is disambiguated with one parent listing: ``list_dir`` RAISES on a
    transport failure and its entry names distinguish missing from present-but-
    unreadable (the #343 discipline). This is what lets `listen` surface a summaries
    failure instead of folding it to a silent [] indistinguishable from empty."""
    path = rec.summaries_path(team)
    try:
        raw = transport.read(path)
    except Exception:
        return [], False, "summaries index unreadable"
    if raw:
        try:
            rows = aggregate.aggregate_rows(json.loads(raw))
        except Exception:
            # index present but corrupt -> unreadable, surface it
            return [], False, "summaries index unreadable"
        # Live-freshness overlay: union in task docs written since the last
        # reconcile (absent from this index). Any overlay problem flips ``ok`` so
        # the inbox source degrades visibly; the index rows are still served.
        overlay, overlay_ok, overlay_reason = _fresh_overlay_rows(transport, team, rows)
        return rows + overlay, overlay_ok, overlay_reason
    parent, entry = path.rsplit("/", 1)
    try:
        names = {e.get("name") for e in transport.list_dir(parent + "/")}
    except TransportError:
        # transport down -> unknown, not a confirmed-empty index
        return [], False, "summaries index unreadable"
    if entry in names:
        # index there yet unreadable (read returned None) -> degraded
        return [], False, "summaries index unreadable"
    return [], True, ""  # genuinely absent -> a real, readable empty


def _load_rows(transport: Any, team: str) -> list[dict[str, Any]]:
    return _load_rows_status(transport, team)[0]


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
    # Shared add-on deadline (see _briefing_budget): opened here so the forge
    # fan-out is bounded cumulatively, not per-section. pending-reviews keeps its
    # own independent, already-shipped budget.
    deadline = time.monotonic() + _briefing_budget()
    got += _pending_reviews_for(transport, args.team, args.agent)
    got += _forge_feedback_for(transport, args.team, args.agent, deadline=deadline)
    if args.json:
        print(json.dumps(got, indent=2))
    else:
        print(f"{len(got)} item(s) need {args.agent}:")
        for r in got:
            if r.get("type") == "review-pending":
                print(f"  [REVIEW] pending verdict: {r['name']} "
                      f"(required: {', '.join(r['pending_required'])})")
            elif r.get("type") == "review-fold-degraded":
                print(_review_degraded_line(r))
            elif r.get("type") == "review-orphan":
                print(f"  [REVIEW] orphan review dir (verdicts, no doc): "
                      f"{r['name']} — needs maintainer repair")
            elif r.get("type") == "review-role-degraded":
                print(f"  review role resolution degraded: "
                      f"{', '.join(r.get('roles') or [])} — holders unknown, retry")
            elif r.get("type") == "forge-feedback":
                print(_forge_feedback_line(r))
            elif r.get("type") == "forge-degraded":
                print(_forge_degraded_line(r))
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
    # A None role-doc read is DISAMBIGUATED with one roles/ listing (fetched only
    # on the None path, so healthy queries pay nothing): doc listed-but-unreadable
    # = transport failure = UNKNOWN rc 1 — a transient doc-read failure must not
    # collapse a long-SLA role onto the 24h default and print a false VACANT.
    # Doc genuinely ABSENT keeps the default-SLA fallback: querying an
    # unregistered role (leases without a doc — `roles claim` supports it) still
    # works. This supersedes the earlier single-read-ambiguity rationale: the
    # disambiguator (`_roles_listing_names`) now exists and its cost lands only
    # on the already-degraded path.
    raw_doc = transport.read(_role_doc_path(team, role))
    if raw_doc is None:
        names = _roles_listing_names(transport, team)
        if names is None or f"{role}.md" in names:
            print(f"role doc unreadable for {role} in team/{team} — "
                  f"state unknown, degraded transport, retry", file=sys.stderr)
            return 1
    reg = okf.parse_frontmatter(raw_doc) or {}
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
            fm = okf.parse_frontmatter(transport.read(_leases_prefix(team, role) + n))
            if fm is None:
                # A JUST-LISTED lease shard read None/unparseable: folding it out
                # as `{}` (timestamp lost -> stale) would be a hidden vacancy.
                leases = None  # UNKNOWN
                break
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
    if status == roles.UNKNOWN:
        # FAIL CLOSED (2026-07-11): the lease listing was unreadable, so the role's
        # state is UNKNOWN — NOT vacant. A degraded transport must not let a caller
        # read this as VACANT and fire a false SLA escalation. rc 1, same register
        # as `review status`'s "tally unknown" (leases dropped/None never asserts).
        print(f"lease state unknown for role {role} in team/{team} — "
              f"degraded transport, retry", file=sys.stderr)
        return 1
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


# Settled-skip: once a review reaches a terminal APPROVED state with no
# outstanding required reviewers, a tiny cache marker is dropped IN the verdicts
# prefix (so the ONE listing the fold already does reveals it — zero extra
# reads). It is not a `.md` file, so the verdict-reading loop already ignores it.
# CONTRACT: a settled review is IMMUTABLE — a new verdict on it is a no-op by
# definition (already APPROVED, required list frozen), and changing the required
# set re-opens the review only via a NEW slug. The marker is a fold cache, never
# a source of truth: `review status` recomputes the full tally every time and so
# self-heals a wrong/stale marker on direct query.
SETTLED_MARKER = ".settled"

DEFAULT_REVIEW_FOLD_BUDGET = 45.0
DEFAULT_BRIEFING_BUDGET = 60.0

#: Aggregate deadline (seconds) for the `threads` fold's per-candidate shard/doc
#: reads (ash-activity attribution + intent_by). Bounds the same slow-bleed class
#: the overlay/briefing budgets do: N principal candidates x per-doc transport
#: timeout under a degraded transport. On breach the fold STOPS and emits a
#: `threads-degraded` row (never silence, never crash).
DEFAULT_THREADS_FOLD_BUDGET = 30.0

#: `threads` window defaults (spec §Surface 1). CLI flags override these, and env
#: `COORD_THREADS_SILENCE_DAYS` / `COORD_THREADS_INTENT_GRACE_HOURS` override the
#: defaults when no flag is passed (flag > env > default).
DEFAULT_THREADS_SILENCE_DAYS = 3.0
DEFAULT_THREADS_INTENT_GRACE_HOURS = 48.0


def _settled_marker_path(team: str, slug: str) -> str:
    return _verdicts_prefix(team, slug) + SETTLED_MARKER


def _review_fold_budget() -> float:
    """Aggregate deadline for `_pending_reviews_for`, seconds. Env override
    ``COORD_REVIEW_FOLD_BUDGET``; anything unparseable or non-positive falls back
    to the default (never let a bad env value disable the fold or make it hang)."""
    raw = os.environ.get("COORD_REVIEW_FOLD_BUDGET")
    if raw is None:
        return DEFAULT_REVIEW_FOLD_BUDGET
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_REVIEW_FOLD_BUDGET
    if not (v > 0) or v == float("inf"):  # NaN, <=0, inf -> default
        return DEFAULT_REVIEW_FOLD_BUDGET
    return v


def _briefing_budget() -> float:
    """Aggregate deadline (seconds) for the transport-heavy briefing/needs-me
    add-on sections — chiefly the team-global forge-feedback fan-out, which did
    unbounded per-PR reads and hung the whole bundle under a degraded transport.
    ONE budget opens when the add-on stack begins and is spent cumulatively: an
    absolute ``time.monotonic()`` deadline is computed at that point and passed
    to each transport-heavy section, so time already burned by an earlier section
    shrinks what the next one gets (pending-reviews keeps its own, independent and
    already-shipped ``COORD_REVIEW_FOLD_BUDGET``; whichever bound is sooner wins).
    Env override ``COORD_BRIEFING_BUDGET``; anything unparseable, non-positive, or
    inf falls back to the default (a bad env value must never disable the bound or
    make the fold hang)."""
    raw = os.environ.get("COORD_BRIEFING_BUDGET")
    if raw is None:
        return DEFAULT_BRIEFING_BUDGET
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_BRIEFING_BUDGET
    if not (v > 0) or v == float("inf"):  # NaN, <=0, inf -> default
        return DEFAULT_BRIEFING_BUDGET
    return v


def _write_settled_marker(transport: Any, team: str, slug: str, *, now: str) -> None:
    """Best-effort settled-cache write. Failure is swallowed: the marker only
    speeds the fan-out fold; its absence just means the next fold recomputes."""
    try:
        transport.write(
            _settled_marker_path(team, slug),
            okf.render_frontmatter({"schema": "review-settled/v1",
                                    "state": review.APPROVED, "ts": now}),
        )
    except Exception:
        pass


def _is_settleable(tally: dict[str, Any]) -> bool:
    """True only for a tally that may be CACHED as settled: APPROVED, nothing
    pending, and a parsed NON-EMPTY required list. The required gate is the
    false-settle guard: ``transport.read()`` returns None on failure (incl.
    timeout — it never raises), so a transient doc-read failure yields
    required=None and ``review.tally(..., required=None)`` goes APPROVED off any
    one readable approval verdict — cache that and a genuinely-pending review is
    hidden from every fold, durably. ``review request`` refuses to open a review
    without --reviewer, so an absent/empty required list can only mean doc-read
    failure, doc corruption, or a legacy/forge-style doc — never a legitimate
    settle state. Such tallies stay UNCACHED (re-tallied each fold); only the
    marker write is gated here, never the reported state."""
    return (tally.get("state") == review.APPROVED
            and not tally.get("pending_required")
            and bool(tally.get("required")))


def _tally_from_verdict_entries(
    transport: Any, team: str, slug: str, entries: list[dict[str, Any]],
    doc_raw: Optional[str], *, deadline: Optional[float] = None,
) -> tuple[dict[str, Any], bool, bool]:
    """Verdict-shard reads -> ``(tally, verdict_reads_ok, fully_scanned)``, given
    an already-fetched verdicts listing and the already-read review doc
    (``doc_raw``). A None ``doc_raw`` means the doc read failed or the doc is
    missing — callers on the fold path must treat that as UNKNOWN (skip +
    count), not pass it here; this helper just tallies what it is given.

    ``verdict_reads_ok`` is False when any listed verdict file's read returned
    None (transport failure — the file EXISTS, its content is unknown): the
    tally is then a floor, not the truth — a lost CHANGES verdict would look
    APPROVED — so settle-marker writers must not cache it. A file that reads
    fine but parses to garbage is NOT a read failure (garbage is simply not a
    verdict). Split out so the fan-out fold can list ONCE, short-circuit on
    `.settled`, read the doc, and only then pay for the verdict reads.

    ``deadline`` (F2) is an absolute ``time.monotonic()`` instant bounding the
    per-verdict read loop: ONE review with many shards would otherwise read every
    shard unbounded (N x transport.timeout), blowing the aggregate fold budget
    with no degraded marker. The deadline is checked BOTH before and AFTER each
    shard read: a strict wall-clock bound is impossible without cancellable
    transport, so the guarantee is that an overrun is DETECTED and REPORTED
    immediately after the blocking op (a single stalled read that sleeps past the
    budget can no longer return a clean row) — budget overshoot is bounded by ONE
    transport timeout. On expiry the loop STOPS mid-slug and returns
    ``fully_scanned=False`` — the partial tally is a floor the caller MUST NOT
    trust (it counts the slug as skipped, surfaces the degraded marker). None
    (``review status``, no budget) never bounds and always scans fully."""
    req_doc = okf.parse_frontmatter(doc_raw) or {}
    required = req_doc.get("required")
    if isinstance(required, str):
        required = [r.strip() for r in required.split(",") if r.strip()]
    elif isinstance(required, list):
        required = [str(r).strip() for r in required if str(r).strip()]
    verdicts: list[dict[str, Any]] = []
    reads_ok = True
    fully_scanned = True
    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md"):
            continue
        if deadline is not None and time.monotonic() >= deadline:
            # Budget expired mid-slug: stop reading shards. The tally built so far
            # is a floor, not the truth — the caller treats this slug as skipped.
            fully_scanned = False
            break
        raw_v = transport.read(_verdicts_prefix(team, slug) + n)
        if deadline is not None and time.monotonic() >= deadline:
            # The deadline passed DURING this read (F2/P1-B): checking only BEFORE
            # the read let one stalled read complete and return a clean row despite
            # blowing the budget. Detect the overrun immediately after the blocking
            # op — the slug is not fully scanned. Overshoot is bounded by ONE read.
            fully_scanned = False
            break
        if raw_v is None:
            reads_ok = False  # listed file unreadable -> tally is incomplete
        fm = okf.parse_frontmatter(raw_v) or {}
        # Key by the FILENAME stem (ACL-controlled path), not the frontmatter
        # `reviewer:` — otherwise a file `mallory.md` claiming `reviewer: alice`
        # could shadow alice's real verdict. One verdict file per reviewer.
        verdicts.append({"reviewer": n[:-3], "verdict": fm.get("verdict")})
    return review.tally(verdicts, required=required), reads_ok, fully_scanned


def _review_tally(
    transport: Any, team: str, slug: str
) -> tuple[dict[str, Any], bool, bool, bool]:
    """Shared review fold: doc + verdict shards ->
    ``(tally, doc_ok, verdict_reads_ok, listing_ok)``.

    ALWAYS computes the full tally — it never consults the `.settled` marker, so
    a corrupt/stale marker can never hide the truth on a direct `review status`
    query (the marker only serves the fan-out fold, `_pending_reviews_for`).

    ``doc_ok`` is False when the review doc could not be read (missing OR
    transport failure — ``read()`` returns None for both, indistinguishably):
    the tally was built on NO required list and must be treated as unknown,
    never as a clean state. ``verdict_reads_ok`` is False when a listed verdict
    file's content could not be read — the tally is a floor, not the truth.

    ``listing_ok`` is False when the verdicts LISTING raised (the prefix is
    unlistable under a degraded transport). We still fall back to ``entries=[]``
    so this never crashes, but that fallback makes ``verdict_reads_ok`` vacuously
    True (no listed files = no failed reads) and the tally a floor built over
    ZERO verdicts — so the caller MUST treat a False ``listing_ok`` exactly like
    the other unknowns (fail closed; never a clean state, never a marker
    delete/write). An EMPTY-but-readable listing (list_dir returns []) is a
    legitimate no-verdicts PENDING and keeps ``listing_ok`` True."""
    raw = transport.read(_review_doc_path(team, slug))
    listing_ok = True
    try:
        entries = transport.list_dir(_verdicts_prefix(team, slug))
    except TransportError:
        entries = []
        listing_ok = False
    # No deadline: `review status` is a direct, per-slug query with no fold
    # budget, so it always scans every verdict shard (fully_scanned ignored).
    tally, vok, _ = _tally_from_verdict_entries(transport, team, slug, entries, raw)
    return tally, raw is not None, vok, listing_ok


def _roles_listing_names(transport: Any, team: str) -> Optional[set[str]]:
    """Entry names under ``team/<team>/roles/``, or None if the listing itself
    raised (membership UNKNOWN). The disambiguator for a role-doc ``read`` that
    returned None: listed-but-unreadable = transport failure; absent = genuinely
    not a role."""
    try:
        return {(e.get("name") or "") for e in transport.list_dir(f"team/{team}/roles/")}
    except TransportError:
        return None


def _role_fresh_holders(
    transport: Any, team: str, name: str, *, now: str,
    listing_cache: Optional[dict[str, Any]] = None,
) -> tuple[list[str], bool]:
    """Fresh lease holders of role name per the CANONICAL fold: the role
    doc's own sla_hours (falling back to the default) fed to
    roles.fresh_holders — the same fold roles status uses, so the two
    can never disagree about a lease.

    Returns ``(holders, ok)``. FAIL CLOSED (2026-07-11, tightened per codex P1):
    ``ok`` is False whenever the lease state is UNKNOWN — never let a degraded
    transport read as "no holders" (asserting vacancy / silently dropping
    role-routed work). UNKNOWN cases:

    - the lease LISTING raises ``TransportError``;
    - a JUST-LISTED lease shard reads None or unparseable (previously ``or {}``
      dropped its timestamp and silently folded the holder out as stale — a
      fail-open vacancy INSIDE the fold);
    - the role-doc read returns None while the name IS present in the roles/
      listing (or that listing itself raised): listed-but-unreadable is a
      transport failure, not a non-role.

    A doc-read None with the name ABSENT from the listing is a genuine non-role
    (``([], True)``) — the literal-agent-id case stays non-degraded, as does a
    doc that reads fine but isn't frontmatter (affirmative knowledge: not a
    role). ``listing_cache`` (a per-tick/per-fold dict) memoizes the one roles/
    listing across role-shaped assignees; pass the same dict for every call in
    a pass."""
    if "/" in name:
        return [], True  # a role name is a single path segment; anything else is not a role
    raw_doc = transport.read(_role_doc_path(team, name))
    reg = okf.parse_frontmatter(raw_doc)
    if reg is None:
        if raw_doc is not None:
            return [], True  # read fine, just not a role doc -> affirmative non-role
        cache = listing_cache if listing_cache is not None else {}
        if "names" not in cache:
            cache["names"] = _roles_listing_names(transport, team)
        names = cache["names"]
        if names is None or f"{name}.md" in names:
            # roles/ listing unreadable (membership unknown) OR the doc is listed
            # yet unreadable (transport failure): UNKNOWN, fail closed.
            return [], False
        return [], True  # genuinely absent -> not a role (literal agent id case)
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
            fm = okf.parse_frontmatter(transport.read(_leases_prefix(team, name) + fn))
            if fm is None:
                # Listed shard, failed/unparseable read: this lease's freshness is
                # UNKNOWN — folding it out as stale would be a hidden vacancy.
                return [], False
            leases.append({"agent": fm.get("agent") or fn[:-3],
                           "timestamp": fm.get("timestamp")})
    except TransportError:
        return [], False  # lease state UNKNOWN -> fail closed, never assert vacant
    return [str(l.get("agent"))
            for l in roles.fresh_holders(leases, now=now, sla_hours=sla)], True


def _pending_reviews_for(
    transport: Any, team: str, agent: str, *, deadline_seconds: Optional[float] = None
) -> list[dict[str, Any]]:
    """Reviews whose pending_required names the agent — directly or via a role
    it holds a fresh lease on. Best-effort: the top listing failing yields []
    (needs-me/briefing must not fail because the review add-on is absent).

    BOUNDED (2026-07-09 incident fix). Two guards keep a degraded transport from
    turning this into a multi-minute hang read as "bus down":

    - **Settled-skip.** Each unsettled review costs one verdicts listing + a doc
      read + a read per verdict. Once a review is terminal-APPROVED with no
      outstanding required reviewers, a `.settled` marker is dropped IN the
      verdicts prefix; the ONE listing this fold already does then reveals it and
      the slug is skipped with ZERO further reads. The fold also drops that marker
      the first time it computes such a tally, so settled history stops costing.

    - **Aggregate budget.** A wall-clock deadline (default 45s, env
      ``COORD_REVIEW_FOLD_BUDGET``) checked BETWEEN slugs. On breach the scan
      STOPS and a ``review-fold-degraded`` marker (``scanned``/``total``) is
      appended — never a clean-looking partial. A single slug whose tally raises
      ``TransportError`` (Task-1 timeout) or whose review DOC read returns None
      (``read()`` never raises — None here means the read failed, since the slug
      came from the listing) is skipped, counted in ``skipped``, and surfaced
      via the same marker (an unreadable slug is UNKNOWN — not settled, not
      silently pending; partial knowledge must be VISIBLE).

    If review counts keep growing the right home for this is the reconcile
    pre-fold (like task rows) — tracked on the bus."""
    if deadline_seconds is None:
        deadline_seconds = _review_fold_budget()
    out: list[dict[str, Any]] = []
    now = _iso(_now())
    role_holders: dict[str, list[str]] = {}
    degraded_roles: set[str] = set()  # roles whose lease read was UNKNOWN (fail-closed)
    roles_listing_cache: dict[str, Any] = {}  # one roles/ listing per pass (doc-None disambiguation)
    try:
        entries = transport.list_dir(f"team/{team}/review/")
    except TransportError:
        return []
    slug_entries = [
        e for e in entries
        if not e.get("is_dir") and (e.get("name") or "").endswith(".md")
    ]
    # Orphan review dirs (visibility only): a `<slug>/` dir with verdicts but no
    # `<slug>.md` doc is invisible to the doc-keyed scan below. Surface each as a
    # `review-orphan` row EVERY pass (repair stays a human/maintainer action).
    doc_slugs = {(e.get("name") or "")[:-3] for e in slug_entries}
    for e in entries:
        if not e.get("is_dir"):
            continue
        oslug = (e.get("name") or "").rstrip("/")
        if oslug and oslug not in doc_slugs:
            out.append({"type": "review-orphan", "name": oslug})
    total = len(slug_entries)
    scanned = 0
    skipped = 0
    start = time.monotonic()
    deadline = start + deadline_seconds  # absolute monotonic instant (F2)
    for e in slug_entries:
        # Budget is checked BETWEEN slugs (after at least one is scanned, so a
        # slow transport still makes measurable progress before degrading).
        if scanned and time.monotonic() >= deadline:
            marker = {"type": "review-fold-degraded", "scanned": scanned, "total": total}
            if skipped:
                marker["skipped"] = skipped
            out.append(marker)
            return out
        slug = (e.get("name") or "")[:-3]
        scanned += 1
        try:
            ventries = transport.list_dir(_verdicts_prefix(team, slug))
            if any((x.get("name") or "") == SETTLED_MARKER for x in ventries):
                continue  # settled -> skip entirely, zero reads beyond this listing
            doc_raw = transport.read(_review_doc_path(team, slug))
            if doc_raw is None:
                # The slug came from the review/ listing, so its doc exists —
                # a None read is a transport failure (read() returns None on
                # timeout, it never raises). The slug's state is UNKNOWN: not
                # settled, not silently pending. Count it, keep scanning.
                skipped += 1
                continue
            if time.monotonic() >= deadline:
                # The doc read itself pushed us over budget (P1-B): check AFTER the
                # blocking op, not only between slugs. Don't start the verdict
                # reads — this slug is UNKNOWN. Count it skipped and surface the
                # degraded marker; the budget is spent.
                skipped += 1
                out.append({"type": "review-fold-degraded", "scanned": scanned,
                            "total": total, "skipped": skipped})
                return out
            tally, vreads_ok, fully = _tally_from_verdict_entries(
                transport, team, slug, ventries, doc_raw, deadline=deadline)
            if not fully:
                # Budget expired MID-SLUG (F2): a single review with many verdict
                # shards would otherwise read them all unbounded. The partial
                # tally is untrusted. This slug was reached (scanned already
                # counts it), so it joins `skipped` — same accounting as a
                # doc-read failure (scanned includes skipped; unscanned=total-scanned).
                # The budget is spent: stop and surface the degraded marker.
                skipped += 1
                marker = {"type": "review-fold-degraded", "scanned": scanned,
                          "total": total, "skipped": skipped}
                out.append(marker)
                return out
        except TransportError:
            # A single slug's tally timed out (Task-1 contract): skip it, keep
            # scanning the rest, and make the gap visible via `skipped` below.
            skipped += 1
            continue
        state = tally.get("state")
        pending = tally.get("pending_required") or []
        if state == review.APPROVED and not pending:
            # Cache only a PROVEN settle: non-empty required (false-settle
            # guard, see _is_settleable) AND every listed verdict actually read
            # (an unreadable verdict could be a hidden CHANGES).
            if _is_settleable(tally) and vreads_ok:
                _write_settled_marker(transport, team, slug, now=now)
            continue
        if state != "PENDING" or not pending:
            continue
        if agent not in pending:  # direct hit needs no role folding at all
            for r in pending:
                if r not in role_holders:
                    holders, ok = _role_fresh_holders(
                        transport, team, r, now=now,
                        listing_cache=roles_listing_cache)
                    role_holders[r] = holders
                    if not ok:
                        # Fail-closed: the role's lease read is UNKNOWN. Do NOT let
                        # it read as "no holders" (a silently dropped obligation) —
                        # record it so a degraded marker surfaces below.
                        degraded_roles.add(r)
        if review.is_pending_for(pending, agent, role_holders):
            out.append({"type": "review-pending", "name": slug,
                        "state": "PENDING", "pending_required": pending})
    if degraded_roles:
        # A role's lease read degraded: the agent might be a holder we couldn't
        # resolve, so a role-routed obligation may be missing. Make it VISIBLE.
        out.append({"type": "review-role-degraded",
                    "roles": sorted(degraded_roles)})
    if skipped:
        # Completed inside budget but some slugs were unreadable: partial
        # knowledge must be visible, so emit the degraded marker anyway.
        out.append({"type": "review-fold-degraded", "scanned": scanned,
                    "total": total, "skipped": skipped})
    return out


def _review_degraded_line(r: dict[str, Any]) -> str:
    line = (f"  review fold degraded: scanned {r.get('scanned')}/{r.get('total')} "
            f"before budget — run per-slug review status for the rest")
    if r.get("skipped"):
        line += f" ({r['skipped']} slug(s) skipped on transport error)"
    return line


def _forge_responsible(
    transport: Any, team: str, *, deadline: Optional[float] = None
) -> tuple[dict[str, set], bool]:
    """``({pr_slug: {responsible agents}}, ok)``. Responsibility comes from two
    sources, unioned: the watch registry (its ``agent``) and, for review-artifact
    PRs, the review's ``requested_by``. Best-effort — any listing failure is
    skipped so needs-me/briefing never fail because the forge add-on is absent.

    BOUNDED. Both sources are team-global fan-outs (list + one read per entry);
    ``deadline`` (an absolute ``time.monotonic()`` instant, or None for no bound)
    is checked BEFORE and AFTER each blocking op, mirroring the review fold — so a
    degraded transport can no longer turn discovery into an unbounded hang. ``ok``
    is False when a source listing raised OR the budget expired mid-scan: the map
    is then a FLOOR (partial), and the caller must surface a degraded row rather
    than treat the partial responsibility set as complete. A cheap zero-read skip
    for "this agent has no forge responsibility" is NOT possible from one listing:
    responsibility lives in per-file frontmatter across TWO sources, so the budget
    is the guard (the empty-store case already costs only the two empty listings).
    """
    resp: dict[str, set] = {}
    ok = True

    def _expired() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    watch_prefix = f"team/{team}/_coord/forge/watch/"
    try:
        watch_entries = transport.list_dir(watch_prefix)
    except TransportError:
        watch_entries = []
        ok = False
    for e in watch_entries:
        if _expired():
            ok = False
            break
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md"):
            continue
        raw = transport.read(watch_prefix + n)
        if _expired():  # the read pushed us over budget — detect it immediately
            ok = False
            break
        fm = okf.parse_frontmatter(raw) or {}
        slug = forge_mod.pr_slug(fm.get("url")) or n[:-3]
        a = fm.get("agent")
        if a:
            resp.setdefault(slug, set()).add(str(a))
    review_prefix = f"team/{team}/review/"
    try:
        review_entries = transport.list_dir(review_prefix)
    except TransportError:
        review_entries = []
        ok = False
    for e in review_entries:
        if _expired():
            ok = False
            break
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md") or n == "index.md":
            continue
        raw = transport.read(review_prefix + n)
        if _expired():
            ok = False
            break
        fm = okf.parse_frontmatter(raw) or {}
        slug = forge_mod.pr_slug(forge_mod.review_artifact(fm))
        who = fm.get("requested_by")
        if slug and who:
            resp.setdefault(slug, set()).add(str(who))
    return resp, ok


def _forge_slug_feedback(
    transport: Any, team: str, agent: str, slug: str,
    entries: list[dict[str, Any]], prefix: str, deadline: Optional[float],
) -> tuple[Optional[dict[str, Any]], bool]:
    """Feedback row for ONE PR from its already-listed feedback dir, ->
    ``(row_or_None, fully_scanned)``. ``fully_scanned`` is False when the budget
    expired mid-scan (checked before AND after each blocking read): a single PR
    with many shards would otherwise read them all unbounded. A truncated scan is
    UNTRUSTED — the caller discards the partial row and counts the slug skipped,
    exactly as the review fold discards a mid-slug tally."""
    items: list[str] = []
    authors: list[str] = []
    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md"):
            continue
        if deadline is not None and time.monotonic() >= deadline:
            return None, False
        stem = n[:-3]
        acked = transport.read(_ack_path(team, stem, agent))
        if deadline is not None and time.monotonic() >= deadline:
            return None, False
        if acked is not None:
            continue  # acked by this agent — hidden
        raw = transport.read(prefix + n)
        if deadline is not None and time.monotonic() >= deadline:
            return None, False
        items.append(stem)
        fm = okf.parse_frontmatter(raw) or {}
        a = fm.get("author")
        if a and str(a) not in authors:
            authors.append(str(a))
    if items:
        return ({"type": "forge-feedback", "pr_slug": slug, "count": len(items),
                 "authors": sorted(authors), "items": sorted(items)}, True)
    return None, True


def _forge_feedback_for(
    transport: Any, team: str, agent: str, *, deadline: Optional[float] = None
) -> list[dict[str, Any]]:
    """Unacked forge-feedback shards on PRs the agent is responsible for, one
    row per PR: ``{type, pr_slug, count, authors, items}``. Ack state reuses the
    directive ack namespace (``_coord/acks/<item-id>/<agent>.md``) — acked items
    drop; a new node id (new shard) re-surfaces. Best-effort; never raises.

    BOUNDED by the shared briefing ``deadline`` (absolute ``time.monotonic()``,
    None = unbounded/legacy). On any breach — the responsibility scan truncating,
    a per-PR feedback listing raising, or the per-PR shard scan overrunning — a
    single ``forge-degraded`` row ``{scanned, total, skipped}`` is appended (same
    shape/discipline as ``review-fold-degraded``): partial forge knowledge stays
    VISIBLE, the section never hangs the entry fold and never dies silently.
    ``total`` is the count of PRs the agent is responsible for (a floor if the
    responsibility scan itself was truncated); ``scanned`` counts those reached;
    ``skipped`` counts those reached-but-unreadable/cut."""
    out: list[dict[str, Any]] = []
    resp, resp_ok = _forge_responsible(transport, team, deadline=deadline)
    mine = sorted(slug for slug, agents in resp.items() if agent in agents)
    total = len(mine)
    scanned = 0
    skipped = 0
    degraded = not resp_ok  # a truncated/failed responsibility scan is already degraded
    for slug in mine:
        if deadline is not None and time.monotonic() >= deadline:
            degraded = True
            break
        scanned += 1
        prefix = f"team/{team}/_coord/forge/feedback/{slug}/"
        try:
            entries = transport.list_dir(prefix)
        except TransportError:
            # This PR's feedback is UNKNOWN (listing raised): count it skipped and
            # keep scanning the rest — never let one PR sink the whole section.
            skipped += 1
            degraded = True
            continue
        if deadline is not None and time.monotonic() >= deadline:
            # The listing itself pushed us over budget: this PR is unscanned.
            skipped += 1
            degraded = True
            break
        row, fully = _forge_slug_feedback(transport, team, agent, slug, entries, prefix, deadline)
        if not fully:
            # Budget expired mid-shard: the partial row is untrusted, discard it,
            # count the PR skipped, and stop — the budget is spent.
            skipped += 1
            degraded = True
            break
        if row:
            out.append(row)
    if degraded:
        marker: dict[str, Any] = {"type": "forge-degraded", "scanned": scanned,
                                  "total": total}
        if skipped:
            marker["skipped"] = skipped
        out.append(marker)
    return out


def _forge_feedback_line(r: dict[str, Any]) -> str:
    who = ", ".join(r.get("authors") or []) or "?"
    return (f"  [FORGE] feedback on {r.get('pr_slug')}: "
            f"{r.get('count')} item(s) from {who}")


def _forge_degraded_line(r: dict[str, Any]) -> str:
    line = (f"  forge fold degraded: scanned {r.get('scanned')}/{r.get('total')} "
            f"before budget — run forge feedback for the rest")
    if r.get("skipped"):
        line += f" ({r['skipped']} PR(s) skipped on transport error)"
    return line


def _normalize_required(required: Any) -> list[str]:
    """Coerce a doc's ``required:`` field (list or legacy comma-string) into a
    clean list of stripped, non-empty reviewer names — the shape `review.tally`
    and the request-identity comparison both consume."""
    if isinstance(required, str):
        return [r.strip() for r in required.split(",") if r.strip()]
    if isinstance(required, list):
        return [str(r).strip() for r in required if str(r).strip()]
    return []


def _review_request_diff(
    fm: dict[str, Any], *, of: Any, required: list[str], requested_by: str,
) -> Optional[tuple[str, str, str]]:
    """Compare an existing review doc's frontmatter against the request being made.

    Returns ``None`` when it is the SAME request (idempotent recovery), else
    ``(field, existing_value, requested_value)`` naming the FIRST identity field
    that differs. Request identity is ``requested_by`` + ``of`` + the required SET
    (order-normalized): a different requester re-opening someone else's review is a
    conflict (not a silent recovery), and a changed required set re-opens a review
    only via a NEW slug (the settled-review immutability contract)."""
    ex_rb = str(fm.get("requested_by") or "")
    if ex_rb != (requested_by or ""):
        return ("requested_by", ex_rb, requested_by or "")
    ex_of = str(fm.get("of") or "")
    if ex_of != (str(of) if of is not None else ""):
        return ("of", ex_of, str(of) if of is not None else "")
    ex_req = sorted(_normalize_required(fm.get("required")))
    if ex_req != sorted(required):
        return ("required set", ", ".join(ex_req), ", ".join(sorted(required)))
    return None


def _deliver_all_review_directives(
    transport: Any, team: str, slug: str, required: list[str], *, owner: str, of: str,
) -> tuple[list[str], list[str]]:
    """Deliver ONE directive per required reviewer through the canonical hash-slug
    path. Returns ``(delivered, failed)``. Payload-hash dedup makes this idempotent:
    a reviewer whose directive already landed re-verifies as "already delivered"
    (rc 0), so this is safe to re-run on a recovery retry — it fills the gaps."""
    delivered: list[str] = []
    failed: list[str] = []
    for r in required:
        if _deliver_review_directive(transport, team, slug, r,
                                     sender=owner, of=of) == 0:
            delivered.append(r)
        else:
            failed.append(r)
    return delivered, failed


def _print_partial_review_failure(
    slug: str, delivered: list[str], failed: list[str], *, doc_note: str,
) -> None:
    """The loud partial-failure line: names exactly who was NOT notified and who
    was, and points the requester at the retry that dedupes the delivered ones."""
    print(f"review {slug} {doc_note} but reviewer notification FAILED for: "
          f"{', '.join(failed)} (delivered: {', '.join(delivered) or 'none'}) — "
          f"retry the request to re-notify; delivered directives dedupe by payload "
          f"hash", file=sys.stderr)


def _print_review_success(
    args: argparse.Namespace, team: str, slug: str, required: list[str], *,
    recovered: bool,
) -> None:
    if recovered:
        print(f"review {slug} already exists (matching) — re-verified reviewer "
              f"delivery (required: {', '.join(required)})")
    else:
        print(f"review {slug} requested (required: {', '.join(required)})")
    for r in required:
        print(f"  reviewer {r} -> file verdict at {_verdicts_prefix(team, slug)}{r}.md")
    # Point the requester at the await primitive for the verdict wait (they poll
    # `review status`; `listen` is the same arm-a-listener discipline every ask uses).
    sender = _known_sender(args)
    if sender:
        print(f"await verdicts: coord-engine listen {team} --agent {sender}")


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
    owner = getattr(args, "sender", None) or _host()
    existing = transport.read(path)
    if existing is not None:
        # A doc already occupies the slot. This is NOT automatically a conflict:
        # the atomic-delivery partial-failure path below tells the requester to
        # RETRY, and after a partial failure the doc necessarily EXISTS — so a
        # blanket "already exists" rc 1 would strand the un-notified reviewers
        # forever (the exact orphan class this command exists to kill). Parse the
        # doc and adjudicate: matching request -> idempotent recovery; different
        # request -> loud conflict; unparseable -> loud, never overwrite.
        existing_fm = okf.parse_frontmatter(existing)
        if existing_fm is None:
            # Present but unparseable/corrupt: we cannot prove it is OUR request,
            # and overwriting could clobber a live review. Fail loud, never write.
            print(f"review {slug} already exists but is unreadable (corrupt "
                  f"frontmatter) — cannot verify, will not overwrite; retry",
                  file=sys.stderr)
            return 1
        diff = _review_request_diff(existing_fm, of=args.of, required=required,
                                    requested_by=owner)
        if diff is not None:
            field, existing_val, requested_val = diff
            print(f"review {slug} already exists with a different {field} "
                  f"(existing: {existing_val!r}, requested: {requested_val!r}) — a "
                  f"different {field} re-opens a review only via a new slug; "
                  f"refusing to overwrite", file=sys.stderr)
            return 1
        # IDEMPOTENT RECOVERY: same requested_by + of + required set. Skip the doc
        # write (it already holds our request), keep the harmless stale-marker
        # delete (a prior fold may have settled it; its absence just makes the next
        # fold recompute), and RE-RUN reviewer delivery for EVERY required reviewer
        # — hash-path dedup re-verifies the ones that landed (rc 0 "already
        # delivered") and delivers the ones a prior partial failure dropped. This
        # is what makes a partial-delivery retry CONVERGE instead of dying here.
        transport.delete(_settled_marker_path(team, slug))
        delivered, failed = _deliver_all_review_directives(
            transport, team, slug, required, owner=owner, of=args.of)
        if failed:
            _print_partial_review_failure(slug, delivered, failed,
                                          doc_note="already exists (matching)")
            return 1
        _print_review_success(args, team, slug, required, recovered=True)
        return 0
    # existing is None is AMBIGUOUS (T1: a read timeout and a genuinely-absent doc
    # both map to None). Treating it as an empty slot would let a degraded transport
    # clobber a live review (I1 / post-#342). Confirm absence via a directory
    # listing before writing: list_dir RAISES TransportError on failure (loud
    # through main's catch-all), and its entry names distinguish missing from
    # present-but-unreadable. One list_dir per request is cheap.
    parent, entry = path.rsplit("/", 1)
    names = {e.get("name") for e in transport.list_dir(parent + "/")}
    if entry in names:
        # Present in the listing yet the read returned None: transport degraded
        # mid-op. We cannot verify what the doc holds and must not overwrite it.
        print(f"review {slug}: doc present but unreadable (transport degraded) — "
              f"cannot verify, will not overwrite; retry", file=sys.stderr)
        return 1
    # Genuinely absent -> write the fresh review doc.
    fm = {
        "type": "Review",
        "schema": "review-request/v1",
        "requested_by": owner,
        "of": args.of,
        "required": required,
        "ts": _iso(_now()),
    }
    body = f"\nReview requested: {args.of}\n"
    if not transport.write(path, okf.render_frontmatter(fm) + body):
        # T1: a timed-out write returns False, not a raise. An rc-0 "review
        # requested" that never landed is the requester-side incident (mirror of
        # C1). Fail loud so the requester retries rather than believing the
        # obligation is durable.
        print("review request write failed (transport)", file=sys.stderr)
        return 1
    # A fresh doc can carry no stale `.settled` marker, but a since-deleted-and-
    # reopened slug at the same path could; clear it best-effort (delete is
    # timeout-safe -> False, which we ignore) so the next fold recomputes.
    transport.delete(_settled_marker_path(team, slug))
    # Atomic notification: with the doc durably landed, deliver ONE directive per
    # required reviewer through the canonical hash-slug directive path, so a
    # verb-opened review FIRES the reviewer's inbox/listen — this is what removes
    # the reason agents hand-send review tells (the PR-344 orphan class) and makes
    # the listener's `await verdicts` breadcrumb genuine. Same C1 write discipline
    # as the doc: any reviewer-directive fail is reported LOUD naming exactly what
    # landed and what did not (partial is never silent), and the requester's retry
    # re-enters the idempotent-recovery path above to fill the gaps.
    delivered, failed = _deliver_all_review_directives(
        transport, team, slug, required, owner=owner, of=args.of)
    if failed:
        _print_partial_review_failure(slug, delivered, failed,
                                      doc_note="requested (doc written)")
        return 1
    _print_review_success(args, team, slug, required, recovered=False)
    return 0


def cmd_review_status(args: argparse.Namespace, transport: Any) -> int:
    team, slug = args.team, args.slug
    result, doc_ok, vreads_ok, listing_ok = _review_tally(transport, team, slug)
    if not doc_ok:
        # The doc read returned None: missing slug OR transport failure —
        # indistinguishable, and either way the tally is UNKNOWN. Without the
        # required list, one readable approval verdict tallies as a clean
        # APPROVED with pending:[] — printing that (or caching it) under a
        # transient timeout would durably hide a pending review. Fail loud.
        print(f"review status failed: {_review_doc_path(team, slug)} unreadable "
              f"(missing slug or degraded transport) — tally unknown, retry",
              file=sys.stderr)
        return 1
    if not listing_ok:
        # F-listing: the verdicts LISTING raised, so `_review_tally` fell back to
        # entries=[] and the tally is a floor built over ZERO verdicts —
        # vreads_ok is vacuously True. Printing that (a false PENDING) rc 0 gives
        # clean output on a failed listing, and letting the F4 self-heal below
        # run on it would DELETE a legitimate `.settled` marker off a vacuous
        # non-settleable tally. Fail closed FIRST — same register as the doc /
        # shard-unreadable cases — so neither the report nor the marker-delete
        # gate is ever reached on an unknown tally.
        print(f"review status failed: verdicts listing unreadable under "
              f"{_verdicts_prefix(team, slug)} — tally unknown, retry",
              file=sys.stderr)
        return 1
    if not vreads_ok:
        # F1: a listed verdict shard read returned None (the file EXISTS, its
        # content is unknown under a degraded transport). The tally is a FLOOR,
        # not the truth — a lost CHANGES verdict reads as APPROVED. Printing that
        # partial tally rc 0 defeats the exact-slug fail-closed sweep watchers
        # run. Fail closed, same register as the doc-unreadable case.
        print(f"review status failed: verdict shard unreadable under "
              f"{_verdicts_prefix(team, slug)} — tally unknown, retry",
              file=sys.stderr)
        return 1
    # A direct query recomputes the truth (never trusts the marker). doc_ok and
    # vreads_ok are both proven above, so the tally is trustworthy here.
    if _is_settleable(result):
        # PROVEN terminal-settled (non-empty required, every listed verdict read):
        # refresh the fold cache so the fan-out fold can skip this slug next time.
        _write_settled_marker(transport, team, slug, now=_iso(_now()))
    else:
        # F4: a full, trustworthy tally that is NOT settleable, yet a `.settled`
        # marker may linger (e.g. a since-reopened review). It is provably stale —
        # the marker only ever caches a terminal-APPROVED state. Best-effort
        # delete (delete is timeout-safe -> False, ignored) so the next fan-out
        # fold recomputes and sees the pending obligation, complementing the I2
        # re-request delete. Self-healing on direct query.
        transport.delete(_settled_marker_path(team, slug))
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


def _responses_prefix(team: str) -> str:
    return f"team/{team}/_coord/responses/"


def _response_path(team: str, slug: str, stamp: str) -> str:
    return f"team/{team}/_coord/responses/{slug}/{stamp}.md"


def _stamp_for_path(now: str, agent: str) -> str:
    safe_time = now.replace(":", "").replace("-", "").replace(".", "")
    return f"{safe_time}-{tasks.agent_key(agent)}"


def _directive_payload(title: Optional[str], summary: Optional[str],
                       next_action: Optional[str],
                       assignee: Optional[str]) -> tuple[str, str, str, str]:
    """The message-identity fields — title, summary, next_action, ASSIGNEE.

    Identity == path: ``_create_directive`` hashes this payload into the canonical
    directive slug (``<title-slug>-<sha256(payload)[:8]>``), so identical payloads
    map to one path (dedupe by construction) and distinct payloads to distinct
    paths (they can never race). Timestamp, owner, and not_before are delivery
    metadata, not the message, so they never enter the identity/dedup comparison
    (a relay re-sending the same reminder to the same agent is the same message).
    Assignee IS identity: the
    same text told to a DIFFERENT agent is a different directive (each recipient
    must get their copy), while broadcast's ``*`` audience means identical
    re-broadcasts still dedupe — and a broadcast stays distinct from a directed
    tell of the same text (different audiences). None and "" normalize to the
    same value so a missing summary compares equal to an empty one.

    By design, not_before and priority are delivery metadata OUTSIDE this
    identity, so a reschedule or priority change of the same title dedupes onto
    the original doc (keeping its schedule) rather than re-delivering: to re-arm
    with a new schedule or priority, send a new title."""
    def norm(x: Optional[str]) -> str:
        return "" if x is None else str(x)
    return (norm(title), norm(summary), norm(next_action), norm(assignee))


def _doc_payload(doc: Optional[str]) -> Optional[tuple[str, str, str, str]]:
    """Message-identity payload of an existing task doc, or ``None`` when its
    frontmatter won't parse. On the write path an unparseable/corrupt doc at our
    canonical (hash-bearing) slot can no longer be a colliding DIFFERENT message —
    only corruption — so the caller fails loud (cannot verify delivery) rather
    than overwriting: never claim a delivery we can't confirm."""
    fm = okf.parse_frontmatter(doc)
    if fm is None:
        return None
    return _directive_payload(fm.get("title"), fm.get("description"),
                              fm.get("next_action"), fm.get("assignee"))


def _payload_hash(payload: tuple[str, str, str, str]) -> str:
    """Stable short id carried by EVERY directive slug. Hashes the payload (NOT
    the time), so a retry of the same message maps to the same slug (dedupe) and
    distinct messages to distinct slugs (no shared slot to race over)."""
    return hashlib.sha256("\x00".join(payload).encode("utf-8")).hexdigest()[:8]


def _write_directive(transport: Any, args: argparse.Namespace, *, slug: str,
                     content: str, payload: tuple[str, str, str, str], assignee: str,
                     not_before: Optional[str]) -> int:
    """Deliver ``content`` at ``slug`` — whose canonical path already carries the
    payload hash (see ``_create_directive``), so the path IS the message identity.

    Two senders of the SAME payload compute the SAME path and write the SAME
    bytes: a race is idempotent (last-writer-wins is a no-op), so the existence
    of the slot means "already delivered". Distinct payloads land on DISTINCT
    paths and can never race each other — the lost-race case that the old
    read-back guarded against cannot arise, so a read-back MISMATCH now means
    only transport corruption (or an astronomically improbable hash collision),
    never a racer's different message. We never overwrite and never claim a
    delivery we cannot verify.
    """
    path = _task_path(args.team, slug)
    existing = transport.read(path)
    if existing is not None:
        # The path is the payload identity, so an existing readable doc here IS
        # our message. Matching payload -> sanctioned dedup (already delivered).
        if _doc_payload(existing) == payload:
            print(f"directive {slug} already delivered")
            return 0
        # Present but NOT our payload: unparseable/corrupt content (or a hash
        # collision). We cannot verify our message is the one on the bus and must
        # never overwrite — fail loud so the caller retries.
        print(f"directive {slug}: slot holds unverifiable content, "
              f"cannot verify delivery, retry", file=sys.stderr)
        return 1
    # existing is None is AMBIGUOUS (T1: timeout and genuinely-absent both map to
    # None). Treating it as "empty slot" would let a degraded transport clobber an
    # occupied slot (I1). Confirm absence via a directory listing: list_dir RAISES
    # TransportError on failure (loud through main's catch-all), and its entry
    # names distinguish missing from unreadable. One list_dir per tell is fine.
    parent, entry = path.rsplit("/", 1)
    names = {e.get("name") for e in transport.list_dir(parent + "/")}
    if entry in names:
        # Present in the listing yet the read returned None: transport degraded
        # mid-op. Cannot verify delivery, must not overwrite.
        print(f"directive {slug}: slot present but unreadable "
              f"(transport degraded), cannot verify delivery, retry", file=sys.stderr)
        return 1
    # Genuinely absent -> write. A write that fails (T1: False, not a raise) must
    # NOT be reported as delivered (C1): a failed write leaves the slot empty, so
    # a retry re-enters this dedup logic cleanly.
    if not transport.write(path, content):
        print("directive write failed (transport)", file=sys.stderr)
        return 1
    # Post-write read-back as WRITE-VERIFICATION only: None (read-back failed) or a
    # mismatch (corruption) both mean we cannot confirm our bytes landed -> fail
    # loud (C1) rather than claim an unverifiable delivery. A mismatch can no
    # longer mean a lost race (distinct payloads never share this path).
    readback = transport.read(path)
    if readback is None:
        print(f"directive {slug}: write unverifiable (read-back failed, "
              f"transport degraded)", file=sys.stderr)
        return 1
    if _doc_payload(readback) != payload:
        print(f"directive {slug}: write unverifiable (read-back mismatch, "
              f"transport corruption)", file=sys.stderr)
        return 1
    print(f"directive {slug} -> {assignee}"
          + (f" (visible {not_before})" if not_before else ""))
    return 0


def _create_directive(args: argparse.Namespace, transport: Any, *, assignee: str,
                      not_before: Optional[str] = None) -> int:
    # The canonical directive path ALWAYS carries the payload hash: identical
    # payloads (any senders, any order) converge on one path and dedupe by
    # construction; distinct payloads occupy distinct paths and can never race.
    payload = _directive_payload(args.title, args.summary, args.next, assignee)
    slug = f"{tasks.slugify(args.title)}-{_payload_hash(payload)}"
    try:
        _, content = tasks.new_task_doc(
            args.title, now=_iso(_now()), workstream=args.workstream,
            status="proposed", priority=args.priority,
            owner=getattr(args, "sender", None) or _host(), assignee=assignee,
            summary=args.summary or "", next_action=args.next, kind="directive",
            not_before=not_before, slug=slug,
        )
    except tasks.TaskError as e:
        print(f"directive failed: {e}", file=sys.stderr)
        return 1
    rc = _write_directive(transport, args, slug=slug, content=content,
                          payload=payload, assignee=assignee, not_before=not_before)
    # On a delivered ask (not a backlog capture — @backlog awaits no reply), point
    # the sender at the reply leg: the return of `respond` surfaces in their listen.
    if rc == 0 and assignee != directives.BACKLOG:
        sender = _known_sender(args)
        if sender:
            print(_replies_breadcrumb(args.team, sender))
    return rc


def _deliver_review_directive(transport: Any, team: str, slug: str, reviewer: str,
                              *, sender: str, of: str) -> int:
    """Deliver ONE review-request directive to ``reviewer`` via the canonical
    hash-slug directive path — the SAME ``_write_directive`` delivery (payload-hash
    dedup + C1 write-verification) every ``tell`` gets, so a verb-opened review
    NOTIFIES its reviewers instead of relying on a hand-sent tell (the PR-344
    orphan class: a review directive sent by hand, with no verdict target). The
    text carries the exact slug AND the verdict-file path (the fail-closed watcher
    contract). Returns ``_write_directive``'s rc (0 delivered/deduped, 1 failed).

    Distinct (slug, reviewer) pairs produce distinct payloads -> distinct paths,
    so reviewers never collide and a re-request idempotently dedupes."""
    verdict_file = f"{_verdicts_prefix(team, slug)}{reviewer}.md"
    title = f"REVIEW REQUEST: {slug}"
    summary = f"Verdict owed on {of} — file it at {verdict_file}"
    next_action = f"file your verdict at {verdict_file}"
    payload = _directive_payload(title, summary, next_action, reviewer)
    dslug = f"{tasks.slugify(title)}-{_payload_hash(payload)}"
    try:
        _, content = tasks.new_task_doc(
            title, now=_iso(_now()), status="proposed", owner=sender,
            assignee=reviewer, summary=summary, next_action=next_action,
            kind="directive", slug=dslug,
        )
    except tasks.TaskError as e:
        print(f"review-request directive for {reviewer} failed: {e}", file=sys.stderr)
        return 1
    # `_write_directive` only needs args.team; a minimal namespace carries it.
    return _write_directive(transport, argparse.Namespace(team=team), slug=dslug,
                            content=content, payload=payload, assignee=reviewer,
                            not_before=None)


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


def _update_intent_window(transport: Any, path: str, existing: str, *, slug: str,
                          intent_by: str) -> int:
    """Rewrite ONLY ``intent_by`` on an existing intent doc, in place, then verify
    by read-back — the trust-eroding-false-drop guard from Surface 2.

    THE SEAM (deliberate divergence from ``_write_directive``'s read-back): the
    generic write-verification compares ``_doc_payload`` — title/summary/next/
    assignee — and ``intent_by`` is NOT in that tuple. So a window change is
    INVISIBLE to the generic read-back (it would pass a stale-window write as
    verified). The update therefore does its OWN ``intent_by``-specific read-back:
    None/unparseable/mismatch all mean "cannot confirm the new window landed" ->
    rc 1 retry, never a claimed-but-false deadline. Identity fields (title/
    assignee) are untouched, so the slot keeps its identity and later identical
    restatements still dedupe onto it.
    """
    split = okf.split_frontmatter(existing)
    fm = okf.parse_frontmatter(existing)
    if split is None or fm is None:  # defensive: caller already parsed, but never write blind
        print(f"intent {slug}: doc unparseable, cannot verify, retry", file=sys.stderr)
        return 1
    fm["intent_by"] = intent_by
    content = okf.render_frontmatter(fm) + "\n" + split[1]
    if not transport.write(path, content):
        print("intent window update failed (transport)", file=sys.stderr)
        return 1
    # intent_by-SPECIFIC read-back (the seam): confirm the NEW window is on the bus.
    readback = transport.read(path)
    if readback is None:
        print(f"intent {slug}: window update unverifiable "
              f"(read-back failed, transport degraded), retry", file=sys.stderr)
        return 1
    rb = okf.parse_frontmatter(readback)
    if rb is None or str(rb.get("intent_by") or "") != str(intent_by or ""):
        print(f"intent {slug}: window update unverifiable "
              f"(read-back mismatch, transport degraded), retry", file=sys.stderr)
        return 1
    print("intent window updated")
    return 0


def cmd_intent(args: argparse.Namespace, transport: Any) -> int:
    """Capture a spoken commitment as an ``intent:<principal>`` directive.

    DELIBERATE IDENTITY DEVIATION from the plain directive path: an intent's
    identity is ``text + assignee ONLY`` — ``intent_by`` (the declared window) is
    EXCLUDED from the hash-slug. Restating the SAME commitment with a revised
    deadline must NOT fork a second item, so the window cannot be part of identity;
    but the plain path's "metadata outside identity dedupes onto the original doc"
    rule would then silently PRESERVE a stale deadline on restatement (the
    trust-eroding false-drop). So intent_by gets a VERIFIED in-place update path
    instead (see ``_update_intent_window``). Identity = ``_directive_payload(text,
    None, None, principal)`` — summary/next_action are constant-empty, so the hash
    ranges over text + assignee exactly.

    Delivery reuses the directive machinery: a genuinely-new capture goes through
    ``_write_directive`` (its absence-confirmation, write, and write-verification
    guards — no second delivery implementation). Only the two intent-specific
    outcomes are handled here: identical restatement -> rc 0 "intent already
    captured"; a different ``--by`` -> in-place window update.
    """
    principal = args.principal
    text = args.title
    now_iso = _iso(_now())
    intent_by: Optional[str] = None
    by = getattr(args, "by", None)
    if by:
        intent_by = directives.parse_when(by, now=now_iso)
        if intent_by is None:
            print(f"intent failed: cannot parse --by {by!r} (ISO or 5d/36h/10m)",
                  file=sys.stderr)
            return 1

    # Identity: text + assignee ONLY (intent_by excluded — see docstring).
    payload = _directive_payload(text, None, None, principal)
    slug = f"{tasks.slugify(text)}-{_payload_hash(payload)}"
    path = _task_path(args.team, slug)

    existing = transport.read(path)
    if existing is not None:
        # Present + readable at our hash slot. Confirm it IS our message (identity
        # match); a payload mismatch/unparseable means corruption or a hash
        # collision — never overwrite, fail loud (mirrors _write_directive).
        doc_payload = _doc_payload(existing)
        if doc_payload is None or doc_payload != payload:
            print(f"intent {slug}: slot holds unverifiable content, "
                  f"cannot verify, retry", file=sys.stderr)
            return 1
        # Our intent already exists. Same window (or no --by) -> pure dedup.
        existing_by = (okf.parse_frontmatter(existing) or {}).get("intent_by")
        if intent_by is None or str(existing_by or "") == str(intent_by or ""):
            print("intent already captured")
            return 0
        # A revised deadline: verified in-place window update, never a fork.
        return _update_intent_window(transport, path, existing, slug=slug,
                                     intent_by=intent_by)

    # existing is None -> absent OR present-but-unreadable (I1). Reuse
    # _write_directive's guards: it re-confirms absence via a directory listing
    # (present-but-unreadable -> rc 1 cannot-verify, no overwrite) then writes +
    # verifies. Build the doc with the capture doctrine: intent:<principal> tag +
    # intent_by frontmatter (both invisible to the payload identity).
    try:
        _, base = tasks.new_task_doc(
            text, now=now_iso, status="proposed",
            priority=getattr(args, "priority", None) or "P2",
            owner=getattr(args, "sender", None) or _host(), assignee=principal,
            summary="", next_action=None, kind="directive", slug=slug,
        )
    except tasks.TaskError as e:
        print(f"intent failed: {e}", file=sys.stderr)
        return 1
    fm = okf.parse_frontmatter(base)
    split = okf.split_frontmatter(base)
    if fm is None or split is None:  # unreachable (we just rendered it), never write blind
        print("intent failed: could not build doc", file=sys.stderr)
        return 1
    tags = fm.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    fm["tags"] = tags + [f"intent:{principal}"]
    fm["intent_by"] = intent_by  # None omitted by render_frontmatter (undeclared)
    content = okf.render_frontmatter(fm) + "\n" + split[1]
    return _write_directive(transport, args, slug=slug, content=content,
                            payload=payload, assignee=principal, not_before=None)


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


def _directed_inbox(transport: Any, team: str, agent: str,
                    rows: list[dict[str, Any]], *,
                    held_roles: "Optional[set[str]]" = None,
                    include_backlog: bool = False) -> list[dict[str, Any]]:
    """The open-directive fold over ALREADY-LOADED ``rows`` — directives assigned
    to ``agent``, ``*``, or a role in ``held_roles`` (role routing), with the same
    ack + read-your-write gating `inbox` applies. Split out from
    ``_inbox_rows_status`` so `listen` can resolve held roles from the rows FIRST
    (bounding the lease reads to role-shaped assignees on unseen directives) and
    then fold once, without re-reading the summaries index."""
    now = _iso(_now())
    acks = {str(r.get("name")): (r.get("acked_by") or []) for r in rows}
    stale_visible = directives.inbox(rows, acks, agent, now=now,
                                     include_backlog=include_backlog,
                                     held_roles=held_roles)
    for r in stale_visible:
        slug = str(r.get("name") or "")
        if agent not in (acks.get(slug) or []) and transport.read(_ack_path(team, slug, agent)):
            acks.setdefault(slug, []).append(agent)
    got = directives.inbox(rows, acks, agent, now=now,
                           include_backlog=include_backlog, held_roles=held_roles)
    # read-your-write: an ack written since the last reconcile hides the item
    # for the acking agent immediately (live shard check, only for shown items).
    return [r for r in got
            if transport.read(_ack_path(team, str(r.get("name")), agent)) is None]


def _inbox_rows_status(transport: Any, team: str, agent: str, *,
                       include_backlog: bool = False) -> tuple[list[dict[str, Any]], bool]:
    """The open-directive fold `inbox` surfaces for `agent`, plus whether the
    summaries index was readable (``ok``). Extracted so `listen` awaits the SAME
    source `inbox` shows — one inbox computation, no second implementation to
    drift. Never raises: an unreadable summaries read folds to an empty list, but
    now with ``ok=False`` so a listener can surface the degradation rather than
    mistake it for an empty inbox."""
    rows, ok, _reason = _load_rows_status(transport, team)
    return _directed_inbox(transport, team, agent, rows,
                           include_backlog=include_backlog), ok


def _inbox_rows(transport: Any, team: str, agent: str, *,
                include_backlog: bool = False) -> list[dict[str, Any]]:
    """Rows-only view of ``_inbox_rows_status`` for callers that don't surface the
    readable flag (cmd_inbox / briefing keep their existing degradation behavior)."""
    return _inbox_rows_status(transport, team, agent,
                              include_backlog=include_backlog)[0]


def cmd_inbox(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    if args.ack:
        fm = {"type": "Ack", "agent": agent, "timestamp": _iso(_now())}
        transport.write(_ack_path(args.team, args.ack, agent),
                        okf.render_frontmatter(fm) + "\nacked\n")
        print(f"acked {args.ack}")
        return 0
    got = _inbox_rows(transport, args.team, agent, include_backlog=args.all)
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
    # The reply leg: this shard is what the directive's owner sees on their listen.
    print("response recorded — the owner's listen surfaces it")
    return 0


# --- listen: the await leg of `tell` (this task) ---------------------------
#
# The bus had send verbs (tell/broadcast/remind) and `respond`, but nothing that
# SURFACED either new inbox directives or the responses that come back to a
# directive's owner — so `respond` wrote shards no fold delivered, and the reply
# leg of `tell` did not exist. Three agents independently hand-rolled watchers
# around `inbox --json`; `listen` ports that id-diff into the engine so the
# lifecycle owns listening. Three event sources, each id-diff'd against a state
# file, per tick:
#   1. new inbox directives for the agent (the SAME fold `inbox` shows).
#   2. new responses to directives the agent OWNS (the reply leg).
#   3. new verdicts on reviews the agent REQUESTED (`requested_by == agent`) —
#      the await leg of `review request`, now that a verb-opened review notifies
#      its reviewers atomically (so the `await verdicts` breadcrumb is genuine).
#
# Five failure SOURCES are tracked independently — inbox (summaries index),
# responses (the responses subtree transport), orphans (a response whose owning
# directive doc won't resolve), verdicts (the review root / a review doc /
# a verdict shard unreadable), and roles (a role-lease listing unreadable while
# resolving role-routed directives). Each is its own degraded streak.
#
# Disciplines (each a real incident this week; state is ADD-ONLY so they hold):
#   * No false advance — a failed/None read during a tick must NOT mark unknown
#     ids as seen. State is a UNION of affirmatively-processed ids, so a degraded
#     read contributes nothing and recovery re-surfaces the still-pending id.
#   * Fail visible, no flooding — a transport failure emits `LISTEN DEGRADED:`
#     ONCE per consecutive-failure streak, PER SOURCE (the streak flags persist IN
#     the state file, so a scheduler re-running `--once` does not re-alarm every
#     tick). Per-source is load-bearing: a single shared flag would let a permanent
#     orphan (`owner unresolved` every tick) pin it TRUE forever and silence a NEW,
#     distinct outage. Each source alerts once per ITS OWN streak and resets on ITS
#     OWN recovery. It goes to STDERR so `--json` stdout stays a clean
#     one-object-per-line event stream for filter-free streaming consumers.
#   * Quiet ticks print NOTHING to stdout (the monitor-flood lesson) — only
#     `--verbose` emits a heartbeat, and only to stderr.
#   * Bounded cost — one list_dir of _coord/responses/ + per-slug work ONLY for
#     slugs the agent owns; a slug's ownership is read once (from its task doc)
#     and cached in state, so not-owned / broadcast slugs cost nothing after the
#     first classification and the scan is never proportional to total history.


# The independent degraded streaks. Each source alarms once per its own streak.
# `roles` (role-lease resolution for role-routed directives) is its own source:
# folding it into `inbox` would let a chronic role degradation pin that streak
# and mask a fresh summaries outage — the independent-streak invariant. Legacy
# state files lack the key; _coerce_degraded defaults it False (free migration).
_LISTEN_SOURCES = ("inbox", "responses", "orphans", "verdicts", "roles")


def _fresh_degraded() -> dict[str, bool]:
    return {s: False for s in _LISTEN_SOURCES}


def _coerce_degraded(value: Any) -> dict[str, bool]:
    """Normalize the persisted ``degraded`` field to the per-source dict. A legacy
    single bool (pre per-source schema) migrates to the same value on EVERY source:
    an in-progress streak stays suppressed across the upgrade (no spurious re-alarm)
    and a clean state stays clean — either way each source then alarms/resets on its
    own going forward."""
    if isinstance(value, dict):
        return {s: bool(value.get(s)) for s in _LISTEN_SOURCES}
    return {s: bool(value) for s in _LISTEN_SOURCES}


def _listen_state_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("COORD_LISTENER_STATE")
                        or (pathlib.Path.home() / ".cache" / "coord-engine"))


def _listen_state_path(team: str, agent: str) -> pathlib.Path:
    # agent_key is injective (distinct agents never share a state file); team is
    # slugified for a filesystem-safe name.
    return _listen_state_dir() / f"listen-{tasks.slugify(team) or 'team'}-{tasks.agent_key(agent)}.json"


def _load_listen_state(path: pathlib.Path) -> dict[str, Any]:
    """Load the one-doc state, tolerating a missing/corrupt/foreign file (fresh
    default). Never raises — a tick never fails on its own bookkeeping."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "inbox_ids": list(data.get("inbox_ids") or []),
        "response_keys": list(data.get("response_keys") or []),
        "slug_owned": dict(data.get("slug_owned") or {}),
        # Source 3 (verdicts) bookkeeping — legacy state files lack these keys;
        # they default empty, so an upgrade re-surfaces nothing spuriously.
        "verdict_keys": list(data.get("verdict_keys") or []),
        "review_requested": dict(data.get("review_requested") or {}),
        "settled_reviews": list(data.get("settled_reviews") or []),
        # Orphan review dirs already reported (verdicts dir, no doc) — cached so
        # each is surfaced ONCE; legacy files lack the key and default empty.
        "orphan_slugs": list(data.get("orphan_slugs") or []),
        "degraded": _coerce_degraded(data.get("degraded")),
    }


def _save_listen_state(path: pathlib.Path, state: dict[str, Any]) -> None:
    # Best-effort: a state-write failure must never crash a tick. Worst case of a
    # lost write is one re-notify on the next run, never a missed event.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    except OSError as e:
        _log.warning("listen state write failed", path=str(path), error=str(e))


def _listen_tick(transport: Any, team: str, agent: str,
                 state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """One listen pass. Returns ``(events, failures)`` where ``failures`` maps each
    degraded SOURCE (see ``_LISTEN_SOURCES``) to its messages, and
    mutates ``state`` with ONLY affirmatively-processed ids (add-only — see the
    section note): a failed read/list adds nothing, so it can never mark unknown
    data as seen."""
    events: list[dict[str, Any]] = []
    failures: dict[str, list[str]] = {}

    def _fail(source: str, msg: str) -> None:
        failures.setdefault(source, []).append(msg)

    inbox_ids = set(state["inbox_ids"])
    response_keys = set(state["response_keys"])
    slug_owned: dict[str, Any] = dict(state["slug_owned"])

    # Source 1 — new inbox directives (the SAME fold `inbox` surfaces), PLUS
    # directives routed to a fresh-lease ROLE this agent holds. An unreadable
    # summaries index is degraded, NOT a legitimately-empty inbox.
    now_iso = _iso(_now())
    rows, inbox_ok, inbox_reason = _load_rows_status(transport, team)
    if not inbox_ok:
        # The reason attributes WHICH leg failed (summaries index vs the freshness
        # overlay — different outages, same inbox source/streak).
        _fail("inbox", inbox_reason or "summaries index unreadable")
    # Role expansion (contract gap): resolve fresh-lease holders ONLY for
    # role-shaped assignees on UNSEEN open directives — one role-doc(+lease) read
    # per distinct such assignee, deduped per tick (NOT persistent state: leases
    # change). HONEST BOUND: a directive assigned to ANOTHER literal agent never
    # enters this agent's inbox_ids, so its assignee is re-probed every tick (one
    # role-doc read resolving to "not a role", no lease reads) for as long as the
    # directive stays open — per-tick cost is O(distinct foreign assignees on open
    # directives), small in practice. A persistent negative "not-a-role" cache was
    # considered and REJECTED: read() can't distinguish absent from failed, and a
    # name later registered as a role would be silently unroutable forever (a
    # staleness hole worse than the read cost). Revisit only with a roles/-listing
    # invalidation if fleets grow. id-diff is unchanged (the directive slug is the
    # id regardless of the route), so a new role holder sees a directive iff its id
    # is unseen in THEIR OWN state file (state is per-agent) — the holder-change
    # semantics fall out.
    candidate_roles: set[str] = set()
    for r in rows:
        if r.get("status") not in directives.OPEN_STATUSES:
            continue
        a = str(r.get("assignee") or "")
        if not a or a in (agent, "*", directives.BACKLOG) or "/" in a:
            continue
        slug = str(r.get("name") or "")
        if not slug or slug in inbox_ids:
            continue  # already seen -> zero role-resolution cost
        candidate_roles.add(a)
    held_roles: set[str] = set()
    roles_listing_cache: dict[str, Any] = {}  # one roles/ listing per tick (doc-None disambiguation)
    for role in sorted(candidate_roles):
        holders, ok = _role_fresh_holders(transport, team, role, now=now_iso,
                                          listing_cache=roles_listing_cache)
        if not ok:
            # Fail-closed: the lease read is UNKNOWN. Degrade VISIBLY (the agent
            # may miss role-routed work) on the DEDICATED `roles` source — never
            # crash, never treat unknown as "not a holder" silently. Its own
            # source is load-bearing: a chronic role degradation must not pin the
            # inbox streak and mask a fresh summaries outage.
            _fail("roles", f"role lease unknown for {role}")
            continue
        if agent in holders:
            held_roles.add(role)
    inbox = _directed_inbox(transport, team, agent, rows,
                            held_roles=held_roles or None)
    for r in inbox:
        slug = str(r.get("name") or "")
        if not slug or slug in inbox_ids:
            continue
        events.append({"type": "directive", "slug": slug,
                       "owner": str(r.get("owner") or "?"),
                       "title": str(r.get("title") or slug)})
        inbox_ids.add(slug)

    # Source 2 — new responses to directives THIS agent owns. One list_dir of the
    # responses root; per-slug work only for owned slugs, ownership cached.
    prefix = _responses_prefix(team)
    try:
        entries = transport.list_dir(prefix)
    except TransportError as e:
        _fail("responses", f"responses listing unreadable ({e})")
        entries = None
    for e in entries or []:
        raw = e.get("name") or ""
        if not (e.get("is_dir") or raw.endswith("/")):
            continue  # only slug dirs live here
        slug = raw.rstrip("/")
        if not slug:
            continue
        owned = slug_owned.get(slug)
        if owned is None:
            doc = transport.read(_task_path(team, slug))
            if doc is None:
                # Ambiguous: a transient read failure OR an orphan response whose
                # directive doc is gone. Either way ownership is UNKNOWN, so we do
                # NOT cache and do NOT advance — unknown != seen, retry next tick.
                # This is its OWN source: a permanent orphan can pin `orphans`
                # forever without silencing a fresh `responses`/`inbox` outage.
                _fail("orphans", f"owner unresolved for {slug}")
                continue
            fm = okf.parse_frontmatter(doc) or {}
            owner = str(fm.get("owner") or "").strip()
            owned = owner == agent  # owner is the directive's SENDER; broadcast/absent -> not owned
            slug_owned[slug] = owned  # definitive classification: cache it
        if not owned:
            continue  # responses to other-owner / broadcast directives are noise
        try:
            stamps = transport.list_dir(prefix + slug + "/")
        except TransportError as ex:
            _fail("responses", f"response dir {slug} unreadable ({ex})")
            continue
        for se in stamps:
            sname = se.get("name") or ""
            if se.get("is_dir") or not sname.endswith(".md"):
                continue
            key = f"{slug}/{sname[:-3]}"
            if key in response_keys:
                continue
            shard = transport.read(prefix + slug + "/" + sname)
            if shard is None:
                # unread shard -> unknown, do NOT advance over it (retry next tick)
                _fail("responses", f"response {key} unreadable")
                continue
            rfm = okf.parse_frontmatter(shard) or {}
            events.append({"type": "response", "slug": slug,
                           "agent": str(rfm.get("agent") or "?"),
                           "outcome": str(rfm.get("outcome") or "?")})
            response_keys.add(key)

    # Source 3 — new verdicts on reviews THIS agent REQUESTED. One list_dir of
    # the review root; per-NEW-slug the review doc is read once and the requester
    # (`requested_by`) cached; verdict dirs are listed ONLY for my still-unsettled
    # slugs. A `.settled` listing first EMITS every unseen shard + one terminal
    # SETTLED event, then drops the slug so it is never listed again (the review
    # is immutable once settled). Its OWN degraded source `verdicts`.
    review_requested: dict[str, Any] = dict(state.get("review_requested") or {})
    verdict_keys = set(state.get("verdict_keys") or [])
    settled_reviews = set(state.get("settled_reviews") or [])
    review_prefix = f"team/{team}/review/"
    try:
        rentries = transport.list_dir(review_prefix)
    except TransportError as e:
        _fail("verdicts", f"review listing unreadable ({e})")
        rentries = None
    for e in rentries or []:
        name = e.get("name") or ""
        # The review DOCS are the `.md` entries; `{slug}/` dirs hold the verdicts.
        if e.get("is_dir") or not name.endswith(".md"):
            continue
        slug = name[:-3]
        if not slug or slug in settled_reviews:
            continue  # settled -> immutable, never list its verdicts again
        requested = review_requested.get(slug)
        if requested is None:
            doc = transport.read(_review_doc_path(team, slug))
            if doc is None:
                # The slug came from the listing so the doc exists — a None read
                # is a transient transport failure. Requester UNKNOWN: do NOT
                # cache and do NOT advance (no-false-advance), retry next tick.
                _fail("verdicts", f"requester unresolved for {slug}")
                continue
            fm = okf.parse_frontmatter(doc) or {}
            requested = str(fm.get("requested_by") or "").strip() == agent
            review_requested[slug] = requested  # definitive classification: cache
        if not requested:
            continue  # someone else's review -> noise
        try:
            ventries = transport.list_dir(_verdicts_prefix(team, slug))
        except TransportError as ex:
            _fail("verdicts", f"verdicts dir {slug} unreadable ({ex})")
            continue
        settling = any((x.get("name") or "") == SETTLED_MARKER for x in ventries)
        # Emit every UNSEEN shard BEFORE any settle-drop. The settling tick is
        # the DOMINANT flow, not an edge: a single approve settles the review and
        # the reviewer settles it themselves (`review status` right after filing,
        # per doctrine), so the final — often only — verdict shard and `.settled`
        # co-exist by the requester's next tick. Dropping the slug first would
        # swallow that verdict and make the `await verdicts:` breadcrumb false.
        # Cost stays bounded: only unseen shards are read, once per slug lifetime.
        unread = False
        for ve in ventries:
            vname = ve.get("name") or ""
            if ve.get("is_dir") or not vname.endswith(".md"):
                continue  # `.settled` and dirs are not verdict shards
            vkey = f"{slug}/{vname[:-3]}"
            if vkey in verdict_keys:
                continue
            shard = transport.read(_verdicts_prefix(team, slug) + vname)
            if shard is None:
                # listed file unreadable -> unknown, do NOT advance (retry)
                _fail("verdicts", f"verdict {vkey} unreadable")
                unread = True
                continue
            vfm = okf.parse_frontmatter(shard) or {}
            events.append({"type": "verdict", "slug": slug,
                           "reviewer": vname[:-3],
                           "verdict": str(vfm.get("verdict") or "?")})
            verdict_keys.add(vkey)
        if settling and not unread:
            # Terminal-settled AND every shard affirmatively seen: emit the one
            # terminal SETTLED event (so the requester learns the outcome even
            # when all shards were seen on earlier ticks), then drop the slug —
            # zero verdict-dir listings hereafter. The marker only ever caches
            # terminal-APPROVED (`_write_settled_marker`), so the state is known
            # without reading it. An unreadable shard keeps the slug ACTIVE
            # (degraded already flagged): settling must not swallow an
            # unreadable final verdict — it emits on recovery, then drops.
            events.append({"type": "settled", "slug": slug,
                           "state": review.APPROVED})
            settled_reviews.add(slug)

    # Orphan review dirs (visibility only): a `<slug>/` dir with verdicts but no
    # `<slug>.md` doc is skipped by the doc-keyed scan above and would never be
    # seen. Surface each ONCE (cached in `orphan_slugs`) so a listener learns the
    # slug exists — repair stays a human/maintainer action, never auto-delete.
    # Skipped entirely when the review listing failed (rentries is None): an
    # unreadable root is UNKNOWN, not an absence of docs.
    orphan_slugs = set(state.get("orphan_slugs") or [])
    if rentries is not None:
        doc_names = {(e.get("name") or "")[:-3] for e in rentries
                     if not e.get("is_dir") and (e.get("name") or "").endswith(".md")}
        for e in rentries:
            if not e.get("is_dir"):
                continue
            oslug = (e.get("name") or "").rstrip("/")
            if oslug and oslug not in doc_names and oslug not in orphan_slugs:
                events.append({"type": "orphan", "slug": oslug})
                orphan_slugs.add(oslug)

    state["inbox_ids"] = sorted(inbox_ids)
    state["response_keys"] = sorted(response_keys)
    state["slug_owned"] = slug_owned
    state["verdict_keys"] = sorted(verdict_keys)
    state["review_requested"] = review_requested
    state["settled_reviews"] = sorted(settled_reviews)
    state["orphan_slugs"] = sorted(orphan_slugs)
    return events, failures


def _format_listen_event(ev: dict[str, Any]) -> str:
    if ev["type"] == "directive":
        return f"DIRECTIVE {ev['slug']} (from {ev['owner']}): {ev['title'][:80]}"
    if ev["type"] == "verdict":
        return f"VERDICT {ev['slug']} by {ev['reviewer']}: {ev['verdict']}"
    if ev["type"] == "settled":
        return f"SETTLED {ev['slug']}: {ev['state']}"
    if ev["type"] == "orphan":
        return f"ORPHAN {ev['slug']} (verdicts dir, no review doc — needs repair)"
    return f"RESPONSE {ev['slug']} by {ev['agent']}: {ev['outcome']}"


def _run_listen_tick(transport: Any, team: str, agent: str, state: dict[str, Any],
                     *, json_mode: bool, verbose: bool) -> tuple[list, dict[str, list[str]]]:
    events, failures = _listen_tick(transport, team, agent, state)
    for ev in events:
        print(json.dumps(ev) if json_mode else _format_listen_event(ev))
    sys.stdout.flush()

    # Per-source streaks: each source alarms ONCE per its own consecutive-failure
    # streak (the flags persist in state across `--once` runs) and resets on its
    # own recovery — a pinned orphan can't swallow a new inbox/responses outage.
    degraded = _coerce_degraded(state.get("degraded"))  # defensive: tolerate legacy bool
    state["degraded"] = degraded
    newly: list[str] = []
    for source in _LISTEN_SOURCES:
        msgs = failures.get(source)
        if msgs:
            if not degraded[source]:  # this source just entered a failure streak
                newly.append("; ".join(msgs))
                degraded[source] = True
        else:
            degraded[source] = False  # clean this tick -> streak reset for this source
    if newly:
        print(f"LISTEN DEGRADED: {'; '.join(newly)}", file=sys.stderr)
    elif verbose and not events and not failures:
        print(f"listen: quiet ({len(state['inbox_ids'])} inbox, "
              f"{len(state['response_keys'])} responses, "
              f"{len(state.get('verdict_keys') or [])} verdicts seen)", file=sys.stderr)
    sys.stderr.flush()
    return events, failures


def cmd_listen(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    state_path = _listen_state_path(args.team, agent)
    if getattr(args, "state_path", False):
        # Resolver for listener-tick.sh's one-time `.items` -> listen-state
        # migration: the slugify/agent_key naming lives here, so the shell asks the
        # engine rather than reimplementing it. Print and exit; no tick, no writes.
        print(str(state_path))
        return 0
    state = _load_listen_state(state_path)
    json_mode = bool(getattr(args, "json", False))
    verbose = bool(getattr(args, "verbose", False))

    def tick() -> None:
        _run_listen_tick(transport, args.team, agent, state,
                         json_mode=json_mode, verbose=verbose)
        _save_listen_state(state_path, state)

    if args.once:
        tick()
        return 0
    interval = args.interval if args.interval and args.interval > 0 else 60
    try:
        while True:
            tick()
            time.sleep(interval)
    except KeyboardInterrupt:
        if verbose:
            print("listen: stopped", file=sys.stderr)
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
    # One shared add-on deadline (see _briefing_budget), opened before the
    # transport-heavy sections and spent cumulatively: time burned by
    # pending-reviews shrinks the window the forge fan-out gets, so the whole
    # add-on stack is bounded, not each section independently. pending-reviews
    # keeps its own tighter, already-shipped budget (whichever bound is sooner).
    add_on_deadline = time.monotonic() + _briefing_budget()
    try:
        out["pending_reviews"] = _pending_reviews_for(transport, args.team, agent)
    except Exception as e:
        print(f"briefing: pending_reviews section unavailable ({type(e).__name__})", file=sys.stderr)
        out["pending_reviews"] = []
    try:
        out["forge_feedback"] = _forge_feedback_for(
            transport, args.team, agent, deadline=add_on_deadline)
    except Exception as e:
        print(f"briefing: forge_feedback section unavailable ({type(e).__name__})", file=sys.stderr)
        out["forge_feedback"] = []
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
    pend_rows = [r for r in out["pending_reviews"]
                 if r.get("type") != "review-fold-degraded"]
    degraded_rows = [r for r in out["pending_reviews"]
                     if r.get("type") == "review-fold-degraded"]
    print(f"  pending reviews: {len(pend_rows)} item(s)")
    for r in pend_rows[:5]:
        print(_line(r))
    for r in degraded_rows:  # always shown — a degraded fold must never hide
        print(_review_degraded_line(r))
    forge_rows = out.get("forge_feedback") or []
    forge_fb = [r for r in forge_rows if r.get("type") != "forge-degraded"]
    forge_deg = [r for r in forge_rows if r.get("type") == "forge-degraded"]
    print(f"  forge feedback: {len(forge_fb)} PR(s)")
    for r in forge_fb[:5]:
        print(_forge_feedback_line(r))
    for r in forge_deg:  # always shown — a degraded fold must never hide
        print(_forge_degraded_line(r))
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
            # `tier` drives the report/dash tier-mix + headline; the outcome
            # fields (model/task_class/outcome) flow through only when present.
            # v1 shards missing any of these reach the folds untouched.
            for k in ("tier", "model", "task_class", "outcome"):
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
    # An empty/whitespace-only --needs (e.g. `--needs ""` or `--needs ,`) is a
    # taxonomy-strictness error, not "match everything" — mirror the unknown-need
    # exit 2 rather than silently routing ALL models.
    if not needs:
        print("route — no needs given", file=sys.stderr)
        return 2
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


# Plan-seeded rolling-window cap defaults for `atc init`. These are OPERATOR-
# CORRECTABLE ESTIMATES, not measured limits — subscriptions don't publish their
# caps, so init seeds a plausible starting point per provider and throttle events
# calibrate them from there (a real rate-limit hit zeroes that window regardless
# of the declared number). An operator edits the numbers freely, and DELETING an
# account's windows declares it uncapped (route treats no-windows as 100%
# headroom). Keyed by provider; anything else falls to the placeholder.
_ATC_SEED_WINDOWS: dict[str, list[dict[str, int]]] = {
    "anthropic": [{"hours": 5, "cap": 1000}, {"hours": 168, "cap": 15000}],
    "openai": [{"hours": 5, "cap": 600}],
}
_ATC_SEED_WINDOWS_DEFAULT: list[dict[str, int]] = [{"hours": 5, "cap": 500}]


def _atc_seed_windows(provider: str) -> list[dict[str, int]]:
    """Fresh copies (never the shared constant) so callers can't mutate defaults."""
    src = _ATC_SEED_WINDOWS.get(provider, _ATC_SEED_WINDOWS_DEFAULT)
    return [dict(w) for w in src]


def _atc_provider_harnesses(defaults: dict[str, Any]) -> dict[str, list[str]]:
    """Per-provider harness union folded from the default model map: every model's
    ``provider`` -> the sorted set of its declared ``harnesses``. This is the
    default an account's ``harnesses[]`` seeds from at init time."""
    acc: dict[str, set[str]] = {}
    for entry in (defaults.get("models") or {}).values():
        prov = entry.get("provider")
        if not isinstance(prov, str) or not prov:
            continue
        for h in entry.get("harnesses") or []:
            if isinstance(h, str) and h:
                acc.setdefault(prov, set()).add(h)
    return {p: sorted(hs) for p, hs in acc.items()}


def _atc_parse_account_spec(spec: str) -> Optional[tuple[str, str, str]]:
    """Parse a ``--account id=provider:plan`` token. ``:plan`` is optional. Returns
    ``(id, provider, plan)`` or ``None`` if the required ``id=provider`` shape is
    absent (the caller turns ``None`` into an exit-2 refusal)."""
    if "=" not in spec:
        return None
    acct_id, rest = spec.split("=", 1)
    acct_id = acct_id.strip()
    if not acct_id or not rest.strip():
        return None
    if ":" in rest:
        provider, plan = rest.split(":", 1)
    else:
        provider, plan = rest, ""
    provider, plan = provider.strip(), plan.strip()
    if not provider:
        return None
    return acct_id, provider, plan


def _atc_build_account(acct_id: str, provider: str, plan: str,
                       prov_harnesses: dict[str, list[str]],
                       harness_override: Optional[list[str]]) -> dict[str, Any]:
    harnesses = (list(harness_override) if harness_override
                 else list(prov_harnesses.get(provider, [])))
    if provider not in prov_harnesses and not harness_override:
        print(f"warning: provider {provider!r} not in default map; seeded "
              "5h/500 with no harnesses — pass --harness or edit accounts.json "
              "to make it routable", file=sys.stderr)
    acct: dict[str, Any] = {"id": acct_id, "provider": provider}
    if plan:
        acct["plan"] = plan
    acct["harnesses"] = harnesses
    acct["windows"] = _atc_seed_windows(provider)
    return acct


def _atc_init_interactive(providers: list[str],
                          prov_harnesses: dict[str, list[str]],
                          harness_override: Optional[list[str]]) -> list[dict[str, Any]]:
    """Numbered-prompt onboarding over the default map's provider set. Reads via
    the builtin ``input`` (monkeypatched in tests). An empty/blank selection
    returns ``[]`` — the caller refuses zero accounts with exit 2."""
    print("Providers in the packaged default model map:")
    for i, p in enumerate(providers, 1):
        print(f"  {i}. {p}")
    sel = input("Select providers to declare (comma-separated numbers): ").strip()
    chosen: list[str] = []
    ignored: list[str] = []
    for tok in sel.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            idx = int(tok)
        except ValueError:
            ignored.append(tok)
            continue
        if not (1 <= idx <= len(providers)):
            ignored.append(tok)
            continue
        if providers[idx - 1] not in chosen:
            chosen.append(providers[idx - 1])
    if ignored:
        print("ignored: " + ", ".join(ignored))
    gathered: list[dict[str, Any]] = []
    for prov in chosen:
        default_id = f"{prov}-main"
        acct_id = input(f"  account id for {prov} [{default_id}]: ").strip() or default_id
        plan = input(f"  plan for {prov} (blank for none): ").strip()
        gathered.append(_atc_build_account(acct_id, prov, plan,
                                           prov_harnesses, harness_override))
    return gathered


def cmd_atc_init(args: argparse.Namespace, transport: Any) -> int:
    """Standalone ATC onboarding: seed ``team/<team>/atc/accounts.json`` so a
    fresh operator has a routable cap ledger in one command.

    Interactive by default (numbered prompts over the default map's providers);
    ``--yes`` runs non-interactively and requires >=1 ``--account id=provider:plan``.
    Idempotent: an existing accounts.json is loaded, the newly-declared accounts
    merged in by id (existing entries and sibling keys like ``tiers``/``models``
    are preserved), and the result written back through the same transport-write
    seam the review-request flow uses. Refuses a zero-account run with exit 2."""
    defaults = atc.load_default_models()
    prov_harnesses = _atc_provider_harnesses(defaults)
    providers = sorted(prov_harnesses)

    # --account is itself an unambiguous statement of non-interactive intent, so
    # its presence implies --yes even when --yes was not passed.
    if args.yes or args.account:
        gathered: list[dict[str, Any]] = []
        for spec in (args.account or []):
            parsed = _atc_parse_account_spec(spec)
            if parsed is None:
                print(f"atc init: malformed --account {spec!r} "
                      "(expected id=provider:plan)", file=sys.stderr)
                return 2
            gathered.append(_atc_build_account(*parsed, prov_harnesses, args.harness))
    else:
        gathered = _atc_init_interactive(providers, prov_harnesses, args.harness)

    if not gathered:
        print("atc init: no accounts declared — nothing written "
              "(--yes needs >=1 --account id=provider:plan)", file=sys.stderr)
        return 2

    # Idempotent merge: load whatever exists, keep it verbatim, append only the
    # new-by-id accounts. Read the raw doc (not parse_accounts) so sibling keys
    # (tiers, models overlay) survive untouched.
    path = _atc_accounts_path(args.team)
    raw = transport.read(path)
    try:
        doc = json.loads(raw) if raw else {}
        if not isinstance(doc, dict):
            doc = {}
    except (ValueError, TypeError):
        doc = {}
    existing = doc.get("accounts")
    if not isinstance(existing, list):
        existing = []
    existing_ids = {a.get("id") for a in existing if isinstance(a, dict)}
    added = [a for a in gathered if a["id"] not in existing_ids]
    doc["accounts"] = existing + added
    doc.setdefault("tiers", {})

    transport.write(path, json.dumps(doc, indent=2) + "\n")

    ex_id = gathered[0]["id"]
    print(f"wrote {path}: {len(doc['accounts'])} account(s) declared "
          f"({len(added)} new this run)")
    print("next steps — paste these:")
    print("  1. install the skill — see skills/fulcra-agent-atc/SKILL.md §Install")
    print(f"  2. coord-engine route {args.team} --needs code")
    print(f"  3. coord-engine usage log {args.team} --account {ex_id} "
          "--tier standard --units <est> --model <model> "
          "--task-class code --outcome clean")
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
        doc = transport.read(_role_doc_path(args.team, role))
        if doc is None:
            # FAIL CLOSED (review fix): this doc was JUST LISTED by the parent
            # roles/ scan, so a None read is knowably transient-or-deleted — never
            # a live role to judge under DEFAULT_SLA_HOURS. Falling through with
            # the 24h default would collapse a longer-SLA role's window and fire a
            # false VACANT escalation (the incident vector, on the acting path).
            # Skip: transient -> retried next sweep (correct); deleted -> role
            # gone (also correct). `roles status` now applies the same
            # disambiguation on its doc-None path (via _roles_listing_names) —
            # both surfaces agree that listed-but-unreadable is UNKNOWN.
            print(f"escalate: role doc unreadable for {role} — state unknown, "
                  f"skipped (degraded transport, retry)", file=sys.stderr)
            continue
        reg = okf.parse_frontmatter(doc) or {}
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
                        transport.read(_leases_prefix(args.team, role) + fn))
                    if fm is None:
                        # A JUST-LISTED lease shard read None/unparseable: `or {}`
                        # here dropped the timestamp and silently folded the holder
                        # out as stale — a fail-open VACANCY on the ACTING path
                        # (same class as the codex P1). UNKNOWN: never escalate.
                        print(f"escalate: lease shard unreadable for {role} — "
                              f"state unknown, skipped", file=sys.stderr)
                        leases = None
                        break
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
    # Extended: mirror also sweeps the three feedback surfaces so a formal review
    # (or inline / conversation comment) can never go unseen.
    fb = forge_mod.feedback_sweep(transport, args.team,
                                  runner=args.runner or forge_mod.default_runner,
                                  repo=args.repo)
    print(f"forge feedback: {fb['prs']} PR(s) swept, {fb['items']} feedback shard(s) written"
          + (f", {len(fb['skipped'])} skipped" if fb["skipped"] else ""))
    for line in fb["skipped"]:
        print(f"  skipped {line}", file=sys.stderr)
    for line in fb.get("notes", []):
        print(f"  note {line}", file=sys.stderr)
    return 0


def cmd_forge_feedback(args: argparse.Namespace, transport: Any) -> int:
    """Sweep-only verb: the three-surface feedback sweep, no state mirroring."""
    import shutil as _sh
    if not _sh.which("gh") and args.runner is None:
        print("forge feedback: gh CLI not found — nothing swept (install GitHub CLI to enable)",
              file=sys.stderr)
        return 0  # degradation, not an error
    fb = forge_mod.feedback_sweep(transport, args.team,
                                  runner=args.runner or forge_mod.default_runner,
                                  repo=args.repo)
    print(f"forge feedback: {fb['prs']} PR(s) swept, {fb['items']} feedback shard(s) written"
          + (f", {len(fb['skipped'])} skipped" if fb["skipped"] else ""))
    for line in fb["skipped"]:
        print(f"  skipped {line}", file=sys.stderr)
    for line in fb.get("notes", []):
        print(f"  note {line}", file=sys.stderr)
    return 0


def _watch_path(team: str, slug: str) -> str:
    return f"team/{team}/_coord/forge/watch/{slug}.md"


def cmd_forge_watch(args: argparse.Namespace, transport: Any) -> int:
    """Register a PR to sweep for feedback even when it is not a review artifact.
    Duplicate watch = idempotent update (overwrite), not an error."""
    slug = forge_mod.pr_slug(args.pr_url)
    if not slug:
        print(f"forge watch: not a GitHub PR url: {args.pr_url}", file=sys.stderr)
        return 1
    url = forge_mod.parse_pr_url(args.pr_url)
    agent = args.agent or _host()
    fm = {"type": "Watch", "schema": "forge-watch/v1", "url": url,
          "agent": agent, "ts": _iso(_now())}
    transport.write(_watch_path(args.team, slug),
                    okf.render_frontmatter(fm) + f"\nWatching {url} for {agent}.\n")
    print(f"forge watch: {slug} -> {agent}")
    return 0


def cmd_forge_unwatch(args: argparse.Namespace, transport: Any) -> int:
    """Remove a watch registration. Absent watch = clean no-op."""
    slug = forge_mod.pr_slug(args.pr_url)
    if not slug:
        print(f"forge unwatch: not a GitHub PR url: {args.pr_url}", file=sys.stderr)
        return 1
    path = _watch_path(args.team, slug)
    if transport.read(path) is None:
        print(f"forge unwatch: {slug} was not watched")
        return 0
    transport.delete(path)
    print(f"forge unwatch: {slug} removed")
    return 0


# --- migrate (incumbent fulcra-coord -> coord, docs 06 approach C) ---

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


# --- annotate (activity-annotation projection) ---

def _emit_projection_spec(spec: Any, *, agent: str) -> bool:
    """Hand ONE projected AnnotationSpec to the hardened fulcra_common writer.

    Best-effort: coord-engine is stdlib-only, so the writer package (and the
    fulcra-api CLI / token it needs) may be entirely absent — that degrades to
    False, never an exception, so `annotate project` still exits 0 on a host
    without the writer. Reuses the writer's typed-record POST path; never opens a
    second one."""
    try:
        from fulcra_common import annotations as _ann
    except Exception:
        return False
    try:
        return bool(_ann.emit_projection_annotation(
            note=spec.note, tags=list(spec.tags), recorded_at=spec.ts,
            id=spec.id, agent=agent))
    except Exception:
        return False


def cmd_annotate_resolution(args: argparse.Namespace, transport: Any) -> int:
    """Set the team's projection RESOLUTION level on the bus (any host's
    heartbeat reads it). Live set today: off, transitions. An unknown level is
    rejected (exit 2) — the axis is a level string, so future levels are additive."""
    level = args.level
    if level not in annotate_mod.LEVELS:
        print(f"unknown resolution; known: {', '.join(annotate_mod.LEVELS)}",
              file=sys.stderr)
        return 2
    annotate_mod.write_resolution(transport, args.team, level)
    print(f"annotate resolution for team/{args.team} -> {level}"
          + ("" if level in annotate_mod.LIVE_PROJECTING else " (no projection)"))
    return 0


def cmd_annotate_status(args: argparse.Namespace, transport: Any) -> int:
    """Print the team's resolution level + the projection cursor position."""
    team = args.team
    res = annotate_mod.read_resolution(transport, team)
    cursor = annotate_mod.read_cursor(transport, team)
    last_ts = cursor.get("last_ts")
    seen = cursor.get("seen_ids") or []
    if args.json:
        print(json.dumps({"team": team, "resolution": res,
                          "projecting": res in annotate_mod.LIVE_PROJECTING,
                          "last_ts": last_ts, "seen_ids": len(seen)}, indent=2))
    else:
        proj = "" if res in annotate_mod.LIVE_PROJECTING else "  (no projection)"
        print(f"annotate — team/{team}: resolution={res}{proj}")
        print(f"  cursor: last_ts={last_ts or '(none)'}, "
              f"{len(seen)} id(s) in the replay window")
    return 0


def cmd_annotate_project(args: argparse.Namespace, transport: Any) -> int:
    """Fold the freshly-reconciled structured transitions onto the operator's
    timeline. Reads reconcile's just-written pending transitions + the cursor
    from the bus, calls the pure fold, emits each spec via the hardened writer,
    and advances the cursor. Refuses (exit 0) when the team has not opted in."""
    team = args.team
    res = annotate_mod.read_resolution(transport, team)
    if res not in annotate_mod.LIVE_PROJECTING:
        print(f"projection off (resolution={res or 'absent'}) — "
              f"opt in with `annotate resolution {team} transitions`")
        return 0
    pending = annotate_mod.read_pending(transport, team)
    cursor = annotate_mod.read_cursor(transport, team)
    now = _iso(_now())
    specs, full_cursor = annotate_mod.project(pending, cursor, team=team, now=now)
    agent = _host()
    # Emit each spec and record which ids ACTUALLY landed — the cursor advances
    # PER LANDED SPEC, not all-or-nothing. A transient partial writer failure
    # (some specs land, some fail) must not re-project the succeeded specs next
    # run (that would manufacture duplicates on the no-dedup endpoint), nor drop
    # the failed ones (that would lose a transition).
    landed_ids = {s.id for s in specs if _emit_projection_spec(s, agent=agent)}
    emitted = len(landed_ids)

    def _persist_cursor(new_cursor: dict) -> None:
        # A swallowed cursor-write failure is a DUPLICATE vector now that pending
        # merges-and-carries: the same landed ids stay in pending, and without an
        # advanced cursor to dedup them the next beat re-emits to the no-dedup
        # endpoint. Surface it loudly rather than silently drop the write.
        if not annotate_mod.write_cursor(transport, team, new_cursor):
            _log.warn("annotate project: cursor write FAILED — landed ids may "
                      "re-project (duplicate risk)", team=team,
                      last_ts=new_cursor.get("last_ts"),
                      seen_ids=len(new_cursor.get("seen_ids") or []))

    if not specs or emitted == len(specs):
        # All landed (or none to emit): the fold's cursor already folds every
        # emitted id into seen_ids and advances the watermark — persist it.
        _persist_cursor(full_cursor)
    elif emitted:
        # Partial success: fold ONLY the landed ids into the cursor and hold the
        # watermark at the oldest failed spec, so the next heartbeat re-projects
        # exactly the un-landed specs and nothing else.
        partial = annotate_mod.cursor_for_landed(
            pending, cursor, landed_ids, team=team, now=now)
        _persist_cursor(partial)
    # else (emitted == 0 with specs present): total writer failure — leave the
    # cursor untouched so every spec retries next run (over-capture beats loss).
    print(f"projected {emitted}/{len(specs)} transition(s) for team/{team}")
    return 0


# --- dropped threads (fulcra-agent — 2026-07-11-dropped-threads) -------------
#
# The bus ADAPTER for `threads.classify`. Reversibility (Ash requirement): the
# pure fold consumes a NEUTRAL row shape; this adapter is the ONLY place bus
# specifics live, so a GitHub/fulcra-pm source later is a new adapter emitting the
# same rows, not a rewrite. v1 reads ONE source — coord bus items on the team
# where the principal is involved (assignee/owner, or an intent:/blocked-on: tag).

def _threads_fold_budget() -> float:
    """Env override ``COORD_THREADS_FOLD_BUDGET`` (same parse discipline as the
    other budgets): unparseable, non-positive, NaN, or inf -> default. A bad env
    value must never disable the bound or make the fold hang."""
    raw = os.environ.get("COORD_THREADS_FOLD_BUDGET")
    if raw is None:
        return DEFAULT_THREADS_FOLD_BUDGET
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_THREADS_FOLD_BUDGET
    if not (v > 0) or v == float("inf"):
        return DEFAULT_THREADS_FOLD_BUDGET
    return v


def _threads_window(flag: Optional[float], env: str, default: float) -> float:
    """Resolve a window: flag > env > default. A bad flag/env value falls back
    (never let a typo disable the bound)."""
    if flag is not None:
        try:
            v = float(flag)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    raw = os.environ.get(env)
    if raw is not None:
        try:
            v = float(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return default


def _threads_is_principal(row: dict[str, Any], principal: str, tags: list[str]) -> bool:
    return (row.get("assignee") == principal or row.get("owner") == principal
            or f"intent:{principal}" in tags or f"blocked-on:{principal}" in tags)


def _threads_blocked_signal(row: dict[str, Any], principal: str,
                            tags: list[str]) -> Optional[str]:
    """Which blocked-on-principal signal fires (spec mode 2), or None. Order is
    just for the human evidence label — any one is sufficient."""
    if row.get("assignee") == principal:
        return f"assignee: {principal}"
    if f"blocked-on:{principal}" in tags:
        return f"blocked-on:{principal} tag"
    if str(row.get("blocked_on") or "") == principal:
        return f"blocked_on: {principal}"
    if "needs:human" in tags and (row.get("assignee") == principal
                                  or row.get("owner") == principal):
        return "needs:human block"
    return None


def _threads_ash_activity(transport: Any, team: str, slug: str, principal: str,
                          response_slugs: set[str], row_ts: Optional[str],
                          ) -> tuple[Optional[str], bool, str]:
    """Last activity ATTRIBUTABLE to the principal for a mode-1 candidate.

    Reads the principal's ack shard + any response shards the principal authored
    (only when the slug is known to have responses — the shared responses listing
    tells us that, so we never list/read for a slug that has none). Returns
    ``(ts, attributed, source)``: when no principal-attributable event is found we
    FALL BACK to the item's own ``timestamp`` (the last doc write, NOT attributable
    to the principal) and FLAG that in ``attributed=False`` — honesty over
    cleverness. A shard read that FAILS is skipped (best-effort attribution never
    crashes the fold); the aggregate budget in the caller bounds the total cost."""
    best: Optional[str] = None
    source = ""

    def _consider(ts: Any, label: str) -> None:
        nonlocal best, source
        if isinstance(ts, str) and ts and (best is None or ts > best):
            best, source = ts, label

    # ack shard: path is principal-keyed, so its existence IS the attribution.
    try:
        ack = transport.read(_ack_path(team, slug, principal))
    except Exception:
        ack = None
    if ack:
        fm = okf.parse_frontmatter(ack) or {}
        _consider(fm.get("timestamp"), "ack shard")

    # response shards authored by the principal (only if this slug has responses).
    if slug in response_slugs:
        prefix = _responses_prefix(team) + slug + "/"
        try:
            entries = transport.list_dir(prefix)
        except Exception:
            entries = []
        for e in entries:
            n = e.get("name") or ""
            if e.get("is_dir") or not n.endswith(".md"):
                continue
            try:
                raw = transport.read(prefix + n)
            except Exception:
                raw = None
            fm = okf.parse_frontmatter(raw) or {}
            if str(fm.get("agent") or "") == principal:
                _consider(fm.get("timestamp"), "response shard")

    if best is not None:
        return best, True, source
    return (row_ts, False, "item timestamp")  # fallback, flagged


def _threads_candidate_rows(
    transport: Any, team: str, principal: str,
) -> tuple[list[dict[str, Any]], bool, str]:
    """Build the NEUTRAL rows `threads.classify` consumes, from summaries + the
    freshness overlay (inherited free via ``_load_rows_status`` — fresh intents ARE
    visible), filtered to principal items. Per-candidate reads only for the signals
    summaries lack: ``intent_by`` (intent window) and ash-activity attribution.

    Returns ``(rows, ok, reason)``: ``ok`` False (with a reason) whenever the
    summaries/overlay load degraded OR the fold budget was exhausted with candidates
    still unread — the caller surfaces a ``threads-degraded`` row, never silence."""
    summary_rows, ok, reason = _load_rows_status(transport, team)

    # One shared responses-root listing: tells us (a) which slugs have a response
    # shard (the mode-3 `responded` signal) and (b) which slugs are worth reading
    # for principal-authored activity. One list_dir, not per-candidate.
    response_slugs: set[str] = set()
    try:
        for e in transport.list_dir(_responses_prefix(team)):
            n = e.get("name") or ""
            if e.get("is_dir") or n.endswith("/"):
                response_slugs.add(n.rstrip("/"))
    except Exception:
        # A responses-listing failure only weakens the `responded` suppression
        # signal + activity attribution — degrade visibly, keep folding.
        ok = False
        reason = reason or "responses listing unreadable"

    deadline = time.monotonic() + _threads_fold_budget()
    budget_hit = False
    rows: list[dict[str, Any]] = []
    for r in summary_rows:
        if time.monotonic() > deadline:
            # After-op discipline, checked at the TOP of each candidate: the
            # PREVIOUS candidate's reads breached the budget — detected before
            # any further reads (and a breach on the FINAL candidate, with
            # nothing left to read, never false-degrades). Stop, serve what we
            # have, degrade visibly.
            budget_hit = True
            break
        if not isinstance(r, dict):
            continue
        tags = [str(t) for t in (r.get("tags") or []) if isinstance(r.get("tags"), list)]
        if not _threads_is_principal(r, principal, tags):
            continue
        slug = str(r.get("name") or r.get("id") or "")
        is_intent = f"intent:{principal}" in tags
        followup_ref = next(
            (t.split(":", 1)[1] for t in tags if t.startswith("followed-up-by:")), None)

        declared_window = None
        ash_ts: Optional[str] = None
        attributed = True
        source = ""
        if is_intent:
            # Intent needs intent_by (summaries lack it) — one doc read. A
            # MISSED read (raise OR None) — or a doc that reads but parses to
            # garbage — means the window is UNKNOWN: ripeness cannot be decided,
            # so this intent is EXCLUDED from this pass (silently windowing it
            # to capture+grace would manufacture a false mode-3 drop with a
            # clean degraded flag — the nagging-sensitive failure the spec
            # forbids) AND the fold degrades visibly. It returns, correctly
            # windowed, once readable. Only a doc that reads+parses fine and
            # GENUINELY lacks intent_by is legitimately undeclared — the
            # capture+grace fallback below stands for that case alone.
            try:
                doc = transport.read(_task_path(team, slug))
            except Exception:
                doc = None
            fm = okf.parse_frontmatter(doc) if doc is not None else None
            if fm is None:
                ok = False
                reason = reason or f"intent window unreadable: {slug}"
                continue
            declared_window = fm.get("intent_by")
        else:
            # Mode-1/2 candidates: attribute ash-activity from shards.
            ash_ts, attributed, source = _threads_ash_activity(
                transport, team, slug, principal, response_slugs, r.get("timestamp"))

        rows.append({
            "id": r.get("id") or slug,
            "title": r.get("title") or slug,
            "status": str(r.get("status") or ""),
            "tags": tags,
            "intent": is_intent,
            "blocked_on_principal": bool(_threads_blocked_signal(r, principal, tags)),
            "blocked_signal": _threads_blocked_signal(r, principal, tags) or "",
            "parked": r.get("assignee") == directives.BACKLOG,
            "not_before": r.get("not_before"),
            "ash_activity_ts": ash_ts,
            "ash_activity_attributed": attributed,
            "ash_activity_source": source,
            "declared_window": declared_window,
            "captured_ts": r.get("timestamp"),
            "followup": {
                "status": str(r.get("status") or "proposed"),
                "responded": slug in response_slugs,
                "followup_ref": followup_ref,
            },
        })

    if budget_hit:
        ok = False
        reason = "threads fold budget exhausted"
    return rows, ok, reason


def cmd_threads(args: argparse.Namespace, transport: Any) -> int:
    """`coord-engine threads <team> --for <principal>` — the dropped-threads fold.

    Windows: ``--silence-days`` (default 3, env ``COORD_THREADS_SILENCE_DAYS``),
    ``--intent-grace-hours`` (default 48, env ``COORD_THREADS_INTENT_GRACE_HOURS``);
    flag > env > default. Text is grouped by mode, oldest-first; ``--json`` emits
    ONE object per line (``{mode, id, title, age, window, evidence}``), plus a
    ``{"type": "threads-degraded", ...}`` object when a source was not fully
    readable. Never crashes, never silently empties on failure."""
    principal = args.principal
    silence_days = _threads_window(getattr(args, "silence_days", None),
                                   "COORD_THREADS_SILENCE_DAYS",
                                   DEFAULT_THREADS_SILENCE_DAYS)
    grace_hours = _threads_window(getattr(args, "intent_grace_hours", None),
                                  "COORD_THREADS_INTENT_GRACE_HOURS",
                                  DEFAULT_THREADS_INTENT_GRACE_HOURS)
    rows, ok, reason = _threads_candidate_rows(transport, args.team, principal)
    dropped = threads_mod.classify(rows, now=_iso(_now()),
                                   silence_days=silence_days,
                                   intent_grace_hours=grace_hours)

    if args.json:
        for o in dropped:
            print(json.dumps(o))
        if not ok:
            print(json.dumps({"type": "threads-degraded",
                              "reason": reason or "threads source degraded"}))
        return 0

    if not ok:
        # Degraded notice on STDERR: stdout stays the clean, pipeable thread
        # list (a consumer grepping/parsing stdout never confuses a degradation
        # notice for a thread), while the degradation is still impossible to
        # miss interactively — the house stdout/stderr split.
        print(f"threads degraded (partial): {reason or 'source unreadable'}",
              file=sys.stderr)
    labels = {1: "started-then-silent", 2: "blocked-on-ash", 3: "intent-never-started"}
    if not dropped:
        print(f"threads — {principal}: nothing dropped")
        return 0
    print(f"threads — {principal}: {len(dropped)} dropped")
    # DELIBERATE divergence from --json: text orders groups 2,3,1 (awaited-now
    # first for the human eye); --json stays mode-ascending (classify's order).
    for mode in (2, 3, 1):  # awaited-now first, then commitments, then silence
        group = [o for o in dropped if o["mode"] == mode]
        if not group:
            continue
        print(f"\n[{mode}] {labels[mode]} ({len(group)})")
        for o in group:  # classify already sorts oldest-first within a mode
            print(f"  {o['id']}: {o['title']} — {o['evidence']}")
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
    it = sub.add_parser("intent", help="capture a spoken commitment (intent:<principal>); restatement never forks, a new --by updates the window in place")
    it.add_argument("team"); it.add_argument("title", help="the commitment text")
    it.add_argument("--for", dest="principal", required=True, help="the principal who owes the commitment (e.g. ash)")
    it.add_argument("--by", help="declared window (ISO or 5d/36h/10m); absent = undeclared -> fold uses capture+grace")
    it.add_argument("--from", dest="sender", help="capturing agent (records ownership)")
    it.add_argument("--priority", "-p", default="P2")
    it.set_defaults(func=cmd_intent)
    ho = sub.add_parser("handoff", help="atomic handoff: assignee + checkpoint ref in one write")
    ho.add_argument("team"); ho.add_argument("name"); ho.add_argument("--to", required=True)
    ho.add_argument("--checkpoint"); ho.add_argument("--next", "-n")
    ho.set_defaults(func=cmd_handoff)
    ib = sub.add_parser("inbox", help="open directives for an agent (--ack <slug> to ack)")
    ib.add_argument("team"); ib.add_argument("--agent", "-a"); ib.add_argument("--ack")
    ib.add_argument("--all", action="store_true", help="include @backlog"); add_json(ib)
    ib.set_defaults(func=cmd_inbox)
    ls = sub.add_parser("listen", help="await new directives + responses to directives you own (the reply leg of tell)")
    ls.add_argument("team"); ls.add_argument("--agent", "-a")
    ls.add_argument("--interval", type=int, default=60, help="loop poll seconds (default 60; ignored with --once)")
    ls.add_argument("--once", action="store_true", help="one tick then exit 0 — scheduler-friendly (a tick never fails the schedule)")
    ls.add_argument("--verbose", action="store_true", help="heartbeat quiet ticks to stderr")
    ls.add_argument("--state-path", action="store_true", dest="state_path",
                    help=argparse.SUPPRESS)  # print resolved state file path, no tick
    add_json(ls); ls.set_defaults(func=cmd_listen)
    hl = sub.add_parser("health", help="fleet health: which hosts reconcile this team (fulcra-agent-health)")
    hl.add_argument("team"); add_json(hl)
    hl.set_defaults(func=cmd_health)

    th = sub.add_parser("threads", help="dropped work-in-progress for a principal (started-then-silent / blocked-on / intent-never-started)")
    th.add_argument("team")
    th.add_argument("--for", dest="principal", required=True, help="the principal (e.g. ash)")
    th.add_argument("--silence-days", dest="silence_days", type=float,
                    help="mode-1 silence window in days (default 3; env COORD_THREADS_SILENCE_DAYS)")
    th.add_argument("--intent-grace-hours", dest="intent_grace_hours", type=float,
                    help="mode-3 grace when an intent declares no window, hours (default 48; env COORD_THREADS_INTENT_GRACE_HOURS)")
    add_json(th)
    th.set_defaults(func=cmd_threads)

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
    ati = atsub.add_parser(
        "init", help="standalone onboarding: seed team/<team>/atc/accounts.json")
    ati.add_argument("team", nargs="?", default="solo",
                     help="team to onboard (default: solo)")
    ati.add_argument("--yes", action="store_true",
                     help="non-interactive; requires >=1 --account id=provider:plan")
    ati.add_argument("--account", action="append", metavar="id=provider:plan",
                     help="declare an account (repeatable); :plan is optional")
    ati.add_argument("--harness", action="append",
                     help="override the seeded harnesses for declared accounts "
                          "(repeatable; default is the map's per-provider union)")
    ati.set_defaults(func=cmd_atc_init)

    def _add_dash_parser(parent: Any) -> None:
        d = parent.add_parser(
            "dash", help="serve the localhost ATC gauge dashboard (127.0.0.1 only)")
        d.add_argument("team")
        d.add_argument("--port", type=int, default=8787,
                       help="loopback port to bind (default 8787)")
        d.set_defaults(func=cmd_dash)

    # `dash` lives both top-level (legacy) and under the `atc` group (spec says
    # `atc dash`) — same handler, so either invocation serves the dashboard.
    _add_dash_parser(sub)
    _add_dash_parser(atsub)

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
    fgm = fgsub.add_parser("mirror", help="one pass: PR state -> evidence shards + auto-verdict on merge (also sweeps feedback)")
    fgm.add_argument("team")
    fgm.add_argument("--repo", help="owner/name allowlist: mirror ONLY PR urls of this repo")
    fgm.set_defaults(func=cmd_forge_mirror, runner=None)

    fgf = fgsub.add_parser("feedback", help="sweep-only: mirror PR reviews/inline/comments to feedback shards")
    fgf.add_argument("team")
    fgf.add_argument("--repo", help="owner/name allowlist: sweep ONLY PR urls of this repo")
    fgf.set_defaults(func=cmd_forge_feedback, runner=None)

    fgw = fgsub.add_parser("watch", help="register a PR to sweep for feedback (owner-repo-number slug)")
    fgw.add_argument("team"); fgw.add_argument("pr_url")
    fgw.add_argument("--agent", help="responsible agent (default: caller)")
    fgw.set_defaults(func=cmd_forge_watch)

    fgu = fgsub.add_parser("unwatch", help="remove a PR watch registration")
    fgu.add_argument("team"); fgu.add_argument("pr_url")
    fgu.set_defaults(func=cmd_forge_unwatch)

    mg = sub.add_parser("migrate", help="one-shot exporter: incumbent fulcra-coord tasks -> this team (docs 06)")
    mg.add_argument("team")
    mg.add_argument("--source", default="/coordination")
    mg.add_argument("--dry-run", action="store_true")
    mg.add_argument("--no-mark", dest="no_mark", action="store_true",
                    help="rehearsal: don't tag incumbent tasks migrated:coord")
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

    an = sub.add_parser("annotate",
                        help="project task transitions onto the Fulcra timeline (heartbeat concern)")
    ansub = an.add_subparsers(dest="annotate_command", required=True)
    anr = ansub.add_parser("resolution",
                           help="set the projection resolution level on the bus (off|transitions)")
    anr.add_argument("team"); anr.add_argument("level")
    anr.set_defaults(func=cmd_annotate_resolution)
    ans = ansub.add_parser("status", help="show resolution level + cursor position")
    ans.add_argument("team"); add_json(ans)
    ans.set_defaults(func=cmd_annotate_status)
    anp = ansub.add_parser("project",
                           help="fold reconcile's fresh transitions onto the timeline (model-free)")
    anp.add_argument("team")
    anp.set_defaults(func=cmd_annotate_project)
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
