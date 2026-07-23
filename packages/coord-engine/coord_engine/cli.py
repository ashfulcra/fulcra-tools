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
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import aggregate, atc, atc_dash, budget as budget_mod, config, continuity, continuity_audit, digest as digest_mod, directives, forge as forge_mod, health as health_mod, jsonutil, okf, presence, query, review, roles, router, stash, tasks
from .budget import Deadline
from . import reconcile as rec
from .log import get_logger
from .transport import FulcraFileTransport, TransportError

__all__ = ["main"]

_log = get_logger("cli")

# Cohesive command groups extracted into focused modules (behavior-preserving
# split). Each imports ``cli`` and reaches shared helpers through it, so there is
# no module-load cycle and ``monkeypatch.setattr(cli, …)`` still steers. Their
# public names are re-exported at the BOTTOM of this module (after every helper is
# defined) so ``build_parser``'s dispatch table and existing ``cli.<name>`` call
# sites (and tests) resolve unchanged.


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
    """Read-COUNT bound for the freshness overlay. Env ``COORD_OVERLAY_CAP``."""
    return config.env_int("COORD_OVERLAY_CAP", DEFAULT_OVERLAY_CAP)


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
    """Time bound (seconds) for the freshness overlay's doc reads. Env
    ``COORD_OVERLAY_BUDGET`` (see the DEFAULT_OVERLAY_BUDGET rationale)."""
    return config.env_float("COORD_OVERLAY_BUDGET", DEFAULT_OVERLAY_BUDGET)


DELTA_FEED_MAX_HOURS = 24.0


def _delta_feed_window(since: Any, *, now: str) -> Optional[str]:
    """Return the inclusive data-updates period for ``since`` → ``now``.

    Missing/corrupt/future/too-old cursors are doubt and therefore return None;
    callers take their existing full-listing fallback.  The same skew margin as
    reconcile makes the rescan inclusive across host/store clock boundaries.
    """
    start = rec._parse_iso_utc(since)
    end = rec._parse_iso_utc(now)
    if start is None or end is None:
        return None
    seconds = (end - start).total_seconds()
    if seconds < 0 or seconds > DELTA_FEED_MAX_HOURS * 3600:
        return None
    return f"{int(seconds) + rec.FAST_PATH_SKEW_MARGIN_SECONDS} seconds"


def _team_updates(
    transport: Any, team: str, *, since: Any, now: str,
) -> Optional[list[dict[str, Any]]]:
    """One parsed, team-filtered feed call, or None for UNKNOWN.

    The TypeError retry preserves duck-typed/mixed-version transports while the
    real transport owns the new ``team=`` filtering contract.
    """
    updates_fn = getattr(transport, "updates", None)
    window = _delta_feed_window(since, now=now)
    if updates_fn is None or window is None:
        return None
    try:
        try:
            changes = updates_fn(window, team=team)
        except TypeError:
            changes = updates_fn(window)
    except Exception:
        return None
    if not isinstance(changes, list):
        return None
    prefix = f"team/{team}/"
    parsed: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict):
            return None
        path = change.get("path", change.get("full_name"))
        if not isinstance(path, str) or not path.strip():
            return None
        path = path.strip().lstrip("/")
        if not path.startswith(prefix):
            continue
        state = change.get("state")
        if state not in ("uploaded", "archived", "deleted"):
            return None
        parsed.append({
            "path": path,
            "state": state,
            "uploaded_at": change.get("uploaded_at"),
            "archived_at": change.get("archived_at"),
            "deleted_at": change.get("deleted_at"),
        })
    return parsed


def _change_instant(change: dict[str, Any]) -> Optional[datetime]:
    state = change.get("state")
    key = {"uploaded": "uploaded_at", "archived": "archived_at",
           "deleted": "deleted_at"}.get(state)
    value = change.get(key) if key else None
    # Some archive/delete payloads retain only uploaded_at.  That timestamp is
    # still usable for deterministic collapsing; total absence is feed doubt.
    return rec._parse_iso_utc(value or change.get("uploaded_at"))


def _feed_task_rows(
    transport: Any, team: str, index_rows: list[dict[str, Any]],
    changes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool, str]:
    """Apply changed task shards to aggregate rows without a task-dir listing."""
    from . import model
    prefix = rec.task_prefix(team)
    # A store rewrite emits same-second archived(old)+uploaded(new) entries and
    # does not promise feed order.  Total state priority makes that lifecycle
    # collapse deterministic: the live upload wins an equal-instant rewrite,
    # while a strictly-later terminal event still removes the row.
    state_priority = {"archived": 0, "deleted": 1, "uploaded": 2}
    latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for change in changes:
        path = str(change.get("path") or "")
        if not path.startswith(prefix) or not path.endswith(".md"):
            continue
        name = path[len(prefix):]
        if "/" in name or name in ("index.md", "log.md"):
            continue
        instant = _change_instant(change)
        if instant is None:
            return [], False, f"data-updates change for {name} lacks a usable timestamp"
        prior = latest.get(path)
        if prior is None or (
            instant, state_priority[str(change.get("state"))]
        ) >= (
            prior[0], state_priority[str(prior[1].get("state"))]
        ):
            latest[path] = (instant, change)

    by_name = {str(r.get("name")): r for r in index_rows
               if isinstance(r, dict) and r.get("name")}
    for path in sorted(latest):
        instant, change = latest[path]
        name = path[len(prefix):]
        slug = name[:-3]
        if change.get("state") in ("archived", "deleted"):
            by_name.pop(slug, None)
            continue
        try:
            raw = transport.read(path)
        except Exception:
            raw = None
        if raw is None:
            return [], False, f"data-updates task shard {name} unreadable"
        try:
            fm = okf.parse_frontmatter(raw)
            if fm is not None and model.is_task(fm):
                by_name[slug] = model.row_from_frontmatter(
                    fm, name=slug, path=f"task/{name}",
                    mtime=instant.strftime("%Y-%m-%d %I:%M%p UTC"))
            else:
                return [], False, (
                    f"data-updates task shard {name} is malformed or not a Task")
        except Exception as exc:
            return [], False, (
                f"data-updates task shard {name} is malformed ({exc})")
    return [by_name[k] for k in sorted(by_name)], True, ""


def _fresh_overlay_rows(
    transport: Any, team: str, index_rows: list[dict[str, Any]], *,
    deadline: "Optional[Deadline]" = None,
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
        wasn't what stopped us). Independent failures compose: an unreadable-doc
        reason is preserved alongside a later budget/cap truncation reason.
    Parse-garbage / not-a-Task docs remain sanctioned SILENT skips (mirrors
    reconcile's own tolerance). Cost: one extra ``list_dir`` per row load, plus one
    ``read`` per genuinely-new (unsummarized) slug, at most the cap, within the
    budget."""
    own_dl = Deadline.open(_overlay_budget())
    # A caller may provide a stricter absolute phase deadline (listen's protected
    # head). The overlay keeps its own cap/budget, but can never outlive that
    # caller: whichever instant arrives first wins.
    if deadline is not None and deadline.instant is not None:
        instant = (deadline.instant if own_dl.instant is None else
                   min(deadline.instant, own_dl.instant))
        dl = Deadline(instant)
    else:
        dl = own_dl
    prefix = rec.task_prefix(team)
    try:
        listing = transport.list_dir(prefix)
    except Exception:
        # listing unknown -> degraded (caller surfaces it), never silent
        return [], False, "task-dir overlay unreadable"
    if dl.expired():
        return [], False, "task-dir overlay budget exhausted after listing"
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
    ok = True
    reasons: list[str] = []
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
            reasons.append(f"task-dir overlay: fresh doc {name} unreadable")
        else:
            try:
                fm = okf.parse_frontmatter(raw)
                if fm is not None and model.is_task(fm):
                    overlay.append(model.row_from_frontmatter(
                        fm, name=name[:-3], path=f"task/{name}", mtime=entry.get("mtime")))
                # else: parse-garbage / not a Task -> sanctioned silent skip
            except Exception:
                pass  # malformed content is a skip, not a transport failure
        if dl.expired():
            # After-op discipline: the budget bounds TIME where the cap bounds
            # COUNT — stop reading, serve what we have, degrade visibly.
            budget_breached = True
            break
    if budget_breached and served < len(absent):
        ok = False
        reasons.append(f"task-dir overlay budget exhausted: served {served} of "
                       f"{len(absent)} fresh docs")
    elif len(absent) > cap:
        ok = False
        reasons.append(f"task-dir overlay truncated: served {cap} of {len(absent)} "
                       f"fresh docs (COORD_OVERLAY_CAP={cap})")
    return overlay, ok, "; ".join(reasons)


def _load_rows_status(
    transport: Any, team: str, *, deadline: "Optional[Deadline]" = None,
    feed_changes: "Optional[list[dict[str, Any]]]" = None,
    feed_attempted: bool = False,
) -> tuple[list[dict[str, Any]], bool, str]:
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
    if deadline is not None and deadline.expired():
        # The protected caller phase did not complete inside its clock. Even if
        # this read returned bytes, do not advance any directive id from a phase
        # whose remaining freshness work is unknown; recovery re-reads/delivers.
        return [], False, "caller row-load budget exhausted after summaries read"
    if raw:
        try:
            aggregate_doc = json.loads(raw)
            rows = aggregate.aggregate_rows(aggregate_doc)
        except Exception:
            # index present but corrupt -> unreadable, surface it
            return [], False, "summaries index unreadable"
        # E2 primary path: one authoritative feed call since the aggregate cursor,
        # then direct reads of only changed task shards.  No task-dir listing is
        # consulted, so listing lag cannot hide a verified feed entry.
        aggregate_cursor = (
            aggregate_doc.get("generated_at")
            if isinstance(aggregate_doc, dict)
            else None
        )
        feed = (feed_changes if feed_attempted else _team_updates(
            transport, team, since=aggregate_cursor, now=_iso(_now())))
        if feed is not None:
            delta_rows, delta_ok, _delta_reason = _feed_task_rows(
                transport, team, rows, feed)
            if delta_ok:
                if deadline is not None and deadline.expired():
                    return [], False, "caller row-load budget exhausted during feed delta"
                return delta_rows, True, ""
            # Any feed/read doubt takes the byte-for-byte legacy listing overlay
            # below.  A healthy fallback is not a degraded public read.
        # Live-freshness overlay: union in task docs written since the last
        # reconcile (absent from this index). Any overlay problem flips ``ok`` so
        # the inbox source degrades visibly; the index rows are still served.
        overlay, overlay_ok, overlay_reason = _fresh_overlay_rows(
            transport, team, rows, deadline=deadline)
        if deadline is not None and deadline.expired():
            return [], False, (overlay_reason or
                               "caller row-load budget exhausted during overlay")
        return rows + overlay, overlay_ok, overlay_reason
    parent, entry = path.rsplit("/", 1)
    try:
        names = {e.get("name") for e in transport.list_dir(parent + "/")}
    except TransportError:
        # transport down -> unknown, not a confirmed-empty index
        return [], False, "summaries index unreadable"
    if deadline is not None and deadline.expired():
        return [], False, "caller row-load budget exhausted after index listing"
    if entry in names:
        # index there yet unreadable (read returned None) -> degraded
        return [], False, "summaries index unreadable"
    return [], True, ""  # genuinely absent -> a real, readable empty


def _load_rows(transport: Any, team: str) -> list[dict[str, Any]]:
    return _load_rows_status(transport, team)[0]


# --- The public-read failure contract (defined ONCE) -----------------------
#
# Every aggregate-backed PUBLIC READ — `status`, `board`, `needs-me`, `search`,
# `inbox` (and the `briefing`/`threads` bundles) — folds the summaries index via
# `_load_rows_status`, whose ``ok`` bit is False when the index/listing is
# UNKNOWN: an unreadable/corrupt index, a read that failed under a degraded
# transport, or a degraded freshness overlay. UNKNOWN is NOT the same as a
# genuinely-absent index (a fresh team, no reconcile yet), which is a real,
# readable EMPTY (``ok`` True). THE CONTRACT: a read whose ``ok`` is False must
# NEVER return a clean-empty result. It emits the shared machine-parseable
# degraded row below (family-consistent with ``review-fold-degraded`` /
# ``forge-degraded`` / ``presence-degraded`` / ``threads-degraded``) and, in text
# mode, a stderr notice — so "unknown" is LOUD, never silently indistinguishable
# from "nothing to do". This is the README's "fails loud, never silent" property;
# `cmd_threads` is the reference implementation this generalizes. The one hazard
# this closes: a silently-empty task fold that reads as "all clear" while a real
# unacked directive (a live P1) is merely unreadable — codex live-reproduced it on
# `inbox --json` under a clamped transport timeout.
_READ_DEGRADED = "read-degraded"


def _read_degraded_row(reason: str, *, marker: str = _READ_DEGRADED) -> dict[str, Any]:
    """Build the ONE public-read degraded marker row — shape ``{type, reason}``
    (the degraded-row family shape ``{type, scanned?, total?, reason}`` with
    scanned/total omitted, because a summaries-index fold is all-or-nothing rather
    than a bounded partial scan). ``marker`` lets `inbox` stamp its named
    ``inbox-degraded`` type while every caller shares this one builder."""
    return {"type": marker, "reason": reason or "summaries index unreadable"}


def _surface_read_degraded(reason: str, *, json_mode: bool,
                           marker: str = _READ_DEGRADED) -> None:
    """Emit the degraded marker the house way for text mode / a stderr notice:
    under ``--json`` the caller is expected to carry the row IN its result (a
    list element or a reserved dict key, so stdout stays a single parseable
    value); this only prints the stderr notice consumed by humans and monitors
    (`json_mode` suppresses stdout noise so a piped consumer never confuses the
    notice for a result). Never suppresses data — the caller still prints its
    partial rows."""
    if not json_mode:
        print(f"{marker}: {reason or 'summaries index unreadable'} — "
              f"unknown, not empty; retry", file=sys.stderr)


def _line(row: dict[str, Any]) -> str:
    return (
        f"  [{row.get('priority', '?'):>2}] {str(row.get('status', '?')):8} "
        f"{row.get('title') or row.get('name')}"
        + (f"  ({row.get('assignee')})" if row.get("assignee") else "")
    )


# --- blocked-on-human: the reserved, un-starvable FIRST section ---------------
#
# A decision parked on a human is the incident this section keeps visible. It is
# derived PURELY from the aggregate rows already in memory (see
# ``query.blocked_on_human``), so it adds ZERO transport ops — that free-ness is
# exactly what makes it un-starvable: no budget cut can hide it, because it spends
# no budget. The classifier needs one input to tell an agent-blocked legacy row
# from a human-blocked one — the caller's known-identity set — which we assemble
# from data the fold already holds (row assignees/owners + the held roles it
# already resolved), never a fresh read.

def _known_identities(
    rows: list[dict[str, Any]], held_roles: "Optional[set[str]]" = None
) -> set[str]:
    """The caller's already-loaded agent/role identity set — the free input the
    blocked-on-human classifier uses to distinguish an agent block from a human
    block. Assembled from in-memory data only (row assignees/owners + held roles)."""
    ids: set[str] = set()
    for r in rows or []:
        for k in ("assignee", "owner"):
            v = r.get(k)
            if v:
                ids.add(str(v))
    ids |= set(held_roles or ())
    return ids


def _blocked_on_human_section(
    rows: list[dict[str, Any]], *, held_roles: "Optional[set[str]]" = None,
    roles_unknown: bool = False,
) -> list[dict[str, Any]]:
    """The FIRST section of `briefing` / `needs-me`: open rows blocked on a human.
    Pure over ``rows`` — no transport. ``roles_unknown`` (the caller's role
    resolution degraded) makes an unresolvable legacy value SURFACE with a degraded
    note rather than hide."""
    return query.blocked_on_human(
        rows, human=_human(),
        known_agents=_known_identities(rows, held_roles),
        roles_unknown=roles_unknown)


def _blocked_on_human_line(r: dict[str, Any]) -> str:
    user = r.get("blocked_on_user") or _human()
    note = " — degraded: agent/role listing unknown" if r.get("blocked_on_degraded") else ""
    return (
        f"  [{r.get('priority', '?'):>2}] {str(r.get('status', '?')):8} "
        f"{r.get('title') or r.get('name')}  (blocked on {user}){note}"
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
    # Public-read failure contract (see _read_degraded_row): consume the readable
    # bit, never fold an UNKNOWN index to clean-empty (all-zero) counts.
    rows, ok, reason = _load_rows_status(transport, args.team)
    counts = query.status_counts(rows)
    if args.json:
        if not ok:
            # Embed the marker under a reserved key so stdout stays ONE parseable
            # object; a consumer summing status counts already knows its status
            # vocabulary and skips the namespaced marker.
            counts = {**counts, _READ_DEGRADED: _read_degraded_row(reason)}
        jsonutil.print_json(counts)
    else:
        if not ok:
            _surface_read_degraded(reason, json_mode=False)
        elif not rows:
            print(f"(no aggregate for team/{args.team} — run `reconcile` first)")
        print(f"team/{args.team}: {len(rows)} tasks — " + ", ".join(
            f"{k}={v}" for k, v in sorted(counts.items())
            if k != _READ_DEGRADED))
    return 0


def cmd_board(args: argparse.Namespace, transport: Any) -> int:
    rows, ok, reason = _load_rows_status(transport, args.team)
    groups = query.board(rows)
    if args.json:
        if not ok:
            # Reserved section-shaped key: value is a list (like every other board
            # section) so stdout stays one parseable object and the text loop's
            # fixed section set ignores it.
            groups[_READ_DEGRADED] = [_read_degraded_row(reason)]
        jsonutil.print_json(groups)
        return 0
    if not ok:
        _surface_read_degraded(reason, json_mode=False)
    for section in ("active", "waiting", "blocked", "proposed"):
        items = groups.get(section, [])
        if items:
            print(f"{section.upper()} ({len(items)})")
            for r in items:
                print(_line(r))
    return 0


def cmd_needs_me(args: argparse.Namespace, transport: Any) -> int:
    now = _iso(_now())
    rows, rows_ok, rows_reason = _load_rows_status(transport, args.team)
    # Role routing: work addressed to a role this agent holds IS work that needs
    # this agent (see _held_roles_for_rows). An unresolved role is UNKNOWN and gets
    # its own marker below — never folded into "no role work".
    held_roles, unresolved_roles = _held_roles_for_rows(
        transport, args.team, args.agent, rows, now=now)
    got = _needs_me_rows(transport, args.team, args.agent, rows, now=now,
                         held_roles=held_roles, include_history=args.all)
    # Public-read failure contract: an UNKNOWN task fold must announce itself with
    # the shared marker BEFORE the review/forge add-ons pile their own markers onto
    # what would otherwise read as a silently-empty (but "complete") needs-me.
    if not rows_ok:
        got = [_read_degraded_row(rows_reason)] + got
    if unresolved_roles:
        got = [_role_degraded_row(unresolved_roles)] + got
    # Shared add-on deadline (see _briefing_budget): opened here so the forge
    # fan-out is bounded cumulatively, not per-section. pending-reviews keeps its
    # own independent, already-shipped budget.
    add_on = Deadline.open(_briefing_budget())
    got += _pending_reviews_for(
        transport, args.team, args.agent, rows=rows, deadline=add_on.instant)
    got += _forge_feedback_for(transport, args.team, args.agent, deadline=add_on.instant)
    # Blocked-on-human is the reserved FIRST section — prepended AFTER every other
    # section is built so it lands at index 0, and derived PURELY from ``rows``
    # (zero transport, un-starvable). It surfaces decisions parked on a human that
    # are assigned to the human (never to this agent), so they are not otherwise in
    # ``got``; de-dup by id guards the rare overlap.
    blocked = _blocked_on_human_section(
        rows, held_roles=held_roles or None, roles_unknown=bool(unresolved_roles))
    seen = {r.get("id") for r in blocked}
    got = blocked + [r for r in got if r.get("id") not in seen]
    if args.json:
        jsonutil.print_json(got)
    else:
        print(f"{len(got)} item(s) need {args.agent}:")
        for r in got:
            if r.get("type") == "blocked-on-human":
                print(_blocked_on_human_line(r))
            elif r.get("type") == _READ_DEGRADED:
                print(f"  read degraded: {r.get('reason')} — task fold unknown "
                      f"(not empty), retry")
            elif r.get("type") == _ROLE_DEGRADED:
                print(_role_degraded_line(r))
            elif (review_line := _review_row_line(r)) is not None:
                # Every review row type (pending / orphan / the degraded + head
                # UNKNOWN markers) dispatches here — a review row must NEVER reach
                # the generic task line below, which would print `[ ?] ? None`.
                print(review_line)
            elif r.get("type") == "forge-feedback":
                print(_forge_feedback_line(r))
            elif r.get("type") == "forge-degraded":
                print(_forge_degraded_line(r))
            else:
                print(_line(r))
    return 0


def cmd_search(args: argparse.Namespace, transport: Any) -> int:
    rows, ok, reason = _load_rows_status(transport, args.team)
    degraded_reasons = [] if ok else [reason]
    if getattr(args, "archived", False):
        # cold path: read archived task docs directly (archives are small + rare)
        from . import model as _model
        months, archive_reason = _archive_months_status(transport, args.team)
        if archive_reason:
            degraded_reasons.append(archive_reason)
        for month in months:
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
                degraded_reasons.append(f"task archive/{month} unreadable")
    got = query.search(rows, args.query)
    # Public-read failure contract: an UNKNOWN hot index or partial cold archive
    # must not return a confident match (or clean-empty result). Preserve readable
    # rows as evidence, but prefix the shared degraded marker so consumers fail
    # closed before acting on an incomplete identity view.
    degraded_reason = "; ".join(dict.fromkeys(filter(None, degraded_reasons)))
    if degraded_reason:
        got = [_read_degraded_row(degraded_reason)] + got
    if args.json:
        jsonutil.print_json(got)
    else:
        if degraded_reason:
            _surface_read_degraded(degraded_reason, json_mode=False)
        real = [r for r in got if r.get("type") != _READ_DEGRADED]
        print(f"{len(real)} match(es) for {args.query!r}:")
        for r in real:
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
    reg = okf.parse_frontmatter(raw_doc)
    if reg is None:
        # A read miss and a body that won't PARSE are the same fact — no usable
        # doc — so they take the same path (2026-07-16: the `raw_doc is None`
        # guard let a listed-but-unparseable doc fall through to `or {}`, i.e.
        # onto the 24h default SLA and a confident VACANT at rc 0, which is the
        # precise collapse the comment above forbids. `_role_fresh_holders` was
        # fixed for the identical hole in the same round; both surfaces must agree
        # or the "same fold" contract between them is a lie).
        names = _roles_listing_names(transport, team)
        if names is None or f"{role}.md" in names:
            print(f"role doc unusable for {role} in team/{team} — state unknown "
                  f"(unreadable or corrupt), retry", file=sys.stderr)
            return 1
        reg = {}  # genuinely absent -> default-SLA fallback (leases without a doc)
    policy = reg.get("policy") or "shared"
    sla = roles.parse_sla_hours(reg.get("sla_hours"))
    if sla is None:
        # A readable doc whose `sla_hours` is EXPLICITLY invalid: same fact as an
        # unreadable one — the SLA is unknown, so every state below (HELD / VACANT /
        # escalation_due) would be asserted off a window we invented. rc 1, assert
        # nothing. Absent/blank keeps the default and prints normally.
        print(f"unusable sla_hours ({reg.get('sla_hours')!r}) for {role} in "
              f"team/{team} — state unknown; fix the role doc", file=sys.stderr)
        return 1
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
    # Dormancy: a deliberately-parked role (future dormant_until) reads as DORMANT
    # instead of VACANT and never shows escalation_due — but a LIVE lease outranks
    # the park (HELD wins the display). Garbage dormant_until fails open with a note.
    dormant, dormant_err = roles.dormant_state(reg.get("dormant_until"), now=now)
    if dormant_err:
        print(f"roles status: unparseable dormant_until for {role} in team/{team} — "
              f"treated as absent (not dormant); fix the date to park it",
              file=sys.stderr)
    if status == roles.VACANT and dormant:
        status = roles.DORMANT
    today = _now().strftime("%Y-%m-%d")
    marker_exists = transport.read(_escalation_marker_path(team, role, today)) is not None
    esc = roles.escalation_due(leases, now=now, sla_hours=sla,
                               marker_exists_today=marker_exists, dormant=dormant)
    fresh = roles.fresh_holders(leases, now=now, sla_hours=sla) if leases else []
    result = {
        "team": team, "role": role, "status": status, "policy": policy, "sla_hours": sla,
        "holders": [l.get("agent") for l in (leases or [])],
        "fresh_holders": [l.get("agent") for l in fresh],
        "escalation_due": esc,
    }
    if status == roles.DORMANT:
        result["dormant_until"] = reg.get("dormant_until")
    if args.json:
        jsonutil.print_json(result)
    else:
        label = (f"DORMANT (until {reg.get('dormant_until')})"
                 if status == roles.DORMANT else status)
        print(f"role {role} in team/{team}: {label} (policy={policy}, sla={sla:g}h)")
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
    # TYPE the human block: `--on-user <name>` writes `blocked_on: user:<name>` so
    # the blocked-on-human fold can classify it at ZERO transport cost (a plain
    # value would need an agent/role lookup to tell human from agent). Additive:
    # `--blocked-on <agent>` stays an untyped agent value, and legacy `user:`-less
    # rows still parse (the fold's legacy branch handles them).
    blocked_val = f"{query._USER_PREFIX}{args.on_user}" if args.on_user else args.blocked_on
    kw = {"status": "blocked", "blocked_on": blocked_val}
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


def _archive_months_status(transport: Any, team: str) -> tuple[list[str], str]:
    try:
        return (
            [
                e["name"].rstrip("/")
                for e in transport.list_dir(rec.archive_prefix(team))
                if e.get("is_dir")
            ],
            "",
        )
    except TransportError:
        return [], "task archive months unreadable"


def _archive_months(transport: Any, team: str) -> list[str]:
    return _archive_months_status(transport, team)[0]


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


def _review_archive_months(transport: Any, team: str) -> Optional[list[str]]:
    try:
        return [
            str(e.get("name") or "").rstrip("/")
            for e in transport.list_dir(rec.review_archive_prefix(team))
            if e.get("is_dir") and e.get("name")
        ]
    except TransportError:
        return None


def cmd_review_restore(args: argparse.Namespace, transport: Any) -> int:
    """Restore a cold-archived review family to the hot review path."""
    months = _review_archive_months(transport, args.team)
    if months is None:
        print("review restore failed: archive root listing unknown", file=sys.stderr)
        return 1
    for month in sorted(months, reverse=True):
        cold_doc = f"{rec.review_archive_prefix(args.team)}{month}/{args.slug}.md"
        cold_prefix = (
            f"{rec.review_archive_prefix(args.team)}{month}/{args.slug}/verdicts/"
        )
        try:
            entries = transport.list_dir(cold_prefix)
        except TransportError:
            print(f"review restore failed: archive listing unknown for {args.slug}",
                  file=sys.stderr)
            return 1
        files = [
            str(e.get("name") or "") for e in entries
            if not e.get("is_dir") and str(e.get("name") or "").endswith(".md")
        ]
        archived_doc = transport.read(cold_doc)
        if archived_doc is None and not files:
            continue
        if archived_doc is not None:
            hot_doc = _review_doc_path(args.team, args.slug)
            if not rec._ensure_verified_copy(transport, cold_doc, hot_doc):
                print(f"review restore failed: {args.slug} conflicts with the hot path",
                      file=sys.stderr)
                return 1
            hot_prefix = _verdicts_prefix(args.team, args.slug)
            copied, pairs = rec._copy_tree_verified(
                transport, cold_prefix, hot_prefix)
            if not copied:
                print(f"review restore failed: verified family copy from reviews/{month}/ failed",
                      file=sys.stderr)
                return 1
            if not hasattr(transport, "delete"):
                print("review restore failed: transport cannot delete archived sources",
                      file=sys.stderr)
                return 1
            deleted = [transport.delete(src) for src, _ in pairs]
            deleted.append(transport.delete(cold_doc))
            if not all(deleted):
                print(f"review restore failed: archive cleanup from reviews/{month}/ failed",
                      file=sys.stderr)
                return 1
            settled = rec._load_settled_index(transport, args.team)
            settled.discard(args.slug)
            transport.write(rec.settled_index_path(args.team), json.dumps({
                "schema": "coord.settled-reviews.v1", "reviews": sorted(settled)
            }, separators=(",", ":")))
            print(f"restored review {args.slug} from reviews/{month}/")
            return 0
        if files != ["codex-reviewer.md"]:
            print(f"review restore failed: unexpected archived verdict shape for {args.slug}",
                  file=sys.stderr)
            return 1
        filename = files[0]
        src = cold_prefix + filename
        dst = f"team/{args.team}/review/{args.slug}/verdicts/{filename}"
        if transport.read(dst) is not None:
            print(f"review restore failed: {args.slug} already exists in the hot path",
                  file=sys.stderr)
            return 1
        if rec._crash_safe_move(transport, src, dst):
            print(f"restored review {args.slug} from reviews/{month}/")
            return 0
        print(f"review restore failed: verified move from reviews/{month}/ failed",
              file=sys.stderr)
        return 1
    print(f"review restore failed: {args.slug} not found in the archive", file=sys.stderr)
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

#: Aggregate deadline (seconds) for ``_pending_reviews_for`` — never let a degraded
#: pending-review scan hang or (via a bad env value) run unbounded.
DEFAULT_REVIEW_FOLD_BUDGET = 45.0
#: Aggregate deadline (seconds) for the transport-heavy briefing/needs-me add-on
#: sections (chiefly the team-global forge-feedback fan-out, which did unbounded
#: per-PR reads and hung the whole bundle under a degraded transport). ONE budget
#: opens when the add-on stack begins and is spent cumulatively across sections;
#: pending-reviews keeps its own independent COORD_REVIEW_FOLD_BUDGET (sooner wins).
DEFAULT_BRIEFING_BUDGET = 60.0
#: Cumulative deadline (seconds) for ONE role-resolution pass (`_held_roles_for_rows`)
#: — the fold `briefing` / `inbox` / `needs-me` / `listen` all run, i.e. every agent,
#: every tick. Its cost is 1 + sum(2 + lease_shards) over the roles the open work
#: references (see `_held_roles_for_rows`), and lease shards accumulate per claiming
#: agent forever (only `roles release` prunes one), so an unbudgeted pass could spend
#: one transport timeout per role doc, per lease listing AND per shard before the hot
#: path renders anything. 20s is a generous ~25 ops at the measured ~0.8s/op — far
#: past the 4-7 a real team pays — while still bounding a degraded transport.
DEFAULT_ROLE_FOLD_BUDGET = 20.0
#: Per-tick bound (seconds) for the listener's dir-only review-slug classification
#: pass. That set is PERMANENT and growing (soft deletes leave every review dir
#: forever), so an unbudgeted pass could spend N x transport-timeout on a degraded
#: tick, on the watcher whose tick latency is load-bearing. 10s is a bounded
#: fraction of the default 60s poll interval.
DEFAULT_LISTEN_CLASSIFY_BUDGET = 10.0
#: Dedicated budget for the caller-directed inbox head.  It is deliberately
#: independent of the listener tail budget: role expansion and the global
#: responses/reviews history must never spend the clock before literal-agent and
#: wildcard directives have been checked.
DEFAULT_LISTEN_HEAD_BUDGET = 10.0
#: Shared aggregate budget for every non-head listener leg (role routing,
#: responses, verdicts, and orphan classification).  The deadline opens at tick
#: start, so an expensive head can legitimately leave less history work; the head
#: itself has the independent budget above and therefore cannot be starved by it.
DEFAULT_LISTEN_TAIL_BUDGET = 20.0

# The `threads` fold/window defaults (DEFAULT_THREADS_*) live with the threads
# command in `commands_threads.py`; they are re-exported onto `cli` at module end.


def _settled_marker_path(team: str, slug: str) -> str:
    return _verdicts_prefix(team, slug) + SETTLED_MARKER


def _review_fold_budget() -> float:
    """Aggregate deadline for `_pending_reviews_for`, seconds. Env
    ``COORD_REVIEW_FOLD_BUDGET`` (see the DEFAULT_REVIEW_FOLD_BUDGET rationale)."""
    return config.env_float("COORD_REVIEW_FOLD_BUDGET", DEFAULT_REVIEW_FOLD_BUDGET)


def _briefing_budget() -> float:
    """Shared aggregate deadline (seconds) for the briefing/needs-me add-on stack.
    Env ``COORD_BRIEFING_BUDGET`` (see the DEFAULT_BRIEFING_BUDGET rationale). One
    absolute ``time.monotonic()`` deadline is computed where the stack opens and
    passed to each transport-heavy section, so an earlier section's spend shrinks
    what the next one gets; pending-reviews keeps its own independent
    ``COORD_REVIEW_FOLD_BUDGET`` (whichever bound is sooner wins)."""
    return config.env_float("COORD_BRIEFING_BUDGET", DEFAULT_BRIEFING_BUDGET)


def _role_fold_budget() -> float:
    """Cumulative deadline (seconds) for one role-resolution pass. Env
    ``COORD_ROLE_FOLD_BUDGET`` (see the DEFAULT_ROLE_FOLD_BUDGET rationale). Its own
    knob, like ``COORD_REVIEW_FOLD_BUDGET``: role resolution runs BEFORE the
    briefing/needs-me add-on stack opens its budget (the held set is an input to the
    inbox fold, not an add-on section), so it cannot spend that one."""
    return config.env_float("COORD_ROLE_FOLD_BUDGET", DEFAULT_ROLE_FOLD_BUDGET)


def _listen_classify_budget() -> float:
    """Per-tick bound (seconds) for the listener's dir-only review-slug
    classification pass. Env ``COORD_LISTEN_CLASSIFY_BUDGET`` (see the
    DEFAULT_LISTEN_CLASSIFY_BUDGET rationale)."""
    return config.env_float("COORD_LISTEN_CLASSIFY_BUDGET", DEFAULT_LISTEN_CLASSIFY_BUDGET)


def _listen_head_budget() -> float:
    """Dedicated caller-directed head budget, seconds. Env
    ``COORD_LISTEN_HEAD_BUDGET``."""
    return config.env_float("COORD_LISTEN_HEAD_BUDGET", DEFAULT_LISTEN_HEAD_BUDGET)


def _listen_tail_budget() -> float:
    """Shared non-head listener budget, seconds. Env
    ``COORD_LISTEN_TAIL_BUDGET``."""
    return config.env_float("COORD_LISTEN_TAIL_BUDGET", DEFAULT_LISTEN_TAIL_BUDGET)


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
    dl = Deadline(deadline)
    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md"):
            continue
        if dl.expired():
            # Budget expired mid-slug: stop reading shards. The tally built so far
            # is a floor, not the truth — the caller treats this slug as skipped.
            fully_scanned = False
            break
        raw_v = transport.read(_verdicts_prefix(team, slug) + n)
        if dl.expired():
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


def _classify_orphan_dir(transport: Any, team: str, slug: str) -> str:
    """Classify a dir-only review slug — a ``<slug>/`` prefix under the review root
    with NO ``<slug>.md`` doc — via ONE listing of its verdicts prefix (the same
    listing the orphan feature needs, so classification is zero extra ops). The
    store's deletes are SOFT: an archived/deleted review leaves its dir prefix
    behind forever, so the three-way tells a live orphan from that ghost:

    - ``"orphan"``    — at least one verdict ``.md`` shard is present: real
      verdicts, no doc. Surface for maintainer repair (unchanged behavior).
    - ``"tombstone"`` — no verdict ``.md`` shards (empty, or only a stale
      ``.settled`` marker whose review doc is gone). The dir carries ZERO
      information; fold it away silently — an orphan/[?] row here is the WRONG
      ontology, not a real pending obligation, and a retry never resurrects a doc.
    - ``"unknown"``   — the verdicts listing RAISED (degraded transport). NEVER
      assume tombstone on a transport failure: the fail-closed rule outranks
      tombstone-skip, so this stays VISIBLY degraded and is retried."""
    try:
        ventries = transport.list_dir(_verdicts_prefix(team, slug))
    except TransportError:
        return "unknown"
    for x in ventries:
        n = x.get("name") or ""
        if not x.get("is_dir") and n.endswith(".md"):
            return "orphan"
    return "tombstone"


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
    deadline: Optional[Deadline] = None,
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
    - no USABLE role document — the read returned None, or returned a body that
      does not parse as frontmatter — for a name the roles/ listing SHOWS is a
      registered role (or while that listing itself raised, leaving membership
      unknown);
    - the doc parses but its ``sla_hours`` is EXPLICITLY INVALID (``abc``, a
      negative, a non-finite): the operator stated a window and it did not parse,
      so there is nothing to measure freshness against. An ABSENT or blank
      ``sla_hours`` is NOT this case — the field is optional and omitting it
      legitimately selects the default (``roles.parse_sla_hours`` draws the line);
    - ``deadline`` expires with role state still unread (see below).

    **Only a complete, successfully parsed LISTING is negative membership
    evidence.** The one non-degraded absence is a doc-read miss for a name the
    listing affirmatively does NOT contain (``([], True)`` — the literal-agent-id
    case). A failed read and a failed PARSE are the same fact: we do not know what
    that document says. Until 2026-07-16 an unparseable body short-circuited to
    "affirmative non-role" — but the listing has already proved the name IS a
    role, so a truncated or malformed doc served its holder a clean, role-blind
    queue with no ``role-degraded`` marker at all (reviewer-reproduced: a
    ``reviewer.md`` of ``"not frontmatter"`` emitted an empty inbox AND an empty
    needs_me, silently). A parse result is not evidence about registration; the
    listing is.

    ``deadline`` bounds the role's own fan-out (its doc read, its lease listing,
    and a read per lease shard — unbounded in the shard count, since shards
    accumulate per claiming agent). Checked before each blocking op that follows
    another, per the module deadline discipline: an overrun is detected
    immediately after the op that caused it, overshoot is bounded by ONE op, and a
    COMPLETED fold is never degraded merely for finishing late (its answer is
    definitive knowledge — keep it). ``None`` -> unbounded, for the direct callers
    (`roles status`, atc) that are not on the hot path.

    ``listing_cache`` (a per-tick/per-fold dict) memoizes the one roles/ listing
    across role-shaped assignees; pass the same dict for every call in a pass."""
    if "/" in name:
        return [], True  # a role name is a single path segment; anything else is not a role
    dl = deadline if deadline is not None else Deadline(None)  # None -> never expires
    raw_doc = transport.read(_role_doc_path(team, name))
    reg = okf.parse_frontmatter(raw_doc)
    if reg is None:
        # No usable role document: absent, empty, truncated, or unparseable. Which
        # of those it is does not matter here — none of them is evidence about
        # whether `name` is a registered role. Only the listing answers that.
        cache = listing_cache if listing_cache is not None else {}
        if "names" not in cache:
            cache["names"] = _roles_listing_names(transport, team)
        names = cache["names"]
        if names is None or f"{name}.md" in names:
            # roles/ listing unreadable (membership unknown) OR the doc is listed
            # yet unusable (transport failure / corrupt doc): UNKNOWN, fail closed.
            return [], False
        return [], True  # genuinely absent -> not a role (literal agent id case)
    sla = roles.parse_sla_hours(reg.get("sla_hours"))
    if sla is None:
        # The doc parsed, but its `sla_hours` did not: an EXPLICITLY invalid value.
        # UNKNOWN — freshness has no window to be measured against. Absent/blank
        # still means "use the default" and resolves normally; see
        # `roles.parse_sla_hours` for why those two are not the same fact.
        return [], False
    if dl.expired():
        return [], False  # the doc read spent the budget; the lease state is UNREAD
    leases: list[dict[str, Any]] = []
    try:
        for f in transport.list_dir(_leases_prefix(team, name)):
            fn = f.get("name") or ""
            if f.get("is_dir") or not fn.endswith(".md"):
                continue
            if dl.expired():
                # The listing (or the previous shard read) spent the budget with
                # shards still unread. A lease we never read is UNKNOWN, exactly as
                # if its read had failed — folding the rest out would assert a
                # vacancy we did not observe.
                return [], False
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


# --- role routing on the READ folds ---------------------------------------
#
# A directive assigned to a ROLE is directed at whoever holds a fresh lease on it
# — the contract AGENTS.md states ("briefing prints your identity, role inboxes,
# and everything that needs you") and the reason role-based identity exists at
# all: work addressed to a role must outlive the session that was holding it.
# `listen` honoured it from the start; `briefing` / `inbox` / `needs-me` did not,
# so a role-addressed `tell` returned 0 and silently landed in a fold nobody read.
#
# ONE resolver for every caller (`_held_roles_for_rows`). The alternative — each
# fold resolving roles its own way — is how the two paths diverged in the first
# place, and the failure is invisible by construction (a fold that resolves no
# roles looks exactly like an agent who holds none).
_ROLE_DEGRADED = "role-degraded"


def _role_degraded_row(roles_unknown: "set[str] | list[str]") -> dict[str, Any]:
    """The marker for roles whose holder set could NOT be determined — shape
    ``{type, roles}``, same family as ``review-role-degraded`` (which reports the
    same UNKNOWN for the review fold). Never omitted: an unresolved role means
    role-routed work may be missing from the fold, and "unknown" must never render
    as "nothing for you"."""
    return {"type": _ROLE_DEGRADED, "roles": sorted(roles_unknown)}


def _role_degraded_line(r: dict[str, Any]) -> str:
    return (f"  role resolution degraded: {', '.join(r.get('roles') or [])} — "
            f"your role inbox is unknown (not empty); role-routed work may be "
            f"missing, retry")


def _held_roles_for_rows(
    transport: Any, team: str, agent: str, rows: list[dict[str, Any]], *,
    now: str, skip_slugs: "Optional[set[str]]" = None,
    deadline_seconds: Optional[float] = None,
) -> tuple[set[str], set[str]]:
    """Roles ``agent`` holds a FRESH lease on, among the role-shaped assignees the
    given rows actually reference. Returns ``(held, unresolved)``.

    The candidate set is the first bound: only DISTINCT foreign assignees on OPEN
    rows are probed, and the roles/ LISTING (one op, cached for the pass) settles
    which of them are roles at all — so the literal-agent-id majority costs ZERO
    reads, and only genuine roles pay. A team with no role-addressed open work pays
    nothing. Self / ``*`` / ``@backlog`` / path-shaped assignees are skipped without
    a read. ``skip_slugs`` lets `listen` narrow further to UNSEEN directives (an
    already-fired id needs no route).

    **The honest op bound** (corrected 2026-07-16 — the docstring here previously
    claimed a tidy ``1 + 3R``, which was simply false, and the claim propagated to
    the PR that shipped it). A pass costs::

        1 + SUM over probed roles r of (2 + L_r)

    ops: one roles/ listing, then per probed role a doc read + a lease listing +
    ``L_r`` shard reads. ``L_r`` is the number of ``.md`` shards in the role's
    leases/ prefix — one per agent that has ever claimed the role and not
    ``roles release``-d it. Nothing prunes an abandoned shard, so ``L_r`` tracks
    lifetime holder CHURN, not current holders, and is unbounded in principle: a
    role with ten lease shards costs 13 ops, not 4. ``3R`` is only the ``L_r == 1``
    special case. "Probed roles" = the candidates the roles/ listing confirms are
    roles; if that listing RAISES, membership is unknown and EVERY candidate is
    probed at 1 op (its doc read) plus the lease terms for those whose docs parse.
    A transport op is a `fulcra-api` subprocess + HTTPS round trip (~0.8s measured)
    and this runs on `briefing` — the hot path — so the terms matter. The per-role
    ops buy a FAIL-CLOSED answer: reading the agent's own lease shard directly
    would be 1 op, but ``read()`` can't tell absent from failed, which is exactly
    why ``_held_roles`` (the older sweep) reports a transport outage as "no roles".

    **The wall-clock bound** is what actually holds under a degraded transport,
    because no op count bounds LATENCY when each op can burn a full transport
    timeout. One cumulative ``COORD_ROLE_FOLD_BUDGET`` deadline opens here — before
    the roles/ listing, which is itself a blocking op (the recurring pre-budget
    class) — and is spent across the listing, every role, and every lease shard
    within a role. Total latency is the budget plus ONE transport timeout of
    overshoot, no matter how many roles or shards exist.

    On a budget cut every candidate not FINISHED — unscanned, or scanned partway —
    lands in ``unresolved``, never in "not held". Running out of time is UNKNOWN,
    the same as a failed read: serving a role-blind queue because the clock ran out
    is the exact failure this fold exists to close.

    The prefilter is PER PASS, never persistent: leases change, and a name later
    registered as a role must route on the very next fold (the staleness hole that
    got a persistent negative cache rejected for `listen` — see there).

    ``unresolved`` is FAIL-CLOSED and load-bearing: a role whose lease state is
    UNKNOWN (see ``_role_fresh_holders``) is neither held nor not-held. Callers
    MUST surface it (``_role_degraded_row``) rather than let it fold into "no
    roles" — that would be the original silent bug one layer down.
    """
    if deadline_seconds is None:
        deadline_seconds = _role_fold_budget()
    candidates: set[str] = set()
    for r in rows:
        if r.get("status") not in directives.OPEN_STATUSES:
            continue
        a = str(r.get("assignee") or "")
        if not a or a in (agent, "*", directives.BACKLOG) or "/" in a:
            continue
        if skip_slugs is not None:
            slug = str(r.get("name") or "")
            if not slug or slug in skip_slugs:
                continue
        candidates.add(a)
    held: set[str] = set()
    unresolved: set[str] = set()
    listing_cache: dict[str, Any] = {}  # one roles/ listing per pass
    # The pass's ONE deadline opens HERE — ahead of the roles/ listing, not after
    # it. That listing is a blocking op like any other, and a deadline opened past
    # it leaves a transport timeout sitting AHEAD of the budget (the pre-budget
    # class the review fold was bitten by). Everything below spends this same
    # deadline cumulatively: the listing, each role's doc + lease listing, and each
    # lease shard read within a role.
    dl = Deadline.open(deadline_seconds)
    if candidates:
        # Prime the cache `_role_fresh_holders` already consults, and use it to
        # drop candidates that are affirmatively NOT roles before paying a read
        # for them. A listing that RAISES (names is None) means membership is
        # unknown: probe every candidate exactly as before — a role with a
        # readable doc still resolves off its leases, and skipping here would
        # manufacture a degraded marker for work we can in fact route.
        listing_cache["names"] = _roles_listing_names(transport, team)
        names = listing_cache["names"]
        if names is not None:
            candidates = {c for c in candidates if f"{c}.md" in names}
    ordered = sorted(candidates)
    for i, role in enumerate(ordered):
        if dl.expired():
            # Budget cut. Every candidate we have not FINISHED is UNKNOWN — mark
            # the whole tail unresolved and stop. The alternative (return what we
            # got) renders a role-blind queue that is indistinguishable from "you
            # hold no roles", which is the silent failure this fold exists to
            # close, now triggered by a slow transport instead of a missing fold.
            # A candidate scanned PARTWAY degrades inside `_role_fresh_holders`
            # (it shares this deadline) and comes back ok=False, so it lands in
            # `unresolved` through the branch below — no candidate can be dropped
            # by the clock without being reported.
            unresolved.update(ordered[i:])
            break
        holders, ok = _role_fresh_holders(transport, team, role, now=now,
                                          listing_cache=listing_cache, deadline=dl)
        if not ok:
            unresolved.add(role)
            continue
        if agent in holders:
            held.add(role)
    return held, unresolved


#: The title a review-request directive carries (``_deliver_review_directive``):
#: ``REVIEW REQUEST: <slug>``, assignee = the reviewer. reconcile indexes that
#: directive as an ordinary aggregate row, so the caller's OWN pending reviews are
#: derivable from the rows already in memory — ZERO transport — which is what makes
#: the head-of-line priority free. One constant, used by the writer AND the reader,
#: so the two can never drift on the exact prefix.
_REVIEW_REQUEST_TITLE_PREFIX = "REVIEW REQUEST: "


def _caller_review_head_slugs(
    rows: "Optional[list[dict[str, Any]]]", agent: str
) -> set[str]:
    """Review slugs the CALLING agent is assigned to review — the head-of-line
    priority — derived for FREE from the review-request directive rows already in
    the aggregate (title ``REVIEW REQUEST: <slug>``, assignee = the reviewer).
    Only OPEN directives count: a done/abandoned one means the caller already
    filed. Pure over ``rows``; no transport."""
    from . import model
    slugs: set[str] = set()
    for r in rows or []:
        if r.get("assignee") != agent:
            continue
        if r.get("status") not in model.OPEN_STATUSES:
            continue
        title = str(r.get("title") or "")
        if title.startswith(_REVIEW_REQUEST_TITLE_PREFIX):
            s = title[len(_REVIEW_REQUEST_TITLE_PREFIX):].strip()
            if s:
                slugs.add(s)
    return slugs


def _pending_reviews_for(
    transport: Any, team: str, agent: str, *,
    rows: "Optional[list[dict[str, Any]]]" = None,
    deadline_seconds: Optional[float] = None, deadline: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Reviews whose pending_required names the agent — directly or via a role
    it holds a fresh lease on. Best-effort: the top listing failing yields []
    (needs-me/briefing must not fail because the review add-on is absent).

    HEAD-OF-LINE (2026-07-20 starvation fix). The review slugs the CALLING agent is
    assigned to review — its OWN obligations, derived for free from the
    review-request directive ``rows`` — are the HEAD: scanned FIRST, under a
    DEDICATED budget (``deadline_seconds``, un-clamped) that the earlier briefing
    legs cannot have already spent. This is the fix for the live ``scanned 0/207``:
    the review leg used to inherit only the shared briefing budget's (already
    drained) remainder, so on a busy board it started already expired and never
    scanned even the caller's own three-day-old review. The head is small (an
    agent's own review queue), so a fresh budget bounds total wake by
    head_count x transport-timeout while GUARANTEEING it completes. **A budget cut
    may then only ever truncate the TAIL** (the other reviews), which is expected;
    a head that STILL cannot complete is UNKNOWN and gets its OWN loud marker
    (``review-head-degraded``), DISTINCT from the expected tail truncation
    (``review-fold-degraded``). Called without ``rows`` (the historical signature),
    there is no head and the fold behaves exactly as before.

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
    # TAIL budget: the shared aggregate deadline a bundled caller passes (spend
    # whichever of it / the standalone budget expires first), or the fold's own when
    # standalone. Re-opened from the smaller REMAINING budget rather than the
    # absolute instant so ``Deadline.reserve`` can carve the classify sub-budget.
    # NOTE: the tail deliberately inherits the drained shared budget — truncating
    # the tail is expected; it is the HEAD (below) that must NOT be starved by it.
    tail_dl: Optional[Deadline] = None
    if deadline is not None:
        remaining = max(0.0, deadline - time.monotonic())
        tail_dl = Deadline.open(min(deadline_seconds, remaining))
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
    if tail_dl is None:
        tail_dl = Deadline.open(deadline_seconds)

    def _scan_one(e: dict[str, Any], phase_dl: Deadline) -> str:
        """Scan ONE slug under ``phase_dl``; mutate ``out`` + the shared role state.
        PHASE-LOCAL accounting: this helper NO LONGER touches any scanned/skipped
        counter — it only reports its OUTCOME, and each phase (head/tail) tallies its
        OWN counts from that. A ``"budget"``/``"unknown"`` outcome is exactly the
        "count this slug skipped" signal; ``"ok"`` is "scanned clean". This is what
        keeps the head marker's numbers from bleeding into the tail marker's (and
        vice-versa). Return values:

        - ``"budget"`` — the slug's own blocking op breached ``phase_dl`` (skip this
          slug and STOP the phase).
        - ``"unknown"`` — the slug is UNKNOWN for a non-budget reason: an unreadable
          doc (``read`` returned None) or a per-slug ``TransportError``. Skip it and
          surface it, but scanning CONTINUES. A HEAD caller distinguishes this from a
          clean scan (an UNKNOWN head slug owes ``review-head-degraded``); the tail
          treats it like ``"ok"`` for control flow (it continues) but still counts it
          skipped, so the terminal tail marker reports it.
        - ``"ok"`` — settled-skip or a clean tally; continue.

        Same UNKNOWN discipline the loop always had: an unreadable doc/tally is
        skipped-and-visible, never silently pending."""
        slug = (e.get("name") or "")[:-3]
        try:
            ventries = transport.list_dir(_verdicts_prefix(team, slug))
            if any((x.get("name") or "") == SETTLED_MARKER for x in ventries):
                return "ok"  # settled -> skip entirely, zero reads beyond this listing
            doc_raw = transport.read(_review_doc_path(team, slug))
            if doc_raw is None:
                # Slug came from the listing, so its doc exists — a None read is a
                # transport failure (read() never raises). UNKNOWN: keep going.
                return "unknown"
            if phase_dl.expired():
                # The doc read itself pushed us over budget (P1-B): after-op check.
                # This slug is UNKNOWN; stop the phase.
                return "budget"
            tally, vreads_ok, fully = _tally_from_verdict_entries(
                transport, team, slug, ventries, doc_raw, deadline=phase_dl.instant)
            if not fully:
                # Budget expired MID-SLUG (F2): the partial tally is untrusted; this
                # reached slug is skipped and the phase stops.
                return "budget"
        except TransportError:
            # A single slug's tally timed out: UNKNOWN. Skip it, keep scanning the
            # rest — but a HEAD slug that ends here still owes its loud marker.
            return "unknown"
        state = tally.get("state")
        pending = tally.get("pending_required") or []
        if state == review.APPROVED and not pending:
            # Cache only a PROVEN settle (non-empty required + every verdict read).
            if _is_settleable(tally) and vreads_ok:
                _write_settled_marker(transport, team, slug, now=now)
            return "ok"
        if state != "PENDING" or not pending:
            return "ok"
        if agent not in pending:  # direct hit needs no role folding at all
            for r in pending:
                if r not in role_holders:
                    holders, ok = _role_fresh_holders(
                        transport, team, r, now=now,
                        listing_cache=roles_listing_cache)
                    role_holders[r] = holders
                    if not ok:
                        # Fail-closed: the role's lease read is UNKNOWN — surface it.
                        degraded_roles.add(r)
        if review.is_pending_for(pending, agent, role_holders):
            out.append({"type": "review-pending", "name": slug,
                        "state": "PENDING", "pending_required": pending})
        return "ok"

    # --- HEAD: the caller's OWN reviews, on a dedicated (un-starvable) budget ----
    head_slugs = _caller_review_head_slugs(rows, agent)
    head_entries = [e for e in slug_entries
                    if (e.get("name") or "")[:-3] in head_slugs]
    # Fail closed on negative-membership inference: a caller directive whose slug has
    # NO .md in the listing must NOT silently vanish (a listing is not proof the
    # obligation is gone, and the caller's OWN obligation least of all). Every such
    # slug is UNKNOWN and named in the marker so the caller can act.
    listed_head = {(e.get("name") or "")[:-3] for e in head_entries}
    missing_head = sorted(head_slugs - listed_head)
    # PHASE-LOCAL head counters — the head marker summarises HEAD work only and never
    # borrows the tail's numbers. ``head_scanned`` also survives the block so the tail
    # guard below can reproduce the old cumulative measurable-progress semantics.
    head_scanned = 0
    if head_entries or missing_head:
        head_dl = Deadline.open(deadline_seconds)  # fresh — NOT the drained remainder
        # head_total counts EVERY caller head obligation the marker summarises — the
        # listed slugs AND the missing ones. A missing slug is UNKNOWN, not absent
        # from the scan: excluding it renders 0/0 (implies nothing to scan) or 1/1
        # (implies fully scanned) while a slug is still unresolved. Count it so the
        # scanned/total makes the UNKNOWN VISIBLE.
        head_total = len(head_entries) + len(missing_head)
        head_skipped = 0
        head_cut = False
        head_unknown = bool(missing_head)  # a missing slug is already UNKNOWN
        for i, e in enumerate(head_entries):
            if i and head_dl.expired():  # between-slug (measurable progress)
                head_cut = True
                break
            outcome = _scan_one(e, head_dl)
            head_scanned += 1
            if outcome == "budget":
                # The slug that HIT the budget is a budget cut, NOT a transport
                # skip — it must not be counted in head_skipped, or the line
                # would blame the budget stop on a transport error that did not
                # happen. The budget_cut flag alone carries this cause.
                head_cut = True
                break
            if outcome == "unknown":
                # An unreadable doc / per-slug TransportError: this head slug is
                # UNKNOWN. It IS a transport skip. Keep scanning the rest of the
                # head (a sibling may still be a live pending we can surface), but
                # the head owes its loud marker.
                head_skipped += 1
                head_unknown = True
        if head_cut or head_unknown:
            # The caller's OWN head could not fully resolve: UNKNOWN, and DISTINCT
            # from an expected tail truncation — this is the incident, loud on its
            # own type. Any unknown outcome (budget cut, unreadable doc, transport
            # error, or a slug missing from the listing) qualifies.
            head_row = budget_mod.degraded_row(
                "review-head-degraded", head_scanned, head_total, head_skipped)
            if head_cut:
                # A genuine budget cut — the ONE cause the head-degraded LINE may
                # attribute to the budget. Unreadable/missing/transport causes must
                # NOT be blamed on the budget, so the base line stays cause-neutral
                # and only this flag adds the budget clause.
                head_row["budget_cut"] = True
            if missing_head:
                head_row["missing"] = missing_head
            out.append(head_row)

    # --- classify dir-only review slugs (visibility) under a TAIL sub-budget ----
    # Dir-only slugs (a `<slug>/` dir with no `<slug>.md`) are invisible to the
    # doc-keyed scan. Classify each via the tombstone three-way: real verdict shards
    # -> ORPHAN (surface every pass); an empty dir / stale `.settled` -> TOMBSTONE
    # (silently skipped); a listing that RAISES -> UNKNOWN (fail closed, per-dir
    # `review-orphan-degraded`). Runs under HALF the tail budget (reserved) so the
    # load-bearing tail doc scan keeps the other half — a visibility-only pass must
    # never starve the critical one. It runs AFTER the head so the caller's own
    # reviews are never behind orphan classification.
    classify_dl = tail_dl.reserve(0.5)
    settled_index = rec._load_settled_index(transport, team)
    doc_slugs = {(e.get("name") or "")[:-3] for e in slug_entries}
    dir_slugs = []
    for e in entries:
        if not e.get("is_dir"):
            continue
        oslug = (e.get("name") or "").rstrip("/")
        if oslug and oslug not in doc_slugs and oslug not in settled_index:
            dir_slugs.append(oslug)
    for i, oslug in enumerate(dir_slugs):
        if classify_dl.expired():
            out.append({"type": "review-orphan-degraded",
                        "unclassified": len(dir_slugs) - i})
            break
        kind = _classify_orphan_dir(transport, team, oslug)
        if kind == "orphan":
            out.append({"type": "review-orphan", "name": oslug})
        elif kind == "unknown":
            out.append({"type": "review-orphan-degraded", "name": oslug})
        # tombstone -> silently skipped

    # --- TAIL: the remaining reviews, under the (possibly drained) shared budget --
    # PHASE-LOCAL tail counters. ``review-fold-degraded`` describes TAIL truncation
    # ONLY — it must never borrow the head's scanned/skipped (a head-only incident is
    # the head marker's business alone). ``tail_total`` is the tail count, not the
    # whole listing; in the legacy no-head path tail_entries == slug_entries so this
    # is byte-identical to the old ``total``.
    tail_entries = [e for e in slug_entries
                    if (e.get("name") or "")[:-3] not in head_slugs]
    tail_total = len(tail_entries)
    tail_scanned = 0
    tail_skipped = 0
    for e in tail_entries:
        # Between-slug check. Measurable-progress uses ``head_scanned or tail_scanned``
        # — the same truthiness the old cumulative ``scanned`` gave: with a head
        # already scanned it fires BEFORE the first tail slug (a spent shared budget
        # truncates the tail to zero — expected); with no head it lets the first tail
        # slug run (the standalone fold's contract, unchanged).
        if (head_scanned or tail_scanned) and tail_dl.expired():
            out.append(budget_mod.degraded_row(
                "review-fold-degraded", tail_scanned, tail_total, tail_skipped))
            return out
        outcome = _scan_one(e, tail_dl)
        tail_scanned += 1
        if outcome != "ok":
            tail_skipped += 1
        if outcome == "budget":
            out.append(budget_mod.degraded_row(
                "review-fold-degraded", tail_scanned, tail_total, tail_skipped))
            return out

    if degraded_roles:
        # A role's lease read degraded: the agent might be a holder we couldn't
        # resolve, so a role-routed obligation may be missing. Make it VISIBLE.
        out.append({"type": "review-role-degraded",
                    "roles": sorted(degraded_roles)})
    if tail_skipped:
        # The TAIL completed inside budget but some tail slugs were unreadable:
        # partial knowledge must be visible, so emit the tail marker anyway. Gated on
        # ``tail_skipped`` (not a shared counter) so a head-only incident — already
        # loud on ``review-head-degraded`` — never ALSO emits a phantom tail marker
        # with no tail behind it.
        out.append(budget_mod.degraded_row(
            "review-fold-degraded", tail_scanned, tail_total, tail_skipped))
    return out


def _review_degraded_line(r: dict[str, Any]) -> str:
    return budget_mod.fold_degraded_line(
        r, label="review", remedy="run per-slug review status for the rest",
        noun="slug")


def _review_head_degraded_line(r: dict[str, Any]) -> str:
    """The caller's OWN review queue could not complete — incident-grade UNKNOWN,
    deliberately DISTINCT from ``_review_degraded_line`` (an expected TAIL
    truncation). Never silent, never counted as a pending item.

    CAUSE-NEUTRAL base. A head incident may be a budget cut OR an unreadable doc OR a
    per-slug transport error OR a slug missing from the listing — and several at once.
    The base line therefore does NOT attribute a cause ("before budget" was wrong for
    every non-budget case); it states the UNKNOWN and appends the specific causes the
    marker actually carries (a budget cut, transport-skipped slugs, missing slugs)."""
    line = (f"  review HEAD degraded: caller's own reviews incomplete — scanned "
            f"{r.get('scanned')}/{r.get('total')} — UNKNOWN, retry")
    causes: list[str] = []
    if r.get("budget_cut"):
        causes.append("budget cut")
    if r.get("skipped"):
        causes.append(f"{r['skipped']} slug(s) skipped on transport error")
    if r.get("missing"):
        # Slugs a caller directive named but that had no doc in the listing: fail
        # closed and name them so the reader knows WHICH obligation went UNKNOWN.
        causes.append(f"missing from listing: {', '.join(r['missing'])}")
    if causes:
        line += " (" + "; ".join(causes) + ")"
    return line


def _review_row_line(r: dict[str, Any]) -> Optional[str]:
    """The ONE text-dispatch for every review row type ``briefing`` / ``needs-me``
    can receive. Both verbs render review rows through this, so an identical row
    type can never diverge between them, and — critically — a review row can never
    fall through to the generic task line (``_line``), whose ``priority`` /
    ``status`` / ``title`` lookups print ``[ ?] ? None`` on these shapes. Returns
    ``None`` for a non-review row so the caller falls back to its own default."""
    t = r.get("type")
    if t == "review-pending":
        return (f"  [REVIEW] pending verdict: {r['name']} "
                f"(required: {', '.join(r['pending_required'])})")
    if t == "review-fold-degraded":
        return _review_degraded_line(r)
    if t == "review-head-degraded":
        return _review_head_degraded_line(r)
    if t == "review-orphan":
        return (f"  [REVIEW] orphan review dir (verdicts, no doc): "
                f"{r['name']} — needs maintainer repair")
    if t == "review-orphan-degraded":
        if r.get("unclassified"):
            return (f"  [REVIEW] dir classification degraded: "
                    f"{r['unclassified']} dir(s) unclassified before budget — retry")
        return (f"  [REVIEW] orphan dir classification degraded: "
                f"{r['name']} — verdicts listing unreadable, retry")
    if t == "review-role-degraded":
        return (f"  review role resolution degraded: "
                f"{', '.join(r.get('roles') or [])} — holders unknown, retry")
    return None


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
    dl = Deadline(deadline)
    watch_prefix = f"team/{team}/_coord/forge/watch/"
    try:
        watch_entries = transport.list_dir(watch_prefix)
    except TransportError:
        watch_entries = []
        ok = False
    for e in watch_entries:
        if dl.expired():
            ok = False
            break
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md"):
            continue
        raw = transport.read(watch_prefix + n)
        if dl.expired():  # the read pushed us over budget — detect it immediately
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
        if dl.expired():
            ok = False
            break
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md") or n == "index.md":
            continue
        raw = transport.read(review_prefix + n)
        if dl.expired():
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
    dl = Deadline(deadline)
    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md"):
            continue
        if dl.expired():
            return None, False
        stem = n[:-3]
        acked = transport.read(_ack_path(team, stem, agent))
        if dl.expired():
            return None, False
        if acked is not None:
            continue  # acked by this agent — hidden
        raw = transport.read(prefix + n)
        if dl.expired():
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
    dl = Deadline(deadline)
    resp, resp_ok = _forge_responsible(transport, team, deadline=deadline)
    mine = sorted(slug for slug, agents in resp.items() if agent in agents)
    total = len(mine)
    scanned = 0
    skipped = 0
    degraded = not resp_ok  # a truncated/failed responsibility scan is already degraded
    for slug in mine:
        if dl.expired():
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
        if dl.expired():
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
        out.append(budget_mod.degraded_row("forge-degraded", scanned, total, skipped))
    return out


def _forge_feedback_line(r: dict[str, Any]) -> str:
    who = ", ".join(r.get("authors") or []) or "?"
    return (f"  [FORGE] feedback on {r.get('pr_slug')}: "
            f"{r.get('count')} item(s) from {who}")


def _forge_degraded_line(r: dict[str, Any]) -> str:
    return budget_mod.fold_degraded_line(
        r, label="forge", remedy="run forge feedback for the rest", noun="PR")


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
        # The doc read returned None: no doc. If the verdicts dir is also empty
        # (or holds only a stale `.settled` marker), this is a TOMBSTONE — an
        # archived/deleted review whose dir prefix soft-deletes lingered. Keep rc 1
        # (still non-clean for a caller sweep), but say tombstone: a retry never
        # resurrects a gone doc, so the generic "unknown, retry" would be dishonest.
        # A dir with real verdict shards (orphan) or a verdicts listing that RAISED
        # (unknown) is NOT a tombstone — fall through to the generic fail-closed
        # message, where a retry may genuinely help.
        if _classify_orphan_dir(transport, team, slug) == "tombstone":
            print(f"review status: {slug} in team/{team} is a tombstone "
                  f"(archived/deleted review) — no doc, no verdicts",
                  file=sys.stderr)
            return 1
        # Missing slug OR transport failure — indistinguishable, and either way the
        # tally is UNKNOWN. Without the required list, one readable approval verdict
        # tallies as a clean APPROVED with pending:[] — printing that (or caching
        # it) under a transient timeout would durably hide a pending review. Fail loud.
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
        jsonutil.print_json(result)
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
        jsonutil.print_json(snap)
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
    title = f"{_REVIEW_REQUEST_TITLE_PREFIX}{slug}"
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


def _directed_inbox(transport: Any, team: str, agent: str,
                    rows: list[dict[str, Any]], *,
                    held_roles: "Optional[set[str]]" = None,
                    include_backlog: bool = False,
                    include_history: bool = False) -> list[dict[str, Any]]:
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
                                     include_history=include_history,
                                     held_roles=held_roles)
    if include_history:
        return stale_visible
    for r in stale_visible:
        slug = str(r.get("name") or "")
        if agent not in (acks.get(slug) or []) and transport.read(_ack_path(team, slug, agent)):
            acks.setdefault(slug, []).append(agent)
    got = directives.inbox(rows, acks, agent, now=now,
                           include_backlog=include_backlog,
                           include_history=include_history,
                           held_roles=held_roles)
    # read-your-write: an ack written since the last reconcile hides the item
    # for the acking agent immediately (live shard check, only for shown items).
    return [r for r in got
            if transport.read(_ack_path(team, str(r.get("name")), agent)) is None]


def _needs_me_rows(transport: Any, team: str, agent: str,
                   rows: list[dict[str, Any]], *, now: str,
                   held_roles: "Optional[set[str]]" = None,
                   include_history: bool = False) -> list[dict[str, Any]]:
    """Needs-me with directive satisfaction and read-your-write semantics.

    Reconciled ``acked_by`` hides old acknowledgements without transport work.
    Only the remaining directive candidates pay one shard read so a fresh ack
    disappears immediately instead of waiting for the next reconcile.
    """
    got = query.needs_me(rows, agent, now=now, held_roles=held_roles,
                         include_history=include_history)
    if include_history:
        return got
    out = []
    for row in got:
        tags = set(str(t) for t in (row.get("tags") or []))
        if ("kind:directive" in tags
                and transport.read(_ack_path(team, str(row.get("name")), agent)) is not None):
            continue
        out.append(row)
    return out


def _inbox_rows_status(transport: Any, team: str, agent: str, *,
                       include_backlog: bool = False,
                       include_history: bool = False,
                       ) -> tuple[list[dict[str, Any]], bool, str, set[str]]:
    """The open-directive fold `inbox` surfaces for `agent` — role-routed
    directives included — plus the readability of the underlying summaries fold:
    ``ok`` False (with a ``reason``) when the index/listing is UNKNOWN — see the
    public-read failure contract at ``_read_degraded_row``. Extracted so `listen`
    awaits the SAME source `inbox` shows — one inbox computation, no second
    implementation to drift. Never raises: an unreadable summaries read folds to
    an empty list, but with ``ok=False`` and a ``reason`` so EVERY caller (inbox,
    listen, briefing) surfaces the degradation as the loud marker rather than
    mistaking UNKNOWN for an empty inbox — the codex-reproduced silent clean-``[]``
    that suppressed a live unacked directive.

    The fourth element is the UNRESOLVED role set (``_held_roles_for_rows``): roles
    whose holders could not be determined. The caller MUST surface it — see
    ``_role_degraded_row``."""
    rows, ok, reason = _load_rows_status(transport, team)
    held, unresolved = _held_roles_for_rows(transport, team, agent, rows,
                                            now=_iso(_now()))
    return (_directed_inbox(transport, team, agent, rows,
                            held_roles=held or None,
                            include_backlog=include_backlog,
                            include_history=include_history),
            ok, reason, unresolved)


def cmd_inbox(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    if args.ack:
        fm = {"type": "Ack", "agent": agent, "timestamp": _iso(_now())}
        transport.write(_ack_path(args.team, args.ack, agent),
                        okf.render_frontmatter(fm) + "\nacked\n")
        print(f"acked {args.ack}")
        return 0
    # Public-read failure contract (see _read_degraded_row): consume the readable
    # bit. Under a degraded transport the summaries index is UNKNOWN, not empty —
    # emit the `inbox-degraded` marker (json row / stderr notice) and RETAIN any
    # partial rows, NEVER a clean-``[]`` exit 0 that would suppress a live unacked
    # directive (the codex CRIT, live-reproduced).
    got, ok, reason, unresolved_roles = _inbox_rows_status(
        transport, args.team, agent, include_backlog=args.all,
        include_history=args.all)
    if args.json:
        rows_out = ([_read_degraded_row(reason, marker="inbox-degraded")] + got
                    if not ok else got)
        if unresolved_roles:
            rows_out = [_role_degraded_row(unresolved_roles)] + rows_out
        jsonutil.print_json(rows_out)
        return 0
    if not ok:
        _surface_read_degraded(reason, json_mode=False, marker="inbox-degraded")
    print(f"inbox — {agent}: {len(got)} item(s)")
    if unresolved_roles:  # always shown — an unknown role inbox must never hide
        print(_role_degraded_line(_role_degraded_row(unresolved_roles)))
    for r in got:
        print(_line(r))
    return 0


def cmd_respond(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    now = _iso(_now())
    path = _task_path(args.team, args.name)
    doc = transport.read(path)
    if doc is None:
        # Fail-loud (same doctrine as `review status` rc-1): the name resolves to
        # NO directive doc — either a display TITLE was used in place of the
        # hash-suffixed slug, or the read failed. Recording a response here would
        # GHOST-CLOSE: the shard lands under a slug nobody owns while the real
        # directive stays open in the owner's needs-me forever (cost three
        # ghost-closes in one day). Write nothing; make the caller retry with the
        # exact slug.
        print(f"respond: no directive '{args.name}' in team/{args.team} "
              f"(absent or unreadable) — nothing recorded. Use the exact slug from "
              f"`inbox`/`briefing --json` (the hash-suffixed name, not the display "
              f"title).", file=sys.stderr)
        return 1
    stamp = _stamp_for_path(now, agent)
    fm = {"type": "Response", "agent": agent, "outcome": args.outcome, "timestamp": now}
    transport.write(_response_path(args.team, args.name, stamp),
                    okf.render_frontmatter(fm) + f"\n{args.evidence or args.outcome}\n")
    try:
        out = tasks.apply_update(doc, now=now, status="done",
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
# Six failure SOURCES are tracked independently — inbox (summaries index / the
# protected caller head),
# responses (the responses subtree transport), orphans (a response whose owning
# directive doc won't resolve), verdicts (the review root / a review doc /
# a verdict shard unreadable), roles (a role-lease listing unreadable while
# resolving role-routed directives), and tail (the shared non-head budget).
# Each is its own degraded streak.
#
# Disciplines (each a real incident this week; state is ADD-ONLY so they hold):
#   * No false advance — a failed/None read during a tick must NOT mark unknown
#     ids as seen. State is a UNION of affirmatively-processed ids, so a degraded
#     read contributes nothing and recovery re-surfaces the still-pending id.
#   * Fail visible, no flooding — a transport failure emits `LISTEN DEGRADED:`
#     ONCE per consecutive-failure streak, PER SOURCE (the streak flags persist IN
#     the state file, so a scheduler re-running `--once` does not re-alarm every
#     tick). Per-source is load-bearing: a single shared flag would let a chronic
#     degradation on one source pin it TRUE forever and silence a NEW, distinct
#     outage on another. Each source alerts once per ITS OWN streak and resets on
#     ITS OWN recovery. It goes to STDERR so `--json` stdout stays a clean
#     one-object-per-line event stream for filter-free streaming consumers.
#     A permanently-absent owner/requester doc is handled a level BELOW the streak:
#     it is emit-once-cached PER SLUG (`flagged_orphan_responses`/`_verdicts`, like
#     the dir-only `orphan_slugs`) and skipped silently thereafter, so it never even
#     reaches its source's streak — a fail-closed watcher (persistent DEGRADED ==
#     fatal) is not murdered by a doc that will never return, while a genuine
#     transport outage on that same source still fails loud.
#   * Quiet ticks print NOTHING to stdout (the monitor-flood lesson) — only
#     `--verbose` emits a heartbeat, and only to stderr.
#   * Bounded cost — one list_dir of _coord/responses/ + per-slug work ONLY for
#     slugs the agent owns; a slug's ownership is read once (from its task doc)
#     and cached in state, so not-owned / broadcast slugs cost nothing after the
#     first classification and the scan is never proportional to total history.


# The independent degraded streaks. Each source alarms once per its own streak.
# `roles` (role-lease resolution for role-routed directives) and `tail` (shared
# non-head budget exhaustion) are their own sources:
# folding it into `inbox` would let a chronic role degradation pin that streak
# and mask a fresh summaries outage — the independent-streak invariant. Legacy
# state files lack the key; _coerce_degraded defaults it False (free migration).
_LISTEN_SOURCES = ("inbox", "responses", "orphans", "verdicts", "roles", "tail")


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


def _listen_store_state_path(team: str, agent: str) -> str:
    """Durable listener state in the agent's existing store namespace."""
    return f"team/{team}/_coord/agents/{agent}/listen-state.json"


def _coerce_listen_state_doc(data: Any) -> Optional[dict[str, Any]]:
    """Normalize one decoded state document, or None when its shape is corrupt."""
    if not isinstance(data, dict):
        return None
    try:
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
            # Emit-once caches for a PERMANENTLY-absent owner/requester doc at the
            # responses / verdicts sources — a slug whose directive|review doc reads
            # None has its degrade emitted ONCE, then is skipped silently (a
            # fail-closed watcher treats persistent DEGRADED as fatal). Distinct
            # from orphan_slugs, which caches emitted-orphan EVENTS; these cache
            # emitted-DEGRADE slugs. Legacy files default empty.
            "flagged_orphan_responses": list(
                data.get("flagged_orphan_responses") or []),
            "flagged_orphan_verdicts": list(
                data.get("flagged_orphan_verdicts") or []),
            # E2 authoritative data-updates cursor. Missing/corrupt -> one legacy
            # full-listing pass, which seeds it only after that tick is conclusive.
            "feed_cursor": data.get("feed_cursor"),
            "degraded": _coerce_degraded(data.get("degraded")),
        }
    except (TypeError, ValueError):
        return None


def _write_local_listen_cache(path: pathlib.Path, payload: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    except OSError as e:
        _log.warning("listen state write failed", path=str(path), error=str(e))


def _load_listen_state(
    path: pathlib.Path, *, transport: Any = None,
    team: Optional[str] = None, agent: Optional[str] = None,
) -> dict[str, Any]:
    """Load durable store state, using the local file as a restart cache.

    A valid store copy is authoritative and refreshes the local cache. Missing,
    corrupt, or unreadable store state falls back to the legacy local file, then
    to a fresh state. Never raises — bookkeeping cannot kill the listener.
    """
    if transport is not None and team is not None and agent is not None:
        try:
            raw = transport.read(_listen_store_state_path(team, agent))
            decoded = json.loads(raw) if raw else None
        except Exception:
            decoded = None
        state = _coerce_listen_state_doc(decoded)
        if state is not None:
            _write_local_listen_cache(path, json.dumps(state, sort_keys=True))
            return state
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        decoded = None
    return (_coerce_listen_state_doc(decoded)
            or _coerce_listen_state_doc({})
            or {})  # the empty schema is statically valid


def _save_listen_state(
    path: pathlib.Path, state: dict[str, Any], *, transport: Any = None,
    team: Optional[str] = None, agent: Optional[str] = None,
) -> None:
    """Write through to local cache and durable store, both best-effort.

    A lost write may cause one re-notify after restart, never a missed event.
    """
    payload = json.dumps(state, sort_keys=True)
    _write_local_listen_cache(path, payload)
    if transport is None or team is None or agent is None:
        return
    store_path = _listen_store_state_path(team, agent)
    try:
        if not transport.write(store_path, payload):
            _log.warning("listen store state write failed", path=store_path)
    except Exception as e:
        _log.warning("listen store state write failed", path=store_path,
                     error=str(e))


def _listen_inbox_phase(
    transport: Any, team: str, agent: str, rows: list[dict[str, Any]],
    *, seen: set[str], deadline: Deadline,
    held_roles: "Optional[set[str]]" = None,
) -> tuple[list[dict[str, Any]], bool, int, int]:
    """Fold one inbox phase under its supplied deadline.

    The protected head calls this with no roles (literal-agent + wildcard); the
    shared tail calls it with only held-role rows. The candidate set is pure over
    the already-loaded rows. Only fresh ack-shard checks cost transport. Returns
    ``(rows, complete, scanned, total)``; an incomplete head is UNKNOWN and the
    caller emits the distinct ``listen-head-degraded`` marker.  Seen ids are
    excluded before ack reads, which keeps quiet ticks cheap.
    """
    now = _iso(_now())
    acks = {str(r.get("name")): (r.get("acked_by") or []) for r in rows}
    candidates = directives.inbox(rows, acks, agent, now=now,
                                   held_roles=held_roles)
    candidates = [r for r in candidates
                  if str(r.get("name") or "") not in seen]
    out: list[dict[str, Any]] = []
    scanned = 0
    for i, row in enumerate(candidates):
        if i and deadline.expired():
            return out, False, scanned, len(candidates)
        slug = str(row.get("name") or "")
        ack = transport.read(_ack_path(team, slug, agent))
        scanned += 1
        if ack is None:
            out.append(row)
        if deadline.expired() and scanned < len(candidates):
            return out, False, scanned, len(candidates)
    return out, True, scanned, len(candidates)


def _listen_feed_history(
    transport: Any, team: str, agent: str, changes: list[dict[str, Any]], *,
    response_keys: set[str], slug_owned: dict[str, Any],
    verdict_keys: set[str], review_requested: dict[str, Any],
    settled_reviews: set[str],
) -> Optional[dict[str, Any]]:
    """Classify changed response/verdict shards without history-root listings.

    The result is copy-on-success. Any unreadable owner/requester/shard returns
    None so the caller discards the partial work and takes the unchanged
    listing-based tail fallback.
    """
    next_response_keys = set(response_keys)
    next_slug_owned = dict(slug_owned)
    next_verdict_keys = set(verdict_keys)
    next_review_requested = dict(review_requested)
    next_settled_reviews = set(settled_reviews)
    events: list[dict[str, Any]] = []
    responses_prefix = _responses_prefix(team)
    review_prefix = f"team/{team}/review/"

    # A settling change must be observed after its verdict shards in the same
    # inclusive feed window, regardless of endpoint ordering.
    ordered = sorted(
        changes,
        key=lambda c: (
            str(c.get("path") or "").endswith("/.settled"),
            str(c.get("path") or ""),
        ),
    )
    for change in ordered:
        if change.get("state") != "uploaded":
            continue
        path = str(change.get("path") or "")
        if path.startswith(responses_prefix):
            rest = path[len(responses_prefix):]
            parts = rest.split("/")
            if len(parts) != 2 or not parts[0] or not parts[1].endswith(".md"):
                continue
            slug, filename = parts
            key = f"{slug}/{filename[:-3]}"
            if key in next_response_keys:
                continue
            owned = next_slug_owned.get(slug)
            if owned is None:
                doc = transport.read(_task_path(team, slug))
                if doc is None:
                    return None
                fm = okf.parse_frontmatter(doc) or {}
                owned = str(fm.get("owner") or "").strip() == agent
                next_slug_owned[slug] = owned
            if not owned:
                continue
            shard = transport.read(path)
            if shard is None:
                return None
            fm = okf.parse_frontmatter(shard) or {}
            events.append({
                "type": "response",
                "slug": slug,
                "agent": str(fm.get("agent") or "?"),
                "outcome": str(fm.get("outcome") or "?"),
            })
            next_response_keys.add(key)
            continue

        if not path.startswith(review_prefix):
            continue
        rest = path[len(review_prefix):]
        parts = rest.split("/")
        if len(parts) != 3 or parts[1] != "verdicts":
            continue
        slug, _verdicts, filename = parts
        if not slug or slug in next_settled_reviews:
            continue
        requested = next_review_requested.get(slug)
        if requested is None:
            doc = transport.read(_review_doc_path(team, slug))
            if doc is None:
                return None
            fm = okf.parse_frontmatter(doc) or {}
            requested = str(fm.get("requested_by") or "").strip() == agent
            next_review_requested[slug] = requested
        if not requested:
            continue
        if filename == SETTLED_MARKER:
            events.append({"type": "settled", "slug": slug,
                           "state": review.APPROVED})
            next_settled_reviews.add(slug)
            continue
        if not filename.endswith(".md"):
            continue
        key = f"{slug}/{filename[:-3]}"
        if key in next_verdict_keys:
            continue
        shard = transport.read(path)
        if shard is None:
            return None
        fm = okf.parse_frontmatter(shard) or {}
        events.append({
            "type": "verdict",
            "slug": slug,
            "reviewer": filename[:-3],
            "verdict": str(fm.get("verdict") or "?"),
        })
        next_verdict_keys.add(key)

    return {
        "events": events,
        "response_keys": next_response_keys,
        "slug_owned": next_slug_owned,
        "verdict_keys": next_verdict_keys,
        "review_requested": next_review_requested,
        "settled_reviews": next_settled_reviews,
    }


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

    # The tail clock opens at tick start and is shared by every history/role leg.
    # The literal-agent/wildcard inbox head below gets a separate fresh clock, so
    # a drained shared budget can truncate only the tail — never caller-directed
    # work.  ``tail`` owns one distinct failure streak/marker.
    tail_dl = Deadline.open(_listen_tail_budget())
    tail_cut = False

    def _tail_ready(phase: str) -> bool:
        nonlocal tail_cut
        if tail_cut:
            return False
        if tail_dl.expired():
            tail_cut = True
            _fail("tail", f"listen-tail-degraded: shared budget spent before {phase}; "
                  "caller-directed head was still served")
            return False
        return True

    inbox_ids = set(state["inbox_ids"])
    response_keys = set(state["response_keys"])
    slug_owned: dict[str, Any] = dict(state["slug_owned"])
    verdict_keys = set(state.get("verdict_keys") or [])
    review_requested: dict[str, Any] = dict(state.get("review_requested") or {})
    settled_reviews = set(state.get("settled_reviews") or [])
    # Emit-once caches: slugs whose owner/requester doc read None and have already
    # had their degrade emitted. Skipped SILENTLY thereafter so a fail-closed
    # watcher (persistent DEGRADED == fatal) survives a permanently-missing doc;
    # recovery (doc reads non-None) discards the slug to re-arm fail-loud. Mirrors
    # the `orphan_slugs` emit-once cache the dir-only review scan uses below.
    flagged_orphan_responses = set(state.get("flagged_orphan_responses") or [])
    flagged_orphan_verdicts = set(state.get("flagged_orphan_verdicts") or [])

    # Source 1 — new inbox directives (the SAME fold `inbox` surfaces), PLUS
    # directives routed to a fresh-lease ROLE this agent holds. An unreadable
    # summaries index is degraded, NOT a legitimately-empty inbox.
    now_iso = _iso(_now())
    feed_attempted = bool(state.get("feed_cursor"))
    feed_changes = (_team_updates(
        transport, team, since=state.get("feed_cursor"), now=now_iso)
        if feed_attempted else None)
    head_dl = Deadline.open(_listen_head_budget())
    rows, inbox_ok, inbox_reason = _load_rows_status(
        transport, team, deadline=head_dl, feed_changes=feed_changes,
        feed_attempted=feed_attempted)
    if not inbox_ok:
        # The reason attributes WHICH leg failed (summaries index vs the freshness
        # overlay — different outages, same inbox source/streak).
        _fail("inbox", "listen-head-degraded: " +
              (inbox_reason or "summaries index unreadable"))
    direct_head, head_complete, head_scanned, head_total = _listen_inbox_phase(
        transport, team, agent, rows, seen=inbox_ids, deadline=head_dl)
    if not head_complete:
        _fail("inbox", "listen-head-degraded: caller-directed inbox budget spent "
              f"after {head_scanned}/{head_total} items")
    for r in direct_head:
        slug = str(r.get("name") or "")
        if not slug:
            continue
        owner = str(r.get("owner") or "?")
        if owner == agent and not r.get("not_before"):
            inbox_ids.add(slug)
            continue
        events.append({"type": "directive", "slug": slug,
                       "owner": owner,
                       "title": str(r.get("title") or slug)})
        inbox_ids.add(slug)
    # Role expansion — the shared resolver (`_held_roles_for_rows`, which owns the
    # candidate bound and the fail-closed contract), narrowed HERE to UNSEEN
    # directives: an already-fired id needs no route, so a steady-state tick pays
    # zero role reads. Not persistent state — leases change, so the resolution is
    # per tick. HONEST BOUND: a directive assigned to ANOTHER literal agent never
    # enters this agent's inbox_ids, so its assignee is re-probed every tick — but
    # the resolver's roles/ listing settles it for free, so the re-probe costs no
    # reads. A persistent negative "not-a-role" cache was considered and REJECTED:
    # read() can't distinguish absent from failed, and a name later registered as a
    # role would be silently unroutable forever (a staleness hole worse than the
    # read cost); the per-pass listing is that invalidation, done fresh every tick.
    # id-diff is unchanged (the directive slug is the id regardless of
    # the route), so a new role holder sees a directive iff its id is unseen in
    # THEIR OWN state file (state is per-agent) — the holder-change semantics fall
    # out.
    held_roles: set[str] = set()
    unresolved_roles: set[str] = set()
    if _tail_ready("role routing"):
        remaining = (None if tail_dl.instant is None else
                     max(0.0, tail_dl.instant - time.monotonic()))
        role_budget = _role_fold_budget()
        if remaining is not None:
            role_budget = min(role_budget, remaining)
        held_roles, unresolved_roles = _held_roles_for_rows(
            transport, team, agent, rows, now=now_iso, skip_slugs=inbox_ids,
            deadline_seconds=role_budget)
    for role in sorted(unresolved_roles):
        # Fail-closed: the lease read is UNKNOWN. Degrade VISIBLY (the agent may
        # miss role-routed work) on the DEDICATED `roles` source — never crash,
        # never treat unknown as "not a holder" silently. Its own source is
        # load-bearing: a chronic role degradation must not pin the inbox streak
        # and mask a fresh summaries outage.
        _fail("roles", f"role lease unknown for {role}")
    # Direct rows were consumed by the protected head.  Restrict this shared-tail
    # pass to actual held-role rows so it cannot repeat their ack reads.
    role_rows = [r for r in rows if str(r.get("assignee") or "") in held_roles]
    inbox: list[dict[str, Any]] = []
    if held_roles and _tail_ready("role inbox"):
        inbox, role_complete, _role_scanned, _role_total = _listen_inbox_phase(
            transport, team, agent, role_rows, seen=inbox_ids, deadline=tail_dl,
            held_roles=held_roles)
        if not role_complete:
            _tail_ready("role inbox completion")
    for r in inbox:
        slug = str(r.get("name") or "")
        if not slug or slug in inbox_ids:
            continue
        owner = str(r.get("owner") or "?")
        # A sender may be in its own audience: explicitly via a self-tell, or
        # implicitly because every broadcast is addressed to ``*``.  Unscheduled
        # rows are real inbox members (and must be consumed into id-diff state),
        # but waking the author for its own send is pure self-echo.  A scheduled
        # self-reminder is deliberately different: its future wake is the point,
        # so a visible row carrying ``not_before`` must still fire.
        # Suppress only the directive event; response/verdict sources below remain
        # the reply legs for work the agent owns or requested.
        if owner == agent and not r.get("not_before"):
            inbox_ids.add(slug)
            continue
        events.append({"type": "directive", "slug": slug,
                       "owner": owner,
                       "title": str(r.get("title") or slug)})
        inbox_ids.add(slug)

    # Feed-first history tail.  This is copy-on-success: any changed shard whose
    # ownership/requester/content cannot be read discards the partial result and
    # falls through to the unchanged listing-based W8-budgeted tail.
    history_feed = False
    if feed_changes is not None and _tail_ready("feed history"):
        targeted = _listen_feed_history(
            transport, team, agent, feed_changes,
            response_keys=response_keys, slug_owned=slug_owned,
            verdict_keys=verdict_keys, review_requested=review_requested,
            settled_reviews=settled_reviews)
        if targeted is not None:
            history_feed = True
            events.extend(targeted["events"])
            response_keys = targeted["response_keys"]
            slug_owned = targeted["slug_owned"]
            verdict_keys = targeted["verdict_keys"]
            review_requested = targeted["review_requested"]
            settled_reviews = targeted["settled_reviews"]

    # Source 2 — new responses to directives THIS agent owns. One list_dir of the
    # responses root; per-slug work only for owned slugs, ownership cached.
    prefix = _responses_prefix(team)
    entries = [] if history_feed else None
    if not history_feed and _tail_ready("responses"):
        try:
            entries = transport.list_dir(prefix)
        except TransportError as e:
            _fail("responses", f"responses listing unreadable ({e})")
    for e in entries or []:
        if not _tail_ready("response classification"):
            break
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
                # Ambiguous: a transient read failure OR a permanent orphan whose
                # directive doc is gone (a settled/archived/tombstoned directive).
                # Ownership is UNKNOWN either way, so we do NOT cache and do NOT
                # advance — unknown != seen, retry next tick. EMIT-ONCE per slug:
                # a fail-closed watcher treats persistent DEGRADED as fatal, so a
                # permanently-missing doc must not re-degrade every tick and murder
                # it. First occurrence fails loud on the `orphans` source; the slug
                # is then skipped silently until it recovers, so it never pins the
                # source either. Other sources still fail-loud on their own
                # transport failures, so a genuine outage is never masked — first
                # occurrence + recovery visibility is retained.
                if slug not in flagged_orphan_responses:
                    _fail("orphans", f"owner unresolved for {slug}")
                    flagged_orphan_responses.add(slug)
                continue
            fm = okf.parse_frontmatter(doc) or {}
            owner = str(fm.get("owner") or "").strip()
            owned = owner == agent  # owner is the directive's SENDER; broadcast/absent -> not owned
            slug_owned[slug] = owned  # definitive classification: cache it
            flagged_orphan_responses.discard(slug)  # recovered -> re-arm fail-loud
            if not _tail_ready("response ownership"):
                break
        if not owned:
            continue  # responses to other-owner / broadcast directives are noise
        try:
            stamps = transport.list_dir(prefix + slug + "/")
        except TransportError as ex:
            _fail("responses", f"response dir {slug} unreadable ({ex})")
            continue
        for se in stamps:
            if not _tail_ready("response shards"):
                break
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
    if _tail_ready("settled-review index"):
        settled_reviews.update(rec._load_settled_index(transport, team))
    review_prefix = f"team/{team}/review/"
    rentries = [] if history_feed else None
    if not history_feed and _tail_ready("verdicts"):
        try:
            rentries = transport.list_dir(review_prefix)
        except TransportError as e:
            _fail("verdicts", f"review listing unreadable ({e})")
    for e in rentries or []:
        if not _tail_ready("review classification"):
            break
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
                # Ordinarily the slug came from the listing so the doc exists and a
                # None read is a transient transport failure — but a settled/archived
                # review can leave its `<slug>/` verdicts subtree listed with the
                # `<slug>.md` doc gone, a PERMANENT None. Requester UNKNOWN either
                # way: do NOT cache and do NOT advance (no-false-advance), retry next
                # tick. EMIT-ONCE per slug: a fail-closed watcher treats persistent
                # DEGRADED as fatal, so a permanently-missing doc must not re-degrade
                # every tick. First occurrence fails loud on `verdicts`; the slug is
                # skipped silently thereafter, never pinning the source. Other
                # sources still fail-loud on their own transport failures, so a real
                # outage is never masked. Recovery below re-arms the slug.
                if slug not in flagged_orphan_verdicts:
                    _fail("verdicts", f"requester unresolved for {slug}")
                    flagged_orphan_verdicts.add(slug)
                continue
            fm = okf.parse_frontmatter(doc) or {}
            requested = str(fm.get("requested_by") or "").strip() == agent
            review_requested[slug] = requested  # definitive classification: cache
            flagged_orphan_verdicts.discard(slug)  # recovered -> re-arm fail-loud
            if not _tail_ready("review requester"):
                break
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
            if not _tail_ready("verdict shards"):
                unread = True
                break
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

    # Dir-only review slugs: a `<slug>/` dir with no `<slug>.md` doc is skipped by
    # the doc-keyed scan above. Classify each via the tombstone three-way (one
    # verdicts listing apiece): a dir with real verdict shards is an ORPHAN —
    # surface it ONCE (cached in `orphan_slugs`) so a listener learns the slug
    # exists (repair stays human/maintainer, never auto-delete); an EMPTY dir (no
    # shards, or only a stale `.settled` marker) is a soft-delete TOMBSTONE carrying
    # zero information — skip it silently and NEVER cache it; a verdicts listing
    # that RAISES is UNKNOWN — fail closed, degrade the `verdicts` source visibly
    # and do not cache (never assume tombstone on transport failure). Skipped
    # entirely when the review listing failed (rentries is None): an unreadable
    # root is UNKNOWN, not an absence of docs.
    #
    # BUDGETED (codex P1): unlike the source's other listings — bounded by
    # MY-unsettled-slugs, a small shrinking set — the dir-only set is PERMANENT
    # and growing (soft deletes), so an unbudgeted pass spends up to
    # N x transport-timeout on a degraded tick, on the listener whose tick
    # latency is load-bearing. The pass runs under ``_listen_classify_budget()``
    # (default 10s, env COORD_LISTEN_CLASSIFY_BUDGET), checked before each
    # classification listing (equivalently after the previous one — adjacent
    # iterations — so an overrunning listing is detected immediately; overshoot
    # is bounded by ONE listing, whose completed result is definitive and kept).
    # On exhaustion: degrade the `verdicts` source (its existing streak), cache
    # NOTHING for the unvisited slugs (unknown != classified — no false
    # orphan/tombstone knowledge may persist), and stop — the next tick retries.
    orphan_slugs = set(state.get("orphan_slugs") or [])
    if rentries is not None:
        doc_names = {(e.get("name") or "")[:-3] for e in rentries
                     if not e.get("is_dir") and (e.get("name") or "").endswith(".md")}
        classify_dl = Deadline.open(_listen_classify_budget())
        for e in rentries:
            if not _tail_ready("orphan review classification"):
                break
            if not e.get("is_dir"):
                continue
            oslug = (e.get("name") or "").rstrip("/")
            if (not oslug or oslug in doc_names or oslug in orphan_slugs
                    or oslug in settled_reviews):
                continue
            if classify_dl.expired():
                _fail("verdicts", "dir classification budget spent — "
                      "unclassified review dirs remain, retried next tick")
                break
            kind = _classify_orphan_dir(transport, team, oslug)
            if kind == "orphan":
                events.append({"type": "orphan", "slug": oslug})
                orphan_slugs.add(oslug)
            elif kind == "unknown":
                _fail("verdicts", f"orphan dir {oslug} unclassifiable "
                      f"(verdicts listing unreadable)")
            # tombstone -> silently skipped, never cached

    # Final after-op check: a last blocking read/list that crossed the aggregate
    # deadline must not return a falsely clean tick merely because there is no
    # next iteration whose pre-op guard would observe it.
    _tail_ready("tail completion")

    state["inbox_ids"] = sorted(inbox_ids)
    state["response_keys"] = sorted(response_keys)
    state["slug_owned"] = slug_owned
    state["verdict_keys"] = sorted(verdict_keys)
    state["review_requested"] = review_requested
    state["settled_reviews"] = sorted(settled_reviews)
    state["orphan_slugs"] = sorted(orphan_slugs)
    state["flagged_orphan_responses"] = sorted(flagged_orphan_responses)
    state["flagged_orphan_verdicts"] = sorted(flagged_orphan_verdicts)
    # Never consume a window whose tick was incomplete.  A conclusive feed tick
    # OR a conclusive full-listing fallback may advance; any degraded source
    # leaves the old cursor intact so recovery inclusively replays the window.
    if not failures:
        state["feed_cursor"] = now_iso
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
        print(jsonutil.dumps(ev) if json_mode else _format_listen_event(ev))
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
    state = _load_listen_state(
        state_path, transport=transport, team=args.team, agent=agent)
    json_mode = bool(getattr(args, "json", False))
    verbose = bool(getattr(args, "verbose", False))

    def tick() -> dict[str, list[str]]:
        _events, failures = _run_listen_tick(
            transport, args.team, agent, state,
            json_mode=json_mode, verbose=verbose)
        _save_listen_state(
            state_path, state, transport=transport, team=args.team, agent=agent)
        return failures

    if args.once:
        failures = tick()
        # A captured transport failure is data, not an exception.  Keep the
        # pulse-once stderr contract in _run_listen_tick, but return a stable
        # machine-readable status on *every* one-shot tick so schedulers do not
        # mistake a suppressed second pulse for recovery.
        return 3 if failures else 0
    interval = args.interval if args.interval and args.interval > 0 else 60
    try:
        while True:
            # Per-tick guard: `listen` is the load-bearing watcher (its tick latency
            # is the reply leg of `tell`/`respond`/`review`). An UNMODELED exception
            # in one tick must degrade THAT tick, never kill the daemon — a
            # transient bug would otherwise silence the whole watcher. Log to stderr
            # in the `LISTEN DEGRADED:` register, keep the streak state, continue.
            # `--once` deliberately stays UNguarded above: a one-shot run surfaces
            # its failure (rc 1 via main's envelope) to whatever scheduled it.
            try:
                tick()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                _log.error("listen tick failed (daemon continues)",
                           team=args.team, agent=agent,
                           error=f"{type(e).__name__}: {e}")
                print(f"LISTEN DEGRADED: tick raised {type(e).__name__}: {e} — "
                      f"daemon continues, next tick in {interval}s", file=sys.stderr)
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


def _held_roles(transport: Any, team: str, agent: str) -> tuple[list[str], bool]:
    """Roles where ``agent`` holds a FRESH lease. Returns ``(held, ok)``.

    ``ok`` is False whenever the answer is UNKNOWN — the roles/ listing raised, or
    any single role's state could not be resolved. FAIL CLOSED: an empty ``held``
    with ``ok=True`` means "holds nothing"; with ``ok=False`` it means "we could not
    find out", and those are different facts that callers must not conflate.

    This is the WRITE path's fold (``continuity park``), and until 2026-07-17 it was
    the FOURTH role surface — the one #410 missed. ``parse_sla_hours``'s docstring
    still says "all three role surfaces" because this one was deferred as
    out-of-scope while the read folds were fixed. It carried every hole they did:
    a raised listing returned a partial list as if complete; ``or {}`` on the role
    doc turned an unparseable body into the default SLA; ``float(...) or DEFAULT``
    under a bare except mapped an explicitly-invalid ``sla_hours`` onto 24h; and
    ``or {}`` on the lease read folded an unreadable shard out as "not a holder".

    On a write path those are worse than on a read one: ``park`` printed
    "nothing to park" and exited 0, so a transport blip at session exit silently
    discarded the checkpoint and told the operator it was a clean no-op — at
    exactly the moment nobody is watching, because the session is ending.

    Now it delegates per-role state to ``_role_fresh_holders``, which is the
    canonical fold and already draws every one of those distinctions, so park and
    ``roles status`` can never disagree about a lease.
    """
    now = _iso(_now())
    names = _roles_listing_names(transport, team)
    if names is None:
        return [], False  # membership UNKNOWN — only a complete listing is evidence
    held: list[str] = []
    ok_all = True
    cache: dict[str, Any] = {}
    for n in sorted(names):
        if not n.endswith(".md") or n == "index.md":
            continue
        role = n[:-3]
        holders, ok = _role_fresh_holders(
            transport, team, role, now=now, listing_cache=cache)
        if not ok:
            ok_all = False  # this role's state is unknown; do not read it as "not held"
            continue
        if agent in holders:
            held.append(role)
    return held, ok_all


def cmd_continuity_park(args: argparse.Namespace, transport: Any) -> int:
    """Session-exit checkpoint: snapshot every role the agent holds and point
    each role's checkpoint_ref at it. The incumbent's `park`."""
    agent = args.agent or _host()
    now = _iso(_now())
    held, ok = _held_roles(transport, args.team, agent)
    if not ok:
        # UNKNOWN is not "nothing to park". Refusing here is the whole point: a
        # session runs park as it exits, so a silent no-op discards the checkpoint
        # the NEXT session resumes from, and nobody is watching to notice. Say the
        # checkpoint was not written, loudly and non-zero, while the operator can
        # still retry with the context still alive.
        print(f"park: could not determine which roles {agent} holds in "
              f"team/{args.team} (role state unreadable, not empty) — "
              f"CHECKPOINT NOT WRITTEN. Nothing was parked; retry before ending "
              f"the session.", file=sys.stderr)
        return 1
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
    # Public-read failure contract (see _read_degraded_row): the CORE task fold is
    # not an add-on — an UNKNOWN summaries index must surface as the shared marker,
    # never a silently-empty board/inbox/needs-me that reads as "all clear". The
    # bundle stays tolerant (rc 0); the marker + stderr notice make it loud.
    rows, rows_ok, rows_reason = _load_rows_status(transport, args.team)
    if not rows_ok:
        out["read_degraded"] = _read_degraded_row(rows_reason)
    # One shared add-on deadline (see _briefing_budget), opened HERE — before the
    # first UNBUDGETED transport-heavy section (presence) — and spent cumulatively
    # across presence + forge + resume, so the WHOLE add-on stack is bounded, not
    # just the forge fan-out. P1 (codex-reviewer): presence shard reads were
    # unbudgeted AND ran before the deadline even opened, so a degraded transport
    # hung `briefing` in `presence.roster(_presence_shards(...))` before any bound
    # applied. (`_load_rows` above carries its OWN COORD_OVERLAY_BUDGET; pending-
    # reviews keeps its own independent, already-shipped COORD_REVIEW_FOLD_BUDGET.)
    add_on = Deadline.open(_briefing_budget())
    try:
        shards, pres_degraded = _presence_shards_bounded(
            transport, args.team, deadline=add_on.instant)
        out["presence"] = presence.roster(shards, now=now)
        if pres_degraded is not None:
            # Same discipline as forge: append the degraded marker to the section
            # list so partial presence knowledge stays VISIBLE (json + text).
            out["presence"].append(pres_degraded)
    except Exception as e:
        print(f"briefing: presence section unavailable ({type(e).__name__})", file=sys.stderr)
        out["presence"] = []
    try:
        out["board"] = query.board(rows)
    except Exception as e:
        print(f"briefing: board section unavailable ({type(e).__name__})", file=sys.stderr)
        out["board"] = {}
    # ONE role resolution for the whole bundle, shared by the inbox and needs-me
    # sections (the two folds AGENTS.md calls "your work queue"). Both consume the
    # same held set, so they can never disagree about a lease, and the lease read
    # is paid once per briefing rather than once per section. Unresolved roles are
    # UNKNOWN — surfaced below as `role_degraded`, never folded to "no roles".
    try:
        held_roles, unresolved_roles = _held_roles_for_rows(
            transport, args.team, agent, rows, now=now)
    except Exception as e:
        # The resolver never raises by contract; if it somehow does, the role set is
        # UNKNOWN for EVERY role-shaped assignee in the bundle — say so, don't
        # quietly serve a role-blind queue.
        print(f"briefing: role resolution unavailable ({type(e).__name__})", file=sys.stderr)
        held_roles, unresolved_roles = set(), {"(all)"}
    if unresolved_roles:
        out["role_degraded"] = _role_degraded_row(unresolved_roles)
    # Blocked-on-human: the reserved FIRST section, on its own dedicated bundle key.
    # Derived PURELY from ``rows`` + the role set already resolved above — ZERO
    # extra transport, so a budget cut can never hide a decision parked on a human.
    out["blocked_on_human"] = _blocked_on_human_section(
        rows, held_roles=held_roles or None, roles_unknown=bool(unresolved_roles))
    try:
        out["inbox"] = _directed_inbox(
            transport, args.team, agent, rows,
            held_roles=held_roles or None, include_backlog=args.all,
            include_history=args.all)
    except Exception as e:
        print(f"briefing: inbox section unavailable ({type(e).__name__})", file=sys.stderr)
        out["inbox"] = []
    try:
        out["needs_me"] = _needs_me_rows(
            transport, args.team, agent, rows, now=now,
            held_roles=held_roles, include_history=args.all)
    except Exception as e:
        print(f"briefing: needs_me section unavailable ({type(e).__name__})", file=sys.stderr)
        out["needs_me"] = []
    # The shared add-on deadline (add_on) was opened at the top of this
    # function, before the presence section — time already burned by presence and
    # pending-reviews shrinks the window the forge fan-out and resume read get, so
    # the whole add-on stack is bounded cumulatively. pending-reviews keeps its own
    # tighter, already-shipped budget (whichever bound is sooner).
    try:
        out["pending_reviews"] = _pending_reviews_for(
            transport, args.team, agent, rows=rows, deadline=add_on.instant)
    except Exception as e:
        print(f"briefing: pending_reviews section unavailable ({type(e).__name__})", file=sys.stderr)
        out["pending_reviews"] = []
    try:
        out["forge_feedback"] = _forge_feedback_for(
            transport, args.team, agent, deadline=add_on.instant)
    except Exception as e:
        print(f"briefing: forge_feedback section unavailable ({type(e).__name__})", file=sys.stderr)
        out["forge_feedback"] = []
    resume_cut = False
    try:
        snaps = []
        for e in transport.list_dir(_continuity_prefix(args.team, agent)):
            if add_on.expired():
                # Shared budget spent by the earlier add-on sections: stop reading
                # this agent's snapshots (a per-file read fan-out) rather than let a
                # slow tail hang the briefing. The resume is a floor, not the truth.
                resume_cut = True
                break
            n = (e.get("name") or "").rstrip("/")
            if e.get("is_dir") and n:
                raw = transport.read(_continuity_path(args.team, agent, n))
                if raw:
                    try:
                        snaps.append(json.loads(raw))
                    except Exception:
                        pass
        out["resume"] = continuity.latest(snaps)
        if resume_cut:
            print("briefing: resume section truncated (shared budget spent) — "
                  "resume may be stale; run `continuity resume` for the latest",
                  file=sys.stderr)
    except Exception as e:
        print(f"briefing: resume section unavailable ({type(e).__name__})", file=sys.stderr)
        out["resume"] = None
    if args.json:
        jsonutil.print_json(out)
        return 0
    print(f"briefing — {agent} in team/{args.team}")
    if not rows_ok:
        _surface_read_degraded(rows_reason, json_mode=False)
    # FIRST — before presence/board/inbox: decisions parked on a human. Free and
    # un-starvable, so this is the one section a budget cut can never hide.
    boh = out.get("blocked_on_human") or []
    if boh:
        print(f"  blocked on human: {len(boh)} item(s)")
        for r in boh:
            print(_blocked_on_human_line(r))
    live = [p["agent"] for p in out["presence"] if p.get("liveness") == "live"]
    print(f"  live now: {', '.join(live) if live else '(nobody)'}")
    for r in out["presence"]:  # always shown — a degraded roster must never hide
        if r.get("type") == "presence-degraded":
            print(_presence_degraded_line(r))
    open_counts = {k: len(v) for k, v in (out["board"] or {}).items() if v}
    print("  board: " + (", ".join(f"{k}={v}" for k, v in open_counts.items()) or "empty"))
    print(f"  inbox: {len(out['inbox'])} item(s)")
    for r in out["inbox"][:5]:
        print(_line(r))
    print(f"  needs-me: {len(out['needs_me'])} item(s)")
    if out.get("role_degraded"):
        # Always shown, and printed against BOTH counts above — the two sections it
        # qualifies. Without it, an unresolved role renders as a clean queue that
        # reads "no role work", which is the bug this whole change closes.
        print(_role_degraded_line(out["role_degraded"]))
    # The degraded / UNKNOWN markers are ALWAYS shown and NEVER counted as pending
    # items: `review-fold-degraded` (expected tail truncation) and, incident-grade,
    # `review-head-degraded` (the caller's OWN review queue could not complete). A
    # degraded/UNKNOWN marker counted as a pending item — or rendered through
    # `_line` — misstates the queue (live: an orphan-classification marker read as
    # "pending reviews: 1 item(s)" with zero actual reviews), so ALL markers are
    # split out and dispatched, never tallied; only real review rows count.
    _review_degraded_markers = (
        "review-fold-degraded", "review-head-degraded",
        "review-orphan-degraded", "review-role-degraded")
    pend_rows = [r for r in out["pending_reviews"]
                 if r.get("type") not in _review_degraded_markers]
    degraded_rows = [r for r in out["pending_reviews"]
                     if r.get("type") in _review_degraded_markers]
    print(f"  pending reviews: {len(pend_rows)} item(s)")
    for r in pend_rows[:5]:
        print(_review_row_line(r) or _line(r))
    for r in degraded_rows:  # always shown — a degraded/UNKNOWN fold must never hide
        print(_review_row_line(r) or _line(r))
    forge_rows = out.get("forge_feedback") or []
    forge_fb = [r for r in forge_rows if r.get("type") != "forge-degraded"]
    forge_deg = [r for r in forge_rows if r.get("type") == "forge-degraded"]
    print(f"  forge feedback: {len(forge_fb)} PR(s)")
    for r in forge_fb[:5]:
        print(_forge_feedback_line(r))
    for r in forge_deg:  # always shown — a degraded fold must never hide
        print(_forge_degraded_line(r))
    # A budget cut means UNKNOWN/stale, not ABSENT. The stderr line above already
    # gives the remedy; do not contradict it with continuity's absence rendering.
    if not resume_cut:
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


def _presence_shards_bounded(
    transport: Any, team: str, *, deadline: Optional[float] = None
) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    """Read presence shards into the roster-fold shape, BOUNDED by an absolute
    ``time.monotonic()`` deadline (None = unbounded/legacy). Returns
    ``(shards, degraded_marker_or_None)``.

    The presence section is a team-global fan-out — one shard per agent, a
    ``list_dir`` plus one read each. Before the P1 fix (codex-reviewer) it ran via
    the unbudgeted ``_presence_shards`` AND before the shared briefing deadline even
    opened, so a degraded transport hung the whole ``briefing`` in
    ``presence.roster(_presence_shards(...))`` (needed a SIGINT). This mirrors the
    forge/review fold discipline: the deadline is checked BOTH before and after
    each blocking read (a single stalled read can't return a clean row — overshoot
    is bounded by ONE read), a listed-but-unreadable shard (read -> None) counts as
    ``skipped``, and a top-level listing failure yields ``scanned=0``. The LISTING
    itself is a blocking op under the same discipline (codex round-2 P1): a deadline
    already spent when we get here skips the call entirely (an earlier section spent
    the budget — paying one more transport timeout of stall would re-open the hang),
    and an overrun detected AFTER the listing surfaces the marker even when the
    listing returned [] (otherwise a slow empty listing fell through the per-shard
    loop to ``([], None)`` — a falsely-clean empty roster). On any breach/failure a
    single ``presence-degraded`` row ``{type, scanned, total[, skipped]}`` (same
    shape family as ``forge-degraded``) is returned alongside the PARTIAL roster —
    the section never hangs, never crashes, never silently truncates.
    Dashboards/digests keep the unbounded ``_presence_shards`` (they are not on the
    briefing hang path)."""
    dl = Deadline(deadline)
    if dl.expired():
        # Budget already spent before the section started: skip the listing — don't
        # pay one more blocking op. total=0: the roster size is UNKNOWN (never listed).
        return [], budget_mod.degraded_row("presence-degraded", 0, 0)
    pfx = _presence_prefix(team)
    try:
        entries = transport.list_dir(pfx)
    except TransportError:
        # The listing itself failed: the roster is UNKNOWN, not empty. Surface a
        # degraded marker (scanned=0) so absence-vs-outage isn't folded to silence.
        return [], budget_mod.degraded_row("presence-degraded", 0, 0)
    files = [e for e in entries
             if not e.get("is_dir") and (e.get("name") or "").endswith(".md")]
    total = len(files)
    if dl.expired():
        # The deadline passed DURING the listing: detect the overrun immediately
        # after the blocking op — even for total==0, where the per-shard loop below
        # never runs and could not surface it. No shard is read (the budget is
        # spent); the listing we already paid for still prices ``total`` honestly.
        return [], budget_mod.degraded_row("presence-degraded", 0, total)
    shards: list[dict[str, Any]] = []
    scanned = 0
    skipped = 0
    degraded = False
    for e in files:
        if dl.expired():
            degraded = True
            break
        scanned += 1
        n = e.get("name") or ""
        raw = transport.read(pfx + n)
        if dl.expired():
            # The deadline passed DURING this read: detect the overrun immediately
            # after the blocking op. Keep the shard we already paid for, then stop.
            degraded = True
            if raw is not None:
                fm = okf.parse_frontmatter(raw) or {}
                fm.setdefault("agent", n[:-3])
                shards.append(fm)
            else:
                skipped += 1
            break
        if raw is None:
            # Listed yet unreadable -> UNKNOWN shard (a transport problem, never a
            # silent vanish): count it skipped and keep scanning the rest.
            skipped += 1
            degraded = True
            continue
        fm = okf.parse_frontmatter(raw) or {}
        fm.setdefault("agent", n[:-3])
        shards.append(fm)
    marker: Optional[dict[str, Any]] = None
    if degraded:
        marker = budget_mod.degraded_row("presence-degraded", scanned, total, skipped)
    return shards, marker


def _presence_degraded_line(r: dict[str, Any]) -> str:
    return budget_mod.fold_degraded_line(
        r, label="presence",
        remedy="roster may be partial, run `presence show` for the rest",
        noun="shard")


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
    now = _now()
    engagement = getattr(args, "engagement", None)
    until = getattr(args, "until", None)
    slug = tasks.agent_key(agent)
    shard_path = f"{_presence_prefix(args.team)}{slug}.md"

    # Build the engagement object (W1). When --engagement is NOT passed we write NO
    # engagement field at all, so the shard stays byte-identical to the legacy
    # shard — that is what keeps this step inert.
    engagement_obj: Optional[dict[str, Any]] = None
    if engagement is None:
        if until is not None:
            print("presence beat: --until requires --engagement session "
                  "(there is no mode to attach the expiry to)", file=sys.stderr)
            return 2
    else:
        resolved_until: Optional[str] = None
        state = "active"
        lapsed_at: Optional[str] = None
        if engagement == "session":
            # Refresh-safe: a repeated session beat (e.g. the launchd heartbeat)
            # must NOT slide the TTL, and W1 must NEVER touch the sweep-owned
            # state/lapsed_at (W3 is their sole writer). Read the prior shard and
            # continue an existing session rather than minting a fresh one. A
            # read/parse failure is NOT fatal — treat it as "no prior engagement".
            # (r3) Fail-closed on unknown prior: the transport read contract is
            # None on ANY failure, so "no content" alone cannot distinguish a
            # genuinely absent shard from an unreadable one — and overwriting an
            # unreadable shard would let a transient read failure replace a
            # sweep-marked lapsed session with fresh active engagement (false
            # liveness through the error path). One parent listing disambiguates,
            # the same idiom the role folds use: absent -> legitimately fresh;
            # listed-but-unreadable or listing-failed -> UNKNOWN -> rc 1, write
            # nothing (retryable). A READABLE prior whose engagement is malformed
            # degrades inside parse_engagement and is treated as fresh — that is
            # deliberate self-heal of a corrupt shard, not an unknown overwrite.
            prior: Optional[dict[str, Any]] = None
            prior_raw: Optional[str] = None
            read_raised = False
            try:
                prior_raw = transport.read(shard_path)
            except Exception:
                read_raised = True
            if prior_raw:
                try:
                    prior = presence.parse_engagement(okf.parse_frontmatter(prior_raw))
                except Exception:
                    prior = None            # readable garbage -> self-heal as fresh
            else:
                exists: Optional[bool] = None
                try:
                    exists = any(e.get("name") == f"{slug}.md"
                                 for e in transport.list_dir(_presence_prefix(args.team)))
                except Exception:
                    exists = None
                if exists is not False:
                    why = "read raised" if read_raised else "read returned no content"
                    print(f"presence beat: prior shard {shard_path} is unreadable or of "
                          f"unknown existence ({why}); refusing to write session "
                          "engagement over an unknown prior — retry", file=sys.stderr)
                    return 1
            # A prior SESSION (parse_engagement only reports mode session when it
            # has a resolved until) is continued; any other/malformed/legacy prior,
            # or a mode change into session, is a new session.
            continuing = bool(prior) and prior.get("mode") == "session"
            if until is not None:
                dt = presence.parse_iso_z(until)
                if dt is None:
                    print(f"presence beat: --until must be ISO-8601 "
                          f"(e.g. 2026-07-23T09:00:00Z); got {until!r}", file=sys.stderr)
                    return 2
                resolved_until = presence.to_iso_z(dt)     # explicit always wins
            elif continuing:
                resolved_until = prior["until"]            # preserve — do not slide
            else:
                resolved_until = presence.to_iso_z(
                    now + timedelta(hours=presence.SESSION_DEFAULT_TTL_HOURS))
            if continuing:
                # Carry forward whatever the sweep last wrote; W1 never resets it
                # (no lapsed->active recovery here — that is W2/W3).
                state = prior["state"]
                lapsed_at = prior["lapsed_at"]
        elif until is not None:
            print(f"presence beat: --until is only valid with --engagement session; "
                  f"mode {engagement!r} carries no expiry", file=sys.stderr)
            return 2
        engagement_obj = {"mode": engagement, "until": resolved_until,
                          "state": state, "lapsed_at": lapsed_at}

    fm = {
        "type": "Presence", "title": f"presence — {agent}", "agent": agent,
        "workstreams": args.workstream or [], "summary": args.summary or "",
        "timestamp": _iso(now),
    }
    if engagement_obj is not None:
        fm["engagement"] = engagement_obj
    body = f"\n# Presence: {agent}\n"
    transport.write(shard_path, okf.render_frontmatter(fm) + body)
    print(f"beat {agent} ({slug}.md)")
    return 0


def cmd_presence_show(args: argparse.Namespace, transport: Any) -> int:
    ros = presence.roster(_presence_shards(transport, args.team), now=_iso(_now()))
    if args.json:
        jsonutil.print_json(ros)
        return 0
    print(f"presence — team/{args.team}: {len(ros)} agent(s)")
    for r in ros:
        ws = ", ".join(r["workstreams"])
        # Render the engagement-aware STATE (may be `lapsed`) and append the
        # annotation — the orthogonal second-axis fact (freshness for a lapsed
        # row, a stale-beat nudge otherwise). dormancy ⊥ staleness: never merged.
        line = (f"  [{r['state']:6}] {r['agent']}" + (f"  ({ws})" if ws else "")
                + (f" — {r['summary']}" if r["summary"] else ""))
        if r.get("annotation"):
            line += f"  · {r['annotation']}"
        print(line)
    return 0


# --- engagement gate (wake-router W2 mixed-fleet gate, plan §3) --------------

def _presence_shards_status(
    transport: Any, team: str
) -> tuple[list[dict[str, Any]], bool]:
    """Read presence shards for the gate, PRESERVING read degradation. Returns
    ``(shards, ok)``.

    ``_presence_shards`` swallows a listing ``TransportError`` to ``[]`` — fine
    for a best-effort roster, but the gate CERTIFIES that population, so an
    UNKNOWN roster read must never look like a confirmed-empty one (an empty gate
    passes vacuously — fail-OPEN). Same read-contract class as the defaults read:
      - the presence-dir ``list_dir`` raises   -> roster UNKNOWN, ``ok=False``;
      - a listed shard reads ``None``           -> that agent present-but-unreadable,
        coverage unknowable, ``ok=False`` (the rest are still collected);
      - a listed shard reads non-empty but its frontmatter will not parse, or
        carries no usable ``timestamp`` -> its freshness (hence coverage) is
        UNKNOWN, ``ok=False``. Emitting a synthesized ``{}``/timestampless row
        (the ``parse_frontmatter(raw) or {}`` idiom) would classify it stale and
        SILENTLY EXCLUDE it from the live population while ``ok`` stayed True —
        the same certification-boundary fail-OPEN as an unreadable shard, so we
        drop the phantom row and degrade instead;
      - listing succeeds and every shard parses with a classifiable timestamp
        -> CONFIRMED, ``ok=True`` (an empty result here is a confirmed-empty
        roster, distinct from UNKNOWN)."""
    pfx = _presence_prefix(team)
    try:
        entries = transport.list_dir(pfx)
    except TransportError:
        return [], False
    shards: list[dict[str, Any]] = []
    ok = True
    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md"):
            continue
        raw = transport.read(pfx + n)
        if raw is None:
            ok = False                    # listed but unreadable -> UNKNOWN coverage
            continue
        fm = okf.parse_frontmatter(raw)
        if not fm or presence.parse_iso_z(fm.get("timestamp")) is None:
            # non-empty read but unparseable frontmatter, or no usable timestamp
            # to classify freshness -> UNKNOWN coverage. Do NOT synthesize a
            # phantom row that gets silently excluded — fail closed.
            ok = False
            continue
        fm.setdefault("agent", n[:-3])
        shards.append(fm)
    return shards, ok


def _router_prefix(team: str) -> str:
    return f"team/{team}/_coord/router/"


def _engagement_defaults_path(team: str) -> str:
    return f"{_router_prefix(team)}engagement-defaults.json"


def _load_engagement_defaults(
    transport: Any, team: str
) -> tuple[dict[str, Any], bool]:
    """Read the operator defaults map (agent -> mode), returning
    ``(defaults, ok)``. ``ok`` is False when coverage is UNKNOWN and the gate must
    fail closed.

    READ-CONTRACT LENS (this class of bug hit W1 and W1.5): ``transport.read``
    returns ``None`` on BOTH a missing file AND a transient failure, so a falsy
    read alone can NEVER be read as "confirmed absent". A ``None`` read is
    disambiguated against the RAISING ``list_dir`` contract:
      - the router dir lists and does NOT contain the file  -> genuinely absent
        -> an empty defaults map, ``ok=True`` (the file is optional; a missing one
        must not fail an otherwise-covered fleet);
      - the router dir lists and DOES contain the file, yet the read returned None
        -> present-but-unreadable -> UNKNOWN, ``ok=False`` (fail closed);
      - the listing itself raises -> UNKNOWN, ``ok=False`` (fail closed).
    A present-but-unparseable body is likewise UNKNOWN (``ok=False``)."""
    path = _engagement_defaults_path(team)
    raw = transport.read(path)
    if raw is not None:
        try:
            data = json.loads(raw)
        except Exception:
            return {}, False              # present but unparseable -> UNKNOWN
        return (data, True) if isinstance(data, dict) else ({}, False)
    # raw is None: missing OR transient failure. Confirm which via the raising list.
    try:
        entries = transport.list_dir(_router_prefix(team))
    except TransportError:
        return {}, False                  # listing failed -> UNKNOWN -> fail closed
    present = any((e.get("name") or "") == "engagement-defaults.json"
                  for e in entries)
    if present:
        return {}, False                  # listed but unreadable -> UNKNOWN
    return {}, True                       # confirmed absent -> legitimately empty


def _engagement_gate_passes(transport: Any, team: str, *, now: str) -> bool:
    """Predicate the gated vacancy/escalation semantic change branches on. True
    ONLY when the gate is PASS; any degradation/failure returns False, so the
    caller falls back to today's behavior verbatim (fail closed)."""
    try:
        shards, roster_ok = _presence_shards_status(transport, team)
        defaults, ok = _load_engagement_defaults(transport, team)
        res = presence.engagement_gate(shards, defaults, now=now,
                                       defaults_ok=ok, roster_ok=roster_ok)
        return res["status"] == "PASS"
    except Exception:
        return False


def cmd_engagement_gate(args: argparse.Namespace, transport: Any) -> int:
    now = _iso(_now())
    shards, roster_ok = _presence_shards_status(transport, args.team)
    defaults, defaults_ok = _load_engagement_defaults(transport, args.team)
    result = presence.engagement_gate(shards, defaults, now=now,
                                      defaults_ok=defaults_ok, roster_ok=roster_ok)
    if args.json:
        out = dict(result)
        out["team"] = args.team
        jsonutil.print_json(out)
        return 0 if result["status"] == "PASS" else 1
    print(f"engagement gate — team/{args.team}: {result['status']}")
    if not roster_ok:
        print("  ! presence roster is UNKNOWN (listing failed or a shard was "
              "present-but-unreadable — coverage cannot be enumerated); failing "
              "closed", file=sys.stderr)
    if not defaults_ok:
        print("  ! engagement-defaults.json is UNKNOWN (present-but-unreadable or "
              "unparseable — cannot be confirmed absent); failing closed, coverage "
              "cannot be certified", file=sys.stderr)
    for a in result["agents"]:
        via = f" (via {a['via']})" if a.get("via") else ""
        print(f"  [{a['coverage']:9}] {a['agent']}{via}")
    if not result["agents"]:
        print("  (no live agents to gate)")
    return 0 if result["status"] == "PASS" else 1


# --- engagement sweep (wake-router W3 zero-token lapse sweep) ----------------

def _split_body_verbatim(raw: str) -> str:
    """Return the shard body — everything after the closing frontmatter ``---``
    delimiter line — BYTE-PRESERVED (unlike ``okf.split_frontmatter``, which
    routes through ``splitlines`` and drops the exact tail). The sweep re-renders
    only the frontmatter, so the body must survive verbatim. A shard with no
    parseable frontmatter block returns ``""`` (the caller never marks such a
    shard, so this is only reached on the mark path)."""
    lines = raw.splitlines(keepends=True)
    i = 0
    if lines and lines[0].startswith("﻿"):
        lines[0] = lines[0][1:]
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        return ""
    for j in range(i + 1, len(lines)):
        if lines[j].strip() == "---":
            return "".join(lines[j + 1:])
    return ""


def cmd_engagement_sweep(args: argparse.Namespace, transport: Any) -> int:
    """Host-tick, model-free lapse sweep. For each presence shard, mark a session
    past its ``until`` as LAPSED by writing EXACTLY ``engagement.state: lapsed`` +
    ``engagement.lapsed_at`` (this sweep's evaluation time) — the ONE sanctioned
    exception to agent-owned presence writes, scoped to those two fields. Never
    parks, never releases roles, never touches any doc but the presence shard.

    READ-CONTRACT LENS: enumeration is via the RAISING ``list_dir``; if it raises,
    the roster is UNKNOWN and the sweep is DEGRADED — loud (stderr + degraded
    line), rc nonzero, and it must NEVER read as a clean ``0 marked`` swept roster.
    Per shard, the ``read``-None / unparseable / ``_engagement_degraded`` cases
    fail CLOSED — SKIP, never mark (a failed read never causes a write)."""
    now_iso = _iso(_now())
    pfx = _presence_prefix(args.team)

    marked: list[str] = []
    already: list[str] = []
    skipped: dict[str, list[str]] = {}
    degraded: list[dict[str, str]] = []
    write_failures: list[str] = []

    try:
        entries = transport.list_dir(pfx)
    except TransportError as e:
        # Enumeration UNKNOWN — the roster cannot be certified, so we cannot claim
        # anything about lapses. Fail loud and closed; never a silent clean sweep.
        result = {
            "team": args.team, "now": now_iso, "dry_run": bool(args.dry_run),
            "enumeration_ok": False, "marked": [], "already_lapsed": [],
            "skipped": {}, "degraded": [], "write_failures": [],
        }
        if args.json:
            jsonutil.print_json(result)
        else:
            print(f"engagement sweep — team/{args.team}: DEGRADED — roster "
                  f"enumeration failed ({e}); NOT swept", file=sys.stderr)
        return 1

    def _agent_of(fm: dict[str, Any], name: str) -> str:
        return str(fm.get("agent") or name[:-3])

    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md"):
            continue
        raw = transport.read(pfx + n)
        if raw is None:
            # listed but unreadable — a failed read must never cause a write.
            degraded.append({"shard": n, "reason": "unreadable"})
            continue
        fm = okf.parse_frontmatter(raw)
        if not fm:
            degraded.append({"shard": n, "reason": "unparseable"})
            continue
        decision = presence.sweep_decision(fm, now=now_iso)
        action, reason = decision["action"], decision["reason"]
        agent = _agent_of(fm, n)
        if reason == "degraded":
            # A malformed engagement is fail-visible degradation, not a clean skip.
            degraded.append({"shard": n, "reason": "engagement-degraded"})
            continue
        if action == presence.NOOP:
            already.append(agent)
            continue
        if action == presence.SKIP:
            skipped.setdefault(reason, []).append(agent)
            continue
        # action == MARK: write EXACTLY the two engagement fields, preserving all
        # else. Mutate the RAW parsed engagement map (not the normalized parse)
        # so mode/until survive byte-for-byte; the top-level timestamp is NOT
        # bumped — the sweep is not a beat.
        marked.append(agent)
        if args.dry_run:
            continue
        new_fm = dict(fm)
        raw_eng = fm.get("engagement")
        new_eng = dict(raw_eng) if isinstance(raw_eng, dict) else {}
        new_eng["state"] = "lapsed"
        new_eng["lapsed_at"] = now_iso
        new_fm["engagement"] = new_eng
        body = _split_body_verbatim(raw)
        content = okf.render_frontmatter(new_fm) + "\n" + body
        if not transport.write(pfx + n, content):
            # per-shard write failure: report + continue, never abort the sweep.
            marked.pop()
            write_failures.append(agent)

    marked.sort(); already.sort()
    for v in skipped.values():
        v.sort()
    clean = not degraded and not write_failures
    result = {
        "team": args.team, "now": now_iso, "dry_run": bool(args.dry_run),
        "enumeration_ok": True, "marked": marked, "already_lapsed": already,
        "skipped": skipped, "degraded": degraded, "write_failures": write_failures,
    }
    if args.json:
        jsonutil.print_json(result)
        return 0 if clean else 1

    skip_n = sum(len(v) for v in skipped.values())
    tag = " [DRY-RUN]" if args.dry_run else ""
    print(f"engagement sweep — team/{args.team}: {len(marked)} marked, "
          f"{len(already)} already-lapsed, {skip_n} skipped, "
          f"{len(degraded)} degraded{tag}")
    if marked:
        print(f"  marked: {', '.join(marked)}")
    if skipped:
        buckets = ", ".join(f"{k}={len(v)}" for k, v in sorted(skipped.items()))
        print(f"  skipped: {buckets}")
    if write_failures:
        print(f"  ! write failed (mark did not land): {', '.join(write_failures)}",
              file=sys.stderr)
    if degraded:
        for d in degraded:
            print(f"  ! DEGRADED shard {d['shard']}: {d['reason']} (skipped, "
                  "not marked)", file=sys.stderr)
    return 0 if clean else 1


def cmd_agents(args: argparse.Namespace, transport: Any) -> int:
    # Public-read failure contract (see _read_degraded_row): an UNKNOWN task fold
    # must not read as every agent having "no open work".
    rows, ok, reason = _load_rows_status(transport, args.team)
    digest = presence.agents_digest(rows, _presence_shards(transport, args.team), now=_iso(_now()))
    if args.json:
        out = digest + [_read_degraded_row(reason)] if not ok else digest
        jsonutil.print_json(out)
        return 0
    if not ok:
        _surface_read_degraded(reason, json_mode=False)
    for a in digest:
        counts = ", ".join(f"{k}={v}" for k, v in sorted(a["open"].items())) or "no open work"
        state = a.get("state", a["liveness"])
        line = (f"  [{state:7}] {a['agent']} — {counts}"
                + (f" — {a['summary']}" if a["summary"] else ""))
        if a.get("annotation"):
            line += f"  · {a['annotation']}"
        print(line)
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
          "timestamp": _iso(_now()), "nonce": nonce,
          "summary": args.summary or ""}
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


# --- router (wake-router W4 — decision plane, enqueue-only) ---

def _router_presence(transport: Any, team: str, agent: str,
                     memo: dict) -> "tuple[Optional[datetime], bool]":
    """(presence timestamp, lapsed?) for one agent, memoized per pass. Exact-id
    shard read only (CONCUR: no substring/prefix matching). Missing/unreadable
    shard reads as (None, False) — no presence signal, never a guess."""
    if agent in memo:
        return memo[agent]
    fm = okf.parse_frontmatter(
        transport.read(f"{_presence_prefix(team)}{tasks.agent_key(agent)}.md"))
    if not fm:
        memo[agent] = (None, False)
        return memo[agent]
    ts = router.parse_iso(fm.get("timestamp"))
    lapsed = presence.parse_engagement(fm).get("state") == "lapsed"
    memo[agent] = (ts, lapsed)
    return memo[agent]


def _router_pass(args: argparse.Namespace, transport: Any) -> int:
    team = args.team
    prefix = router.router_prefix(team)
    task_prefix = f"team/{team}/task/"
    now = _now()

    cursor, cursor_reason = router.parse_cursor(transport.read(prefix + "cursor.json"))
    observe = cursor is None
    if observe:
        print(f"router: OBSERVE-ONLY pass — {cursor_reason}; decisions are "
              f"logged, nothing is enqueued, and a fresh cursor is written at "
              f"the end of this pass to arm the next one", file=sys.stderr)

    agents_cfg, _executors, cfg_errors = router.validate_config(
        transport.read(prefix + "config.json"))
    if "_config" in cfg_errors:
        print(f"router: {cfg_errors['_config']} — every agent reads "
              f"unconfigured (observe-only) until config.json is fixed",
              file=sys.stderr)
    agent_errors = {k: v for k, v in cfg_errors.items() if k != "_config"}
    for agent, problem in sorted(agent_errors.items()):
        print(f"router: config invalid for {agent}: {problem}", file=sys.stderr)

    # delivered view — decision-plane-owned fold over the delivery records
    delivered_shards: list[dict] = []
    try:
        dl_entries = transport.list_dir(prefix + "delivered/")
    except TransportError:
        dl_entries = []  # empty on first runs; a listing error just skips refold
    for e in dl_entries:
        name = e.get("name") or ""
        if e.get("is_dir") or not name.endswith(".json"):
            continue
        raw = transport.read(prefix + "delivered/" + name)
        if raw:
            try:
                delivered_shards.append(json.loads(raw))
            except ValueError:
                pass  # a corrupt record shard is bookkeeping loss, not a stop
    delivered_view = router.fold_delivered(delivered_shards)

    # prior queue entries — per-agent last queued_at, for cross-pass debounce
    queue_last: dict[str, Any] = {}
    try:
        q_entries = transport.list_dir(prefix + "queue/")
    except TransportError as e:
        q_entries = []
        print(f"router: queue listing degraded ({e}) — cross-pass debounce may "
              f"under-coalesce this pass", file=sys.stderr)
    for e in q_entries:
        name = e.get("name") or ""
        if e.get("is_dir") or not name.endswith(".json"):
            continue
        raw = transport.read(prefix + "queue/" + name)
        try:
            entry = json.loads(raw) if raw else None
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        qa = router.parse_iso(entry.get("queued_at"))
        agent = entry.get("agent")
        if isinstance(agent, str) and qa is not None:
            if agent not in queue_last or qa > queue_last[agent]:
                queue_last[agent] = qa

    # the cursor scan — the store listing IS the event source (never `listen`)
    try:
        entries = transport.list_dir(task_prefix)
    except TransportError as e:
        print(f"router: scan degraded: {e}, retry next pass", file=sys.stderr)
        return 1
    watermark_dt = router.parse_iso(cursor["watermark"]) if not observe else None
    processed = dict(cursor["processed"]) if not observe else {}
    max_seen = watermark_dt
    candidates = []
    for e in entries:
        name = e.get("name") or ""
        if e.get("is_dir") or not name.endswith(".md") or name in ("index.md", "log.md"):
            continue
        mt = router.parse_store_mtime(e.get("mtime"))
        if mt is None:
            continue
        # INCLUSIVE >= — equal-mtime shards are the common case (minute
        # granularity); the processed ledger suppresses replays.
        if watermark_dt is not None and mt < watermark_dt:
            continue
        candidates.append((mt, name))
    candidates.sort()

    counts = {d: 0 for d in router.DECISIONS}
    presence_memo: dict = {}
    enqueued = 0
    pass_failed = False
    for mt, name in candidates:
        if max_seen is None or mt > max_seen:
            max_seen = mt
        shard_id = name[:-3]
        fm = okf.parse_frontmatter(transport.read(task_prefix + name))
        if not fm:
            continue
        assignee = str(fm.get("assignee") or "").strip()
        # population = DIRECTED items only: concrete assignee, not settled
        if not assignee or assignee == "*":
            continue
        if str(fm.get("status") or "").strip().lower() in router.TERMINAL_STATUSES:
            continue
        key = router.idempotency_key(shard_id, assignee)
        if key in processed:
            continue
        presence_ts, lapsed = _router_presence(transport, team, assignee, presence_memo)
        d_row = delivered_view.get(assignee) or {}
        priority = str(fm.get("priority") or "P2").strip().upper()
        decision, not_before, reason = router.decide(
            item_priority=priority,
            agent_cfg=agents_cfg.get(assignee),
            config_error=agent_errors.get(assignee),
            presence_ts=presence_ts,
            lapsed=lapsed,
            last_wake_at=queue_last.get(assignee),
            last_delivered_at=router.parse_iso(d_row.get("last_delivered_at")),
            now=now,
        )
        counts[decision] += 1
        suffix = " [observe-only: not enqueued]" if observe and decision in (
            "interrupt", "defer", "checkin") else ""
        print(f"decision {assignee} {shard_id} -> {decision} ({reason}){suffix}")
        if decision == "unroutable":
            # fail-visible lane: never a silent drop
            print(f"router: wake unroutable for {assignee} — {reason}; item "
                  f"{shard_id} batches to the digest until config is fixed")
        if not observe and decision in ("interrupt", "defer", "checkin"):
            cfg = agents_cfg[assignee]
            entry = {
                "agent": assignee,
                "reason": f"{decision}: directed item {shard_id} ({priority}) — "
                          f"check your bus (idempotency {key})",
                "source_shard": shard_id,
                "priority": priority,
                "queued_at": router.iso(now),
                "not_before": router.iso(not_before or now),
                "adapter": cfg["adapter"],
                "executor": cfg["executor"],
            }
            if not transport.write(
                    prefix + "queue/" + router.queue_filename(assignee, key),
                    json.dumps(entry, sort_keys=True) + "\n"):
                # A checkpointed-but-unwritten wake would be lost FOREVER (the
                # ledger suppresses it on every future scan). Fail the pass:
                # this key is not ledgered, later candidates are left for the
                # retry, and the watermark stops at this item's minute — the
                # inclusive rescan re-surfaces it next pass.
                print(f"router: queue write failed for {key} — pass fails, "
                      f"item is NOT ledgered and retries next pass",
                      file=sys.stderr)
                pass_failed = True
                break
            queue_last[assignee] = now
            enqueued += 1
        processed[key] = router.iso(now)

    if not observe:
        if not transport.write(prefix + "delivered.json",
                               json.dumps(delivered_view, sort_keys=True) + "\n"):
            # observability bookkeeping only — dedup authority is the ledger
            print("router: delivered.json refold write failed (non-fatal, "
                  "view regenerates next pass)", file=sys.stderr)
    new_watermark = router.iso(max_seen) if max_seen is not None else (
        cursor["watermark"] if not observe else None)
    # checkpoint AFTER the batch — whole-file overwrite is the store's
    # atomicity unit; a crash before this line replays safely (ledger no-ops)
    if not transport.write(prefix + "cursor.json",
                           router.render_cursor(new_watermark, processed)):
        print("router: checkpoint write failed — pass fails; the next pass "
              "rescans from the prior cursor (ledger no-ops make the replay "
              "safe)", file=sys.stderr)
        return 1

    if args.json:
        jsonutil.print_json({"observe_only": observe, "scanned": len(candidates),
                             "enqueued": enqueued, "decisions": counts,
                             "pass_failed": pass_failed})
        return 1 if pass_failed else 0
    summary = ", ".join(f"{d}={counts[d]}" for d in router.DECISIONS if counts[d])
    print(f"router pass: {len(candidates)} candidate(s), {enqueued} enqueued"
          + (f" — {summary}" if summary else "")
          + (" [observe-only]" if observe else "")
          + (" [PASS FAILED — retrying next pass]" if pass_failed else ""))
    return 1 if pass_failed else 0


def cmd_router_run(args: argparse.Namespace, transport: Any) -> int:
    rc = _router_pass(args, transport)
    if getattr(args, "once", False):
        return rc
    while True:  # resident decision plane: FIXED 60s cadence (plan §2.5)
        time.sleep(router.ROUTER_POLL_SECONDS)
        _router_pass(args, transport)


# --- stash (fulcra-agent-durable-state) ---

def _stash_prefix(team: str, agent: str) -> str:
    # Raw agent id, not agent_key: the stash path is a documented convention
    # (SKILL + pre-existing stashes) that agents also address with plain
    # `fulcra-api file` commands, so the engine must not remap it.
    return f"team/{team}/_coord/agents/{agent}/stash/"


def cmd_stash_push(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    prefix = _stash_prefix(args.team, agent)
    now = _iso(_now())
    # Stage + guard EVERYTHING before the first upload: a batch with one
    # refused file uploads nothing, so a retry can't silently diverge from
    # what the failed run half-pushed.
    staged: list[tuple[str, str, bool]] = []
    for f in args.files:
        p = pathlib.Path(f)
        try:
            content = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"stash: no such file: {f}", file=sys.stderr)
            return 1
        except UnicodeDecodeError:
            print(f"stash: {f} is not UTF-8 text — binary files don't survive "
                  f"the text transport, refusing rather than corrupt", file=sys.stderr)
            return 1
        except OSError as e:
            print(f"stash: cannot read {f}: {e}", file=sys.stderr)
            return 1
        name = p.name
        if not stash.safe_name(name):
            print(f"stash: refused {name!r}: not a plain stash filename", file=sys.stderr)
            return 1
        reason = stash.secret_reason(name, content)
        if reason is not None:
            if getattr(args, "unsafe_allow_secrets", False):
                print(f"WARNING: secrets guard overridden — {reason}. "
                      f"team/{args.team}/** is readable by every agent on the bus.",
                      file=sys.stderr)
            else:
                print(f"stash: refused (fail-closed secrets guard): {reason}\n"
                      f"  nothing was uploaded. Secrets belong in env config or the "
                      f"keychain, never the stash (fulcra-agent-durable-state); for a "
                      f"false positive re-run with --unsafe-allow-secrets",
                      file=sys.stderr)
                return 1
        staged.append((name, content, bool(p.stat().st_mode & 0o111)))
    manifest = stash.parse_manifest(transport.read(prefix + stash.MANIFEST_NAME))
    for name, content, executable in staged:
        if not transport.write(prefix + name, content):
            print(f"stash: upload failed for {name} — manifest not advanced, re-run",
                  file=sys.stderr)
            return 1
        manifest["files"][name] = stash.file_entry(content, executable=executable, now=now)
        print(f"pushed {name} -> {prefix}{name}")
    if not transport.write(prefix + stash.MANIFEST_NAME,
                           stash.render_manifest(manifest, agent=agent, now=now)):
        print("stash: manifest write failed — files landed but are unmanifested, re-run",
              file=sys.stderr)
        return 1
    print(f"manifest: {len(manifest['files'])} file(s)")
    return 0


def cmd_stash_pull(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    prefix = _stash_prefix(args.team, agent)
    manifest = stash.parse_manifest(transport.read(prefix + stash.MANIFEST_NAME))
    files = manifest.get("files", {})
    names = list(args.names or [])
    for name in names:
        # listing/manifest names are remote data — never let one path-traverse
        # out of dest.
        if not stash.safe_name(name):
            print(f"stash: refused {name!r}: not a plain stash filename", file=sys.stderr)
            return 1
    if not names:
        try:
            entries = transport.list_dir(prefix)
        except TransportError as e:
            print(f"stash pull degraded: {e}, retry", file=sys.stderr)
            return 1
        names = sorted({e["name"] for e in entries
                        if not e.get("is_dir") and stash.safe_name(e.get("name") or "")}
                       | set(n for n in files if stash.safe_name(n)))
        if not names:
            print("stash: empty — nothing to pull", file=sys.stderr)
            return 1
    dest = pathlib.Path(getattr(args, "dest", None) or ".")
    dest.mkdir(parents=True, exist_ok=True)
    rc = 0
    for name in names:
        content = transport.read(prefix + name)
        if content is None:
            print(f"stash: {name} not in the stash", file=sys.stderr)
            rc = 1
            continue
        target = dest / name
        target.write_text(content, encoding="utf-8")
        entry = files.get(name) or {}
        if "exec" in entry:
            # Re-apply the manifest's exec bit in BOTH directions: a restore
            # over a stale executable must clear it, and only the x bits move
            # (never widen read/write for group/other).
            mode = target.stat().st_mode
            target.chmod(mode | 0o111 if entry["exec"] else mode & ~0o111)
        if entry.get("sha256") and entry["sha256"] != stash.sha256_hex(content):
            # The bytes still land (an operator wants to inspect what drifted),
            # but the exit is loud: a silently-diverged restore is the exact
            # failure mode the manifest exists to catch.
            print(f"stash: checksum drift on {name} — store copy does not match "
                  f"the manifest; inspect before trusting it", file=sys.stderr)
            rc = 1
            continue
        state = "verified" if entry.get("sha256") else "no manifest entry"
        print(f"pulled {name} -> {target} ({state})")
    return rc


def cmd_stash_list(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or _host()
    prefix = _stash_prefix(args.team, agent)
    try:
        entries = transport.list_dir(prefix)
    except TransportError as e:
        print(f"stash list degraded: {e}, retry", file=sys.stderr)
        return 1
    files = stash.parse_manifest(transport.read(prefix + stash.MANIFEST_NAME)).get("files", {})
    rows, seen = [], set()
    for e in entries:
        name = e.get("name") or ""
        if e.get("is_dir") or name == stash.MANIFEST_NAME:
            continue
        seen.add(name)
        entry = files.get(name)
        rows.append({"name": name, "size": e.get("size"), "mtime": e.get("mtime"),
                     "manifest": "ok" if entry else "unmanifested",
                     "sha256": (entry or {}).get("sha256"),
                     "exec": (entry or {}).get("exec")})
    for name in sorted(set(files) - seen):
        # manifested but gone from the store: surfaced, not silently dropped
        rows.append({"name": name, "size": None, "mtime": None, "manifest": "missing",
                     "sha256": files[name].get("sha256"), "exec": files[name].get("exec")})
    if args.json:
        jsonutil.print_json(rows)
        return 0
    if not rows:
        print(f"stash — {agent} in team/{args.team}: empty")
        return 0
    print(f"stash — {agent} in team/{args.team}: {len(rows)} file(s)")
    for r in rows:
        marks = r["manifest"] + (", exec" if r.get("exec") else "")
        print(f"  {r['name']}  [{marks}]")
    return 0


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
        jsonutil.print_json(view)
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

def _digest_record_id(team: str, day: str, window: str) -> str:
    """Deterministic record id for the (team, day, window) digest moment.

    The typed ingest endpoint UPSERTS on an explicit id (live-verified
    2026-07-14), so every host that emits this window's digest converges on ONE
    timeline record — idempotency lives at the ingestion layer, not in any
    read-then-write marker race."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"fulcra-coord-digest:{team}:{day}:{window}"))


def _emit_digest_timeline(*, name: str, note: str, window: str, agent: str,
                          record_id: str) -> bool:
    """Hand ONE rendered digest to the hardened fulcra_common digest writer.

    Best-effort, mirrors ``_emit_projection_spec``: coord-engine is stdlib-only,
    so the writer package (and the fulcra-api CLI / token it needs) may be
    entirely absent — that degrades to False, never an exception. Lands on the
    'Agent Tasks — Digest' track via the writer's own definition resolution."""
    try:
        from fulcra_common import annotations as _ann
    except Exception:
        return False
    try:
        # gated=False: this seam's opt-in is the heartbeat's explicit
        # --emit-timeline flag, not the machine-local writer mode (same
        # contract as projection emits). The deterministic record_id makes
        # concurrent same-window emits upsert into one record.
        return bool(_ann.emit_digest_annotation(
            name=name, note=note, window=window, agent=agent, gated=False,
            id=record_id))
    except Exception:
        return False


def cmd_digest(args: argparse.Namespace, transport: Any) -> int:
    now = _iso(_now())
    # Public-read failure contract (see _read_degraded_row): don't fold an UNKNOWN
    # index into a falsely-quiet health digest.
    rows, ok, reason = _load_rows_status(transport, args.team)
    d = digest_mod.build(rows, _presence_shards(transport, args.team),
                         now=now, human=args.human or _human())
    if args.json:
        if not ok:
            d = {**d, _READ_DEGRADED: _read_degraded_row(reason)}
        jsonutil.print_json(d)
    else:
        if not ok:
            _surface_read_degraded(reason, json_mode=False)
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
    emit_timeline = getattr(args, "emit_timeline", False)
    if args.store or emit_timeline:
        day = now[:10]
        window = digest_mod.window_for(now)
        marker = f"team/{args.team}/_coord/digests/{day}-{window}.md"
        # The store marker dedups the BUS COPY (a lost race just re-writes an
        # equivalent copy as a new version — harmless). It is NOT the timeline
        # correctness guard: that lives in the deterministic record id below.
        stored_body = transport.read(marker)
        if stored_body is not None:
            print(f"(digest for {day} {window} already stored — skipped)", file=sys.stderr)
        else:
            stored_body = digest_mod.render(d)
            transport.write(marker, stored_body)
            print(f"stored digest -> _coord/digests/{day}-{window}.md", file=sys.stderr)
        if emit_timeline:
            # Timeline emit state is SEPARATE from the store marker and written
            # only after a confirmed emit, so a transient failure (missing
            # writer, token flake, HTTP error) RETRIES on the next heartbeat
            # tick instead of consuming the window (codex P1). The deterministic
            # record id makes any concurrent or ambiguously-acked re-emit an
            # ingestion-layer upsert of the same record, so retries and races
            # can never duplicate the digest (codex P1).
            emitted_marker = f"team/{args.team}/_coord/digests/{day}-{window}.emitted"
            if transport.read(emitted_marker) is not None:
                pass  # this window's digest is confirmed on the timeline
            else:
                rid = _digest_record_id(args.team, day, window)
                if _emit_digest_timeline(
                        name=f"Agent digest — {day} {window}",
                        note=stored_body, window=window, agent=_host(),
                        record_id=rid):
                    transport.write(emitted_marker,
                                    f"emitted {now} by {_host()} record {rid}\n")
                    print(f"emitted digest timeline moment ({day} {window})",
                          file=sys.stderr)
                else:
                    # LOUD but rc 0: the bus copy exists; the next heartbeat
                    # tick retries this window's emit (no marker written).
                    print("digest timeline emit FAILED (fulcra_common writer "
                          "missing or degraded) — bus copy stored; will retry "
                          "on the next heartbeat tick", file=sys.stderr)
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
    # W2 gated (dormant today): a VACANT role whose holder's SESSION has LAPSED is
    # EXPLAINED absence — role-retaining, not gone-dark — so the vacancy escalation
    # is suppressed WITH a note. This mirrors the dormant_until suppress discipline
    # (roles.escalation_due) and activates ONLY when the mixed-fleet gate PASSES
    # (plan §3). While the gate is BLOCKED/DEGRADED (the fleet is not fully
    # covered) the branch is dormant and every role escalates by today's rules
    # verbatim. The gate + presence roster are team-global — read them ONCE, and
    # only when the gate passes (a BLOCKED fleet pays nothing new).
    gate_passes = _engagement_gate_passes(transport, args.team, now=now)
    pres_shards = _presence_shards(transport, args.team) if gate_passes else []
    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md") or n == "index.md":
            continue
        role = n[:-3]; checked += 1
        doc = transport.read(_role_doc_path(args.team, role))
        reg = okf.parse_frontmatter(doc)
        if reg is None:
            # FAIL CLOSED (review fix): this doc was JUST LISTED by the parent
            # roles/ scan, so no usable doc is knowably transient-or-deleted-or-
            # corrupt — never a live role to judge under DEFAULT_SLA_HOURS.
            # Falling through with the 24h default would collapse a longer-SLA
            # role's window and fire a false VACANT escalation (the incident
            # vector, on the acting path). Skip: transient -> retried next sweep
            # (correct); deleted -> role gone (also correct); corrupt -> a human
            # must fix the doc, and a P1 minted off a doc we cannot read is noise
            # at best. 2026-07-16: this guard read `doc is None`, so an unparseable
            # body sailed past it into exactly the false escalation the comment
            # describes — the same one-line class as `_role_fresh_holders` and
            # `roles status`, which were fixed in the same round. All three
            # surfaces agree: no usable doc for a LISTED role is UNKNOWN.
            print(f"escalate: role doc unusable for {role} — state unknown, "
                  f"skipped (unreadable or corrupt, retry)", file=sys.stderr)
            continue
        sla = roles.parse_sla_hours(reg.get("sla_hours"))
        if sla is None:
            # An EXPLICITLY invalid `sla_hours` on the ACTING path. Judging the role
            # under the 24h default would collapse an unknown (possibly much longer)
            # window and fire a false VACANT — the incident vector this function's
            # doc-guard above already names, reached through the value instead of
            # the document. A P1 to a human minted off an SLA we invented is worse
            # than noise; a malformed field is a doc fix, not an escalation. Skip:
            # the sweep retries every heartbeat, so a repaired doc escalates on the
            # next pass if it genuinely is vacant.
            print(f"escalate: unusable sla_hours ({reg.get('sla_hours')!r}) for "
                  f"{role} — state unknown, skipped (fix the role doc)",
                  file=sys.stderr)
            continue
        # Dormancy: a deliberately-parked role (future dormant_until) is exempt from
        # the mechanical vacancy sweep regardless of lease state — the parked role
        # is vacant BY DESIGN, so re-firing a P1 every heartbeat host, daily, is the
        # bug. Garbage dormant_until fails OPEN (treated absent + a visible note) so
        # a typo can never silently suppress escalations.
        dormant, dormant_err = roles.dormant_state(reg.get("dormant_until"), now=now)
        if dormant_err:
            print(f"escalate: unparseable dormant_until for {role} — treated as "
                  f"absent, escalation NOT suppressed (fix the date to park it)",
                  file=sys.stderr)
        if dormant:
            print(f"escalate: {role} dormant until {reg.get('dormant_until')} — "
                  f"vacancy escalation suppressed", file=sys.stderr)
            continue
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
        # W2 gated semantic change (dormant while the gate is BLOCKED): a lapsed
        # session holder is explained absence — suppress, and SAY so (never
        # silently). When gate_passes is False this block is skipped entirely and
        # control falls through to today's escalation behavior verbatim.
        if gate_passes:
            holder = presence.lapsed_holder(
                [str(l.get("agent")) for l in (leases or [])], pres_shards, now=now)
            if holder is not None:
                print(f"escalate: {role} vacancy explained — holder {holder}'s "
                      f"session has lapsed (declared window ended; role retained, "
                      f"not gone-dark); escalation suppressed", file=sys.stderr)
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
    if fb.get("degraded"):
        print(budget_mod.fold_degraded_line(
            fb["degraded"], label="forge sweep",
            remedy="feedback state is partial, retry", noun="PR"), file=sys.stderr)
        return 1
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
    if fb.get("degraded"):
        print(budget_mod.fold_degraded_line(
            fb["degraded"], label="forge sweep",
            remedy="feedback state is partial, retry", noun="PR"), file=sys.stderr)
        return 1
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


# --- operator loop (fulcra-agent-operator): asks + answer ---

def cmd_asks(args: argparse.Namespace, transport: Any) -> int:
    # Public-read failure contract (see _read_degraded_row): an UNKNOWN index must
    # not read as "nothing waiting on the human".
    rows, ok, reason = _load_rows_status(transport, args.team)
    got = query.asks(rows, now=_iso(_now()), human=args.human or _human())
    if args.json:
        out = [_read_degraded_row(reason)] + got if not ok else got
        jsonutil.print_json(out)
        return 0
    if not ok:
        _surface_read_degraded(reason, json_mode=False)
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
                   help="archive quiet terminal/proposed tasks and settled-single orphan reviews older than N days (or env COORD_RETENTION_DAYS)")
    r.set_defaults(func=cmd_reconcile)

    s = sub.add_parser("status", help="counts by status")
    s.add_argument("team"); add_json(s); s.set_defaults(func=cmd_status)

    b = sub.add_parser("board", help="open work grouped by status")
    b.add_argument("team"); add_json(b); b.set_defaults(func=cmd_board)

    nm = sub.add_parser("needs-me", help="open work assigned to / blocking an agent")
    nm.add_argument("team"); nm.add_argument("--agent", required=True)
    nm.add_argument("--all", action="store_true",
                    help="include acknowledged, closed, and future history")
    add_json(nm)
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
    rcl.add_argument("--summary", "-s")
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
    prb.add_argument("--engagement", choices=list(presence.ENGAGEMENT_MODES),
                     help="occupancy mode written to the shard's engagement object "
                          "(default: no engagement field — reads as resident)")
    prb.add_argument("--until", help="session expiry (ISO-8601); only valid with "
                     "--engagement session, defaults to beat time + 8h")
    prb.set_defaults(func=cmd_presence_beat)
    prs = prsub.add_parser("show", help="roster with live/idle/stale/lapsed liveness")
    prs.add_argument("team"); add_json(prs)
    prs.set_defaults(func=cmd_presence_show)

    en = sub.add_parser("engagement", help="engagement coverage gate (wake-router mixed-fleet gate)")
    ensub = en.add_subparsers(dest="engagement_command", required=True)
    eng = ensub.add_parser("gate", help="mixed-fleet gate: is every LIVE agent's engagement covered? (PASS/BLOCKED)")
    eng.add_argument("team"); add_json(eng)
    eng.set_defaults(func=cmd_engagement_gate)
    esw = ensub.add_parser("sweep", help="host-tick: mark expired sessions LAPSED "
                           "(zero-token; idempotent; never parks/releases roles)")
    esw.add_argument("team"); add_json(esw)
    esw.add_argument("--dry-run", action="store_true",
                     help="preview what WOULD be marked without writing")
    esw.set_defaults(func=cmd_engagement_sweep)

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
    ib = sub.add_parser("inbox", help="open directives for an agent (--ack <slug> to ack)")
    ib.add_argument("team"); ib.add_argument("--agent", "-a"); ib.add_argument("--ack")
    ib.add_argument("--all", action="store_true",
                    help="include acknowledged, closed, future, and @backlog history")
    add_json(ib)
    ib.set_defaults(func=cmd_inbox)
    ls = sub.add_parser("listen", help="await new directives + responses to directives you own (the reply leg of tell)")
    ls.add_argument("team"); ls.add_argument("--agent", "-a")
    ls.add_argument("--interval", type=int, default=60, help="loop poll seconds (default 60; ignored with --once)")
    ls.add_argument("--once", action="store_true", help="one tick then exit — 0 clean or nothing-new, 3 if the tick captured degradation")
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
    rt.add_argument("--for-role", dest="for_role", metavar="ROLE",
                    help="filter to ROLE's bound account (atc/bindings.json) and "
                         "report the role's lease liveness alongside the ranking")
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
    ath = atsub.add_parser(
        "harvest", help="derive outcome shards from settled review families "
                        "(attribution via atc/bindings.json; idempotent)")
    ath.add_argument("team")
    ath.set_defaults(func=cmd_atc_harvest)

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
    bf.add_argument("team"); bf.add_argument("--agent", "-a")
    bf.add_argument("--all", action="store_true",
                    help="include acknowledged, closed, and future queue history")
    add_json(bf)
    bf.set_defaults(func=cmd_briefing)

    dg = sub.add_parser("digest", help="operator digest: blocked-on-you / upcoming / agents / stale")
    dg.add_argument("team"); dg.add_argument("--human"); add_json(dg)
    dg.add_argument("--store", action="store_true",
                    help="persist to _coord/digests/<date>-<window>.md (deduped per day+window)")
    dg.add_argument("--emit-timeline", action="store_true",
                    help="also emit the digest as a moment on the 'Agent Tasks — Digest' "
                         "timeline track (deterministic per-window record id upserts at "
                         "ingestion, so fleets and retries converge on one record; failed "
                         "emits retry on the next tick; best-effort via fulcra-common)")
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
    rvr = rvsub.add_parser("restore", help="move an archived settled-single review back to the hot path")
    rvr.add_argument("team"); rvr.add_argument("slug")
    rvr.set_defaults(func=cmd_review_restore)

    ro = sub.add_parser("router", help="wake-router decision plane (wake-router-PLAN.md W4 — cursor scan + policy, enqueue-only)")
    rosub = ro.add_subparsers(dest="router_command", required=True)
    ror = rosub.add_parser("run", help="scan the store by cursor and enqueue wake decisions (fixed 60s cadence; --once for one pass)")
    ror.add_argument("team")
    ror.add_argument("--once", action="store_true", help="one pass then exit (default: resident loop)")
    add_json(ror)
    ror.set_defaults(func=cmd_router_run)

    sh = sub.add_parser("stash", help="durable per-agent tooling stash + manifest (fulcra-agent-durable-state)")
    shsub = sh.add_subparsers(dest="stash_command", required=True)
    shp = shsub.add_parser("push", help="upload local files into your stash (fail-closed secrets guard) + refresh the manifest")
    shp.add_argument("team"); shp.add_argument("files", nargs="+")
    shp.add_argument("--agent", "-a")
    shp.add_argument("--unsafe-allow-secrets", action="store_true",
                     help="override the secrets guard for a FALSE POSITIVE only — the stash is bus-readable, a real credential never goes here")
    shp.set_defaults(func=cmd_stash_push)
    shu = shsub.add_parser("pull", help="restore stash files to local disk, verifying manifest checksums")
    shu.add_argument("team"); shu.add_argument("names", nargs="*")
    shu.add_argument("--agent", "-a")
    shu.add_argument("--dest", default=".", help="directory to restore into (default .)")
    shu.set_defaults(func=cmd_stash_pull)
    shl = shsub.add_parser("list", help="stash contents with manifest status (ok/unmanifested/missing)")
    shl.add_argument("team"); shl.add_argument("--agent", "-a"); add_json(shl)
    shl.set_defaults(func=cmd_stash_list)

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


# --- W1.5 activity-implies-liveness -----------------------------------------
#
# Every engine bus WRITE verb refreshes the ACTOR's presence timestamp at the
# single dispatch chokepoint below, so no verb can be missed and none has to
# opt in. The set is keyed on the command FUNCTIONS themselves (not on parsed
# subcommand strings) — read verbs (status/board/search/needs-me/briefing,
# presence show, review status) and the W1 ``presence beat`` are deliberately
# absent. See AGENTS.md, "Activity implies liveness".
_ACTIVITY_WRITE_FUNCS = frozenset({
    cmd_tell, cmd_respond,
    cmd_task_start, cmd_task_update, cmd_task_block, cmd_task_pause,
    cmd_task_abandon, cmd_task_assign, cmd_task_restore, cmd_task_done,
    cmd_review_request, cmd_review_restore,
    cmd_reconcile,
})

#: Process-global throttle memo: actor -> monotonic time of its last activity
#: refresh. Module state by design (one process = one live agent); the test
#: suite resets it between cases.
_ACTIVITY_BEAT_MEMO: dict[str, float] = {}


def _now_monotonic() -> float:
    """Monotonic clock seam for the activity throttle — patchable in tests."""
    return time.monotonic()


def _refresh_activity_presence(
    transport: Any, team: str, actor: str, *, now_monotonic: float, now_iso: str,
) -> None:
    """Bump ``actor``'s presence timestamp to mark write-path activity.

    THROTTLE: at most one write per ``ACTIVITY_REFRESH_INTERVAL`` per process —
    N writes in one interval collapse to ONE presence write via the module memo.

    PRESERVE-ALL-BUT-TIMESTAMP: this is a timestamp BUMP, not a ``presence beat``
    re-run. It reads the actor's existing shard and rewrites ONLY the top-level
    ``timestamp`` line, leaving every other byte — engagement (mode/until/**state
    /lapsed_at**), workstreams, summary, body — verbatim. It never slides a
    session's ``until`` and never touches ``state``/``lapsed_at`` (W3-owned). If
    no shard exists, it writes a minimal beat that carries NO engagement object.

    FAILURE ISOLATION: any error is swallowed with a single stderr note; this
    function never raises and never affects the bus write's rc.
    """
    last = _ACTIVITY_BEAT_MEMO.get(actor)
    if last is not None and now_monotonic - last < presence.ACTIVITY_REFRESH_INTERVAL:
        return  # throttled — already refreshed within this interval
    _ACTIVITY_BEAT_MEMO[actor] = now_monotonic
    try:
        slug = tasks.agent_key(actor)
        shard_path = f"{_presence_prefix(team)}{slug}.md"
        raw = transport.read(shard_path)
        if raw:
            lines = raw.split("\n")
            for i, line in enumerate(lines):
                # Top-level ``timestamp:`` only (engagement's nested keys are
                # indented, so they never match) — a pure value swap keeps every
                # other byte, incl. engagement and body, untouched.
                if line.startswith("timestamp:"):
                    lines[i] = f"timestamp: {now_iso}"
                    transport.write(shard_path, "\n".join(lines))
                    return
            # PRESENT but malformed (no top-level timestamp line). Do NOT write a
            # minimal beat over it — that would erase the shard's engagement
            # (incl. state/lapsed_at/until) and workstreams, the clobber this path
            # exists to prevent. Skip non-destructively; the agent's next real
            # ``presence beat`` repairs the shard.
            print(f"presence activity-refresh skipped: {shard_path} has no "
                  "top-level timestamp; left intact for the next beat to repair",
                  file=sys.stderr)
            return
        # ``read`` returned falsy — but the transport contract is None on BOTH a
        # missing file AND a transient read failure, so this is NOT yet proof of
        # absence. Confirm independently via the RAISING ``list_dir`` contract and
        # FAIL CLOSED on any UNKNOWN (same idiom as the W1 session beat): a minimal
        # beat over a shard that merely failed to read would erase live engagement.
        present: Optional[bool]
        try:
            present = any(e.get("name") == f"{slug}.md"
                          for e in transport.list_dir(_presence_prefix(team)))
        except Exception:
            present = None                         # listing failed -> UNKNOWN
        if present is not False:
            # UNKNOWN (listing failed) or shard-present-but-unreadable -> never
            # clobber a possibly-live shard.
            print(f"presence activity-refresh skipped: {shard_path} existence "
                  "unconfirmed (read returned no content); not writing over a "
                  "possibly-live shard", file=sys.stderr)
            return
        # list_dir-CONFIRMED absent — the sole safe case for a minimal beat. No
        # engagement object: an activity bump must not manufacture one.
        fm = {"type": "Presence", "title": f"presence — {actor}",
              "agent": actor, "timestamp": now_iso}
        transport.write(shard_path, okf.render_frontmatter(fm) + f"\n# Presence: {actor}\n")
    except Exception as e:
        print(f"presence activity-refresh failed: {e}", file=sys.stderr)


def main(argv: Optional[list[str]] = None, transport: Any = None) -> int:
    args = build_parser().parse_args(argv)
    transport = transport if transport is not None else FulcraFileTransport()
    try:
        rc = args.func(args, transport)
    except Exception as e:  # never dump a traceback at the user
        # Registered error envelope. An UNEXPECTED exception is NOT a retryable
        # degrade: the `error:` register token (distinct from the "…, retry" /
        # tombstone voice of the degraded single-slug paths) makes it
        # machine-distinguishable to a watcher grepping stderr, carrying the
        # command + exception type as structured fields rather than an off-register
        # `coord-engine: {type}: {e}` prose line. rc 1 is preserved (behavior
        # unchanged); only the surface is now parseable. See AGENTS.md, "the
        # public-read + error register".
        cmd = getattr(args, "command", None) or "?"
        print(f"coord-engine: error: command={cmd} type={type(e).__name__}: {e}",
              file=sys.stderr)
        return 1

    # W1.5: a SUCCESSFUL bus write proves the actor is working -> refresh its
    # presence beat. Actor is the WRITER (``--from``/``FULCRA_COORD_AGENT`` via
    # ``_known_sender`` — never a target assignee); the anonymous host fallback
    # is not a presence identity, so a missing actor/team skips silently. The
    # whole step is best-effort and cannot change ``rc``.
    if rc == 0 and args.func in _ACTIVITY_WRITE_FUNCS:
        actor = _known_sender(args)
        team = getattr(args, "team", None)
        if actor and team:
            _refresh_activity_presence(
                transport, team, actor,
                now_monotonic=_now_monotonic(), now_iso=_iso(_now()))
    return rc


# --- extracted command groups: import + re-export ---------------------------
# Imported here, at module end, so ``cli`` is fully defined when each group binds
# (no load-time cycle); the re-exports republish every moved public name into this
# module's namespace so ``build_parser``, the staying commands that call ATC
# helpers (``cmd_dash``/``cmd_digest``), and ``cli.<name>`` in tests all resolve.
from . import commands_atc  # noqa: E402

_atc_accounts_path = commands_atc._atc_accounts_path
_atc_bindings_path = commands_atc._atc_bindings_path
_atc_usage_prefix = commands_atc._atc_usage_prefix
_atc_usage_shards = commands_atc._atc_usage_shards
_atc_models_overlay = commands_atc._atc_models_overlay
_atc_seed_windows = commands_atc._atc_seed_windows
_atc_provider_harnesses = commands_atc._atc_provider_harnesses
_atc_parse_account_spec = commands_atc._atc_parse_account_spec
_atc_build_account = commands_atc._atc_build_account
_atc_init_interactive = commands_atc._atc_init_interactive
cmd_usage_log = commands_atc.cmd_usage_log
cmd_headroom = commands_atc.cmd_headroom
cmd_route = commands_atc.cmd_route
cmd_atc_harvest = commands_atc.cmd_atc_harvest
cmd_atc_report = commands_atc.cmd_atc_report
cmd_atc_init = commands_atc.cmd_atc_init

from . import commands_annotate  # noqa: E402

# ``_emit_projection_spec`` is re-exported so tests can steer it via
# ``setattr(cli, …)`` (``cmd_annotate_project`` reaches through ``cli`` to read it).
_emit_projection_spec = commands_annotate._emit_projection_spec
cmd_annotate_resolution = commands_annotate.cmd_annotate_resolution
cmd_annotate_status = commands_annotate.cmd_annotate_status
cmd_annotate_project = commands_annotate.cmd_annotate_project

from . import commands_threads  # noqa: E402

DEFAULT_THREADS_FOLD_BUDGET = commands_threads.DEFAULT_THREADS_FOLD_BUDGET
DEFAULT_THREADS_SILENCE_DAYS = commands_threads.DEFAULT_THREADS_SILENCE_DAYS
DEFAULT_THREADS_INTENT_GRACE_HOURS = commands_threads.DEFAULT_THREADS_INTENT_GRACE_HOURS
_threads_fold_budget = commands_threads._threads_fold_budget
_threads_window = commands_threads._threads_window
_threads_is_principal = commands_threads._threads_is_principal
_threads_blocked_signal = commands_threads._threads_blocked_signal
_threads_ash_activity = commands_threads._threads_ash_activity
_threads_candidate_rows = commands_threads._threads_candidate_rows
cmd_threads = commands_threads.cmd_threads


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
