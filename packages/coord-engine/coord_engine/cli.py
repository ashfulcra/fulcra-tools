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
import contextlib
import contextvars
from copy import deepcopy
import hashlib
import concurrent.futures
import io
import json
import math
import os
import pathlib
import secrets
import re
import socket
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from . import (
    aggregate, atc, atc_dash, budget as budget_mod, bus_tags,
    checkpoint_channel, classifier, config, continuity,
    continuity_audit, digest as digest_mod, directives, forge as forge_mod,
    generation,
    health as health_mod, jsonutil, okf, outcome as outcome_mod, presence,
    projection as projection_mod, public_read,
    handoff, pin_currency, query, read_retry, records, review, review_gc,
    obligations as obligations_mod, roles, router, stash, tasks, wake_adapters,
)
from .budget import Deadline
from . import reconcile as rec
from .log import get_logger
from .transport import FulcraFileTransport, TransportError

__all__ = ["main"]

_log = get_logger("cli")

# Set exactly once by ``main`` after Unit 5 proves a generation and overlay.
# All task-backed folds then share those same sealed bytes rather than each
# opening an independent discovery path. Context-local state prevents nested
# test invocations (or future in-process callers) from leaking one team's view
# into another.
_PUBLIC_READ_CONTEXT: contextvars.ContextVar[
    Optional[public_read.PublicReadResult]
] = contextvars.ContextVar("coord-public-read", default=None)

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


#: Characters an identity may contain. Matches the charset the installer already
#: validates agent ids against — applied here at ID CONSTRUCTION, which is where
#: the unvalidated value actually enters the keyspace.
_hostname_rewrite_warned = False


def _sanitize_hostname(raw: str) -> tuple[str, bool]:
    """→ (safe, rewritten). Runs of unsafe characters collapse to one ``-``,
    leading/trailing separators are stripped, and a REWRITTEN name carries a
    short digest of the raw hostname so the mapping stays INJECTIVE.

    The digest is the whole point (codex 558 r1). Collapsing alone is lossy:
    ``bad/name``, ``bad name`` and ``bad-name`` all reduce to ``bad-name``, so
    three distinct machines would share one fleet key and MERGE their presence,
    lease, health and role history. That is strictly worse than the single
    unaddressable identity this function exists to prevent — an unreachable host
    is a hole, a merged one is corruption of two live records.

    ``tasks.agent_key`` already solved this exact problem for agent ids
    ("slugify is lossy … suffix a short hash of the raw id to make the key
    injective"), with the same 6-hex convention used here.

    An UNCHANGED hostname is returned byte-identical, with no suffix — every
    identity currently in the fleet must survive untouched.

    A hostname is whatever the OS hands back, and the fleet id built from it is
    the KEY for presence, health, roles and leases. One host has been registered
    as ``coord-reconcile: <control chars>`` since at least 2026-07-16: unmatched
    by any fold that keys on name, impossible to ``tell``, invisible to a
    version-skew audit, and permanently unaddressable — a hole in a shared
    keyspace that nothing can now reference.
    """
    return classifier.sanitize_hostname(raw)


#: Ambient environment a real caller exports that the TEST SUITE must neutralise,
#: mapped to a representative value the hermeticity wall populates. One mapping
#: rather than a name list plus a parallel list of samples: the fixture clears
#: these keys and the wall sets them, so there is nothing for the two to drift on.
#:
#: Two families, one shape — the suite reading state its caller happens to have:
#:
#: - **identity** (`FULCRA_COORD_AGENT`, `FULCRA_COORD_HUMAN`): 25 tests read
#:   these as a fallback sender, so an inherited identity made a green tree
#:   report failures for anyone following the standing wake procedure, which
#:   opens by exporting one of them.
#: - **channel** (`COORD_RECORDS_TYPE`): 8 tests, and every one is a test whose
#:   PREMISE is that the records config is absent or unreadable —
#:   `test_documented_send_fails_closed_without_a_records_config`,
#:   `test_doctor_self_unreadable_config_is_unknown`, the `[config-absent]`
#:   envelope case. An exported channel supplies the very thing they test the
#:   absence of, so they do not merely fail: they stop being the tests they are
#:   named after.
#:
#: A variable belongs here when the suite reading it makes the ANSWER depend on
#: who ran it. It does NOT belong here when the variable legitimately changes
#: behaviour a test is about — see `NOT_YET_WALLED` in the wall for
#: `COORD_TRANSPORT_HTTP`, where a test asserting "no subprocess" SHOULD fail
#: when you disable the HTTP path, and separating those from real leaks is work
#: nobody has done yet.
INHERITED_ENV: dict[str, str] = {
    "FULCRA_COORD_AGENT": "some-other-agent",
    "FULCRA_COORD_HUMAN": "some-other-human",
    "COORD_RECORDS_TYPE": "some.other.channel",
}


def _identity(explicit: Optional[str] = None) -> str:
    """The one CLI identity resolver: argument, environment, state, then host."""
    global _hostname_rewrite_warned
    def warn(raw: str, safe: str) -> None:
        global _hostname_rewrite_warned
        if not _hostname_rewrite_warned:
            _hostname_rewrite_warned = True
            print(f"coord-engine: hostname {raw!r} is not a usable fleet id; using "
                  f"{safe!r}. This host's presence/lease/health keys depend on it — "
                  f"set FULCRA_COORD_AGENT explicitly to pin your identity.",
                  file=sys.stderr)

    resolved = classifier.resolve_identity(
        explicit,
        environ=os.environ,
        persisted=config.persisted_identity,
        hostname=socket.gethostname,
        on_hostname_rewritten=warn,
    )
    assert resolved is not None
    return resolved


def _host() -> str:
    """Backward-compatible anonymous-host entry point for existing call sites."""
    return _identity()


def _declared_identity(explicit: Optional[str] = None) -> Optional[str]:
    """Resolve a usable identity without manufacturing a host identity."""
    return classifier.resolve_identity(
        explicit, environ=os.environ, persisted=config.persisted_identity
    )


def cmd_wake_queue_file(args: argparse.Namespace, transport: Any) -> int:
    inv = {
        "adapter": "queued-wake-file",
        "agent": args.agent,
        "idempotency_key": args.key,
    }
    print(wake_adapters.queue_wake_file(args.team, inv))
    return 0


def cmd_wake_consume(args: argparse.Namespace, transport: Any) -> int:
    result = wake_adapters.consume_wake_files(args.team, args.agent)
    if result["context"]:
        print(result["context"])
    for error in result["errors"]:
        print(f"queued-wake degraded: {error}", file=sys.stderr)
    return 1 if result["errors"] else 0


def _human() -> str:
    return os.environ.get("FULCRA_COORD_HUMAN") or "human"


def _known_sender(args: argparse.Namespace) -> Optional[str]:
    """The sender identity a reply would be addressed to, or None when only the
    anonymous host fallback is available. `_create_directive` records ownership as
    ``--from`` or ``FULCRA_COORD_AGENT`` (else ``coord-reconcile:<host>``); the
    breadcrumb points others at ``queue --agent <sender>``, so we print it only
    when the sender is a real identity someone actually reads a queue as — never
    the bare host tag."""
    return classifier.resolve_identity(
        getattr(args, "sender", None), environ=os.environ,
        persisted=config.persisted_identity,
    )


def _replies_breadcrumb(team: str, sender: str) -> str:
    #: bus v3 (2026-07-27): the reply leg is the sender's own bounded queue read,
    #: not a resident watcher — `listen` was retired as the wake surface and its
    #: implementation removed, so the breadcrumb points at `queue`.
    return f"replies: coord-engine queue {team} --agent {sender}"


def _close_on_reply_breadcrumb(team: str, owner: str, slug: str) -> str:
    """The CLOSING move, with the slug already filled in, for the RECIPIENT.

    SURFACE MATTERS AND I GOT IT WRONG FIRST (coord-opus-worker, 2026-08-08).
    `_replies_breadcrumb` prints on the SENDER's terminal — its own comment says
    so — and it has exactly one callsite, in the dispatch path. Putting the
    closing command there hands it to the only agent with NO standing to close
    the row: runnable, plausible-looking, and it would close a directive nobody
    had answered. That is precisely the ghost-closure `cmd_respond` fails loud
    to prevent.

    The recipient's point of contact is the queue read, where the slug is
    already on screen. That is the one place this line can be both correct and
    addressed to someone who can act on it.

    Measured 2026-08-08: 912 of 919 proposed board items were dispatch residue —
    directives whose recipients had acted and replied, by `tell`, which opens a
    NEW directive instead of closing the one it answers. Every exchange netted
    +2 open items instead of 0.

    `respond` has always closed, and a spread sample of 25 across eight owners
    found ZERO response shards: nobody was failing to comply, the tool simply
    never showed the closing move at the moment it was wanted. The old
    breadcrumb pointed at a STREAM to read (`queue`) and carried no slug, so a
    recipient who wanted to answer replied the way they knew how.

    `--closes` is the mechanism; this line is the cure.
    """
    return (f"    reply+close: coord-engine tell {team} {owner} "
            f"\"<your title>\" --closes {slug}")


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
#: needs-me/queue have no other budget on this path (the briefing budget opens
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


def _feed_task_rows(
    transport: Any, team: str, index_rows: list[dict[str, Any]],
    changes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool, str]:
    """Apply changed task shards to aggregate rows without a task-dir listing."""
    from . import model
    prefix = rec.task_prefix(team)
    relevant: list[dict[str, Any]] = []
    for change in changes:
        path = str(change.get("path") or "")
        if not path.startswith(prefix) or not path.endswith(".md"):
            continue
        name = path[len(prefix):]
        if "/" in name or name in ("index.md", "log.md"):
            continue
        relevant.append(change)

    # Shared with E1 reconcile: one deterministic lifecycle winner per path.
    latest = rec._collapse_feed_changes(relevant)
    if latest is None:
        return [], False, "data-updates task change lacks a usable timestamp"

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

    ``inbox``/every canonical surface read the reconcile-built summaries
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
    # A caller may provide a stricter absolute phase deadline (a caller's protected
    # head; the retired `listen` tick was the original such caller). The overlay
    # keeps its own cap/budget, but can never outlive that caller: whichever
    # instant arrives first wins.
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
    doc_sink: "Optional[list[Any]]" = None,
    feed_sink: "Optional[list[Any]]" = None,
    feed_section_key: "Optional[str]" = None,
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
    unreadable (the #343 discipline). This is what lets a fold surface a summaries
    failure instead of folding it to a silent [] indistinguishable from empty.

    ``doc_sink`` (sink idiom, like ``degraded_sink``): when given, exactly one
    element is appended — the PARSED aggregate document, or None when it could
    not be read/parsed. Callers hand it to the review/forge folds so they can
    consume the projection sections (``projection.py``) from the summaries read
    this load already paid for, never a second ~MiB read."""
    authority = _PUBLIC_READ_CONTEXT.get()
    if authority is not None:
        task_value = authority.section("tasks")
        sealed_rows = task_value.get("rows") if isinstance(task_value, Mapping) else None
        if isinstance(sealed_rows, list) and all(isinstance(row, dict)
                                                 for row in sealed_rows):
            if doc_sink is not None:
                # Preserve the established add-on seam: reviews/forge consume
                # their values from the SAME validated generation rather than
                # paying a second summaries read.
                doc_sink.append({
                    "rows": sealed_rows,
                    projection_mod.REVIEWS_KEY: dict(authority.section("reviews")),
                    projection_mod.FORGE_KEY: dict(authority.section("forge")),
                    projection_mod.NEEDS_ME_KEY: {"complete": True},
                })
            if feed_sink is not None:
                feed_sink.append({
                    "ok": True,
                    "changes": [],
                    "coverage_horizon": authority.coverage_horizon,
                })
            return [dict(row) for row in sealed_rows], True, ""
        return [], False, "validated generation tasks section unrecognized"

    path = rec.summaries_path(team)
    if doc_sink is not None:
        doc_sink.append(None)
    if feed_sink is not None:
        feed_sink.append({"ok": False, "reason": "data-updates feed not attempted"})
    try:
        raw = read_retry.read_retrying(transport, path)
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
        if doc_sink is not None:
            doc_sink[-1] = aggregate_doc
        # E2 primary path: one authoritative feed call since the aggregate cursor,
        # then direct reads of only changed task shards.  No task-dir listing is
        # consulted, so listing lag cannot hide a verified feed entry.
        aggregate_cursor = None
        if isinstance(aggregate_doc, dict):
            aggregate_cursor = aggregate_doc.get("generated_at")
            # A mixed-fleet host can refresh the aggregate while carrying an
            # engine-owned section unchanged. Projection consumers must query
            # from THAT section's anchor, never the newer container stamp.
            section = aggregate_doc.get(feed_section_key) if feed_section_key else None
            if isinstance(section, dict):
                aggregate_cursor = section.get("generated_at")
        feed = (feed_changes if feed_attempted else _team_updates(
            transport, team, since=aggregate_cursor, now=_iso(_now())))
        if feed is not None:
            delta_rows, delta_ok, _delta_reason = _feed_task_rows(
                transport, team, rows, feed)
            if delta_ok:
                if feed_sink is not None:
                    feed_sink[-1] = {"ok": True, "changes": feed}
                if deadline is not None and deadline.expired():
                    return [], False, "caller row-load budget exhausted during feed delta"
                return delta_rows, True, ""
            if feed_sink is not None:
                feed_sink[-1] = {"ok": False, "reason": _delta_reason}
            # Any feed/read doubt takes the byte-for-byte legacy listing overlay
            # below.  A healthy fallback is not a degraded public read.
        # Live-freshness overlay: union in task docs written since the last
        # reconcile (absent from this index). Any overlay problem flips ``ok`` so
        # the inbox source degrades visibly; the index rows are still served.
        if feed is None and feed_sink is not None:
            feed_sink[-1] = {"ok": False,
                             "reason": "data-updates feed unreadable"}
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
        print(f"reconcile degraded: {res.get('reason')}", file=sys.stderr)
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
    # Contract 2, Class B additive stamp (ratified errata): board already leads
    # stdout with one object keyed by section — the stamp is the shape detector,
    # every existing key and the rc untouched.
    groups["contract"] = 2
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


def print_close_hint(row: dict[str, Any], *, team: str) -> None:
    """Show the recipient how to answer AND close, with the slug filled in.

    NOT on `queue`: its text output is a BYTE-IDENTICAL contract for shell
    consumers on BOTH streams, pinned by two golden tests.

    THE PREDICATE READS `tags`, NOT `kind` (coord-opus-worker, measured against
    939 live rows). Board/needs-me rows carry no `kind` key at all — 0 of 939 —
    so `row.get("kind") or "task"` was always "task" and this returned before
    printing, for every row including the 932 that ARE directives. Directive-ness
    lives in `tags` as `kind:directive`.

    `team` is a PARAMETER for the same reason: rows carry no `team` key either,
    so recovering it from the row rendered the literal placeholder `<team>` and
    the command could not be run. The caller has `args.team`; it is not the
    row's to know.
    """
    if "kind:directive" not in (row.get("tags") or []):
        return
    owner, slug = row.get("owner"), row.get("id")
    if not owner or not slug or owner == "*":
        return
    print(_close_on_reply_breadcrumb(team, owner, slug))


def cmd_needs_me(args: argparse.Namespace, transport: Any) -> int:
    now = _iso(_now())
    doc_sink: list[Any] = []
    feed_sink: list[Any] = []
    rows, rows_ok, rows_reason = _load_rows_status(
        transport, args.team, doc_sink=doc_sink, feed_sink=feed_sink,
        feed_section_key=projection_mod.NEEDS_ME_KEY)
    agg_doc = doc_sink[0] if doc_sink else None
    feed_evidence = feed_sink[0] if feed_sink else None
    # Role routing: work addressed to a role this agent holds IS work that needs
    # this agent (see _held_roles_for_rows). An unresolved role is UNKNOWN and gets
    # its own marker below — never folded into "no role work".
    role_resolution: dict[str, tuple[list[str], bool]] = {}
    held_roles, unresolved_roles = _held_roles_for_rows(
        transport, args.team, args.agent, rows, now=now,
        deadline_seconds=_role_fold_budget(),
        resolution_sink=role_resolution)
    got = _needs_me_rows(transport, args.team, args.agent, rows, now=now,
                         held_roles=held_roles, include_history=args.all,
                         aggregate_doc=agg_doc, feed_evidence=feed_evidence)
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
        transport, args.team, args.agent, rows=rows, deadline=add_on.instant,
        aggregate_doc=agg_doc, feed_evidence=feed_evidence,
        role_resolution=role_resolution)
    got += _forge_feedback_for(transport, args.team, args.agent,
                               deadline=add_on.instant, aggregate_doc=agg_doc,
                               feed_evidence=feed_evidence)
    # Blocked-on-human is the reserved FIRST section — prepended AFTER every other
    # section is built so it lands at index 0, and derived PURELY from ``rows``
    # (zero transport, un-starvable). It surfaces decisions parked on a human that
    # are assigned to the human (never to this agent), so they are not otherwise in
    # ``got``; de-dup by id guards the rare overlap.
    blocked = _blocked_on_human_section(
        rows, held_roles=held_roles or None, roles_unknown=bool(unresolved_roles))
    seen = {r.get("id") for r in blocked}
    got = blocked + [r for r in got if r.get("id") not in seen]
    # Contract 2 (OC2/OC3, ladder PR 1): the envelope is sealed FIRST and rc is
    # a pure function of its health — UNKNOWN/DEGRADED exit 3 even when partial
    # rows were served (this widens the old forge-only rc: a degraded role or
    # review fold now fails closed too, which is the clause's whole point).
    envelope, rc = class_a_envelope(got, source_type="needs-me-source")
    # The verdict, on stderr, BEFORE the unbounded payload it is a verdict about:
    # emitted unconditionally (json mode included) and independent of whether the
    # rows below survive the reader's context. See `emit_envelope`. Under OC2
    # this line is the courtesy DUPLICATE; the stdout envelope is the authority.
    emit_envelope(
        "needs-me", count=len(got), rc=rc,
        health=envelope["health"],
        forge=_fold_source(got, "forge-source"),
        source=envelope["source"],
        degraded=sum(1 for r in got if _is_degraded_row(r)),
    )
    if getattr(args, "envelope_only", False):
        # The durable answer for a truncating harness: the verdict WITHOUT the
        # records. Same rc, same envelope, no payload to truncate.
        return rc
    if args.json:
        jsonutil.print_json(envelope)
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
            elif r.get("type") == "forge-source":
                print(_source_line("forge", r))
            elif r.get("type") == "needs-me-source":
                print(_source_line("needs-me", r))
            else:
                print(_line(r))
                print_close_hint(r, team=args.team)
    return rc


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
    # Contract 2 (OC2/OC3, ladder PR 4): envelope seals first, rc follows its
    # health in BOTH modes. Both failure legs currently fold into the shared
    # read-degraded marker, so an unreadable hot index AND a partial archive
    # both classify UNKNOWN (fail-closed) — splitting the archive-partial leg
    # into a DEGRADED floor needs its own marker type and is deferred to the
    # marker's owner rather than smuggled into this migration.
    envelope, rc = class_a_envelope(got, source_type="search-source")
    if args.json:
        jsonutil.print_json(envelope)
    else:
        if degraded_reason:
            _surface_read_degraded(degraded_reason, json_mode=False)
        real = [r for r in got if r.get("type") != _READ_DEGRADED]
        print(f"{len(real)} match(es) for {args.query!r}:")
        for r in real:
            print(_line(r))
    return rc


# --- roles (fulcra-agent-roles fold) ---

def _role_doc_path(team: str, role: str) -> str:
    return f"team/{team}/roles/{role}.md"


def _reviews_newest_first(root: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order review dirs so a BUDGETED attendance scan spends itself on the
    recent ones.

    Attendance asks a purely RECENCY question — "did a holder file a verdict
    inside the SLA window" — and the scan answered it by walking
    ``reviews[:budget]`` in whatever order the store listed, which is LEXICAL.
    Measured on the live store 2026-08-23: 569 review dirs, and
    `pr-677-release-2-0-3` — holding codex-reviewer's verdict from 15:38Z, nine
    hours inside its 12h SLA — sat at lexical position 407. The scan stops around
    24. That verdict was not merely missed; it was unreachable, and so was every
    other recent one, because recency and alphabetical order are unrelated.

    The consequence was not a slow scan, it was an INERT FIX. `attended` exists
    precisely to stop false vacancy P1s (see this module's `_role_attended` and
    tests/test_roles_attendance.py, whose docstring is the original incident),
    but it can only ever return True if the scan reaches the dir holding the
    work. It could not, so every acting role with a lapsed lease escalated as
    "attendance UNVERIFIED" — including codex-reviewer at 2026-08-23T00:41Z,
    nine hours after the verdict that explained it.

    The recency signal is ALREADY IN THIS LISTING and costs nothing extra: the
    review DOC (``<slug>.md``) sits beside its dir in the same root listing and
    carries an mtime, while directory entries carry none. Ordering by that doc
    mtime puts pr-677 at rank 1 of 340 instead of 407 of 569.

    This ORDERS, it never FILTERS. Dirs with no datable doc (229 of 569 live —
    archived or doc-less) keep their listing order and follow the dated ones, so
    the scanned SET at any budget is the same size and the coverage the caller
    reports stays honest. A cut scan still returns UNKNOWN, never "absent", so
    no truth claim rests on this ordering — only the odds of answering at all.
    """
    dirs = [e for e in root if e.get("is_dir")]
    doc_mtime: dict[str, Any] = {}
    for e in root:
        if e.get("is_dir"):
            continue
        name = e.get("name") or ""
        if not name.endswith(".md"):
            continue
        raw = e.get("mtime")
        mt = router.parse_store_mtime(raw) or router.parse_iso(raw)
        if mt is not None:
            doc_mtime[name[:-3]] = mt
    dated, undated = [], []
    for i, e in enumerate(dirs):
        mt = doc_mtime.get((e.get("name") or "").rstrip("/"))
        (dated if mt is not None else undated).append((mt, i, e))
    # `i` breaks ties on identical mtimes so the order is total and stable —
    # a comparison that fell through to the dict would raise, not reorder.
    dated.sort(key=lambda t: (t[0], -t[1]), reverse=True)
    return [e for _, _, e in dated] + [e for _, _, e in undated]


def _verdict_activity_index(
    transport: Any, team: str, *, budget: int = 40,
    deadline: Optional["Deadline"] = None,
) -> tuple[dict[str, Any], int, int, bool, bool]:
    """ONE pass over the review register -> ``{reviewer: latest verdict mtime}``.

    Returns ``(index, scanned, total, undatable, cut_by_deadline)``.

    ``cut_by_deadline`` rides in the RETURN, not in module state. It was a
    module-level flag for one round and codex-reviewer caught the leak: only the
    normal return updated it, so the two early returns inherited the PREVIOUS
    invocation's value and an identical unreadable scan produced a different
    `escalate` rc depending on whether some earlier scan had timed out. A
    per-scan diagnostic belongs to the scan.

    WHY THIS EXISTS. `_role_attended` used to do this scan itself, per call, and
    `escalate` calls it once per about-to-escalate role — so a sweep re-listed
    the review root and up to ``budget`` verdict prefixes for EVERY such role.
    Profiled on the live store 2026-08-08: 23.6s per call, 47.3s of a 98.2s
    `escalate`, entirely transport latency (41 sequential listings x ~0.59s).
    The cost was never algorithmic; it was fan-out repeated per role. Hoisting
    the scan to one shared pass makes a sweep's attendance cost constant in the
    number of roles instead of linear.

    BOUNDED BY BOTH a count (``budget``) and a WALL-CLOCK ``deadline``. The count
    alone could not stop this: it caps how many dirs are visited and says nothing
    about how long each takes, which is exactly how a 40-dir scan became a
    two-minute call on a slow transport.
    """
    index: dict[str, Any] = {}
    try:
        root = transport.list_dir(f"team/{team}/review/")
    except TransportError:
        # Root listing failed: nothing was scanned, and this is NOT a deadline
        # cut. Every return below states its own cut reason for the same reason.
        return index, 0, 0, False, False
    reviews = _reviews_newest_first(root)
    total = len(reviews)
    scanned = 0
    undatable = False
    cut_by_deadline = False
    for e in reviews[:budget]:
        if deadline is not None and deadline.expired():
            cut_by_deadline = True
            break
        slug = (e.get("name") or "").rstrip("/")
        if not slug:
            continue
        scanned += 1
        try:
            entries = transport.list_dir(_verdicts_prefix(team, slug))
        except TransportError:
            # An unreadable prefix is UNKNOWN for the whole index: we cannot say
            # whose work we failed to see, so the caller must not conclude "no
            # work" from what we did see.
            return index, scanned, total, True, cut_by_deadline
        for v in entries:
            name = v.get("name") or ""
            if not name.endswith(".md") or "--" not in name:
                continue
            reviewer = name[:-3].split("--", 1)[1]
            raw = v.get("mtime")
            mt = router.parse_store_mtime(raw) or router.parse_iso(raw)
            if mt is None:
                undatable = True
                continue
            prev = index.get(reviewer)
            if prev is None or mt > prev:
                index[reviewer] = mt
    return index, scanned, total, undatable, cut_by_deadline


def _role_attended(transport: Any, team: str, holders: list[str], *,
                   since: Any, budget: int = 40,
                   index: Optional[tuple[dict[str, Any], int, int, bool, bool]] = None,
                   deadline: Optional["Deadline"] = None,
                   ) -> tuple[Optional[bool], int, int]:
    """Did any ``holders`` file a verdict since ``since``?

    Returns ``(attended, scanned, total)``. ``attended`` is None when the answer
    could not be established — an unreadable listing or a budget cut-off — never
    False, because "I did not look at everything" is not "nobody worked".

    Verdict filenames are ``<head>--<reviewer>.md``, so the reviewer identity is
    in the name and a listing answers the question without reading any file.
    Bounded and reported (``scanned N/M``) per the budgeted-fold rule: an
    unbounded scan on a big team is how a status verb becomes a two-minute call.
    """
    if not holders:
        return None, 0, 0
    authority = _PUBLIC_READ_CONTEXT.get()
    if authority is not None:
        # The review projection proves current tally state, but it does not
        # carry per-reviewer verdict timestamps.  Reopening the live review tree
        # would mix a later observation into the sealed decision.  Distinguish
        # this checked-but-unprovable result from the NOT_RUN default.
        rows = authority.section("reviews").get("rows")
        total = len(rows) if isinstance(rows, list) else 0
        return None, min(total, budget), total
    # ``index`` lets a caller with MANY roles pay the scan ONCE (see
    # `_verdict_activity_index`). Without it, behaviour is exactly as before.
    if index is None:
        index = _verdict_activity_index(transport, team, budget=budget,
                                        deadline=deadline)
    idx, scanned, total, undatable, _cut = index
    if not total:
        # NOTHING to scan. A complete sweep of an empty set is not evidence of
        # absence — it is evidence we looked somewhere with no data (a wrong
        # prefix looks exactly like this). UNKNOWN, never False.
        return None, 0, 0
    for h in holders:
        if not h:
            continue
        mt = idx.get(str(h))
        if mt is not None and mt >= since:
            return True, scanned, total
    if undatable:
        # A verdict we could not DATE, or a prefix we could not READ. Either way
        # we cannot rule out that a holder worked. UNKNOWN, never False.
        return None, scanned, total
    # Everything we were allowed to look at is clean. Only a COMPLETE sweep can
    # say "no work"; a truncated one stays UNKNOWN.
    return (False if scanned >= total else None), scanned, total


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


def _escalation_delivery_path(team: str, role: str, date: str) -> str:
    """Where DELIVERY is recorded, separately from whether the task exists.

    codex-reviewer, 682 r1: keying redelivery on the task document made the
    first failure PERMANENT. The mint branch runs only when the doc is absent,
    so a sweep that wrote the doc but could not emit (no bus config, a failed
    record write, a crash between the two) left the daily marker in place — and
    every later sweep short-circuited on that marker and never reached the emit
    again. The vacancy stayed invisible to every fold even after the bus
    recovered, which is precisely the incident the emit was added to close.

    Document existence answers "was this escalated today". It cannot answer "did
    anyone's fold hear about it". Two questions, two records.

    Deliberately NOT under ``roles/<role>/escalations/``: that prefix is the
    suppressor, and the closed-loop path leaves it empty on purpose so an
    undeliverable notice re-surfaces every sweep. A delivery record living there
    would be one refactor away from being read as a suppressor.
    """
    return f"team/{team}/_coord/bus-v3/escalation-delivery/{role}/{date}.json"


def _read_escalation_delivery(transport: Any, team: str, role: str,
                              date: str) -> Optional[dict[str, Any]]:
    """Delivery state, or None when it was never recorded.

    None is UNKNOWN, never "delivered": a marker written by an engine older than
    this one has no delivery record, and treating that silence as success would
    reintroduce the permanent loss by a different door.
    """
    raw = transport.read(_escalation_delivery_path(team, role, date))
    if raw is None:
        return None
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None
    # STRICT EVIDENCE SCHEMA (codex-reviewer, 682 r2). Callers ask this marker
    # whether delivery is CONFIRMED, and they used to ask it by truthiness — so
    # `{"delivered": "false"}`, a non-empty string, read as delivered, printed
    # "event delivered", returned rc 0, and permanently suppressed the retry
    # with no event anywhere. That is the same permanent-loss failure this file
    # has now closed twice, reached through a third door: a malformed record
    # accepted as proof.
    #
    # A marker is EVIDENCE, so it is validated like evidence: `delivered` must
    # be a real boolean (note `isinstance(1, bool)` is False, so a stray int is
    # rejected too), and a marker claiming delivery must name the slug it
    # delivered and the agent it reached. Anything malformed, partial, or
    # unexpected is UNKNOWN — which means redelivery runs. Failing toward a
    # duplicate event is noise; failing toward a silent drop is the incident.
    delivered = doc.get("delivered")
    if not isinstance(delivered, bool):
        return None
    for field in ("slug", "to"):
        value = doc.get(field)
        if value is not None and not (isinstance(value, str) and value):
            return None
    if delivered and not (isinstance(doc.get("slug"), str) and doc["slug"]
                          and isinstance(doc.get("to"), str) and doc["to"]):
        return None
    return doc


def _write_escalation_delivery(transport: Any, team: str, role: str, date: str,
                               *, slug: str, to: str, delivered: bool) -> None:
    """Record delivery state. Best-effort: a failure here costs a duplicate
    event on a later sweep, which is noise — the alternative, a lost vacancy,
    is not."""
    try:
        transport.write(_escalation_delivery_path(team, role, date),
                        json.dumps({"slug": slug, "to": to,
                                    "delivered": bool(delivered)},
                                   sort_keys=True))
    except Exception:
        pass


def _role_liveness_fact(
    transport: Any,
    team: str,
    role: str,
    leases: Optional[list[dict[str, Any]]],
    *,
    status: str,
    now: str,
) -> dict[str, Any]:
    """Seal lease assignment and holder presence as one auditable fact.

    Presence never grants a role, so lease status remains the fact's state.
    It does, however, independently show whether a stale holder is alive. Both
    observations and their store prefixes travel together so a consumer cannot
    turn a fresh holder plus stale lease into the stronger claim ``VACANT``.
    """
    holders = [str(lease.get("agent")) for lease in (leases or [])
               if lease.get("agent")]
    shards, roster_ok = _presence_shards_status(transport, team)
    holder_shards = [shard for shard in shards
                     if str(shard.get("agent") or "") in holders]
    if not roster_ok:
        presence_state = "UNKNOWN"
        fact_state = roles.UNKNOWN
    elif not holder_shards:
        presence_state = "absent"
        fact_state = status
    else:
        observations = [presence.liveness(shard, now=now) for shard in holder_shards]
        freshness = [str(item.get("freshness") or "UNKNOWN") for item in observations]
        presence_state = (
            "live" if "live" in freshness
            else "idle" if "idle" in freshness
            else "stale" if "stale" in freshness
            else "UNKNOWN"
        )
        fact_state = roles.UNKNOWN if presence_state == "UNKNOWN" else status
    return {
        "state": fact_state,
        "observations": {
            "lease": {"state": status, "holders": holders},
            "presence": {"state": presence_state, "holders": [
                str(shard.get("agent")) for shard in holder_shards
            ]},
        },
        "provenance": [
            _leases_prefix(team, role),
            _presence_prefix(team),
        ],
    }


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
    if status in (roles.VACANT, roles.LAPSED) and dormant:
        # A deliberately-parked role reads DORMANT whether its last holder's
        # lease merely lapsed or there was never one — the park is the operator's
        # statement either way, and it outranks both.
        status = roles.DORMANT
    today = _now().strftime("%Y-%m-%d")
    marker_exists = transport.read(_escalation_marker_path(team, role, today)) is not None
    # ATTENDANCE (opt-in): a lapsed lease is not proof nobody is doing the job.
    # Default None = NOT CHECKED, which still escalates but must never be
    # reported as "unattended" — see roles.escalation_due.
    attended: Optional[bool] = None
    att_scanned = att_total = 0
    if getattr(args, "check_attendance", False):
        anchor = roles._parse(now)
        if anchor is not None:
            attended, att_scanned, att_total = _role_attended(
                transport, team, [l.get("agent") for l in (leases or [])],
                since=anchor - timedelta(hours=sla))
    if not getattr(args, "check_attendance", False):
        attendance = {
            "state": "NOT_RUN", "scanned": 0, "total": 0,
            "reason": "--check-attendance not requested",
        }
    elif attended is None:
        attendance = {
            "state": "UNKNOWN", "scanned": att_scanned, "total": att_total,
            "reason": ("attendance check budget-truncated"
                       if att_total and att_scanned < att_total
                       else "attendance check inconclusive"),
        }
    else:
        attendance = {
            "state": "DATA", "scanned": att_scanned, "total": att_total,
            "attended": attended,
        }
    esc = roles.escalation_due(leases, now=now, sla_hours=sla,
                               marker_exists_today=marker_exists, dormant=dormant,
                               attended=attended)
    fresh = roles.fresh_holders(leases, now=now, sla_hours=sla) if leases else []
    liveness_fact = _role_liveness_fact(
        transport, team, role, leases, status=status, now=now,
    )
    result = {
        "team": team, "role": role, "status": status, "policy": policy, "sla_hours": sla,
        "contract": 2,
        "holders": [l.get("agent") for l in (leases or [])],
        "fresh_holders": [l.get("agent") for l in fresh],
        "escalation_due": esc,
        "attended": attended,
        "attendance_scanned": f"{att_scanned}/{att_total}" if att_total else None,
        "attendance": attendance,
        "liveness_fact": liveness_fact,
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
        if attended is True:
            print(f"  LEASE LAPSED, ROLE IS BEING SERVED — a holder filed a verdict "
                  f"within {sla:g}h (scanned {att_scanned}/{att_total} reviews). "
                  f"Ask for a lease renewal; do NOT escalate as unattended.")
        if esc:
            if attended is False:
                print("  ESCALATION DUE — UNATTENDED: lease lapsed past SLA and no "
                      f"holder work product found (scanned {att_scanned}/{att_total}).")
            else:
                print("  ESCALATION DUE — lease not renewed past SLA, no marker "
                      "today. ATTENDANCE NOT CHECKED: this says the LEASE lapsed, "
                      "not that nobody is working. Re-run with --check-attendance "
                      "before calling a role unattended.")
    if (status == roles.UNKNOWN or liveness_fact["state"] == roles.UNKNOWN
            or attendance["state"] == "UNKNOWN"):
        # FAIL CLOSED (2026-07-11): the lease listing was unreadable, so the role's
        # state is UNKNOWN — NOT vacant. A degraded transport must not let a caller
        # read this as VACANT and fire a false SLA escalation. rc 1, same register
        # as `review status`'s "tally unknown" (leases dropped/None never asserts).
        if attendance["state"] == "UNKNOWN":
            print(f"attendance state unknown for role {role} in team/{team} — "
                  f"checked but incomplete, retry after reconcile", file=sys.stderr)
            return 3
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
    prior = transport.read(path)
    before = _blocked_on_of(prior)
    try:
        out = tasks.apply_update(
            prior, now=_iso(_now()), status=args.status, summary=args.summary,
            next_action=args.next, assignee=args.assignee, blocked_on=args.blocked_on,
            priority=args.priority, evidence=args.evidence,
        )
    except tasks.TaskError as e:
        print(f"task update failed: {e}", file=sys.stderr)
        return 1
    written = transport.write(path, out)
    if written:
        _emit_blocked_signal(args, transport, before=before,
                             after=_blocked_on_of(out), doc=out)
    if written and args.status in tasks.TERMINAL_STATUSES:
        # `update --status done/abandoned` is a close like any other (round 3):
        # it must emit, or the stream replays the obligation open forever.
        _emit_task_close(args, transport, doc=out)
    print(f"updated {args.name}" + (f" → {args.status}" if args.status else ""))
    return 0


def _task_apply(args, transport, *, close_event: bool = False, **kw) -> int:
    """Shared read-modify-write for the dedicated lifecycle verbs.

    ``close_event=True`` marks a TERMINAL transition (supersede, abandon):
    after the write lands, the close is emitted to the stream naming the
    ASSIGNEE it discharges — round 3 (2026-08-21): these verbs closed the doc
    and emitted nothing, so the assignee's stream fold replayed the obligation
    open forever, and the sender-attributed close could never have worked
    anyway because the closer is usually not the assignee.
    """
    path = _task_path(args.team, args.name)
    prior = transport.read(path)
    before = _blocked_on_of(prior)
    try:
        out = tasks.apply_update(prior, now=_iso(_now()), **kw)
    except tasks.TaskError as e:
        verb = getattr(args, "verb", getattr(args, "task_command", "update"))
        print(f"task {verb} failed: {e}", file=sys.stderr)
        return 1
    written = transport.write(path, out)
    if written:
        # Covers `task block`, `task unblock` and every other lifecycle verb
        # routed here — the signal must not depend on which verb was used.
        _emit_blocked_signal(args, transport, before=before,
                             after=_blocked_on_of(out), doc=out)
    if close_event and written:
        _emit_task_close(args, transport, doc=out)
    print(f"{getattr(args, 'verb', 'updated')} {args.name}")
    return 0



def _blocked_on_of(doc: Optional[str]) -> str:
    """The ``blocked_on`` value of a task doc, normalized for comparison."""
    if not doc:
        return ""
    fm = okf.parse_frontmatter(doc) or {}
    return str(fm.get("blocked_on") or "").strip()


def _emit_blocked_signal(args, transport, *, before: str, after: str,
                         doc: str) -> None:
    """Announce a change in what a task waits on. Best-effort, never fatal.

    THE POINT OF THIS FUNCTION. ``blocked_on`` was read by seven modules and
    announced by none, so every consumer that wanted "what is blocked, and on
    whom" had to enumerate the whole task corpus — the fold-by-enumeration the
    stream architecture rejects. In practice that meant only one bespoke
    tracker bridge ever surfaced it, and only into one tracker. As an event it
    is available to anything reading the bus forward from a cursor: a tracker
    projection, a dashboard, a phone notification, a peer agent across the
    mesh, or the blocker themselves.

    Addressed to the BLOCKER (``to``), not to the assignee: the whole value is
    telling whoever is holding something up that they are. Both directions are
    emitted — a new block and a clear — because a block announced but never
    retracted leaves every downstream queue growing forever.
    """
    if before == after:
        return
    fm = okf.parse_frontmatter(doc) or {}
    target = after or before
    if not target:
        return
    state = "blocked" if after else "cleared"
    # `user:<name>` is the typed human form; anything else is an agent or role
    # name. Strip the prefix for addressing, keep it raw in `on` so a consumer
    # applies its own classifier rather than inheriting ours.
    to = target.split()[0] if target.split() else target
    if to.lower().startswith("user:"):
        to = to[len("user:"):] or to
    cfg, _status = records.load_config_classified(transport, args.team)
    if not cfg:
        return
    try:
        records.emit_event(
            transport, cfg, sender=_identity(getattr(args, "agent", None)),
            to=to, kind="blocked", priority=str(fm.get("priority") or "P2"),
            slug=args.name, ptr=f"task/{args.name}.md",
            on=(after or before), state=state, team=args.team)
    except Exception as e:
        # The doc is the truth and the event is delivery: a bus that is down
        # degrades latency, never the record of what is blocked.
        print(f"record: blocked signal not emitted ({e}) — the block rides the "
              f"file plane only", file=sys.stderr)


def _emit_task_close(args, transport, *, doc: str) -> None:
    """Emit the stream close for a terminal doc transition, best-effort."""
    fm = okf.parse_frontmatter(doc) or {}
    responder = _identity(getattr(args, "agent", None))
    _emit_response_companion(
        transport, args.team, slug=args.name,
        owner=str(fm.get("owner") or ""),
        responder=responder,
        shard_ptr=f"task/{args.name}.md",
        for_agent=str(fm.get("assignee") or "") or responder)


def cmd_task_block(args: argparse.Namespace, transport: Any) -> int:
    if not args.blocked_on and not args.on_user:
        print("task block failed: requires --blocked-on or --on-user", file=sys.stderr)
        return 1
    if args.blocked_on and args.on_user:
        print("task block failed: pass --blocked-on OR --on-user, not both", file=sys.stderr)
        return 1
    if not args.unlock and not args.on_user:
        # D4 (respec 2026-07-28): an agent-blocked item without a named unlock
        # is malformed — the sweep can chase "who" but not "what would clear
        # it", and unlock-less blocks rot. Rejected at write time. `--on-user`
        # asks are exempt: the ask itself is the unlock (auto-derived below).
        print("task block failed: --unlock <what specifically unblocks this> "
              "is required with --blocked-on (name the concrete unlock, not "
              "just the blocker)", file=sys.stderr)
        return 1
    # TYPE the human block: `--on-user <name>` writes `blocked_on: user:<name>` so
    # the blocked-on-human fold can classify it at ZERO transport cost (a plain
    # value would need an agent/role lookup to tell human from agent). Additive:
    # `--blocked-on <agent>` stays an untyped agent value, and legacy `user:`-less
    # rows still parse (the fold's legacy branch handles them).
    blocked_val = f"{query._USER_PREFIX}{args.on_user}" if args.on_user else args.blocked_on
    unlock_val = args.unlock or (f"answer from {args.on_user}" if args.on_user else None)
    kw = {"status": "blocked", "blocked_on": blocked_val, "unlock": unlock_val}
    if args.on_user:
        kw["assignee"] = _human()
        kw["add_tags"] = ["needs:human"]
    return _task_apply(args, transport, **kw)


def cmd_task_supersede(args: argparse.Namespace, transport: Any) -> int:
    """D3 (respec 2026-07-28): reassignment without closure is data loss with
    extra steps. Supersession closes the origin copy from ANY live state and
    names its successor — the dispatcher's duty made mechanical."""
    reason = args.reason or f"work re-dispatched as {args.by}"
    kw: dict[str, Any] = {"status": "done", "superseded_by": args.by,
                          "evidence": f"superseded by {args.by} ({reason})"}
    if getattr(args, "record", None):
        kw["superseded_record_id"] = args.record
    return _task_apply(args, transport, close_event=True, **kw)


def cmd_task_pause(args: argparse.Namespace, transport: Any) -> int:
    return _task_apply(args, transport, status="waiting", next_action=args.next)


def cmd_task_abandon(args: argparse.Namespace, transport: Any) -> int:
    return _task_apply(args, transport, close_event=True,
                       status="abandoned", evidence=args.reason)


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
        # ABSENT, not merely unreadable. This guard is the only thing standing
        # between a restore and overwriting a live hot-path doc, and
        # `transport.read` returns None for a 500 exactly as it does for a
        # missing file — so a transient read failure used to read as "the
        # destination is free". Measured 2026-09-04: an hour of HTTP 500s on
        # every path three levels deep, which is where task docs live.
        existing_dst, dst_status = transport.read_classified(dst)
        if dst_status == "error":
            print(f"restore refused: cannot READ {dst} to check whether it is "
                  f"occupied — that is UNKNOWN, not free, and a restore over a "
                  f"live doc is unrecoverable. Retry when the store answers.",
                  file=sys.stderr)
            return 3
        if existing_dst is not None:
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
        # A doc-less archive: verdict shards with no request doc, so the family
        # path above cannot run. Exactly ONE shard is restorable here; the count
        # is a deliberate risk bound, not an accident, because a restore with no
        # doc recreates an ORPHAN review dir (verdicts, no doc) and doing that
        # for N shards at once makes a bigger claim than this verb can justify.
        #
        # The NAME was hardcoded to one team's reviewer (`codex-reviewer.md`),
        # which made `review restore` work for exactly one agent on exactly one
        # team and fail with "unexpected archived verdict shape" for everyone
        # else. Team-particular content does not belong in this repo — the shape
        # is "one shard", and whose shard it is was never the engine's business.
        # APPEND-ONLY FAMILIES LAND HERE BY DESIGN (coord-boss constraint 5,
        # ruling b99fb8da): a reviewer may now hold several shards for one head,
        # so an archived family of >1 is legitimate rather than corrupt. This
        # path is the COUNTED SKIP, not a failure of the new shape — it says how
        # many it found and points at the doc-restore route, which handles a
        # family. Stated rather than changed, deliberately.
        if len(files) != 1:
            print(f"review restore failed: {args.slug} has {len(files)} archived "
                  f"verdict shard(s) and no request doc. Exactly one is "
                  f"restorable without a doc; restore the doc alongside them to "
                  f"take the family path.", file=sys.stderr)
            return 1
        filename = files[0]
        src = cold_prefix + filename
        dst = f"team/{args.team}/review/{args.slug}/verdicts/{filename}"
        # ABSENT, not merely unreadable. This fork guards a MOVE: read the
        # destination's 500 as "nothing there" and _crash_safe_move lands on
        # top of a live verdict shard. Same family as `task restore` above and
        # missed by the same audit that caught it -- found by collect-maintainer
        # while reviewing PR 694, in the stale-doctrine sweep rather than in the
        # caller audit, which is its own lesson about where the fifth one hides.
        existing_dst, dst_status = transport.read_classified(dst)
        if dst_status == "error":
            print(f"review restore failed: cannot READ {dst} to tell whether the "
                  f"hot path is occupied. That is UNKNOWN, not free, and this "
                  f"path MOVES -- refusing rather than risk landing on a live "
                  f"verdict shard.", file=sys.stderr)
            return 3
        if existing_dst is not None:
            print(f"review restore failed: {args.slug} already exists in the hot path",
                  file=sys.stderr)
            return 1
        if rec._crash_safe_move(transport, src, dst):
            print(f"restored review {args.slug} from reviews/{month}/")
            print(f"review restore: {args.slug} came back WITHOUT a request doc, "
                  f"so it is now an orphan (verdicts, no doc) and will surface "
                  f"as needing maintainer repair until a doc is written.",
                  file=sys.stderr)
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
    if not transport.write(path, out):
        # No ghost closes (round-2 finding 3): a failed durable write with an
        # emitted close event would tell the stream "closed" while the file
        # authority stays open. Emit nothing, say so, exit nonzero.
        print(f"task done failed: the durable close for {args.name} did not "
              f"land — no close event emitted, the task is still open",
              file=sys.stderr)
        return 3
    print(f"done {args.name}")
    # THE CLOSE MUST REACH THE STREAM (2026-08-21). A done task closed the DOC
    # but emitted nothing, so stream readers replayed it as open forever — 92 of
    # 121 stream-fold opens were closes that never became events. In the stream
    # architecture an unemitted event is a lie of omission: everything that
    # closes an obligation emits its close. Best-effort like every companion —
    # the doc is truth, the event is delivery.
    fm = okf.parse_frontmatter(out) or {}
    responder = _identity(getattr(args, "agent", None))
    _emit_response_companion(
        transport, args.team, slug=args.name,
        owner=str(fm.get("owner") or ""),
        responder=responder,
        shard_ptr=f"task/{args.name}.md",
        for_agent=str(fm.get("assignee") or "") or responder)
    return 0


def cmd_owed(args: argparse.Namespace, transport: Any) -> int:
    """Open obligations folded from the annotation stream. Zero enumeration.

    The stream-architecture sibling of `needs-me`: one bounded stream read
    forward from a durable cursor, cost proportional to new events. `needs-me`
    remains the file-plane authority until the cutover seed; both truthfully
    label their coverage so a reader can tell which world answered.
    """
    from . import stream_fold
    agent = _identity(getattr(args, "agent", None))
    out = stream_fold.fold(transport, args.team, agent)
    if getattr(args, "json", False):
        print(out.render_json())
    else:
        print(f"owed [{agent}] — {out.state.value}")
        for cov in out.coverage:
            print(f"  {cov.surface}: {cov.reason or cov.state.value}")
        for row in out.rows:
            # THE SLUG IS THE OPERAND (coord-maintainer, 2026-08-22): this line
            # cut slugs at 88 chars and the bus mints 89-char slugs for long
            # titles, so the cut removed the last character OF THE HASH SUFFIX
            # on 68 of 207 live rows, silently. A reader copying from the
            # surface we tell agents to use would paste a slug one character
            # short of what `--closes` requires. Never truncate an identifier.
            print(f"  [{row.get('pri')}] {row.get('at','')}  {row.get('slug','')}")
    return out.rc


# --- review (fulcra-agent-review verdict tally) ---

def _review_doc_path(team: str, slug: str) -> str:
    return f"team/{team}/review/{slug}.md"


def _verdicts_prefix(team: str, slug: str) -> str:
    return f"team/{team}/review/{slug}/verdicts/"


# Settled-skip: once a review reaches a terminal APPROVED state with no
# outstanding required reviewers, a tiny cache marker is dropped IN the verdicts
# prefix (so the ONE listing the fold already does reveals it — zero extra
# reads). It is not a `.md` file, so the verdict-reading loop already ignores it.
# CONTRACT: a settled ROUND is immutable. A new exact head advances the same PR
# slug and clears this marker; changing artifact/requester/required set still
# needs a NEW slug. The marker is a fold cache, never a source of truth:
# `review status` recomputes the active-head tally every time.
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
#: — the fold `briefing` / `inbox` / `needs-me` all run, i.e. every agent,
#: every tick. Its cost is 1 + sum(2 + lease_shards) over the roles the open work
#: references (see `_held_roles_for_rows`), and lease shards accumulate per claiming
#: agent forever (only `roles release` prunes one), so an unbudgeted pass could spend
#: one transport timeout per role doc, per lease listing AND per shard before the hot
#: path renders anything. 20s is a generous ~25 ops at the measured ~0.8s/op — far
#: past the 4-7 a real team pays — while still bounding a degraded transport.
DEFAULT_ROLE_FOLD_BUDGET = 20.0
#: Wall-clock bound for the shared verdict-activity scan (see
#: `_attendance_scan_budget`). 30s: the measured scan is ~24s on a slow transport
#: and must be able to COMPLETE — a bound tighter than the honest cost would just
#: convert a slow answer into a permanent UNKNOWN.
DEFAULT_ATTENDANCE_SCAN_BUDGET = 30.0

# The `threads` fold/window defaults (DEFAULT_THREADS_*) live with the threads
# command in `commands_threads.py`; they are re-exported onto `cli` at module end.


def _settled_marker_path(team: str, slug: str) -> str:
    return _verdicts_prefix(team, slug) + SETTLED_MARKER


def _clear_settled_marker(transport: Any, team: str, slug: str) -> bool:
    """Authoritatively clear a prior round's fold cache, failing closed.

    A head advance must become visible to ``needs-me`` before reviewers are
    notified. The transport operation is idempotent: only a successful deletion
    or the server's explicit exact-path-not-found response proves absence.
    """
    marker_path = _settled_marker_path(team, slug)
    return bool(
        hasattr(transport, "delete_idempotent")
        and transport.delete_idempotent(marker_path)
    )


def _review_fold_budget() -> float:
    """Aggregate deadline for `_pending_reviews_for`, seconds. Env
    ``COORD_REVIEW_FOLD_BUDGET`` (see the DEFAULT_REVIEW_FOLD_BUDGET rationale)."""
    return config.env_float("COORD_REVIEW_FOLD_BUDGET", DEFAULT_REVIEW_FOLD_BUDGET)


#: Aggregate deadline for `presence show`'s work-evidence scan, seconds.
#: `presence show` is a DIRECT command with no surrounding add-on stack to
#: borrow a deadline from, so it opens its own. 20s is deliberately below the
#: review-fold budget: this scan lists every review's `verdicts/` directory (435
#: on the live store today) plus each agent's reports, and it is decorating a
#: roster — a slow truthful answer is worse here than a fast one that says which
#: part it did not reach, which the PARTIAL rendering does.
DEFAULT_PRESENCE_WORK_BUDGET = 20.0


def _presence_work_budget() -> float:
    """Deadline for the `presence show` work-evidence scan, seconds. Env
    ``COORD_PRESENCE_WORK_BUDGET`` (see DEFAULT_PRESENCE_WORK_BUDGET)."""
    return config.env_float("COORD_PRESENCE_WORK_BUDGET",
                            DEFAULT_PRESENCE_WORK_BUDGET)


def _briefing_budget() -> float:
    """Shared aggregate deadline (seconds) for the briefing/needs-me add-on stack.
    Env ``COORD_BRIEFING_BUDGET`` (see the DEFAULT_BRIEFING_BUDGET rationale). One
    absolute ``time.monotonic()`` deadline is computed where the stack opens and
    passed to each transport-heavy section, so an earlier section's spend shrinks
    what the next one gets; pending-reviews keeps its own independent
    ``COORD_REVIEW_FOLD_BUDGET`` (whichever bound is sooner wins)."""
    return config.env_float("COORD_BRIEFING_BUDGET", DEFAULT_BRIEFING_BUDGET)


#: Deadline (seconds) for the obligation fold's PROBES. Setup is measured
#: separately and does not draw on this, so the two can no longer starve each
#: other. Env ``COORD_OBLIGATION_BUDGET``.
#:
#: 90, and the number carries a warning. At 20 the fold was measured blind on
#: three hosts (0/7, 0/7, 1/7 components consulted) while 110, 64 and 6 owed
#: items sat hidden behind a blanket UNKNOWN. 90 is the smallest value OBSERVED
#: TO HELP — **it is a floor, not a proven ceiling**: at 90 `role_duties` still
#: reports UNREADABLE on at least one host, and `role_duties` is where
#: role-routed obligations live.
#:
#: So do not read 90 as "enough". The real cost is `_held_roles_for_rows` at
#: 19.3s of a 26.1s setup; lowering that is the actual fix and it helps every
#: caller. Raising a budget to outrun a cost is the move that produced the
#: original collapse, and this raise is deliberately the stopgap half of a
#: two-part ruling (coord-boss, 2026-08-07) — chosen first only because the
#: default ships in the engine PIN, and the pin is the one channel that reaches
#: a host whose environment is rebuilt every wake and therefore cannot hold an
#: env-var mitigation at all.
DEFAULT_OBLIGATION_BUDGET = 90.0


def _obligation_budget() -> float:
    return config.env_float("COORD_OBLIGATION_BUDGET", DEFAULT_OBLIGATION_BUDGET)


def _attendance_scan_budget() -> float:
    """Wall-clock bound (seconds) for the ONE shared verdict-activity scan an
    `escalate` sweep performs. Env ``COORD_ATTENDANCE_SCAN_BUDGET``.

    Its own knob because the scan is transport-bound fan-out, not role
    resolution: measured 2026-08-08, 41 sequential listings at ~0.59s each. A
    count budget alone cannot bound that — which is how `escalate` reached 98s
    locally and timed out at 170s+ on the watchdog."""
    return config.env_float("COORD_ATTENDANCE_SCAN_BUDGET",
                            DEFAULT_ATTENDANCE_SCAN_BUDGET)


def _role_fold_budget() -> float:
    """Cumulative deadline (seconds) for one role-resolution pass. Env
    ``COORD_ROLE_FOLD_BUDGET`` (see the DEFAULT_ROLE_FOLD_BUDGET rationale). Its own
    knob, like ``COORD_REVIEW_FOLD_BUDGET``: role resolution runs BEFORE the
    briefing/needs-me add-on stack opens its budget (the held set is an input to the
    inbox fold, not an add-on section), so it cannot spend that one."""
    return config.env_float("COORD_ROLE_FOLD_BUDGET", DEFAULT_ROLE_FOLD_BUDGET)


#: Tri-state classification of a `.settled` marker, because the two-state answer
#: (delete / keep) silently conflated "known cache" with "cannot tell".
SETTLED_CACHE = "cache"      #: an APPROVED tally cache — recomputable, safe to drop
SETTLED_MERGED = "merged"    #: merge evidence — a PR landed; nothing recomputes it
SETTLED_UNKNOWN = "unknown"  #: unreadable or unrecognised — preserve AND say so


def _settled_marker_present(transport: Any, team: str, slug: str) -> Optional[bool]:
    """Does a ``.settled`` OBJECT exist? ``True`` / ``False`` / ``None`` = UNKNOWN.

    `read` returning None conflates "absent" with "unreadable", and the UNKNOWN
    branch needs them apart: an ABSENT marker is nothing to warn about, an
    UNREADABLE one is. A listing answers presence without parsing.

    TRI-STATE, because the previous two-state version returned False on a raised
    listing — so an unreadable marker plus an unreadable listing produced a
    silent rc 0 exactly where this code promises to fail closed. That is the
    UNKNOWN-collapsed-to-a-definite-answer bug, inside the helper written to
    prevent it.
    """
    try:
        names = {(e.get("name") or "") for e in
                 transport.list_dir(_verdicts_prefix(team, slug))}
    except TransportError:
        return None
    return SETTLED_MARKER in names


def _classify_settled_marker(transport: Any, team: str, slug: str) -> str:
    """``cache`` / ``merged`` / ``unknown`` for a slug's ``.settled``.

    Both recognised shapes must be POSITIVELY identified — by schema AND the
    fields that give them meaning — not by one surviving field:

    - ``cache``  — ``schema: review-settled/v1`` AND ``state: APPROVED``. A
      truncated or hand-edited marker that kept only ``state: APPROVED`` is NOT
      a cache; treating it as one deletes a file we do not understand.
    - ``merged`` — that SAME schema AND ``state: MERGED`` AND a well-formed
      ``merge_sha``. MERGED without a sha is INCOMPLETE evidence: preserved like
      any unknown, but reported, because silently accepting it would let a
      half-written marker pass as proof a PR landed.
    - ``unknown`` — everything else, including unreadable and absent.

    The schema gate is checked ONCE, ahead of both shapes, and that placement is
    the fix rather than a matter of style. The r3 version demanded a schema for
    APPROVED and admitted MERGED on ``state`` plus a sha, so a truncated marker
    that lost its schema line still classified as merge evidence and returned a
    clean rc — the guard covered the direction I was thinking about and left its
    neighbour. Both writers emit ``review-settled/v1``, so requiring it of both
    rejects nothing this code produces; and an unrecognised FUTURE schema now
    lands in ``unknown``, which preserves the file, instead of being read by
    rules written before it existed.

    Only ``cache`` is deletable. Every other answer preserves the file.
    """
    raw = transport.read(_settled_marker_path(team, slug))
    if raw is None:
        return SETTLED_UNKNOWN
    fm = okf.parse_frontmatter(raw) or {}
    if str(fm.get("schema") or "") != "review-settled/v1":
        return SETTLED_UNKNOWN
    state = str(fm.get("state") or "")
    if state == "MERGED":
        sha = str(fm.get("merge_sha") or "")
        return SETTLED_MERGED if _MERGE_SHA.match(sha) else SETTLED_UNKNOWN
    if state == review.APPROVED:
        return SETTLED_CACHE
    return SETTLED_UNKNOWN


def _write_settled_marker(transport: Any, team: str, slug: str, *, now: str,
                          evidence: Optional[str] = None) -> str:
    """Best-effort settled-cache write. Failure is swallowed: the marker only
    speeds the fan-out fold; its absence just means the next fold recomputes.

    NEVER overwrites MERGE EVIDENCE. PR 572 stopped `review status` from
    DELETING a `.settled` that records a merge, and left this WRITE path
    unguarded — so a settleable tally would happily stamp the recomputable
    APPROVED cache straight over `state: MERGED` + `merge_sha`. Found in
    production by coord-boss: they closed 585 with a sha at ~01:20, and the
    `review status` I ran at ~01:22 to CHECK the closure is what destroyed it.
    Same bug as 572 wearing a smaller hat, and my own guard covered the
    direction I was thinking about and left its neighbour — again.

    Refusing costs nothing real. The cache exists so the fan-out fold can skip
    the slug; a MERGED marker ALREADY makes it skip. Overwriting buys no speed
    and loses the only durable record that the PR landed.

    UNBOUND EVIDENCE IS NOT WRITTEN AT ALL. An empty digest means the caller
    could not fingerprint this directory — a mutable plain shard participates —
    and every reader refuses such a marker, so writing one is cost with no
    reader (codex-reviewer, 595 r5).
    """
    if not evidence:
        return "unbound-evidence"
    try:
        # Overwrite ONLY a positively-identified CACHE, or a positively ABSENT
        # marker. Everything else is preserved.
        #
        # r1 of this fix guarded MERGED and left SETTLED_UNKNOWN clobberable —
        # even though the classifier's whole contract is that only `cache` is
        # disposable, precisely because an unreadable / unrecognised /
        # FUTURE-schema marker is one this build cannot prove is ours to drop.
        # codex-reviewer reproduced it with a `review-settled/v2` marker
        # carrying a real merge sha: classified UNKNOWN, then overwritten by the
        # v1 APPROVED cache. Guarded the case I was thinking about and left its
        # neighbour — the same shape as the delete-vs-write split this PR exists
        # to close, one level in.
        #
        # The absence check costs ONE listing, and only when the read came back
        # empty. A slug settles once and is skipped by the fold forever after,
        # so that is one listing per slug lifetime, not per fold.
        state = _classify_settled_marker(transport, team, slug)
        if state != SETTLED_CACHE:
            if state != SETTLED_UNKNOWN:
                return "kept-merged"
            if _settled_marker_present(transport, team, slug) is not False:
                # Present-but-unclassifiable, or presence itself unknown. Either
                # way we cannot prove it is a cache, so it stays.
                return "kept-unknown"
        transport.write(
            _settled_marker_path(team, slug),
            okf.render_frontmatter(review.settled_marker_fields(
                state=review.APPROVED, ts=now, evidence=evidence)),
        )
        return "written"
    except Exception:
        return "unknown-error"


#: A merge sha is EVIDENCE, so it must look like one. Closure that accepts an
#: arbitrary string is closure by assertion, which is the inference this verb
#: exists to replace.
_MERGE_SHA = re.compile(r"\A[0-9a-f]{40}\Z|\A[0-9a-f]{64}\Z")


def cmd_review_conclude(args: argparse.Namespace, transport: Any) -> int:
    """Mark a row CONCLUDED: its review finished, but no merge evidence exists.

    The row this exists for has a verdict on file and an unbound head — the
    reviewer did the work, and there is no merge sha to bind a closure to. Such
    rows sat in the register forever, and `unknown=82` beside `live=41` made the
    board read twice as busy as it was, in the fold reviewers use to decide what
    they owe.

    IT IS NOT `.settled`, AND MUST NEVER BECOME IT. `.settled` asserts
    APPROVED-with-bound-evidence. Widening it to swallow these rows would launder
    evidence-free rows into evidence-bearing state — the busy-register problem
    does not justify weakening the one marker that carries a claim.

    A verdict must actually be on file. Concluding a row nobody reviewed is
    abandonment wearing a completion label, and that is a different act needing a
    different word.
    """
    prefix = _verdicts_prefix(args.team, args.slug)
    try:
        names = {(e.get("name") or "") for e in transport.list_dir(prefix)}
    except TransportError:
        print(f"review conclude: cannot list {args.slug} — refusing to write a "
              f"terminal marker on an unreadable row", file=sys.stderr)
        return 3
    if review_gc.is_terminal(names) or SETTLED_MARKER in names:
        print(f"review conclude: {args.slug} is already terminal — left alone")
        return 0

    # ELIGIBILITY IS CHECKED, NOT ASSUMED (codex-reviewer, 643 r1).
    # The marker's entire meaning is "head unbound". The first cut never read the
    # review doc, so it asserted the one condition that defines it — and an
    # ACTIVE v2 review could be hidden behind a terminal marker.
    doc = transport.read(_review_doc_path(args.team, args.slug))
    if doc is None:
        print(f"review conclude: cannot read the review doc for {args.slug} — "
              f"eligibility unverifiable, refusing", file=sys.stderr)
        return 3
    fm = okf.parse_frontmatter(doc) or {}
    head = (review.normalize_head(fm.get("head"))
            or review_gc.head_from_prose(fm.get("of")))
    if head:
        print(f"review conclude: {args.slug} has a BOUND head ({head[:12]}) — "
              f"this verb is only for rows no closure can bind evidence to; "
              f"use `review close` with the merge sha, or leave it to gc",
              file=sys.stderr)
        return 2

    # A FILENAME IS NOT REVIEW EVIDENCE (codex-reviewer, 643 r2).
    #
    # Round 1 checked nothing. Round 2 checked the NAME — rejecting head-scoped
    # shards but accepting every other `*.md`, so an unrelated `notes.md`
    # authorized a terminal marker on a row nobody had reviewed; codex
    # reproduced exactly that. Each candidate is now READ and PARSED: it must
    # carry a recognized verdict and name a reviewer, under the legacy
    # unbound-head naming rule.
    #
    # Fails CLOSED. A candidate we cannot read or parse is not evidence of a
    # review, and this is the write that ends a row's life.
    # HEAD-SCOPED VERDICTS COUNT ON AN UNBOUND REVIEW (coord-boss ruling
    # `three-rulings-your-withdrawal-is-half-right-and-i-measured-which-half-hold-the-b-804e53c7`).
    # codex-reviewer's old-head guard is SUPERSEDED FOR UNBOUND ROWS ONLY; it
    # keeps its teeth on bound rows and its reproduction still asserts there.
    # The gate used to ask `parse_verdict_filename(name, head=None)`, which
    # returns None for a name that CARRIES a head — so it failed closed on MORE
    # information than it asked for, and called a review with five verdicts
    # "abandonment, no applicable verdict". A verdict naming the exact head it
    # reviewed is strictly better evidence than one naming nothing; refusing it
    # punished the reviewer for being more precise than the request.
    #
    # This widens which NAMES are candidates. It does not weaken 643 r2: every
    # candidate is still READ, must still carry a recognized verdict and a
    # reviewer, and an unrelated `notes.md` is still rejected — by its contents,
    # exactly as before.
    verdicts: list[str] = []
    unreadable: list[str] = []
    heads_seen: list[str] = []
    for name in sorted(n for n in names if n.endswith(".md")):
        parsed = review.parse_verdict_filename(name)
        name_head = None
        if parsed is None:
            stem = name[:-3]
            name_head = (review.normalize_head(stem.split("--", 1)[0])
                         if "--" in stem else None)
            if name_head:
                parsed = review.parse_verdict_filename(name, head=name_head)
        if parsed is None:
            continue                      # not a verdict name in either form
        reviewer = parsed[0]
        raw = transport.read(prefix + name)
        if raw is None:
            unreadable.append(name)
            continue
        vfm = okf.parse_frontmatter(raw) or {}
        if review.normalize_verdict(vfm.get("verdict")) is None:
            continue                      # not a verdict document
        if not (vfm.get("reviewer") or reviewer):
            continue                      # no reviewer identity to credit
        verdicts.append(name)
        # RECORD THE HEAD WE ACTUALLY FOUND. The document is the evidence, so
        # its own `head:` wins; the filename is only the fallback. Without this
        # the conclusion says "concluded on N verdicts" and names nothing an
        # auditor could check it against.
        vhead = review.normalize_head(vfm.get("head")) or name_head
        if vhead and vhead not in heads_seen:
            heads_seen.append(vhead)

    if unreadable:
        print(f"review conclude: {len(unreadable)} candidate shard(s) in "
              f"{args.slug} could not be READ ({', '.join(unreadable)}) — "
              f"UNKNOWN is not evidence; refusing", file=sys.stderr)
        return 3
    if not verdicts:
        others = sorted(n for n in names if n.endswith(".md"))
        detail = (f" ({len(others)} .md file(s) present, none a parseable "
                  f"verdict for an unbound head)" if others else "")
        print(f"review conclude: {args.slug} has NO applicable verdict on file"
              f"{detail} — that is abandonment, not conclusion; refusing",
              file=sys.stderr)
        return 2
    body = okf.render_frontmatter({
        "schema": "review-concluded/v1",
        "state": "CONCLUDED",
        "verdicts": sorted(verdicts),
        **({"heads": sorted(heads_seen)} if heads_seen else {}),
        "closed_by": args.sender or _human(),
        "reason": args.reason or ("review concluded; head unbound and no merge "
                                  "evidence available to bind a closure to"),
        "ts": _iso(_now()),
    })
    # A DURABLE STATE TRANSITION IS NOT A PRINT STATEMENT (codex-reviewer, 643
    # r1). The first cut ignored the write's return and never read it back, so a
    # transport returning False printed CONCLUDED with rc 0 and stored nothing —
    # a terminal state that exists only in the log.
    path = prefix + review_gc.CONCLUDED_MARKER
    if transport.write(path, body) is False:
        print(f"review conclude: the transport REFUSED the marker write for "
              f"{args.slug} — nothing was recorded", file=sys.stderr)
        return 3
    if transport.read(path) is None:
        print(f"review conclude: wrote the marker for {args.slug} but could not "
              f"read it back — treat this row as NOT concluded and retry",
              file=sys.stderr)
        return 3
    print(f"review conclude: {args.slug} CONCLUDED on {len(verdicts)} verdict(s)")
    _close_review_request_rows(
        transport, args.team, args.slug,
        why=(f"review concluded on {len(verdicts)} verdict(s); terminal marker "
             f"{review_gc.CONCLUDED_MARKER} written and read back"),
        agent=getattr(args, "sender", None))
    return 0


def _close_review_request_rows(transport: Any, team: str, slug: str, *,
                               why: str, agent: "Optional[str]") -> None:
    """Close the review-request rows for a slug whose review just concluded.

    SETTLE-TIME CLOSE. The divergence between a finished review and an open
    request row is what the residue sweep exists to clean up; the cheapest place
    to never open it is here, the moment a DECISION verb records a terminal
    marker. Only the decision verbs call this. The fold and projection settle
    writers are CACHES in build paths that must not mutate task state, so their
    rows stay the scheduled sweep's job.

    Best-effort and LOUD. The marker is the durable truth and the rows are
    bookkeeping, so a failure here must not fail the closure the caller just
    verified — but a SILENT failure rebuilds the exact backlog this exists to
    prevent, so every failure prints and names the recovery.
    """
    from . import model

    try:
        rows, ok, reason = _load_rows_status(transport, team)
    except TransportError as e:
        print(f"review rows unreadable ({type(e).__name__}) — {slug} is closed "
              f"but its request row(s) are NOT; run `review residue {team}` later",
              file=sys.stderr)
        return
    if not ok:
        print(f"review rows DEGRADED ({reason}) — {slug} is closed but its "
              f"request row(s) are NOT; run `review residue {team}` later",
              file=sys.stderr)
        return

    wanted = _REVIEW_REQUEST_TITLE_PREFIX + slug
    closed = failed = 0
    for r in rows or []:
        if str(r.get("title") or "").strip() != wanted:
            continue
        if r.get("status") not in model.OPEN_STATUSES:
            continue
        ns = argparse.Namespace(team=team, name=str(r.get("id") or ""),
                                evidence=why, agent=agent)
        if cmd_task_done(ns, transport) == 0:
            closed += 1
        else:
            failed += 1
    if failed:
        print(f"{failed} review-request row(s) for {slug} did NOT close; "
              f"run `review residue {team}` to finish", file=sys.stderr)
    if closed:
        print(f"  closed {closed} review-request row(s) for {slug}")


def cmd_review_close(args: argparse.Namespace, transport: Any) -> int:
    """Close a review because its PR MERGED — an artifact of the merge, not an
    inference about it (coord-boss ruling 1, 2026-08-07).

    A merged PR leaves an immortal obligation: the register keeps asking for a
    verdict on a head nobody will ever review again. `review gc` is deliberately
    NOT the answer — its predicate asks whether a HEAD is still alive, 551 raised
    that bar on purpose, and "the PR merged" is a different question that a
    liveness probe cannot answer.

    Unlike :func:`_write_settled_marker`, which is a best-effort CACHE and
    swallows its failures, this is a DURABLE RECORD: a swallowed failure would
    leave the row open while reporting closed, so the write is verified by
    read-back and a failure is a non-zero exit.
    """
    slug = args.slug
    sha = (args.merge_sha or "").strip().lower()
    if not _MERGE_SHA.match(sha):
        print(f"review close: --merge-sha must be a full 40- or 64-hex commit "
              f"sha, got {args.merge_sha!r}. Closure carries evidence; an "
              f"abbreviation or a branch name is an assertion.", file=sys.stderr)
        return 2

    # Refuse to close a slug that does not exist: a write into a nonexistent
    # register entry succeeds silently and reads exactly like a closure.
    try:
        vnames = {(e.get("name") or "")
                  for e in transport.list_dir(_verdicts_prefix(args.team, slug))}
    except TransportError as e:
        print(f"review close: cannot read the verdicts prefix for {slug} ({e}) "
              f"— UNKNOWN, not closed. Retry.", file=sys.stderr)
        return 1
    doc = transport.read(_review_doc_path(args.team, slug))
    if doc is None and not vnames:
        print(f"review close: {slug} has no review doc and no verdicts — "
              f"nothing to close. Check the slug.", file=sys.stderr)
        return 2

    now = _iso(_now())
    marker = {
        "schema": "review-settled/v1",
        "state": "MERGED",
        "merge_sha": sha,
        "merged_at": args.merged_at or now,
        "closed_by": _identity(args.sender),
        "reason": args.reason or "PR merged; the head will not be reviewed again",
        "ts": now,
    }
    payload = okf.render_frontmatter(marker)
    path = _settled_marker_path(args.team, slug)
    try:
        transport.write(path, payload)
    except Exception as e:
        print(f"review close: write FAILED for {slug} ({type(e).__name__}) — "
              f"the row is still open.", file=sys.stderr)
        return 1
    # Verify the marker IS THE ONE WE WROTE, not merely that something is
    # there (codex 561 r1). `.settled` is very often already occupied by the
    # fold's APPROVED cache marker — that is the NORMAL state for a terminal
    # review, which is exactly the review a merged PR has. So a silently
    # dropped write leaves the OLD content behind, a presence check passes, and
    # the command reports a merge sha the durable record does not contain.
    # Presence is not identity; that distinction is the whole point of this verb.
    back = transport.read(path)
    if back is None:
        print(f"review close: wrote {slug} but the read-back was empty — "
              f"closure UNVERIFIED, treat the row as still open and retry.",
              file=sys.stderr)
        return 1
    if back != payload:
        # Compare EVERY field we wrote, derived from the payload itself
        # (codex 561 r2). My r1 fix hand-picked `state` and `merge_sha` and
        # called them load-bearing — but this verb's contract records
        # merged_at, closed_by and reason too, so re-closing the SAME sha with
        # a corrected timestamp or reason could silently drop the write, match
        # on the two fields I happened to check, and exit 0 while the requested
        # evidence never landed. A hand-picked subset goes stale the moment a
        # field is added; deriving the list from `marker` cannot.
        got = okf.parse_frontmatter(back) or {}
        stale = sorted(k for k, want in marker.items()
                       if str(got.get(k) if got.get(k) is not None else "")
                       != str(want))
        if stale:
            print(f"review close: {slug} read back a DIFFERENT marker — "
                  f"{', '.join(stale)} did not match what was written. The "
                  f"write did not land; closure UNVERIFIED and the row is "
                  f"still open.", file=sys.stderr)
            return 1
    print(f"review close: {slug} closed as MERGED at {sha[:12]} "
          f"(marker {path})")
    _close_review_request_rows(
        transport, args.team, slug,
        why=(f"review closed as MERGED at {sha[:12]}; settle marker written "
             f"and verified field-by-field against what was requested"),
        agent=getattr(args, "sender", None))
    return 0


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


def _is_unattributable(name: str, *, keyed: bool) -> bool:
    """Can this verdict filename name a reviewer on THIS review?

    ``keyed`` is whether the REQUEST carries a head. That parameter is the fix
    codex-reviewer found in r2: without it the predicate answered a question
    about the filename alone, and a filename is only meaningful against the
    review it sits under.

    - On a KEYED review, ``<valid-head>--<reviewer>.md`` for a non-active head
      is a superseded round: skipped deliberately and silently, because making
      every multi-round review noisy would train everyone past the warning that
      matters.
    - On a HEADLESS review there are no rounds at all, so a keyed-looking shard
      names a round that cannot exist here. It is uncounted, and the r2 build
      said nothing: `PENDING`, `awaiting required: bob`, rc 0, with bob's
      approve verdict present. The same silent false negative this whole change
      exists to remove, hiding one branch deeper.
    - A ``--`` prefix that is not a well-formed head is unattributable either
      way: it names no round that could ever exist, under any review.
    """
    stem = name[:-3] if name.endswith(".md") else name
    if "--" not in stem:
        return False
    if not keyed:
        return True  # no rounds here, so nothing keyed can belong
    return review.normalize_head(stem.split("--", 1)[0]) is None


def _store_mtime_iso(mtime: Any) -> Optional[str]:
    """Listing mtime -> comparable ISO, or None.

    Store mtimes render on a TWELVE-HOUR clock, so comparing them as strings
    inverts the midnight hour. Parsed through the one existing parser, never
    compared raw.
    """
    if not isinstance(mtime, str):
        return None
    dt = aggregate._parse_store_mtime(mtime)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


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
    head = review.normalize_head(req_doc.get("head"))
    required = req_doc.get("required")
    if isinstance(required, str):
        required = [r.strip() for r in required.split(",") if r.strip()]
    elif isinstance(required, list):
        required = [str(r).strip() for r in required if str(r).strip()]
    verdicts: list[dict[str, Any]] = []
    unattributable: list[str] = []
    unrecognised: list[tuple[str, str]] = []
    mismatched: list[tuple[str, str]] = []
    rows: list[dict[str, Any]] = []
    reads_ok = True
    fully_scanned = True
    dl = Deadline(deadline)
    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md"):
            continue
        parsed_name = review.parse_verdict_filename(n, head=head)
        reviewer = parsed_name[0] if parsed_name else None
        parsed_ts = parsed_name[1] if parsed_name else None
        if reviewer is None:
            # A keyed review reads only the active head's append-only shards;
            # legacy reviews ignore keyed files. Superseded heads cost zero reads.
            #
            # But "skipped a superseded round" and "cannot attribute this file at
            # all" are different facts, and collapsing them cost a real reviewer
            # their verdict: a shard named `<date>--<reviewer>.md` on a headless
            # review was read as a superseded head, dropped before it was opened,
            # and `review status` then reported pending_required=[that reviewer]
            # — not "a file here is unreadable", but the affirmative claim that
            # they had not voted. A false negative wearing the costume of a fact,
            # and the person who did the work is the last one who would check.
            #
            # A prefix that is not a well-formed head can never name a round, so
            # it is unattributable under ANY head, not merely under this one.
            if _is_unattributable(n, keyed=bool(head)):
                unattributable.append(n)
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
        if head and review.normalize_head(fm.get("head")) != head:
            # The path selects the round, and the verdict must independently
            # attest the exact reviewed head. A mismatch cannot discharge it —
            # that rule is right and is unchanged.
            #
            # Doing it SILENTLY was the mistake. I left this skip alone while
            # fixing the other two, reasoning that "the path selects the round,
            # so a mismatch is a different claim". collect-maintainer pushed
            # back; the user-visible failure is identical. A well-formed shard
            # from alice sits in the directory, is read, is discarded, and
            # `review status` reports `pending_required: [alice]` at rc 0 —
            # the same affirmative falsehood, arrived at by a different route.
            # By the rule this very change establishes, a verdict that cannot
            # be counted is reported, never dropped.
            mismatched.append((n, str(fm.get("head") or "")))
            continue
        # Key by the requirement token encoded in the ACL-controlled FILENAME,
        # not the frontmatter
        # `reviewer:` — otherwise a file `mallory.md` claiming `reviewer: alice`
        # could shadow alice's real verdict. One verdict file per reviewer.
        token = fm.get("verdict")
        if token is not None and review.normalize_verdict(token) is None:
            # A reviewer who wrote `approve-with-required-changes` has
            # unmistakably voted. Normalising that to None and moving on is the
            # engine choosing the least informative reading of a clear intent —
            # and it lands in the same PENDING as no verdict at all. Record it
            # so the surface can say WHY the vote did not count.
            unrecognised.append((n, str(token)))
        # Collect, do not decide: with append-only shards a reviewer may have
        # several, and only the NEWEST counts. Sorting on a ts we can defend —
        # the name's for an append shard, the frontmatter's for a plain one,
        # falling back to the listing mtime — keeps two hosts folding the same
        # directory to the same answer.
        rows.append({
            "reviewer": reviewer,
            "name": n,
            "verdict": token,
            "supersedes": [str(x) for x in (fm.get("supersedes") or [])
                           if isinstance(fm.get("supersedes"), list)],
            "digest": review.content_digest(raw_v),
            "mtime_iso": _store_mtime_iso(e.get("mtime")) or "",
            # ONE canonical form (second from the ACL-controlled name, fraction
            # from frontmatter `ts` only within that second) so two same-second
            # shards of one reviewer order by chronology, not by digest.
            "sort_key": review.canonical_sort_key(
                parsed_ts, str(fm.get("ts") or ""),
                _store_mtime_iso(e.get("mtime"))),
        })
    kept, folded_away = review.fold_newest_per_reviewer(rows)
    verdicts = [{"reviewer": r["reviewer"], "verdict": r["verdict"]}
                for r in kept]
    tally = review.tally(verdicts, required=required)
    # THE EXACT WINNING SHARD PER REVIEWER, exposed so a consumer (a ship gate)
    # reads what the fold decided instead of refolding filenames itself — the
    # reviewers' explicit ask: "do not independently refold ambiguous filenames".
    tally["winning"] = {
        r["reviewer"]: {"name": r["name"],
                        "verdict": review.normalize_verdict(r["verdict"]),
                        "sort_key": r["sort_key"]}
        for r in kept
    }
    bad_edges = review.invalid_supersession_edges(rows)
    if bad_edges:
        tally["malformed_supersedes"] = bad_edges   # NEVER silently: a self-link is a shard trying to erase itself
    # Computed HERE, from the same entries the fold consumed, so the cache's
    # fingerprint provably describes what it summarises. EMPTY when a mutable
    # plain shard participates: a name digest cannot see that shard's in-place
    # rewrite, so there is nothing here honest enough to bind a cache to, and an
    # empty digest is what every reader already treats as unbound (595 r5).
    _vnames = [e.get("name") for e in entries]
    tally["evidence"] = (review.evidence_digest(_vnames)
                         if review.evidence_is_immutable(_vnames) else "")
    if folded_away:
        # NEVER SILENTLY (coord-boss constraint 4). A reader told "APPROVED"
        # while shards were quietly discarded has the same affirmative
        # falsehood this whole cycle was about; superseded evidence is still on
        # disk and the count is how a reader knows to go look.
        tally["superseded_verdicts"] = folded_away
    if unattributable:
        tally["unattributable"] = sorted(unattributable)
    if unrecognised:
        tally["unrecognised_verdicts"] = [
            {"file": f, "verdict": v} for f, v in sorted(unrecognised)]
    if mismatched:
        tally["head_mismatched_verdicts"] = [
            {"file": f, "claimed_head": h} for f, h in sorted(mismatched)]
    # OC5/C01: the artifact pointer rides the tally so every consumer of this
    # fold can serve it without a second doc read. None when the register doc
    # genuinely lacks it — the doc is the honest source, never a guess.
    _of = req_doc.get("of")
    tally["of"] = str(_of) if _of else None
    if head:
        tally["head"] = head
        try:
            tally["round"] = max(1, int(req_doc.get("round") or 1))
        except (TypeError, ValueError):
            tally["round"] = 1
    return tally, reads_ok, fully_scanned


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
        if (names is None or f"{name}.md" in names or name in names
                or f"{name}/" in names):
            # roles/ listing unreadable (membership unknown) OR the doc is listed
            # yet unusable, OR a lease directory exists without its role doc:
            # UNKNOWN, fail closed.  A lease namespace is positive role evidence;
            # it must not disappear merely because its metadata writer failed.
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


def _role_membership_for_agent(
    transport: Any, team: str, name: str, agent: str, *, now: str,
    listing_cache: Optional[dict[str, Any]] = None,
    deadline: Optional[Deadline] = None,
) -> tuple[list[str], bool]:
    """Resolve one caller's role membership without reading peer leases.

    Once the lease listing positively identifies the caller's shard, peer shards
    cannot change whether this caller holds the role. Return the minimal holder
    evidence downstream matching needs (``[agent]`` or ``[]``); UNKNOWN remains
    loud when the role doc, lease listing, or caller's own listed shard is
    unreadable.
    """
    if "/" in name:
        return [], True
    dl = deadline if deadline is not None else Deadline(None)
    raw_doc = transport.read(_role_doc_path(team, name))
    reg = okf.parse_frontmatter(raw_doc)
    if reg is None:
        cache = listing_cache if listing_cache is not None else {}
        if "names" not in cache:
            cache["names"] = _roles_listing_names(transport, team)
        names = cache["names"]
        if (names is None or f"{name}.md" in names or name in names
                or f"{name}/" in names):
            return [], False
        return [], True
    sla = roles.parse_sla_hours(reg.get("sla_hours"))
    if sla is None:
        return [], False
    if dl.expired():
        return [], False
    try:
        entries = transport.list_dir(_leases_prefix(team, name))
    except TransportError:
        return [], False
    own_name = f"{tasks.agent_key(agent)}.md"
    own_listed = any(
        not entry.get("is_dir") and (entry.get("name") or "") == own_name
        for entry in entries
    )
    if not own_listed:
        return [], True
    if dl.expired():
        return [], False
    fm = okf.parse_frontmatter(
        transport.read(_leases_prefix(team, name) + own_name))
    if fm is None:
        return [], False
    lease_agent = str(fm.get("agent") or agent)
    if lease_agent != agent:
        return [], True
    fresh = roles.fresh_holders(
        [{"agent": lease_agent, "timestamp": fm.get("timestamp")}],
        now=now, sla_hours=sla)
    return ([agent] if fresh else []), True


# --- role routing on the READ folds ---------------------------------------
#
# A directive assigned to a ROLE is directed at whoever holds a fresh lease on it
# — the contract AGENTS.md states ("briefing prints your identity, role inboxes,
# and everything that needs you") and the reason role-based identity exists at
# all: work addressed to a role must outlive the session that was holding it.
# The retired `listen` tick honoured it from the start; `briefing` / `inbox` /
# `needs-me` did not,
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
    # "assignee token(s)", not "roles". On the pre-budget path
    # `_held_roles_for_rows` returns its RAW candidate set — every distinct
    # assignee on the rows — because the budget was already spent before the
    # roles listing that would tell us which of them are roles at all. So this
    # line can name plain agent identities, and calling them roles states
    # something we did not check. Mid-scan the set IS filtered to real roles;
    # one wording that is true of both beats a second message that drifts.
    #
    # It over-reports UNKNOWN and never under-reports, which is the safe
    # direction — the point of the fix is that the words match the evidence.
    # (coord-boss, P3 fidelity note on 560.)
    return (f"  role resolution degraded: {', '.join(r.get('roles') or [])} — "
            f"membership unresolved for these assignee token(s); your role "
            f"inbox is unknown (not empty), role-routed work may be missing, "
            f"retry")


def _held_roles_for_rows(
    transport: Any, team: str, agent: str, rows: list[dict[str, Any]], *,
    now: str, deadline_seconds: Optional[float] = None,
    resolution_sink: "Optional[dict[str, tuple[list[str], bool]]]" = None,
) -> tuple[set[str], set[str]]:
    """Roles ``agent`` holds a FRESH lease on, among the role-shaped assignees the
    given rows actually reference. Returns ``(held, unresolved)``.

    The candidate set is the first bound: only DISTINCT foreign assignees on OPEN
    rows are probed, and the roles/ LISTING (one op, cached for the pass) settles
    which of them are roles at all — so the literal-agent-id majority costs ZERO
    reads, and only genuine roles pay. A team with no role-addressed open work pays
    nothing. Self / ``*`` / ``@backlog`` / path-shaped assignees are skipped without
    a read. (A ``skip_slugs`` prefilter narrowing to UNSEEN directives lived here
    for the retired `listen` tick's benefit; it went with that verb — every
    surviving caller resolves the full open set.)

    **The hot-path op bound** is caller-specific. A pass costs::

        1 + SUM over probed roles r of (2 + M_r)

    ops: one roles/ listing, then per probed role a doc read + a lease listing +
    ``M_r`` caller-shard reads, where ``M_r`` is 1 when the listing names the
    caller's lease and 0 otherwise. Peer shards cannot change whether this caller
    holds the role and are not read. Thus the pass is at most ``1 + 3R`` regardless
    of lifetime holder churn. "Probed roles" = the candidates the roles/ listing
    confirms are roles; if that listing RAISES, membership is unknown and EVERY
    candidate is probed at 1 op (its doc read) plus the lease terms for those whose
    docs parse.
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
    got a persistent negative cache rejected for the retired `listen` tick).

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
    if candidates and dl.expired():
        # The budget was ALREADY spent when we were called (a caller passing its
        # own remaining window, e.g. briefing after a slow earlier phase). The
        # listing below is an unconditional blocking op and the first expiry
        # check used to sit AFTER it, so a spent budget still paid one full
        # transport op — under the degraded transport this bound exists for,
        # that is one whole timeout, charged to the sections the caller was
        # protecting. Nothing can be classified without the listing, so every
        # candidate is UNKNOWN. Return that, having spent nothing.
        # (Found by coord-opus-worker reviewing PR 559; it is the same
        # pre-budget class this function's own docstring names four lines up.)
        #
        # NOTE the fidelity limit, deliberately accepted: these are RAW
        # candidates — every distinct assignee on the rows — not roles. Telling
        # them apart needs the roles listing, which is the one blocking op this
        # branch exists to avoid paying for. So the set can include plain agent
        # identities, it over-reports UNKNOWN and never under-reports, and the
        # degraded LINE is worded for that ("assignee token(s)", not "roles")
        # rather than asserting a membership nobody checked.
        return set(), set(candidates)
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
            tail = ordered[i:]
            unresolved.update(tail)
            if resolution_sink is not None:
                # UNKNOWN is reusable evidence within this ONE wake: a later
                # review fold must surface the same degraded roles, not spend a
                # second network budget retrying the exact tail immediately.
                for pending_role in tail:
                    resolution_sink[pending_role] = ([], False)
            break
        holders, ok = _role_membership_for_agent(
            transport, team, role, agent, now=now,
            listing_cache=listing_cache, deadline=dl)
        if resolution_sink is not None:
            resolution_sink[role] = (holders, ok)
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
    degraded_sink: "Optional[list[str]]" = None,
    aggregate_doc: Any = None,
    feed_evidence: Any = None,
    role_resolution: "Optional[dict[str, tuple[list[str], bool]]]" = None,
) -> list[dict[str, Any]]:
    """The pending-review fold, projection-first (the annotation read side).

    With ``aggregate_doc`` (the parsed summaries document — callers get it for
    free via ``_load_rows_status``'s ``doc_sink``), the fold consumes the
    reconcile-built ``reviews`` projection section in ZERO extra transport ops
    when it is FRESH (see ``projection.fresh_section``), and SAYS SO with a
    trailing ``{"type": "review-source", "source": "projection", "as_of": T}``
    row. A projection that exists but cannot be served (stale / incomplete /
    unrecognized) falls back to the raw scan LOUDLY — same row shape,
    ``"source": "raw-scan"`` plus the ``reason`` — never silently serving old
    state as current. A team whose aggregate carries no projection at all (or a
    caller that passes no ``aggregate_doc``) takes the raw scan with no source
    row: byte-identical to the pre-projection behavior."""
    if aggregate_doc is not None:
        feed_supplied = feed_evidence is not None
        feed_ok = isinstance(feed_evidence, dict) and feed_evidence.get("ok") is True
        if feed_supplied and feed_ok:
            section, reason = projection_mod.feed_fresh_section(
                aggregate_doc, projection_mod.REVIEWS_KEY,
                projection_mod.REVIEWS_SCHEMA, now=_iso(_now()))
        elif feed_supplied:
            section = None
            _unused, age_reason = projection_mod.fresh_section(
                aggregate_doc, projection_mod.REVIEWS_KEY,
                projection_mod.REVIEWS_SCHEMA, now=_iso(_now()))
            reason = (age_reason or (feed_evidence or {}).get("reason")
                      or "data-updates feed unreadable")
        else:
            section, reason = projection_mod.fresh_section(
                aggregate_doc, projection_mod.REVIEWS_KEY,
                projection_mod.REVIEWS_SCHEMA, now=_iso(_now()))
        if section is not None:
            changed = (projection_mod.review_changed_slugs(
                team, feed_evidence.get("changes") or []) if feed_ok else None)
            served = _pending_reviews_from_projection(
                transport, team, agent, section, rows=rows,
                degraded_sink=degraded_sink, changed_slugs=changed,
                role_resolution=role_resolution)
            if served is not None:
                return served
            reason = "reviews projection malformed"
        if reason:
            out = _pending_reviews_raw(
                transport, team, agent, rows=rows,
                deadline_seconds=deadline_seconds, deadline=deadline,
                degraded_sink=degraded_sink)
            out.append({"type": "review-source", "source": "raw-scan",
                        "reason": reason})
            return out
    return _pending_reviews_raw(
        transport, team, agent, rows=rows, deadline_seconds=deadline_seconds,
        deadline=deadline, degraded_sink=degraded_sink)


def _validated_review_projection(
    section: dict[str, Any],
) -> "Optional[tuple[dict[str, dict[str, Any]], list[str], list[str], list[str]]]":
    """Use the shared review-v3 validator used by producer and authority."""
    return generation.validated_review_projection(section)


def _pending_reviews_from_projection(
    transport: Any, team: str, agent: str, section: dict[str, Any], *,
    rows: "Optional[list[dict[str, Any]]]" = None,
    degraded_sink: "Optional[list[str]]" = None,
    changed_slugs: "Optional[set[str]]" = None,
    role_resolution: "Optional[dict[str, tuple[list[str], bool]]]" = None,
) -> "Optional[list[dict[str, Any]]]":
    """Serve the pending-review fold from a FRESH ``reviews`` projection section.

    One in-memory pass over the projection rows replaces the whole raw fan-out:
    settled rows skip, PENDING rows with a non-empty ``pending_required`` resolve
    role holders exactly as the raw scan does (role leases stay a live read —
    they are cheap and per-agent-relevant), and orphan/tombstone knowledge comes
    pre-classified. Every nested row/list is positively validated FIRST
    (``_validated_review_projection``); any shape doubt returns None and the
    caller falls back to the raw scan, loudly.

    HEAD COVERAGE (round-2 P0): without positive feed evidence, every open
    caller-owned review-request slug is authoritative head coverage and is
    raw-tallied per slug, guarding the reconcile/write race where a projected
    settled row predates a rewritten head. With a clean update feed, that race is
    observable: only feed-changed slugs require raw head coverage, while unchanged
    caller heads are safely served from the projected tail. An unresolvable head
    slug stays UNKNOWN-loud (``review-head-degraded``), same as the raw fold."""
    validated = _validated_review_projection(section)
    if validated is None:
        return None
    by_name, orphans, orphans_unknown, tombstones = validated
    now = _iso(_now())
    out: list[dict[str, Any]] = []
    role_holders: dict[str, list[str]] = {}
    degraded_roles: set[str] = set()
    roles_listing_cache: dict[str, Any] = {}

    def _match_pending(pending: list[str]) -> bool:
        if agent not in pending:
            for role in pending:
                if role not in role_holders:
                    cached = (role_resolution or {}).get(role)
                    if cached is None:
                        holders, ok = _role_fresh_holders(
                            transport, team, role, now=now,
                            listing_cache=roles_listing_cache)
                    else:
                        holders, ok = cached
                    role_holders[role] = holders
                    if not ok:
                        degraded_roles.add(role)
        return review.is_pending_for(pending, agent, role_holders)

    # Without feed proof, caller-owned heads are raw-tallied for race safety.
    # A clean feed narrows that authoritative head to changed slugs only.
    feed_proven = changed_slugs is not None
    changed_slugs = changed_slugs or set()
    caller_heads = _caller_review_head_slugs(rows, agent)
    head_slugs = sorted(changed_slugs if feed_proven
                        else caller_heads | changed_slugs)
    head_set = set(head_slugs)

    for slug in sorted(by_name):
        if slug in head_set:
            continue  # authoritative head coverage: raw-tallied below
        r = by_name[slug]
        if r["settled"]:
            continue
        pending = r["pending_required"]
        if r["state"] != review.PENDING or not pending:
            continue
        if _match_pending(pending):
            out.append({"type": "review-pending", "name": slug,
                        "state": "PENDING", "pending_required": list(pending),
                        "of": r.get("of"), "head": r.get("head")})

    head_scanned = head_skipped = 0
    for slug in head_slugs:
        # Same per-slug fail-closed guard the raw fold's _scan_one carries: a
        # transient transport failure on ONE verdict read (which escapes
        # _review_tally — its inner read loop has no TransportError guard) must
        # degrade THIS slug to UNKNOWN-loud, never crash the whole
        # projection-backed fold (round-3 P1a).
        try:
            tally, doc_ok, vok, listing_ok = _review_tally(transport, team, slug)
        except TransportError:
            tally, doc_ok, vok, listing_ok = {}, False, False, False
        head_scanned += 1
        if not (doc_ok and vok and listing_ok):
            head_skipped += 1
            if degraded_sink is not None:
                degraded_sink.append(f"review-verdicts:{slug}")
            continue
        pending = tally.get("pending_required") or []
        if tally.get("state") == review.PENDING and pending and _match_pending(
                [str(x) for x in pending if str(x)]):
            out.append({"type": "review-pending", "name": slug,
                        "state": "PENDING",
                        "pending_required": [str(x) for x in pending],
                        "of": tally.get("of"), "head": tally.get("head")})
    if head_skipped:
        out.append(budget_mod.degraded_row(
            "review-head-degraded", head_scanned, len(head_slugs), head_skipped))

    for slug in (s for s in orphans if s not in changed_slugs):
        out.append({"type": "review-orphan", "name": slug})
    for slug in (s for s in orphans_unknown if s not in changed_slugs):
        out.append({"type": "review-orphan-degraded", "name": slug})
    if degraded_roles:
        out.append({"type": "review-role-degraded",
                    "roles": sorted(degraded_roles)})
    out.append({"type": "review-source", "source": "projection",
                "as_of": section.get("generated_at")})
    return out


def _pending_reviews_raw(
    transport: Any, team: str, agent: str, *,
    rows: "Optional[list[dict[str, Any]]]" = None,
    deadline_seconds: Optional[float] = None, deadline: Optional[float] = None,
    degraded_sink: "Optional[list[str]]" = None,
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
        # Best-effort for needs-me/briefing (they must not fail because the review
        # add-on is down), but the obligation fold MUST NOT read this [] as "no
        # reviews pending" — that is a false CLEAR. A caller that needs the
        # distinction passes ``degraded_sink`` and gets told.
        if degraded_sink is not None:
            degraded_sink.append("review-listing")
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
            vnames = {(x.get("name") or "") for x in ventries}
            # gc-retired entries skip for the same reason settled ones do — they
            # can never become pending for anybody. Skipping them HERE is what
            # makes `review gc` recover budget at all; the marker alone changed
            # nothing (codex-reviewer, review-gc round 1).
            if review_gc.is_terminal(vnames):
                return "ok"  # terminal -> skip entirely, zero reads beyond this listing
            if SETTLED_MARKER in vnames:
                # A `.settled` used to skip this slug on PRESENCE ALONE, which is
                # the same unvalidated short-circuit 595 r4 fixed in the register
                # projection — fixed THERE and left here, one reader apart. A
                # stale cache made this scan report an agent owed nothing while
                # the newest verdict was CHANGES: an obligation hidden by a file.
                #
                # Same shared rule, so the two readers cannot drift. One read per
                # settled slug still skips the doc and every shard.
                if review.settle_shortcircuit(
                        okf.parse_frontmatter(
                            transport.read(_settled_marker_path(team, slug))) or {},
                        vnames) != review.SETTLE_NO:
                    return "ok"
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
            # An unreadable verdict is an unknown obligation, so the fold hears
            # about it too: partial coverage is not coverage.
            if degraded_sink is not None:
                degraded_sink.append(f"review-verdicts:{slug}")
            return "unknown"
        state = tally.get("state")
        pending = tally.get("pending_required") or []
        if state == review.APPROVED and not pending:
            # Cache only a PROVEN settle (non-empty required + every verdict read).
            if _is_settleable(tally) and vreads_ok:
                _write_settled_marker(transport, team, slug, now=now,
                                      evidence=tally.get("evidence"))
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
                        "state": "PENDING", "pending_required": pending,
                        "of": tally.get("of"), "head": tally.get("head")})
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
        # PR 634 made every emit path carry `of` + `head` on these dicts, so the
        # reviewer's artifact is already in hand — it just was not rendered, and a
        # reviewer who cannot search SHAs had no way from this line to the thing
        # under review (codex-coder sat blocked 20h on exactly that). `head` rides
        # along because review is exact-head: the verdict filename is
        # `<head>--<reviewer>.md`, so the slug alone does not say which head to
        # file against. Both are omitted cleanly when absent — legacy rows and
        # rows whose register entry predates 634 render exactly as before.
        line = (f"  [REVIEW] pending verdict: {r['name']} "
                f"(required: {', '.join(r['pending_required'])})")
        of = str(r.get("of") or "").strip()
        head = str(r.get("head") or "").strip()
        if of:
            line += f" — of: {_clip(of)}"
        if head:
            line += f" @ {head[:12]}"
        return line
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
    if t == "review-source":
        return _source_line("review", r)
    return None


#: Every degraded marker row type in this module ends in ``degraded``
#: (``read-degraded``, ``role-degraded``, ``forge-degraded``, the four
#: ``review-*-degraded``, ``inbox-degraded``, ``presence-degraded``,
#: ``engagement-degraded``). ONE predicate, so a marker type added tomorrow is
#: counted by the envelope that day rather than the day someone notices it never
#: was.
def _is_degraded_row(r: Any) -> bool:
    return isinstance(r, dict) and str(r.get("type") or "").endswith("degraded")


def _fold_source(rows: list, type_name: str) -> str:
    """``projection`` / ``raw`` / ``absent`` for the ``*-source`` row named by
    ``type_name`` — the envelope's one-word form of `_source_line`."""
    for r in rows:
        if isinstance(r, dict) and str(r.get("type") or "") == type_name:
            return "projection" if r.get("source") == "projection" else "raw"
    return "absent"


# OC2 contract-2 basis vocabulary: every Class A degraded-marker type maps to
# the failure class the design's health rules key on. `read-degraded` is the
# one UNKNOWN class today — the authority itself (task fold) could not be
# trusted, so served rows may describe a world that does not exist. Everything
# else is partial COVERAGE over a readable authority: rows are a floor.
_CLASS_A_BASIS: dict[str, str] = {
    "read-degraded": "source-unreadable",
    "inbox-degraded": "source-unreadable",
    "role-degraded": "role-resolution-partial",
    "review-role-degraded": "role-resolution-partial",
    "forge-degraded": "budget-cut",
    "review-fold-degraded": "budget-cut",
    "review-head-degraded": "subset-unreadable",
    "review-orphan-degraded": "subset-unreadable",
}
_UNKNOWN_BASIS = {"source-unreadable", "source-invalid", "fallback-failed"}


def class_a_outcome(
    rows: list, *, source_type: str,
) -> tuple[outcome_mod.CommandOutcome, dict[str, Any]]:
    """Adapt legacy Class A rows into the v2 typed outcome spine.

    The second return is compatibility metadata for the contract-2 envelope.
    Partial legacy health remains ``DEGRADED`` at that boundary, while the v2
    outcome truthfully records the incomplete required surface as ``UNKNOWN``.
    """
    degraded_types: list[str] = []
    for row in rows:
        if _is_degraded_row(row):
            marker = str(row.get("type") or "")
            if marker not in degraded_types:
                degraded_types.append(marker)
    basis: list[str] = []
    for marker in degraded_types:
        reason = _CLASS_A_BASIS.get(marker, "source-invalid")
        if reason not in basis:
            basis.append(reason)

    src_row = next((row for row in rows if isinstance(row, dict)
                    and str(row.get("type") or "") == source_type), None)
    src_token = src_row.get("source") if src_row is not None else None
    if src_row is not None and src_token not in ("projection", "raw-scan"):
        if "source-invalid" not in basis:
            basis.append("source-invalid")

    actionable = any(
        isinstance(row, dict)
        and not str(row.get("type") or "").endswith(("degraded", "-source"))
        for row in rows)
    coverage = tuple(
        outcome_mod.SurfaceCoverage(
            marker, outcome_mod.CoverageState.UNKNOWN,
            required=True, reason=_CLASS_A_BASIS.get(marker, "source-invalid"))
        for marker in degraded_types
    )
    if not coverage:
        coverage = (outcome_mod.SurfaceCoverage(
            source_type.removesuffix("-source"),
            outcome_mod.CoverageState.DATA if actionable
            else outcome_mod.CoverageState.CLEAR,
        ),)
    if any(reason in _UNKNOWN_BASIS for reason in basis):
        state = outcome_mod.OutcomeState.UNKNOWN
        health = "UNKNOWN"
    elif degraded_types:
        state = outcome_mod.OutcomeState.UNKNOWN
        health = "DEGRADED"
    else:
        state = (outcome_mod.OutcomeState.DATA if actionable
                 else outcome_mod.OutcomeState.CLEAR)
        health = state.value
    source = ("projection" if src_token == "projection" else
              "raw-scan" if src_token == "raw-scan" or src_row is None else None)
    typed = outcome_mod.CommandOutcome(
        state=state, rows=tuple(rows), coverage=coverage, source=source)

    scanned = total = 0
    bounded = False
    for row in rows:
        if (_is_degraded_row(row) and isinstance(row.get("scanned"), int)
                and isinstance(row.get("total"), int)):
            scanned += row["scanned"]
            total += row["total"]
            bounded = True
    legacy: dict[str, Any] = {
        "health": health,
        "source": source,
        "as_of": src_row.get("as_of") if src_token == "projection" else None,
        "degraded": degraded_types,
        "basis": basis,
    }
    if bounded:
        legacy["scanned"] = scanned
        legacy["total"] = total
    return typed, legacy


def class_a_envelope(
    rows: list, *, source_type: str,
) -> tuple[dict[str, Any], int]:
    """Seal the contract-2 envelope for a Class A (bare-array-fold) verb.

    Returns ``(envelope, rc)``. The envelope is the SINGLE JSON value the verb
    prints on stdout under ``--json``: ``health`` is transport/fold health only
    (never a domain state — rows keep their own fields untouched), selected by
    the design's ordered rules (oc2-oc3-envelope-design-r2, APPROVED
    2026-08-18): UNKNOWN when the authority itself could not be trusted (rows
    MUST NOT be acted on), DEGRADED when coverage is partial over a readable
    authority (rows are a usable FLOOR; absence-inference forbidden), DATA /
    CLEAR on a complete scan — CLEAR is the only health that licenses "there
    is nothing for me". rc is a pure function of health (UNKNOWN|DEGRADED → 3,
    DATA|CLEAR → 0), sealed HERE, before row serialization, so a later
    serializer failure can never flip a dirty rc clean (OC3/E4).

    Marker and source rows stay inside ``rows`` exactly as before — derived
    duplicates for greppability, never the only carrier (OC10 unchanged)."""
    typed, legacy = class_a_outcome(rows, source_type=source_type)
    envelope: dict[str, Any] = {"contract": 2, "health": legacy["health"]}
    if legacy["source"] == "projection":
        envelope["source"] = "projection"
        envelope["as_of"] = legacy["as_of"]
    elif legacy["source"] == "raw-scan":
        envelope["source"] = "raw-scan"
    else:
        envelope["source"] = None  # corrupt provenance: null under UNKNOWN
    # Coverage (pr-641 r1, finding 3): where bounded work ran, the marker rows
    # carry per-fold scanned/total — the envelope aggregates them (sums across
    # every marker carrying BOTH numbers) so a strict consumer reads coverage
    # without scanning rows. Omitted entirely when no bounded fold reported.
    if "scanned" in legacy:
        envelope["scanned"] = legacy["scanned"]
        envelope["total"] = legacy["total"]
    envelope["degraded"] = legacy["degraded"]
    envelope["basis"] = legacy["basis"]
    envelope["rows"] = rows
    return envelope, typed.rc


def emit_envelope(verb: str, *, count: int, rc: int, **fields: Any) -> None:
    """Write ONE compact verdict line to **stderr**.

    A fold that prints an unbounded row list puts its verdict — the degraded
    markers, the source disclosure, and the rc they imply — at the END of that
    list. A harness whose context truncates the stream loses exactly the part
    that says whether the read can be trusted, and a truncated read then looks
    identical to a clean one. codex-coder hit this 2026-08-08: ``needs-me``
    completed, stdout was truncated before the markers, and the wake could not
    certify the durable-assignment read either way. PR 565 had just made the rc
    load-bearing for that condition, which is what made the loss bite.

    stderr is a separate, tiny stream that survives stdout truncation on every
    harness we have. It is a DUPLICATE of the inline markers, never a
    replacement — the inline rows stay exactly where they are for readers that
    can see them.
    """
    parts = [f"{verb}: {count} item(s)"]
    parts += [f"{k}={v}" for k, v in fields.items()]
    parts.append(f"rc={rc}")
    print(", ".join(parts), file=sys.stderr)


def _source_line(label: str, r: dict[str, Any]) -> str:
    """Render a fold's source row — the no-silent-staleness disclosure: every
    projection-aware fold SAYS whether it served the reconcile-built projection
    (and as of when) or fell back to the raw scan (and why)."""
    if r.get("source") == "projection":
        return f"  {label} fold: projection (as of {r.get('as_of')})"
    return f"  {label} fold: raw scan — {r.get('reason') or 'projection unusable'}"


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
    transport: Any, team: str, agent: str, *, deadline: Optional[float] = None,
    aggregate_doc: Any = None,
    feed_evidence: Any = None,
) -> list[dict[str, Any]]:
    """The forge-feedback fold, projection-first (the annotation read side).

    Same contract as ``_pending_reviews_for``: with ``aggregate_doc``, a FRESH
    ``forge`` projection section replaces the team-global responsibility +
    feedback fan-out — the only remaining transport work is one ack read per
    feedback item for THIS agent (ack state is per-agent and stays live) — and
    the fold appends ``{"type": "forge-source", "source": "projection",
    "as_of": T}``. A projection present but unservable falls back to the raw
    scan loudly (``"source": "raw-scan"`` + reason), except a projection
    rejected by the shared deep validator: that is UNKNOWN and returns no
    domain rows without reopening canonical state. No projection / no
    ``aggregate_doc`` is the pre-projection raw scan, byte-identical."""
    if aggregate_doc is not None:
        feed_supplied = feed_evidence is not None
        feed_ok = isinstance(feed_evidence, dict) and feed_evidence.get("ok") is True
        if feed_supplied and feed_ok:
            section, reason = projection_mod.feed_fresh_section(
                aggregate_doc, projection_mod.FORGE_KEY,
                projection_mod.FORGE_SCHEMA, now=_iso(_now()))
        elif feed_supplied:
            section = None
            _unused, age_reason = projection_mod.fresh_section(
                aggregate_doc, projection_mod.FORGE_KEY,
                projection_mod.FORGE_SCHEMA, now=_iso(_now()))
            reason = (age_reason or (feed_evidence or {}).get("reason")
                      or "data-updates feed unreadable")
        else:
            section, reason = projection_mod.fresh_section(
                aggregate_doc, projection_mod.FORGE_KEY,
                projection_mod.FORGE_SCHEMA, now=_iso(_now()))
        if section is not None:
            changed, responsibility_changed = _forge_feed_delta(
                team, feed_evidence.get("changes") or []) if feed_ok else (set(), False)
            if responsibility_changed:
                section = None
                reason = "forge responsibility changed since projection"
            else:
                served = _forge_feedback_from_projection(
                    transport, team, agent, section, deadline=deadline,
                    changed_slugs=changed)
                if served is not None:
                    return served
                reason = "forge projection malformed"
                degraded = budget_mod.degraded_row("forge-degraded", 0, 0)
                degraded["reason"] = reason
                return [
                    degraded,
                    {"type": "forge-source", "source": "projection",
                     "reason": reason},
                ]
        if reason:
            out = _forge_feedback_raw(transport, team, agent, deadline=deadline)
            out.append({"type": "forge-source", "source": "raw-scan",
                        "reason": reason})
            return out
    return _forge_feedback_raw(transport, team, agent, deadline=deadline)


def _forge_feed_delta(team: str, changes: list[Any]) -> tuple[set[str], bool]:
    """Changed feedback PR slugs and whether responsibility itself moved."""
    feedback_pfx = f"team/{team}/_coord/forge/feedback/"
    watch_pfx = f"team/{team}/_coord/forge/watch/"
    review_pfx = f"team/{team}/review/"
    changed: set[str] = set()
    responsibility_changed = False
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or change.get("full_name") or "").lstrip("/")
        if path.startswith(feedback_pfx):
            rest = path[len(feedback_pfx):]
            if "/" in rest and rest.split("/", 1)[0]:
                changed.add(rest.split("/", 1)[0])
        elif path.startswith(watch_pfx) or path.startswith(review_pfx):
            responsibility_changed = True
    return changed, responsibility_changed


def _forge_feedback_from_projection(
    transport: Any, team: str, agent: str, section: dict[str, Any], *,
    deadline: Optional[float] = None,
    changed_slugs: "Optional[set[str]]" = None,
) -> "Optional[list[dict[str, Any]]]":
    """Serve the forge-feedback fold from a FRESH ``forge`` projection section.

    Responsibility and the feedback item ids/authors come from the section; the
    fold reads only THIS agent's ack shard per item (acked items hide, exactly
    as the raw fold hides them). Bounded by the caller's shared ``deadline``; a
    breach truncates with the same ``forge-degraded`` marker discipline. Every
    nested collection is positively validated FIRST
    (``generation.validated_forge_projection``); any shape doubt returns None
    so the caller marks coverage UNKNOWN without reopening canonical state."""
    validated = generation.validated_forge_projection(section)
    if validated is None:
        return None
    resp, fb = validated
    changed_slugs = changed_slugs or set()
    mine = sorted(slug for slug, agents in resp.items() if agent in agents)
    out: list[dict[str, Any]] = []
    dl = Deadline(deadline)
    total = len(mine)
    scanned = 0
    skipped = 0
    degraded = False
    for slug in mine:
        if dl.expired():
            degraded = True
            break
        scanned += 1
        if slug in changed_slugs:
            prefix = f"team/{team}/_coord/forge/feedback/{slug}/"
            try:
                entries = transport.list_dir(prefix)
            except TransportError:
                skipped += 1
                degraded = True
                continue
            row, ok = _forge_slug_feedback(
                transport, team, agent, slug, entries, prefix, deadline)
            if not ok:
                skipped += 1
                degraded = True
                break
            if row is not None:
                out.append(row)
            continue
        items = fb.get(slug) or []
        unacked: list[str] = []
        authors: list[str] = []
        cut = False
        for it in items:  # shapes proven by generation.validated_forge_projection
            stem = it["id"]
            if dl.expired():
                cut = True
                break
            acked = transport.read(_ack_path(team, stem, agent))
            if dl.expired():
                cut = True
                break
            if acked is not None:
                continue  # acked by this agent — hidden
            unacked.append(stem)
            author = it.get("author")
            if author and author not in authors:
                authors.append(author)
        if cut:
            # Budget expired mid-PR: the partial row is untrusted — discard it,
            # count the PR skipped, stop (the raw fold's discipline).
            skipped += 1
            degraded = True
            break
        if unacked:
            out.append({"type": "forge-feedback", "pr_slug": slug,
                        "count": len(unacked), "authors": sorted(authors),
                        "items": sorted(unacked)})
    if degraded:
        out.append(budget_mod.degraded_row("forge-degraded", scanned, total, skipped))
    out.append({"type": "forge-source", "source": "projection",
                "as_of": section.get("generated_at")})
    return out


def _forge_feedback_raw(
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
    only via a NEW slug. The exact head is deliberately not an identity field:
    changing it advances the same PR review to a new append-only round."""
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
    head: Optional[str] = None,
) -> tuple[list[str], list[str]]:
    """Deliver ONE directive per required reviewer through the canonical hash-slug
    path. Returns ``(delivered, failed)``. Payload-hash dedup makes this idempotent:
    a reviewer whose directive already landed re-verifies as "already delivered"
    (rc 0), so this is safe to re-run on a recovery retry — it fills the gaps."""
    delivered: list[str] = []
    failed: list[str] = []
    for r in required:
        if _deliver_review_directive(transport, team, slug, r,
                                     sender=owner, of=of, head=head) == 0:
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
    recovered: bool, head: Optional[str] = None, round_no: Optional[int] = None,
    advanced: bool = False,
) -> None:
    if recovered:
        print(f"review {slug} already exists (matching) — re-verified reviewer "
              f"delivery (required: {', '.join(required)})")
    elif advanced:
        print(f"review {slug} advanced to head {head} "
              f"(round {round_no or 1}; required: {', '.join(required)})")
    else:
        print(f"review {slug} requested (required: {', '.join(required)})")
    if head:
        print(f"  active head {head} (round {round_no or 1})")
    for r in required:
        filename = review.verdict_filename(r, head=head)
        print(f"  reviewer {r} -> file verdict at "
              f"{_verdicts_prefix(team, slug)}{filename}")
    # Point the requester at the await primitive for the verdict wait (they poll
    # `review status`; `queue` is the same read-your-events discipline every ask
    # uses since bus v3 retired the resident listener).
    sender = _known_sender(args)
    if sender:
        print(f"await verdicts: coord-engine queue {team} --agent {sender}")


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
    requested_head: Optional[str] = None
    if getattr(args, "head", None) is not None:
        requested_head = review.normalize_head(args.head)
        if requested_head is None:
            print("review request --head must be an exact 40- or 64-hex commit SHA",
                  file=sys.stderr)
            return 2
    path = _review_doc_path(team, slug)
    owner = _identity(getattr(args, "sender", None))
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
        existing_head_raw = existing_fm.get("head")
        existing_head = review.normalize_head(existing_head_raw)
        if existing_head_raw and existing_head is None:
            print(f"review {slug} already exists with an invalid head "
                  f"{existing_head_raw!r} — cannot verify, will not overwrite; retry",
                  file=sys.stderr)
            return 1
        if existing_head and requested_head is None:
            print(f"review {slug} is head-keyed at {existing_head}; an unkeyed "
                  f"re-request would discard exact-head gating — pass --head",
                  file=sys.stderr)
            return 1
        if requested_head and requested_head != existing_head:
            # SAME PR slug + requester + required set, NEW exact head: advance the
            # single durable review to an append-only head-keyed round. Prior
            # verdict files remain in place and the tally ignores them.
            if not _clear_settled_marker(transport, team, slug):
                print("review request cannot clear the prior settled marker — "
                      "active-head obligations may remain hidden; retry",
                      file=sys.stderr)
                return 1
            try:
                prior_round = max(1, int(existing_fm.get("round") or 1))
            except (TypeError, ValueError):
                prior_round = 1
            round_no = prior_round + 1
            advanced_fm = dict(existing_fm)
            advanced_fm.update({
                "schema": "review-request/v2",
                "head": requested_head,
                "round": round_no,
                "ts": _iso(_now()),
            })
            body = (f"\nReview requested: {args.of}\n"
                    f"Active head: {requested_head} (round {round_no})\n")
            if not transport.write(path, okf.render_frontmatter(advanced_fm) + body):
                print("review request head advance write failed (transport)",
                      file=sys.stderr)
                return 1
            delivered, failed = _deliver_all_review_directives(
                transport, team, slug, required, owner=owner, of=args.of,
                head=requested_head)
            if failed:
                _print_partial_review_failure(
                    slug, delivered, failed,
                    doc_note=f"advanced to head {requested_head} (round {round_no})")
                return 1
            _print_review_success(args, team, slug, required, recovered=False,
                                  head=requested_head, round_no=round_no,
                                  advanced=True)
            return 0
        # IDEMPOTENT RECOVERY: same requested_by + of + required set. Skip the doc
        # write (it already holds our request), keep the harmless stale-marker
        # delete (a prior fold may have settled it; its absence just makes the next
        # fold recompute), and RE-RUN reviewer delivery for EVERY required reviewer
        # — hash-path dedup re-verifies the ones that landed (rc 0 "already
        # delivered") and delivers the ones a prior partial failure dropped. This
        # is what makes a partial-delivery retry CONVERGE instead of dying here.
        if not _clear_settled_marker(transport, team, slug):
            print("review request cannot clear the settled marker — active-head "
                  "obligations may remain hidden; retry", file=sys.stderr)
            return 1
        delivered, failed = _deliver_all_review_directives(
            transport, team, slug, required, owner=owner, of=args.of,
            head=existing_head)
        if failed:
            _print_partial_review_failure(slug, delivered, failed,
                                          doc_note="already exists (matching)")
            return 1
        try:
            round_no = max(1, int(existing_fm.get("round") or 1))
        except (TypeError, ValueError):
            round_no = 1
        _print_review_success(args, team, slug, required, recovered=True,
                              head=existing_head, round_no=round_no)
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
        "schema": "review-request/v2" if requested_head else "review-request/v1",
        "requested_by": owner,
        "of": args.of,
        "required": required,
        "ts": _iso(_now()),
    }
    if requested_head:
        fm.update({"head": requested_head, "round": 1})
    body = f"\nReview requested: {args.of}\n"
    if requested_head:
        body += f"Active head: {requested_head} (round 1)\n"
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
    #
    # DELIBERATELY UNGUARDED, unlike the `review status` F4 delete. This clear
    # destroys merge evidence when the slug was a closed orphan, and that is the
    # LESSER harm: the fan-out fold SKIPS any slug carrying this marker
    # (see `SETTLED_MARKER in vnames`), so keeping it here would make the review
    # being requested INVISIBLE — a silently hidden obligation, versus evidence
    # that one `review close` restores. The marker does two jobs with opposite
    # lifetimes (durable evidence vs a fold-skip cache); separating them is the
    # real fix, and it is a design decision rather than a local one.
    transport.delete(_settled_marker_path(team, slug))
    # Atomic notification: with the doc durably landed, deliver ONE directive per
    # required reviewer through the canonical hash-slug directive path, so a
    # verb-opened review FIRES the reviewer's inbox/queue — this is what removes
    # the reason agents hand-send review tells (the PR-344 orphan class) and makes
    # the `await verdicts` breadcrumb genuine. Same C1 write discipline
    # as the doc: any reviewer-directive fail is reported LOUD naming exactly what
    # landed and what did not (partial is never silent), and the requester's retry
    # re-enters the idempotent-recovery path above to fill the gaps.
    delivered, failed = _deliver_all_review_directives(
        transport, team, slug, required, owner=owner, of=args.of,
        head=requested_head)
    if failed:
        _print_partial_review_failure(slug, delivered, failed,
                                      doc_note="requested (doc written)")
        return 1
    _print_review_success(args, team, slug, required, recovered=False,
                          head=requested_head, round_no=1 if requested_head else None)
    return 0


def cmd_review_verdict(args: argparse.Namespace, transport: Any) -> int:
    """File a verdict for a review round.

    SUGAR OVER THE EXISTING ARTIFACT, deliberately (coord-boss constraint a):
    this writes exactly the canonical `<head>--<reviewer>.md` shard at the path
    `review request` already prints, with the frontmatter the tally already
    reads. Nothing downstream — tally, settle, retention — learns that a verb
    exists, and DIRECT shard-writing stays valid (constraint b): reviewers who
    write the file themselves are unaffected the day this ships.

    Why it exists at all: filing a verdict was the one act with NO verb, so a
    reviewer touched no chokepoint, refreshed no presence, and left no work
    event. Every liveness fix of this cycle could not see reviewers for that one
    reason. As a verb it inherits all of them at once.

    REFUSES TO OVERWRITE. A verdict is evidence a merge decision may already
    rest on; replacing one silently could erase a CHANGES. A changed head is a
    new round with its own filename, which is the supported way to revise.
    """
    reviewer = _known_sender(args)
    if not reviewer:
        print("review verdict: no reviewer identity — set FULCRA_COORD_AGENT "
              "or pass --from", file=sys.stderr)
        return 2
    normalized = review.normalize_verdict(args.verdict)
    if normalized is None:
        print(f"review verdict: {args.verdict!r} is not a verdict — use "
              f"approve or changes; anything else reads as unparseable to the "
              f"tally and stalls the review silently", file=sys.stderr)
        return 2

    # THE REGISTER IS AUTHORITATIVE FOR THE ROUND (codex-reviewer, 595 r1).
    # `--head` used to be optional and the register was never read, so omitting
    # it wrote `<reviewer>.md`, printed success and returned 0 — while the tally
    # ignored that headless shard and the reviewer stayed pending. A confident
    # false success: the reviewer believes they voted, the round still waits.
    doc_raw = transport.read(_review_doc_path(args.team, args.name))
    if not doc_raw:
        print(f"review verdict: cannot read the review register for "
              f"{args.name} — a read of None is missing OR unreadable, and "
              f"neither tells me which round you are voting in. Refusing rather "
              f"than guessing.", file=sys.stderr)
        return 3
    doc_fm = okf.parse_frontmatter(doc_raw) or {}
    active_head = review.normalize_head(doc_fm.get("head"))
    supplied = review.normalize_head(getattr(args, "head", None))
    if getattr(args, "head", None) and supplied is None:
        print(f"review verdict: --head {args.head!r} is not a valid exact "
              f"commit id", file=sys.stderr)
        return 2
    if active_head and supplied and supplied != active_head:
        print(f"review verdict: {args.name} is at head {active_head}; a verdict "
              f"pinned to {supplied} cannot discharge the current round and "
              f"would sit unread. Re-run against the active head.",
              file=sys.stderr)
        return 1
    if active_head and not supplied:
        head = active_head          # resolved, never orphaned
        print(f"(resolved active head {active_head} from the register)",
              file=sys.stderr)
    else:
        head = supplied
    if supplied and not active_head:
        print(f"review verdict: {args.name} is not head-keyed, but --head was "
              f"given; refusing rather than writing a shard the tally ignores",
              file=sys.stderr)
        return 1

    # APPEND-ONLY (coord-boss ruling b99fb8da, after codex reproduced a
    # concurrent CHANGES being overwritten by APPROVE at rc 0). The name is
    # unique to this write, so it can never touch a file another writer holds —
    # which closes verb-vs-verb AND verb-vs-hand races WITHOUT a store
    # primitive. There is deliberately NO existence check here: the previous
    # check-then-write could not keep the promise it printed, and a promise the
    # code cannot keep is the false-success family one level up.
    #
    # A correction is therefore a NEW file, and the original evidence stays on
    # disk — which is also the same-head correction path codex asked for.
    # SECOND precision in the name and the shard ts. `_iso` carries
    # microseconds, which the append-suffix pattern rejects — the filename then
    # parsed as a reviewer literally called `codex-reviewer--2026-...-<digest>`
    # and the tally credited a phantom while the real reviewer read as pending.
    # Mixed precision would also misorder a plain shard against an append one
    # when compared as strings.
    # ONE instant, sampled ONCE (both reviewers, review-winning-envelope r3):
    # sampling _now() twice let a verdict straddle a UTC second — named :10Z,
    # stamped :11.xZ — and canonical_sort_key then rightly discarded the
    # cross-second fraction, so the later correction sorted as :10.000000 and
    # LOST to an earlier :10.900000 shard. Name second and frontmatter fraction
    # must come from the same reading.
    stamp = _now().astimezone(timezone.utc)
    now_iso = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = secrets.token_hex(8)
    # The random nonce is part of the NAME (codex-reviewer, r7 secondary): without
    # it, two identical same-second filings collided and overwrote — an append-only
    # claim with a non-unique name.
    digest = hashlib.sha1(
        f"{reviewer}|{normalized}|{getattr(args, 'note', None) or ''}|{nonce}"
        .encode()).hexdigest()[:8]
    filename = review.verdict_filename(reviewer, head=head, ts=now_iso,
                                       digest=digest)
    path = _verdicts_prefix(args.team, args.name) + filename
    # NAME WHAT THIS VERDICT SUPERSEDES (review-winning-envelope r5). The fold
    # no longer orders corrections by clock: an APPROVE lifts a prior CHANGES
    # only by naming it. So list this reviewer's prior shards for this head and
    # name every one. If the listing is degraded, name NOTHING and say so — an
    # unseen CHANGES keeps dominating, which is the fail-closed answer.
    supersedes: list[str] = []
    listing_ok = True
    unquotable: list[str] = []
    try:
        for e in transport.list_dir(_verdicts_prefix(args.team, args.name)):
            n = str(e.get("name") or "")
            parsed = review.parse_verdict_filename(n, head=head)
            if not (parsed and parsed[0] == reviewer and n != filename):
                continue
            # QUOTE THE TARGET'S CURRENT BYTES (r8): the edge binds the content
            # digest, and the fold honours it only if the STORE's mtime proves
            # the target earlier than this shard. A prior that cannot be READ
            # cannot be named — it stays live, which is the fail-closed answer.
            prior = transport.read(_verdicts_prefix(args.team, args.name) + n)
            if prior is None:
                unquotable.append(n)
                continue
            supersedes.append(f"{n}@sha256:{review.content_digest(prior)}")
    except TransportError:
        # IF YOU CANNOT ENUMERATE THE PRIORS YOU CANNOT CLAIM TO SUPERSEDE THEM
        # (coord-boss, ratification 1bce3da9): a supersedes list computed from a
        # partial listing is a false claim of coverage. Name NOTHING.
        listing_ok = False
        supersedes = []

    body = okf.render_frontmatter({
        "type": "Verdict",
        "reviewer": reviewer,
        "head": head,
        "verdict": normalized,
        # Microseconds live HERE, not in the name (the name stays second-
        # precision for the reasons above); the fold uses this fraction only to
        # order shards that share the name's second. See review.canonical_sort_key.
        "ts": _iso(stamp),
        "nonce": nonce,
        "supersedes": sorted(supersedes),
    }) + f"\n{getattr(args, 'note', None) or normalized}\n"
    if unquotable:
        print(f"review verdict: {len(unquotable)} prior shard(s) of yours could not be read, "
              f"so this verdict does NOT supersede them; a CHANGES among them still dominates "
              f"until you re-file when they are readable",
              file=sys.stderr)
    if not listing_ok:
        print(f"review verdict: could not list your prior shards for {args.name} — "
              f"this verdict supersedes NOTHING; an existing CHANGES of yours "
              f"still dominates the fold until you re-file with a working listing",
              file=sys.stderr)
    if not transport.write(path, body):
        print(f"review verdict: write FAILED for {path} — the verdict did NOT "
              f"land, so the review still awaits you", file=sys.stderr)
        return 1
    # A NEW VERDICT INVALIDATES THE FOLD CACHE (codex-reviewer, 595 r3).
    # Without this the same-head correction contract is FALSE the moment a prior
    # result has settled: APPROVE -> `review status` writes `.settled` -> a later
    # CHANGES correction lands and is ignored, because the projection treats any
    # marker hit as immutable approval and never reads the shards. rc 0, both
    # shards on disk, and readers still say APPROVED.
    #
    # ONLY THE CACHE. `.settled` carries two meanings (572/588): `state:
    # APPROVED` is a recomputable tally cache and is clearable; `state: MERGED`
    # with a merge_sha is EVIDENCE that a merge happened and must never be
    # destroyed by a late verdict. UNKNOWN is not permission either.
    # Absent vs unreadable FIRST: `_classify_settled_marker` folds both into
    # `unknown` (its docstring says so), and "no marker yet" is the COMMON case
    # — treating that as ambiguous would fail every ordinary verdict. The
    # tri-state listing separates them.
    present = _settled_marker_present(transport, args.team, args.name)
    if present is None:
        print(f"review verdict: recorded at {path}, but I cannot tell whether a "
              f"settle marker exists for {args.name}, so I cannot tell whether "
              f"a stale cache is hiding this verdict. Verify with "
              f"`review status`.", file=sys.stderr)
        return 3
    if present:
        state = _classify_settled_marker(transport, args.team, args.name)
        if state == SETTLED_CACHE:
            if not _clear_settled_marker(transport, args.team, args.name):
                print(f"review verdict: recorded at {path}, but the stale fold "
                      f"cache for {args.name} could NOT be cleared — readers "
                      f"may still report the previous result. Clear it before "
                      f"trusting the tally.", file=sys.stderr)
                return 3
        elif state == SETTLED_MERGED:
            print(f"review verdict: recorded at {path}, but {args.name} is "
                  f"already closed as MERGED — that marker is EVIDENCE, not a "
                  f"cache, so it stands. Your verdict is on disk and does not "
                  f"change the merge record.", file=sys.stderr)
        else:
            print(f"review verdict: recorded at {path}, but the settle marker "
                  f"for {args.name} is UNRECOGNISED, so I will not delete it "
                  f"and cannot promise readers see this verdict.",
                  file=sys.stderr)
            return 3

    # Tell the chokepoint which artifact this was, so the work event names it.
    record_activity_artifact(args, path)
    print(f"verdict {normalized} recorded for {args.name} at {path}")
    return 0


def cmd_review_status(args: argparse.Namespace, transport: Any) -> int:
    team, slug = args.team, args.slug
    authority = _PUBLIC_READ_CONTEXT.get()
    if authority is not None:
        section = authority.section("reviews")
        rows = section.get("rows") if isinstance(section, Mapping) else None
        row = next((item for item in (rows or [])
                    if isinstance(item, dict) and item.get("name") == slug), None)
        if row is None:
            print(f"review status failed: {slug} is absent from the validated "
                  "generation — tally unknown", file=sys.stderr)
            return 1
        tally = row.get("tally")
        required_fields = {
            "state", "approvals", "changes", "required", "pending_required",
            "evidence", "of",
        }
        if (not isinstance(tally, dict)
                or not required_fields.issubset(tally)
                or not all(isinstance(tally.get(key), list)
                           for key in ("approvals", "changes", "required",
                                       "pending_required"))):
            print(f"review status failed: validated generation row for {slug} "
                  "does not prove the full direct tally — reconcile with a "
                  "compatible writer", file=sys.stderr)
            return 3
        if not isinstance(tally.get("winning"), dict):
            # A generation written by a projection that did not record which
            # shard won cannot serve the winning identity a ship gate consumes,
            # and silently returning the tally without it would let the direct
            # and generation-backed readers answer differently. Fail closed.
            print(f"review status failed: validated generation row for {slug} "
                  "does not record the winning shard per reviewer — reconcile "
                  "with a compatible writer", file=sys.stderr)
            return 3
        result = deepcopy(tally)
        result.update({"team": team, "slug": slug, "contract": 2})
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
        unproven = bool(result.get("unattributable")
                        or result.get("unrecognised_verdicts")
                        or result.get("head_mismatched_verdicts"))
        return 3 if unproven else 0
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
    _marker_unknown = False
    if _is_settleable(result):
        # PROVEN terminal-settled (non-empty required, every listed verdict read):
        # refresh the fold cache so the fan-out fold can skip this slug next time.
        if _write_settled_marker(
                transport, team, slug, now=_iso(_now()),
                evidence=result.get("evidence")) == "kept-unknown":
            # Same register as the F4 branch below: a marker this build cannot
            # classify was PRESERVED, and the caller must not read a clean tally
            # as "everything here is understood" (codex-reviewer, 588 r1 —
            # report consistently with the existing unknown-marker discipline).
            print(f"review status: {slug} carries a `.settled` marker this "
                  f"build cannot classify — unreadable, an unrecognised "
                  f"state:, or a FUTURE schema. PRESERVED, and the settled "
                  f"cache was NOT refreshed over it.", file=sys.stderr)
            _marker_unknown = True
    else:
        # F4: a full, trustworthy tally that is NOT settleable, yet a `.settled`
        # marker may linger. Which of the marker's TWO meanings it carries decides
        # what we may do — see `_classify_settled_marker`. Before PR 561 there was
        # only one meaning and this branch deleted unconditionally, which is how
        # `review status` came to destroy merge evidence just by being run.
        marker_state = _classify_settled_marker(transport, team, slug)
        if marker_state == SETTLED_CACHE:
            # Recomputable: drop it so the next fan-out fold sees the pending
            # obligation. Best-effort (delete is timeout-safe -> False, ignored).
            transport.delete(_settled_marker_path(team, slug))
        elif marker_state == SETTLED_UNKNOWN and (
                _settled_marker_present(transport, team, slug) is not False):
            # A marker EXISTS but this build could not classify it — unreadable
            # under a degraded transport, or a `state:` it does not know. PRESERVE
            # it (never delete what we failed to parse) and say so LOUDLY: a
            # silent keep leaves a marker suppressing the fan-out fold with
            # nothing on any surface explaining why. rc is nonzero so an
            # unattended caller cannot read this as a clean tally.
            # `is not False` covers BOTH "a marker is definitely there" and
            # "we could not even determine presence". Only a POSITIVE absence
            # (listing succeeded, no marker) is silent — anything less certain
            # fails closed, which is the promise the two-state version broke.
            print(f"review status: {slug} may carry a `.settled` marker this "
                  f"build cannot classify — unreadable, an unrecognised "
                  f"state:, or MERGED without a well-formed merge_sha. "
                  f"PRESERVED, not deleted. The fan-out fold may skip this slug "
                  f"until it is resolved.", file=sys.stderr)
            _marker_unknown = True
        # SETTLED_MERGED, or absent: nothing to do. Merge evidence is not a cache
        # and is never dropped by a tally recompute.
    result.update({"team": team, "slug": slug, "contract": 2})
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
    # Verdicts that EXIST but could not be counted. Printed even in JSON mode's
    # sibling branch above (they ride in the payload), and always to stderr here
    # so a reviewer reading `awaiting required: <themselves>` is not left to
    # conclude they forgot to vote when the file is sitting right there.
    for name in result.get("unattributable") or []:
        print(f"review status: {slug} carries a verdict file that names no "
              f"round and no reviewer: {name}. It is NOT counted. RULE: the "
              f"text before `--` is read as a commit head, and this one is not "
              f"a 40/64-hex id, so the file can be attributed to no round and "
              f"no reviewer. A verdict filename is either `<reviewer>.md` or "
              f"`<head>--<reviewer>.md` — the request breadcrumb prints the "
              f"exact path to use.", file=sys.stderr)
    for row in result.get("unrecognised_verdicts") or []:
        print(f"review status: {slug} has a verdict in {row['file']} whose "
              f"`verdict: {row['verdict']}` is outside the accepted vocabulary "
              f"[{review.accepted_vocabulary()}], so it does NOT count toward "
              f"the tally. The file was read; only the token was unrecognised.",
              file=sys.stderr)
    for row in result.get("head_mismatched_verdicts") or []:
        print(f"review status: {slug} has a verdict at {row['file']} whose own "
              f"frontmatter claims head {row['claimed_head'] or '(none)'}, which "
              f"is not this round's head. It is NOT counted, and that is "
              f"deliberate — a verdict must independently attest the exact "
              f"commit it reviewed, so a copied or stale shard cannot discharge "
              f"a new round. Reported rather than dropped: the file exists and "
              f"its author believes they voted.", file=sys.stderr)
    # An unclassifiable marker is a DEGRADED answer, not a clean tally: the slug
    # may stay invisible to the fan-out fold and this verb cannot say why. rc 3,
    # the established UNKNOWN code, so an unattended caller fails closed.
    #
    # An uncounted verdict is the same register of answer: the tally is not the
    # truth about who reviewed, and reporting `pending_required: [x]` while a
    # file from x sits unread is a falsehood, not an omission. Same rc.
    _uncounted = bool(result.get("unattributable")
                      or result.get("unrecognised_verdicts")
                      or result.get("head_mismatched_verdicts"))
    return 3 if (_marker_unknown or _uncounted) else 0


# --- continuity (fulcra-agent-continuity snapshots) ---

def _continuity_path(team: str, agent: str, task: str) -> str:
    return f"team/{team}/member/{agent}/continuity/{task}/latest.json"


def _continuity_latest_path(team: str, agent: str) -> str:
    """One pointer per agent naming their newest snapshot.

    Exists because a listing cannot date a COLLECTION: 0 of 161 directory
    entries under an agent's continuity prefix carry an mtime, and task dirs are
    slugs with no ordering, so "when did this agent last checkpoint" otherwise
    costs one read per task. Measured: 203 reads ~149s for three agents, which
    is why `health` was killed at 240s and again at 590s. Upstream register U8.

    A write-side pointer converts that to ONE read per agent. It is a
    convention, not an invariant — the store has no conditional write and
    nothing forces a writer to maintain it — which is exactly why the reader
    treats a MISSING pointer as UNKNOWN rather than as "no snapshots".
    """
    return f"team/{team}/member/{agent}/continuity/LATEST.json"


def _continuity_prefix(team: str, agent: str) -> str:
    return f"team/{team}/member/{agent}/continuity/"


def _checkpoint_moment(transport: Any, team: str, snap: dict[str, Any],
                       path: str) -> None:
    """Cast a checkpoint's shadow on the timeline. NEVER fails the caller.

    THE FAIL-OPEN RULE, deliberately the inverse of park's loud one. ``park``
    exits non-zero and shouts ``CHECKPOINT NOT WRITTEN`` when it cannot save,
    because a silently-skipped park discards the state the next session wakes
    on. Emission is the opposite: **the checkpoint file is the source of truth
    and the moment is its shadow**, so a failure here costs a row in a
    visualization, while failing the park over it would cost the checkpoint
    itself. One line on stderr, exit code untouched, always.

    The snapshot is read for every field so the moment can never disagree with
    the bytes on disk — same agent, same task, same objective.
    """
    try:
        checkpoint_channel.emit(
            transport, team, agent=str(snap.get("agent") or ""),
            task=str(snap.get("task") or ""), objective=snap.get("objective"),
            path=path)
    except Exception as exc:  # unreachable by contract; the rule is absolute
        print(f"checkpoint moment: emission failed ({exc!r}) — the checkpoint "
              f"file at {path} was written and is unaffected", file=sys.stderr)


def cmd_continuity_snapshot(args: argparse.Namespace, transport: Any) -> int:
    task = tasks.slugify(args.task)  # single path segment; a slash breaks the no-task fold
    snap = continuity.build_snapshot(
        agent=args.agent, task=task, objective=args.objective, now=_iso(_now()),
        decisions=args.decision, next_actions=args.next, open_questions=args.open_question,
        artifacts=args.artifact, context_used_percent=args.context_percent,
        transcript_path=args.transcript,
    )
    path = _continuity_path(args.team, args.agent, task)
    wrote = transport.write(path, json.dumps(snap, indent=2))
    if wrote is False:
        # The failure was ALREADY KNOWN here and was being spent on the
        # cosmetic decision below while the exit code and the success line went
        # out unchanged. A continuity snapshot is the durability mechanism: a
        # park that reports success without reaching the store leaves the
        # successor resuming from a stale checkpoint believing it is current —
        # and that happens exactly when the host is in trouble, which is when
        # parking matters most. Found live during a bus outage, where
        # `snapshot <id>` printed and rc was 0 with the store unreachable.
        print(f"continuity snapshot FAILED to persist: {path} was not written "
              f"(transport failure, not a rejected write). NOTHING was saved — "
              f"a successor resuming now would read the PREVIOUS checkpoint and "
              f"believe it is current. Re-run when the store is reachable.",
              file=sys.stderr)
        return 3
    print(f"snapshot {snap['checkpoint_id']}")
    # The pointer is written only AFTER the snapshot itself is known-persisted.
    # A pointer to a checkpoint that does not exist is worse than no pointer: a
    # reader would take it as evidence of a save that never happened, which is
    # the failure this whole verb was just fixed for.
    ptr_path = _continuity_latest_path(args.team, args.agent)
    # MONOTONICITY, best-effort. There is no conditional write in this store
    # (upstream register U3), so two snapshots racing can land out of order and
    # an OLDER invocation can clobber a newer pointer. Read-then-write closes
    # the common case; it cannot close the race, and it does not pretend to.
    prev_ts = None
    prev_raw = transport.read(ptr_path)
    if prev_raw is not None:
        try:
            prev_ts = continuity._parse_created_at(json.loads(prev_raw).get("created_at"))
        except (ValueError, TypeError, AttributeError):
            prev_ts = None
    mine_ts = continuity._parse_created_at(snap.get("created_at"))
    if prev_ts is not None and mine_ts is not None and prev_ts > mine_ts:
        print(f"continuity snapshot: LEFT the existing LATEST pointer alone — it "
              f"names a NEWER checkpoint than this one, so overwriting it would "
              f"move {args.agent}'s reported age BACKWARDS.", file=sys.stderr)
        ptr_ok = True
    else:
        ptr_ok = transport.write(ptr_path, json.dumps({
            "schema": "coord.continuity-latest.v1",
            "agent": args.agent,
            "task": task,
            "checkpoint_id": snap["checkpoint_id"],
            "created_at": snap.get("created_at"),
            "path": path,
        }, indent=2))
    if ptr_ok is False:
        # codex-reviewer, 585 r1: a FAILED pointer update does not make the
        # pointer missing. If an older LATEST.json is already there it SURVIVES,
        # and the audit then reads a stale timestamp as authoritative and calls
        # an agent who just checkpointed STALE — manufacturing exactly the false
        # finding this design claims to prevent, and making the message below
        # ("will report UNKNOWN") a lie.
        #
        # With no conditional write, the only way to stop a stale cache being
        # believed is to REMOVE it. Deleting loses nothing recoverable: the
        # pointer is a cache, the snapshots behind it are untouched, and a
        # missing pointer is UNKNOWN, which is the honest answer here.
        stale_gone = True
        if prev_raw is not None:
            stale_gone = (transport.delete(ptr_path)
                          if hasattr(transport, "delete") else False)
        if stale_gone is False:
            # Unrecoverable by this process: a stale pointer is in place and
            # will be read as current. That is a WRONG ANSWER waiting to be
            # given, not a degradation, so it is the loudest thing this verb
            # says and it changes the exit code.
            print(f"continuity snapshot: SAVED, but the LATEST pointer for "
                  f"{args.agent} could not be updated OR removed. A STALE "
                  f"pointer is still in place and `health` will read it as "
                  f"current — it may report this agent stale while they are "
                  f"actively checkpointing. Remove "
                  f"{ptr_path} by hand, or re-run when the store is healthy.",
                  file=sys.stderr)
            return 3
        print(f"continuity snapshot: saved, but the LATEST pointer for "
              f"{args.agent} was not written"
              + (" (the previous one was removed, so nothing stale survives)"
                 if prev_raw is not None else "")
              + f" — `health` will report this agent's snapshot age as UNKNOWN "
                f"until the next successful snapshot.", file=sys.stderr)
    # Only a SUCCESSFUL save casts a shadow: a moment for a checkpoint that is
    # not in the store would be a visualization of work that does not exist.
    _checkpoint_moment(transport, args.team, snap, path)
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
    age_seconds = continuity.checkpoint_age_seconds(snap, now=_now())
    error_code = None
    max_age_seconds = None
    if args.max_age is not None:
        max_age_seconds = continuity.parse_duration_seconds(args.max_age)
        if max_age_seconds is None:
            error_code = "invalid-max-age"
        elif age_seconds is None:
            error_code = "checkpoint-age-unknown"
        elif age_seconds > max_age_seconds:
            error_code = "checkpoint-stale"
    if args.json:
        out = dict(snap) if snap else {"snapshot": None}
        out["checkpoint_age_seconds"] = age_seconds
        out["error_code"] = error_code
        jsonutil.print_json(out)
    else:
        print(continuity.render_resume(snap))
        print(f"  checkpoint age: {continuity.format_age(age_seconds)}")
    if args.max_age is not None:
        if error_code == "invalid-max-age":
            print(f"resume: invalid --max-age duration {args.max_age!r}; use s, m, h, or d",
                  file=sys.stderr)
            return 2
        if error_code == "checkpoint-age-unknown":
            print("resume: checkpoint age is unknown; freshness requirement failed",
                  file=sys.stderr)
            return 2
        if error_code == "checkpoint-stale":
            print(f"resume: checkpoint is {continuity.format_age(age_seconds)} old, "
                  f"exceeding --max-age {args.max_age}", file=sys.stderr)
            return 2
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


FYI_TAG = "mode:fyi"


def _directive_slug(args: argparse.Namespace, assignee: Optional[str]) -> str:
    """THE one place a directive's durable slug is derived (codex-reviewer, 605 r3).

    r3 added notification mode to the identity but left TWO post-write callers
    recomputing the OLD four-field slug by hand: `cmd_remind` scheduled its future
    event against a slug no document had, and `tell --fyi --closes` closed a
    parent with evidence naming a reply that did not exist. Both wrote correctly
    and then pointed somewhere else — a dangling ptr is worse than a failure,
    because it reports success.

    Three hand-rolled copies of one derivation is the defect; a fourth caller
    would have reintroduced it. Everything that needs this slug asks here.
    """
    return (f"{tasks.slugify(args.title)}-"
            f"{_payload_hash(_directive_payload(args.title, args.summary, args.next, assignee, fyi=bool(getattr(args, 'fyi', False))))}")


def _directive_payload(title: Optional[str], summary: Optional[str],
                       next_action: Optional[str],
                       assignee: Optional[str],
                       fyi: bool = False) -> tuple[str, ...]:
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
    with a new schedule or priority, send a new title.

    NOTIFICATION MODE IS IDENTITY (codex-reviewer, 605 r2). The slug was computed
    before `--fyi` was consulted, so the same text sent as a notification and as a
    real ask landed on ONE path — and the dedupe then resolved the collision in
    whichever direction happened to arrive first. FYI first, ask second: the ask
    dedupes onto a `done` row and emits no companion event, so genuine work is
    silently absent from both the obligation plane and the recipient's event
    window. Ask first, FYI second: the FYI dedupes onto a `proposed` row and the
    no-obligation promise is simply false. An FYI and an ask are DIFFERENT
    messages; they must occupy different paths.

    Only the notification case appends a marker, so every ordinary directive's
    hash — and therefore every slug already in the store — is byte-identical to
    before."""
    def norm(x: Optional[str]) -> str:
        return "" if x is None else str(x)
    base = (norm(title), norm(summary), norm(next_action), norm(assignee))
    return base + ("fyi",) if fyi else base


def _doc_payload(doc: Optional[str]) -> Optional[tuple[str, str, str, str]]:
    """Message-identity payload of an existing task doc, or ``None`` when its
    frontmatter won't parse. On the write path an unparseable/corrupt doc at our
    canonical (hash-bearing) slot can no longer be a colliding DIFFERENT message —
    only corruption — so the caller fails loud (cannot verify delivery) rather
    than overwriting: never claim a delivery we can't confirm."""
    fm = okf.parse_frontmatter(doc)
    if fm is None:
        return None
    # Mode is read from an EXPLICIT tag, never inferred from status. A completed
    # ordinary directive is also terminal, so inferring would make every finished
    # ask start reading as a notification — the same two-states-into-one collapse
    # this identity fix exists to remove.
    return _directive_payload(fm.get("title"), fm.get("description"),
                              fm.get("next_action"), fm.get("assignee"),
                              fyi=(FYI_TAG in (fm.get("tags") or [])))


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
            # Outcome signal for callers that act differently on dedupe vs
            # fresh write (remind's timer emission): set ONLY at the two
            # verified success exits, so "written" can never mean "unverified".
            args._directive_outcome = "deduped"
            args._directive_existing = existing
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
    args._directive_outcome = "written"
    print(f"directive {slug} -> {assignee}"
          + (f" (visible {not_before})" if not_before else ""))
    return 0


def _create_directive(args: argparse.Namespace, transport: Any, *, assignee: str,
                      not_before: Optional[str] = None) -> int:
    # The canonical directive path ALWAYS carries the payload hash: identical
    # payloads (any senders, any order) converge on one path and dedupe by
    # construction; distinct payloads occupy distinct paths and can never race.
    fyi = bool(getattr(args, "fyi", False))
    payload = _directive_payload(args.title, args.summary, args.next, assignee,
                                 fyi=fyi)
    slug = _directive_slug(args, assignee)
    # NOTIFICATIONS DO NOT OPEN (2026-08-11). `tell` minted EVERY message as a
    # `proposed` row that only the recipient could close — so a report, an ack or
    # an FYI became a permanent obligation nobody could discharge, because there
    # was never anything to do. Measured on this bus 2026-08-11: 1239 of 1250
    # proposed rows were dispatch, and two agents authored 79% of them doing
    # exactly this.
    #
    # This is Ruling 1's sibling one plane over (PR 561: a merged PR closes its
    # review as an ARTIFACT of the merge). Closure belongs to the terminal event,
    # not to a separate discipline step nobody performs. A notification's
    # terminal event IS its delivery, so it is born closed and never enters the
    # open pile — while still writing its durable ptr doc and still emitting the
    # companion event that puts it in the recipient's queue. Delivery is
    # unchanged; only the false obligation goes away.
    try:
        _, content = tasks.new_task_doc(
            args.title, now=_iso(_now()), workstream=args.workstream,
            status=("done" if fyi else "proposed"), priority=args.priority,
            owner=_identity(getattr(args, "sender", None)), assignee=assignee,
            summary=args.summary or "", next_action=args.next, kind="directive",
            not_before=not_before, slug=slug, fyi=fyi,
            evidence=("notification delivered; no action was requested of the "
                      "assignee" if fyi else None),
        )
    except tasks.TaskError as e:
        print(f"directive failed: {e}", file=sys.stderr)
        return 1
    rc = _write_directive(transport, args, slug=slug, content=content,
                          payload=payload, assignee=assignee, not_before=not_before)
    if (rc == 0 and not_before is None
            and getattr(args, "_directive_outcome", None) == "written"):
        _emit_dispatch_companion(transport, args, slug=slug, assignee=assignee)
    # On a delivered ask (not a backlog capture — @backlog awaits no reply), point
    # the sender at the reply leg: the return of `respond` surfaces in their queue.
    if rc == 0 and assignee != directives.BACKLOG:
        sender = _known_sender(args)
        if sender:
            print(_replies_breadcrumb(args.team, sender))
    return rc


def _emit_dispatch_companion(transport: Any, args: argparse.Namespace, *,
                             slug: str, assignee: str) -> None:
    """One ``v:1`` bus event pointing at the durable directive doc.

    WHY A COMPANION EVENT AND NOT A RESHAPED PROJECTION. `tell` writes a durable
    task document and the activity projection annotates it on the timeline —
    but that projection's note is PROSE, and the documented queue filter keeps
    only notes that parse as JSON with ``"v":1``. So for three days every
    dispatch was durable, correctly recorded, and invisible to the recipient's
    queue: 2026-08-06, `queue --peek` CLEAR while two P1 tasks sat unstarted
    (see the coord-boss RCA and its two-defects follow-up).

    The alternative fix was to make the projection itself emit a ``v:1`` note.
    Rejected, and this is the design call the RCA asked the lane holder to make
    explicitly:

    - The projection is the TIMELINE surface; prose is its product value. Making
      it JSON to satisfy a control-plane filter subordinates the product to the
      transport.
    - The projection fires on EVERY transition (create/pickup/update/complete).
      Routing all of those onto the bus would put a stream of updates nobody
      addressed in front of every queue reader — the "non-routable noise" that
      :mod:`coord_engine.checkpoint_channel` gave a separate stream to avoid.
      A dispatch is the subset that has a recipient and warrants delivery.
    - This ships on the CURRENT engine pin. The channel fix lives in
      ``fulcra-common``, which the fleet installs from a pinned tag, so it
      reaches nobody until that pin moves.

    Fires ONLY on a verified fresh write — never on a dedupe (a second event for
    an already-delivered slug is noise the recipient cannot distinguish from new
    work) and never on failure. Also never for a DEFERRED directive: `remind`
    already emits its own future-dated record timed to ``not_before``, and a
    companion sent now would surface the reminder immediately, which is the one
    thing a reminder must not do. Best-effort by design: the durable doc is the
    truth and this is delivery, so a bus that is down or unconfigured degrades to
    file-plane-only exactly as `remind`'s timer does, and NEVER fails the tell.
    """
    if assignee == directives.BACKLOG:
        return  # nobody is being dispatched; @backlog awaits no reply
    sender = _known_sender(args) or _host()
    cfg, _status = records.load_config_classified(transport, args.team)
    if not cfg:
        print("record: no bus-v3 records config — dispatch rides the file "
              "plane only (recipient must run `needs-me`)")
        return
    try:
        # TRANSLATE THE EVERYONE-TOKEN AT THE PLANE BOUNDARY. The task plane
        # says "*" and the event plane says "all" (records.BROADCAST); the
        # reader filter is `to in (agent, BROADCAST)`, so an event addressed to
        # "*" matches NOBODY. Passing the assignee straight through was correct
        # for every DIRECTED dispatch -- the two strings coincide there, which
        # is why tell worked and was tested -- and silently dropped every
        # BROADCAST. Neither plane is wrong on its own; the defect was that
        # nothing translated between them (coord-boss, 2026-08-07).
        to = records.BROADCAST if assignee == directives.EVERYONE else assignee
        ok = records.emit_event(
            transport, cfg, sender=sender, to=to, kind="directive",
            priority=getattr(args, "priority", None) or "P2", slug=slug,
            ptr=_task_path(args.team, slug).split("/", 2)[-1],
            fyi=bool(getattr(args, "fyi", False)),
            team=args.team)
    except Exception as e:
        print(f"record: dispatch companion not emitted ({e}) — dispatch rides "
              f"the file plane only", file=sys.stderr)
        return
    if not ok:
        print("record: dispatch companion emission failed — dispatch rides the "
              "file plane only (recipient must run `needs-me`)")


def _emit_response_companion(transport: Any, team: str, *, slug: str,
                             owner: str, responder: str, shard_ptr: str,
                             for_agent: Optional[str] = None) -> bool:
    """One ``v:1`` bus event telling the ASKER their directive was answered.

    THE MIRROR OF THE DISPATCH COMPANION, and it was missing. `tell` emits a
    companion so a dispatch reaches the recipient's queue; `respond` emitted
    nothing, so the reply reached nobody. The queue reads EVENTS — a shard under
    ``_coord/responses/`` is durable, correct, and cannot appear there.

    What that cost on 2026-08-08: coord-boss asked at 18:15, was answered via
    `respond` at 18:57 (task `done`, shard written), and re-asked at 20:14
    believing nothing had come — then attributed the silence to their own
    message rather than to the reply leg. Using the verb correctly produced
    silence, so "answered" was indistinguishable from "ignored".

    Note the asymmetry this closes: `tell --closes` already delivered, because
    the reply IS a tell and tells emit companions. Plain `respond` did not, so
    the two closure paths had opposite notification behaviour and the
    recommended one was the silent one.

    Returns True ONLY when a distinct OWNER was actually notified, so the
    caller can tell the truth about delivery instead of asserting it.

    THE CLOSE EMITS EVEN WHEN THERE IS NOBODY TO TELL (2026-08-21, pilot
    round-trip probe): this event is dual-purpose. It notifies the asker AND it
    is the close the stream fold discharges by — so a self-answered, unowned,
    or backlog-parked task must still emit it, addressed to the responder
    themselves, or the fold replays the obligation open forever. The pilot
    caught exactly that: a self-assigned probe closed in the doc and stayed
    open in the seeded stream fold. ``for_agent`` names whose copy discharges
    (the assignee); it defaults to the responder.

    Best-effort by design: the shard is the record and this is delivery, so a
    bus that is down or unconfigured degrades to file-plane-only and NEVER
    fails the respond.

    ``for_agent`` arrives in TASK-PLANE vocabulary (the doc's assignee) and is
    translated to stream vocabulary here: a broadcast task stores ``"*"``, the
    stream's broadcast token is ``"all"``, and emitting ``"*"`` verbatim would
    match no reader — a terminal transition on a broadcast would close NOBODY
    (codex-reviewer, PR 671 round 2).
    """
    if for_agent == directives.EVERYONE:
        for_agent = records.BROADCAST
    cfg, _status = records.load_config_classified(transport, team)
    if not cfg:
        print("record: no bus-v3 records config — the response rides the file "
              "plane only")
        return False
    owner_notified = bool(owner) and owner not in (directives.BACKLOG, responder)
    try:
        if owner == directives.EVERYONE:
            to = records.BROADCAST
        else:
            to = owner if owner_notified else responder
        ok = records.emit_event(
            transport, cfg, sender=responder, to=to, kind="response",
            priority="P2", slug=slug, ptr=shard_ptr, team=team,
            for_agent=for_agent or responder)
    except Exception as e:
        print(f"record: response companion not emitted ({e}) — the response "
              f"rides the file plane only", file=sys.stderr)
        return False
    if not ok:
        print("record: response companion emission failed — the response rides "
              "the file plane only")
        return False
    return owner_notified


def _deliver_review_directive(transport: Any, team: str, slug: str, reviewer: str,
                              *, sender: str, of: str,
                              head: Optional[str] = None) -> int:
    """Deliver ONE review-request directive to ``reviewer`` via the canonical
    hash-slug directive path — the SAME ``_write_directive`` delivery (payload-hash
    dedup + C1 write-verification) every ``tell`` gets, so a verb-opened review
    NOTIFIES its reviewers instead of relying on a hand-sent tell (the PR-344
    orphan class: a review directive sent by hand, with no verdict target). The
    text carries the exact slug AND the verdict-file path (the fail-closed watcher
    contract). Returns ``_write_directive``'s rc (0 delivered/deduped, 1 failed).

    Distinct (slug, head, reviewer) tuples produce distinct payloads -> distinct
    paths, so reviewers and rounds never collide while a same-head re-request
    idempotently dedupes.

    A verified FRESH write also emits the same ``v:1`` companion event ``tell``
    emits (pr-630 root cause #2, bitten live 2026-08-14 on agent-skills pr-176:
    this path wrote the durable task but no event, so a raw queue read had
    nothing to deliver and the reviewer learned of the request only from a
    ``needs-me`` fold — or from the operator). Same contract as the tell path:
    fresh-write-only (never on dedupe), best-effort (a down or unconfigured bus
    degrades to file-plane-only and never fails the request)."""
    verdict_file = (
        f"{_verdicts_prefix(team, slug)}"
        f"{review.verdict_filename(reviewer, head=head)}"
    )
    title = f"{_REVIEW_REQUEST_TITLE_PREFIX}{slug}"
    summary = f"Verdict owed on {of} — file it at {verdict_file}"
    if head:
        summary += f" after reviewing exact head {head}"
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
    # The namespace carries team for `_write_directive` plus sender/priority for
    # the companion emit (`_known_sender` reads .sender; review dispatch is P1
    # per the pr-630 design).
    ns = argparse.Namespace(team=team, sender=sender, priority="P1")
    rc = _write_directive(transport, ns, slug=dslug,
                          content=content, payload=payload, assignee=reviewer,
                          not_before=None)
    if rc == 0 and getattr(ns, "_directive_outcome", None) == "written":
        _emit_dispatch_companion(transport, ns, slug=dslug, assignee=reviewer)
    return rc


def _close_answered_directive(transport: Any, args: argparse.Namespace, *,
                              reply_slug: str) -> int:
    """Close the directive this reply answers, with the reply as the artifact.

    Closure is an ARTIFACT of the recipient acting — the same shape as
    `review close` carrying a merge sha. The reply already exists, so nothing
    new has to be remembered by anyone.

    Fails LOUD on an unresolvable slug, for the reason `respond` does: writing a
    close against a name nobody owns GHOST-CLOSES — the record lands under a
    slug that does not exist while the real directive stays open in the owner's
    needs-me forever.
    """
    target = args.closes
    path = _task_path(args.team, target)
    doc = transport.read(path)
    if doc is None:
        print(f"tell: --closes {target!r} resolves to no directive in "
              f"team/{args.team} (absent or unreadable) — the reply was sent, "
              f"NOTHING was closed. Use the exact hash-suffixed slug from "
              f"`inbox`/`briefing --json`, not the display title.",
              file=sys.stderr)
        return 1
    agent = _known_sender(args) or _host()

    # RELATIONSHIP GUARD (codex 564 r3). A resolvable slug is not permission to
    # close it: a mistyped-but-valid slug would close unrelated work, and the
    # only signal would be somebody else's row going quiet. The reply must
    # actually answer THIS directive — it must be a directive, addressed to the
    # agent replying, and owned by the agent being replied to.
    fm = okf.parse_frontmatter(doc) or {}
    recipient = getattr(args, "assignee", None)
    problems = []
    if "kind:directive" not in (fm.get("tags") or []):
        problems.append("it is not a directive")
    if str(fm.get("assignee") or "") != agent:
        problems.append(f"it is assigned to {fm.get('assignee')!r}, not to you "
                        f"({agent})")
    if recipient and str(fm.get("owner") or "") != recipient:
        problems.append(f"it is owned by {fm.get('owner')!r}, but this reply "
                        f"goes to {recipient!r}")
    if problems:
        print(f"tell: --closes {target!r} is not a directive this reply "
              f"answers — {'; '.join(problems)}. The reply was sent, NOTHING "
              f"was closed.", file=sys.stderr)
        return 1

    evidence = f"answered by {agent} in {reply_slug}"
    try:
        out = tasks.apply_update(doc, now=_iso(_now()), status="done",
                                 evidence=evidence)
        transport.write(path, out)
    except tasks.TaskError as e:
        print(f"tell: reply sent; {target} NOT closed ({e})", file=sys.stderr)
        return 1

    # VERIFY THE WRITE LANDED (codex 564 r3). `review close` took three rounds
    # to learn this and I did not carry it to the sibling verb in the same
    # branch: a silently dropped write left the directive open while the
    # command printed "closed" and exited 0. Read back and confirm BOTH the
    # transition and the evidence naming this reply — status alone would pass
    # on a row somebody else closed.
    back = transport.read(path)
    got = okf.parse_frontmatter(back) if back else None
    if got is None or str(got.get("status") or "") != "done" or \
            evidence not in (back or ""):
        print(f"tell: reply sent, but {target} did NOT close — the write did "
              f"not land (read-back shows status="
              f"{(got or {}).get('status')!r}). The row is still open; retry.",
              file=sys.stderr)
        return 1
    # THE CLOSE MUST REACH THE STREAM (round 3, 2026-08-21): this verb closed
    # the doc and emitted nothing, so the answered directive stayed open in
    # the REPLIER's stream fold forever. Same duty as `task done`: everything
    # that closes an obligation emits its close. Best-effort — the doc is
    # truth, the event is delivery.
    _emit_response_companion(
        transport, args.team, slug=target,
        owner=str(fm.get("owner") or ""),
        responder=agent,
        shard_ptr=f"task/{target}.md",
        for_agent=agent)
    print(f"closed {target} — answered by {reply_slug}")
    return 0


def cmd_tell(args: argparse.Namespace, transport: Any) -> int:
    rc = _create_directive(args, transport, assignee=args.assignee)
    if rc != 0 or not getattr(args, "closes", None):
        return rc
    # The reply is durable first; only then does the answered row close. If the
    # close fails the reply still stands and the row stays open — visibly wrong
    # in the safe direction, never a closed row with no answer behind it.
    reply_slug = _directive_slug(args, args.assignee)
    return _close_answered_directive(transport, args, reply_slug=reply_slug)


def cmd_broadcast(args: argparse.Namespace, transport: Any) -> int:
    return _create_directive(args, transport, assignee="*")


def cmd_remind(args: argparse.Namespace, transport: Any) -> int:
    when = directives.parse_when(args.when, now=_iso(_now()))
    if when is None:
        print(f"remind failed: cannot parse WHEN {args.when!r} (ISO or 5d/36h/10m)", file=sys.stderr)
        return 1
    # Identity excludes WHEN (same rule as intent): a repeated identical
    # reminder dedupes onto the existing doc, which KEEPS its original
    # not_before. The write path itself reports which outcome happened —
    # a pre-read cannot distinguish absent from degraded (None is ambiguous),
    # so only the verified "written" outcome may emit the timer record.
    slug = _directive_slug(args, args.assignee)
    rc = _create_directive(args, transport, assignee=args.assignee, not_before=when)
    if rc == 0:
        outcome = getattr(args, "_directive_outcome", None)
        if outcome == "written":
            _emit_scheduled_record(args, transport, when=when, slug=slug)
        elif outcome == "deduped":
            fm = okf.parse_frontmatter(
                getattr(args, "_directive_existing", "") or "") or {}
            orig = fm.get("not_before") or "unknown"
            print(f"record: reminder already scheduled (existing doc keeps "
                  f"not_before {orig}); no second timer emitted")
        else:
            # No outcome set on a zero rc: an unexpected path. Emitting could
            # double-deliver; not emitting only costs latency. Fail safe.
            print("record: directive outcome unknown — no timer emitted "
                  "(file-plane visibility stands)")
    return rc


def _emit_scheduled_record(args: argparse.Namespace, transport: Any, *,
                           when: str, slug: str) -> None:
    """Best-effort bus-v3 timer for a reminder: a FUTURE-DATED record.

    The platform hides a future ``recorded_at`` from every "what's new" window
    until it comes due, then it surfaces in the assignee's ordinary queue read
    (verified live 2026-07-27) — so the reminder DELIVERS itself at WHEN with
    no timer service anywhere. Durable-first: the directive doc has already
    landed and is the truth; this record is delivery. Absent config or a
    failed write therefore degrades latency (file-plane visibility only),
    never loses the reminder — say which, quietly, and move on.
    """
    cfg = records.load_config(transport, args.team)
    if cfg is None:
        print("record: no bus-v3 records config — reminder rides the file plane only")
        return
    ok = False
    try:
        ok = records.emit_event(
            transport, cfg,
            sender=_known_sender(args) or _host(),
            # Same plane boundary as the dispatch companion: the task plane's
            # "*" is not the event plane's "all". `remind --assignee '*'` hit
            # the identical drop, unreported only because nobody had used it.
            to=(records.BROADCAST if args.assignee == directives.EVERYONE
                else args.assignee),
            kind="directive",
            priority=getattr(args, "priority", None) or "P2",
            slug=slug, ptr=f"task/{slug}.md", recorded_at=when,
            team=args.team)
    except ValueError as e:  # unknown kind cannot happen here; belt and braces
        print(f"record: not emitted ({e})", file=sys.stderr)
        return
    if ok:
        print(f"record: scheduled, due {when} (surfaces in {args.assignee}'s queue read)")
    else:
        print("record: emission failed — reminder rides the file plane only")


def cmd_later(args: argparse.Namespace, transport: Any) -> int:
    # `--fyi` CANNOT MEAN ANYTHING HERE (codex-reviewer, 605 r2). `later` captures
    # to @backlog, an audience the companion emitter deliberately skips, and the
    # backlog fold shows open rows. So a `later --fyi` would be born terminal,
    # excluded from the backlog view, and delivered to nobody: the captured idea
    # silently vanishes. A flag whose only effect is to destroy the thing the verb
    # exists to preserve must refuse, not shrug — and `later` already asks nothing
    # of anyone, so there is no obligation for `--fyi` to remove.
    if getattr(args, "fyi", False):
        print("later: --fyi is not meaningful for a backlog capture — @backlog "
              "gets no delivery event and the backlog fold shows open rows, so "
              "an --fyi capture would be invisible to everyone. Drop the flag; "
              "`later` already asks nothing of anyone.", file=sys.stderr)
        return 2
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
            owner=_identity(getattr(args, "sender", None)), assignee=principal,
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
    rc = _write_directive(transport, args, slug=slug, content=content,
                          payload=payload, assignee=principal, not_before=None)
    # Third instance of pr-630 root cause #2 (fixed for `tell` 2026-08-06 and for
    # `review request` 2026-08-14 after it was bitten live). Without this, intent
    # wrote the durable doc and returned, so the obligation existed ONLY as a
    # file: absent from the annotation stream, and therefore invisible to every
    # stream consumer — a source that does not emit is not slow on the channel,
    # it is missing from it, and no cursor advance can surface it. Same contract
    # as the other two: fresh-write-only (never on the dedupe or the in-place
    # --by window update, where a second event is indistinguishable from new
    # work), and best-effort so an unconfigured bus degrades to file-plane-only
    # rather than failing the intent.
    if rc == 0 and getattr(args, "_directive_outcome", None) == "written":
        _emit_dispatch_companion(transport, args, slug=slug, assignee=principal)
    return rc


def _directed_inbox(transport: Any, team: str, agent: str,
                    rows: list[dict[str, Any]], *,
                    held_roles: "Optional[set[str]]" = None,
                    include_backlog: bool = False,
                    include_history: bool = False) -> list[dict[str, Any]]:
    """The open-directive fold over ALREADY-LOADED ``rows`` — directives assigned
    to ``agent``, ``*``, or a role in ``held_roles`` (role routing), with the same
    ack + read-your-write gating `inbox` applies. Split out from
    ``_inbox_rows_status`` so a caller can resolve held roles from the rows FIRST
    (bounding the lease reads to role-shaped assignees) and then fold once, without
    re-reading the summaries index."""
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
                   include_history: bool = False,
                   aggregate_doc: Any = None,
                   feed_evidence: Any = None) -> list[dict[str, Any]]:
    """Needs-me, projection-first, with a raw-tallied live head.

    A fresh, complete ``needs_me`` section proves the ack state for every task
    whose name + mtime still match.  Those covered rows are a pure in-memory
    fold.  New or modified caller-owned rows are deliberately raw-tallied so
    work at the live head never waits for the next reconcile (the PR519 review
    head/tail precedent).  Stale, incomplete, or malformed sections fall back
    to the legacy raw fold loudly; an absent section preserves legacy output.
    """
    if aggregate_doc is not None and not include_history:
        has_section = (isinstance(aggregate_doc, dict)
                       and projection_mod.NEEDS_ME_KEY in aggregate_doc)
        feed_ok = isinstance(feed_evidence, dict) and feed_evidence.get("ok") is True
        if not has_section:
            section, reason = None, ""  # mixed-fleet legacy behavior
        elif feed_ok:
            section, reason = projection_mod.feed_fresh_section(
                aggregate_doc, projection_mod.NEEDS_ME_KEY,
                projection_mod.NEEDS_ME_SCHEMA, now=now)
        else:
            # The wall-clock policy is only an OUTER diagnostic bound when the
            # feed is unavailable. UNKNOWN is not fresh even inside that bound.
            section = None
            _unused, age_reason = projection_mod.fresh_section(
                aggregate_doc, projection_mod.NEEDS_ME_KEY,
                projection_mod.NEEDS_ME_SCHEMA, now=now)
            feed_reason = (feed_evidence or {}).get("reason") if isinstance(
                feed_evidence, dict) else None
            reason = age_reason or feed_reason or "data-updates feed unreadable"
        if section is not None:
            validated = _validated_needs_me_projection(section)
            if validated is not None:
                changed = _needs_me_changed_slugs(
                    team, feed_evidence.get("changes") or [])
                covered: list[dict[str, Any]] = []
                live_head: list[dict[str, Any]] = []
                for row in rows:
                    name = str(row.get("name") or "")
                    snap = validated.get(name)
                    if name in changed or snap is None or snap[0] != row.get("mtime"):
                        live_head.append(row)
                        continue
                    current = dict(row)
                    current["acked_by"] = list(snap[1])
                    covered.append(current)
                got = query.needs_me(
                    covered, agent, now=now, held_roles=held_roles)
                got += _needs_me_rows_raw(
                    transport, team, agent, live_head, now=now,
                    held_roles=held_roles, include_history=False)
                from . import model as model_mod
                got = model_mod.sort_rows(got)
                got.append({"type": "needs-me-source", "source": "projection",
                            "as_of": section.get("generated_at")})
                return got
            reason = "needs-me projection malformed"
        if reason:
            got = _needs_me_rows_raw(
                transport, team, agent, rows, now=now,
                held_roles=held_roles, include_history=include_history)
            got.append({"type": "needs-me-source", "source": "raw-scan",
                        "reason": reason})
            return got
    return _needs_me_rows_raw(
        transport, team, agent, rows, now=now, held_roles=held_roles,
        include_history=include_history)


def _needs_me_changed_slugs(team: str, changes: list[Any]) -> set[str]:
    """Task/ack slugs named by the already-paid team updates call.

    These are the projection's bounded live head. Direct task changes were
    already overlaid by ``_load_rows_status``; ack changes keep the row mtime but
    still require the authoritative shard read, so both namespaces land here.
    """
    task_pfx = rec.task_prefix(team)
    ack_pfx = f"team/{team}/_coord/acks/"
    out: set[str] = set()
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or "").lstrip("/")
        if path.startswith(task_pfx):
            rest = path[len(task_pfx):]
            if "/" not in rest and rest.endswith(".md") and rest not in (
                    "index.md", "log.md"):
                out.add(rest[:-3])
        elif path.startswith(ack_pfx):
            rest = path[len(ack_pfx):]
            if "/" in rest:
                slug = rest.split("/", 1)[0]
                if slug:
                    out.add(slug)
    return out


def _validated_needs_me_projection(
    section: dict[str, Any],
) -> "Optional[dict[str, tuple[Any, list[str]]]]":
    """Positively validate every nested value before deriving coverage."""
    projected = section.get("rows")
    if not isinstance(projected, list):
        return None
    out: dict[str, tuple[Any, list[str]]] = {}
    for item in projected:
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        mtime = item.get("mtime")
        acked = item.get("acked_by")
        if (not isinstance(name, str) or not name or name in out
                or (mtime is not None and not isinstance(mtime, str))
                or not isinstance(acked, list)
                or not all(isinstance(a, str) and a for a in acked)
                or len(set(acked)) != len(acked)):
            return None
        out[name] = (mtime, list(acked))
    return out


def _needs_me_rows_raw(transport: Any, team: str, agent: str,
                       rows: list[dict[str, Any]], *, now: str,
                       held_roles: "Optional[set[str]]" = None,
                       include_history: bool = False) -> list[dict[str, Any]]:
    """Legacy authoritative tally for uncovered rows and fallback paths."""
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
    public-read failure contract at ``_read_degraded_row``. Extracted so every
    await surface reads the SAME source `inbox` shows — one inbox computation, no
    second implementation to drift. Never raises: an unreadable summaries read folds
    to an empty list, but with ``ok=False`` and a ``reason`` so EVERY caller (inbox,
    needs-me, briefing) surfaces the degradation as the loud marker rather than
    mistaking UNKNOWN for an empty inbox — the codex-reproduced silent clean-``[]``
    that suppressed a live unacked directive.

    The fourth element is the UNRESOLVED role set (``_held_roles_for_rows``): roles
    whose holders could not be determined. The caller MUST surface it — see
    ``_role_degraded_row``."""
    rows, ok, reason = _load_rows_status(transport, team)
    held, unresolved = _held_roles_for_rows(transport, team, agent, rows,
                                            now=_iso(_now()),
                                            deadline_seconds=_role_fold_budget())
    return (_directed_inbox(transport, team, agent, rows,
                            held_roles=held or None,
                            include_backlog=include_backlog,
                            include_history=include_history),
            ok, reason, unresolved)


def _obligation_probes(transport: Any, team: str, agent: str, *, now: str
                       ) -> "list[obligations_mod.Component]":
    """Bind the real coordination surface to obligation probes.

    One task-index read serves the four row-derived components, so a single index
    failure degrades all four — which is correct, not lazy: if the index is
    unreadable then nothing derived from it is known, and reporting three of them
    as CLEAR would be inventing coverage.

    ``reviews`` uses the ``degraded_sink`` added to ``_pending_reviews_for``. That
    helper is deliberately best-effort for needs-me and briefing (they must not
    fail because a review add-on is down), and its ``[]`` on a failed listing is
    exactly the false CLEAR this fold exists to refuse. The sink is how the fold
    hears the difference.
    """
    P, S = obligations_mod.ProbeResult, obligations_mod.ProbeState
    # ONE shared deadline for the whole fold, not one per component. The forge
    # probe is a data-dependent fan-out (responsibility scan, then a listing per
    # responsible PR), so an unbounded fold grows with the agent's PR count and
    # can overrun the wake cadence it is supposed to ride inside. A shared
    # deadline also means an expensive earlier probe shrinks what the next gets,
    # rather than each one starting the clock fresh.
    # THE FIX (codex/coord-boss P0): setup is accounted SEPARATELY from the
    # probes. Previously one shared deadline opened here and the two setup calls
    # below spent all of it (measured on the live store: 6.8s + 19.3s = 26.1s
    # against a 20s budget), so every probe hit its expired guard and returned
    # UNREADABLE without ever touching the store — 0 of 7 consulted, hiding 110
    # owed items behind a blanket UNKNOWN. Setup now gets its own bounded
    # allowance and the probe budget opens AFTER it, so a slow store degrades
    # GRADUALLY (an expensive early probe still shrinks what later ones get)
    # instead of collapsing to zero coverage.
    setup_dl = Deadline.open(_obligation_budget())
    doc_sink: list[Any] = []
    feed_sink: list[Any] = []
    rows, rows_ok, rows_reason = _load_rows_status(
        transport, team, doc_sink=doc_sink, feed_sink=feed_sink,
        feed_section_key=projection_mod.NEEDS_ME_KEY)
    agg_doc = doc_sink[0] if doc_sink else None
    feed_evidence = feed_sink[0] if feed_sink else None
    role_resolution: dict[str, tuple[list[str], bool]] = {}
    # setup_dl now BINDS this call instead of merely timing it. Before, it
    # opened from _obligation_budget() and the role fold fell through to
    # COORD_ROLE_FOLD_BUDGET — so the deadline that was MEASURED was not the
    # deadline that BOUND the work, and the warning below sent operators to a
    # variable that governs something else (coord-boss, 2026-08-07).
    held_roles, unresolved_roles = _held_roles_for_rows(
        transport, team, agent, rows, now=now,
        deadline_seconds=setup_dl.remaining(),
        resolution_sink=role_resolution)
    if setup_dl.expired():
        # Never silent: setup outrunning its own allowance is the leading
        # indicator of the collapse this fix removed, and on a growing store it
        # will come back. Say it while the fold still succeeds.
        print("obligations: setup (task index + role resolution) outran "
              "COORD_OBLIGATION_BUDGET, which now bounds it; probes still get a "
              "full budget, but this store is near the edge. Raise "
              "COORD_OBLIGATION_BUDGET to give setup more room. NOTE: the role "
              "fold's own default is COORD_ROLE_FOLD_BUDGET — it applies "
              "wherever the role resolver is called WITHOUT an enclosing "
              "budget (needs-me, inbox, briefing), not here.", file=sys.stderr)
    # Probes get their full allowance regardless of what setup cost.
    fold_dl = Deadline.open(_obligation_budget())

    def _rows_probe(kinds: "tuple[str, ...]"):
        def probe():
            if not rows_ok:
                return P(state=S.UNREADABLE, detail=rows_reason)
            if fold_dl.expired():
                return P(state=S.UNREADABLE,
                         detail="obligation probe budget exhausted — raise "
                                "COORD_OBLIGATION_BUDGET")
            mine = _needs_me_rows(transport, team, agent, rows, now=now,
                                  held_roles=held_roles, include_history=False,
                                  aggregate_doc=agg_doc,
                                  feed_evidence=feed_evidence)
            owed = [r for r in mine
                    if not str(r.get("type") or "").endswith("-source")
                    and (not kinds or (r.get("kind") or "task") in kinds)]
            return P(state=S.OK, owed=owed)
        return probe

    def _roles_probe():
        if not rows_ok:
            return P(state=S.UNREADABLE, detail=rows_reason)
        if fold_dl.expired():
            return P(state=S.UNREADABLE, detail="obligation probe budget exhausted — raise "
                                "COORD_OBLIGATION_BUDGET")
        if unresolved_roles:
            # A role whose lease could not be read might route work here. Doubt.
            return P(state=S.UNREADABLE,
                     detail="unresolved roles: " + ", ".join(sorted(unresolved_roles)))
        return P(state=S.OK)

    def _split_markers(found):
        """(real work, degradation marker types) from a best-effort fold's rows.

        Detects ANY ``*-degraded`` row rather than an enumerated list. The review
        and forge folds signal incomplete coverage with marker ROWS
        (review-head/fold/orphan/role-degraded, forge-degraded), and only some
        paths reach the degraded_sink — so a sink check alone let a marker ride
        through as ordinary owed work and the fold reported DATA/rc 0 with
        incomplete coverage (codex-reviewer, PR 501). Matching the suffix means a
        marker added later degrades the fold automatically instead of silently
        joining the work list.
        """
        markers = [r.get("type") for r in found
                   if isinstance(r.get("type"), str)
                   and r["type"].endswith("-degraded")]
        # ``*-source`` rows are the folds' provenance disclosure (projection vs
        # raw scan) — informational, never owed work and never degradation.
        real = [r for r in found
                if not (isinstance(r.get("type"), str)
                        and (r["type"].endswith("-degraded")
                             or r["type"].endswith("-source")))]
        return real, markers

    def _reviews_probe():
        if fold_dl.expired():
            # The budget was gone before this probe ran. A helper handed an
            # already-dead deadline can legitimately return [] without ever
            # attempting a read — and [] here would be a false CLEAR caused by
            # our own cost control, which is the worst possible source for one.
            return P(state=S.UNREADABLE, detail="obligation probe budget exhausted — raise "
                                "COORD_OBLIGATION_BUDGET")
        sink: list[str] = []
        found = _pending_reviews_for(transport, team, agent, rows=rows,
                                     deadline=fold_dl.instant,
                                     degraded_sink=sink,
                                     aggregate_doc=agg_doc,
                                     feed_evidence=feed_evidence,
                                     role_resolution=role_resolution)
        real, markers = _split_markers(found)
        if sink or markers:
            # Degraded, but the pending rows that WERE read stay available: the
            # fold's promise is that partial work survives while the terminal
            # state stays honest.
            detail = "; ".join(sorted(set(sink) | set(markers)))
            return P(state=S.UNREADABLE, owed=real, detail=detail)
        return P(state=S.OK, owed=real)

    def _forge_probe():
        """Unacknowledged forge feedback — a durable obligation surfaced by
        needs-me and briefing, and absent from the first cut of this registry."""
        if fold_dl.expired():
            return P(state=S.UNREADABLE, detail="obligation probe budget exhausted — raise "
                                "COORD_OBLIGATION_BUDGET")
        found = _forge_feedback_for(transport, team, agent,
                                    deadline=fold_dl.instant,
                                    aggregate_doc=agg_doc,
                                    feed_evidence=feed_evidence)
        real, markers = _split_markers(found)
        if markers:
            return P(state=S.UNREADABLE, owed=real,
                     detail="; ".join(sorted(set(markers))))
        return P(state=S.OK, owed=real)

    C = obligations_mod.Component
    return [
        C(name="blocks", probe=_rows_probe(("block",))),
        C(name="directives", probe=_rows_probe(("directive",))),
        C(name="forge_feedback", probe=_forge_probe),
        C(name="reminders", probe=_rows_probe(("remind", "reminder"))),
        C(name="reviews", probe=_reviews_probe),
        C(name="role_duties", probe=_roles_probe),
        C(name="tasks", probe=_rows_probe(())),
    ]


def cmd_obligations(args: argparse.Namespace, transport: Any) -> int:
    """The normative "do I owe anything?" answer (r2 spec item 3).

    Exit codes carry the terminal state so automation never has to parse prose:
    0 = CLEAR, 0 = DATA (with rows), 3 = UNKNOWN, 4 = INVALID. A wake that reads
    an empty queue has NOT established that nothing is owed; this has.
    """
    now = _iso(_now())
    result = obligations_mod.fold(
        _obligation_probes(transport, args.team, args.agent, now=now),
        expected=obligations_mod.OBLIGATION_COMPONENTS)
    state = result.state.value
    if getattr(args, "json", False):
        print(jsonutil.dumps({
            "type": "obligations",
            "contract": 2,
            "state": state,
            "owed_count": len(result.owed),
            "consulted": result.consulted,
            "degraded": result.degraded,
            "malformed": result.malformed,
            "details": result.details,
            "reason": result.reason(),
        }))
    else:
        print(f"obligations: {state} — {result.reason()}")
        for row in result.owed:
            print(f"  - {row.get('slug') or row.get('id') or row}")
        for name in result.degraded:
            why = result.details.get(name)
            suffix = f" ({why})" if why else ""
            print(f"  ! {name}: UNREADABLE — cannot claim clear{suffix}",
                  file=sys.stderr)
        for name in result.malformed:
            why = result.details.get(name)
            suffix = f" ({why})" if why else ""
            print(f"  ! {name}: INVALID — human fix needed{suffix}",
                  file=sys.stderr)
    if result.state is obligations_mod.ObligationState.UNKNOWN:
        return 3
    if result.state is obligations_mod.ObligationState.INVALID:
        return 4
    return 0





# --- obligations checkpoint: the open set as of a stated instant --------------
# The piece that lets the stream fold answer COMPLETELY instead of only for its
# window. Shape: {"v": 1, "as_of": ISO, "open": [{"slug","ptr","priority"}...],
# "seeded_by": "corpus-fold" | "stream-fold"}. Seeded ONCE by the corpus walk
# (the 111s fold, run deliberately and off the wake path), then ADVANCED by
# every clean stream run — so the expensive enumeration happens once per agent
# plus repair, not once per wake.


def _obligations_checkpoint_path(team: str, agent: str) -> str:
    return f"team/{team}/_coord/agents/{agent}/obligations-checkpoint.json"


def _load_obligations_checkpoint(transport: Any, team: str,
                                 agent: str) -> "Optional[dict[str, Any]]":
    """The checkpoint, or None when absent/unreadable/malformed.

    None means "no trustworthy claim about the past exists" — the stream fold
    then reports everything before its window as UNKNOWN, exactly as it did
    before checkpoints existed. A malformed checkpoint must never pass as a
    recent empty one: that would convert a parse error into "nothing was owed".
    """
    raw = transport.read(_obligations_checkpoint_path(team, agent))
    if raw is None:
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get("v") != 1:
        return None
    as_of, open_rows = doc.get("as_of"), doc.get("open")
    if not isinstance(as_of, str) or not as_of or not isinstance(open_rows, list):
        return None
    rows = []
    for row in open_rows:
        if not isinstance(row, dict) or not row.get("slug"):
            return None  # a checkpoint with unreadable rows proves nothing
        rows.append({"slug": str(row["slug"]),
                     "ptr": str(row.get("ptr") or f"task/{row['slug']}.md"),
                     "priority": str(row.get("priority") or "P2")})
    unk = doc.get("unknown_components")
    return {"as_of": as_of, "open": rows,
            "unknown_components": [str(u) for u in unk] if isinstance(unk, list) else [],
            "seeded_by": str(doc.get("seeded_by") or "unknown")}


def _save_obligations_checkpoint(transport: Any, team: str, agent: str, *,
                                 as_of: str, open_rows: list, seeded_by: str,
                                 unknown_components: "Optional[list[str]]" = None) -> bool:
    doc = {"v": 1, "as_of": as_of, "seeded_by": seeded_by,
           "unknown_components": sorted(unknown_components or []),
           "open": [{"slug": r["slug"], "ptr": r.get("ptr"),
                     "priority": r.get("priority", "P2")} for r in open_rows]}
    try:
        return bool(transport.write(
            _obligations_checkpoint_path(team, agent), json.dumps(doc, sort_keys=True)))
    except Exception:
        return False


def cmd_obligations_seed(args: argparse.Namespace, transport: Any) -> int:
    """`obligations --seed-checkpoint`: run the corpus fold ONCE, deliberately,
    and write its answer down as the checkpoint the stream fold starts from.

    This is the one sanctioned use of the enumerating fold going forward: a
    seed or a repair, invoked on purpose and off the wake path — never the
    per-wake answer to "what do I owe". It refuses to write on UNKNOWN or
    INVALID: a checkpoint seeded from a degraded fold would launder the
    degradation into a clean-looking past.
    """
    now = _iso(_now())
    result = obligations_mod.fold(
        _obligation_probes(transport, args.team, args.agent, now=now),
        expected=obligations_mod.OBLIGATION_COMPONENTS)
    degraded = sorted(set(result.degraded) | set(result.malformed))
    # The components whose open rows the stream fold takes over from this
    # checkpoint. A gap in one of THESE is a gap in the checkpoint's own
    # subject matter; a gap elsewhere (forge, roles) is recorded by name and
    # surfaced on every stream answer, but must not chain this seed on an
    # unrelated standing defect.
    stream_served = {"tasks", "directives", "blocks", "reminders"}
    unseedable = sorted(set(degraded) & stream_served)
    if unseedable and not getattr(args, "seed_partial", False):
        print(f"obligations seed: REFUSED — corpus fold is {result.state.value} "
              f"and the degraded set {degraded} includes stream-served "
              f"component(s) {unseedable}, whose open rows this checkpoint "
              "exists to carry. Seeding would launder that gap into a "
              "clean-looking past. Fix the fold, or pass --seed-partial to "
              "write a checkpoint that NAMES the unknown components — the "
              "stream fold will then report them as UNKNOWN every run rather "
              "than clear.", file=sys.stderr)
        return 3
    if degraded:
        print(f"obligations seed: proceeding with named gaps {degraded} — "
              "these components stay UNKNOWN in every stream answer until the "
              "underlying fold is fixed and the checkpoint is re-seeded")
    rows = [{"slug": str(r.get("slug") or r.get("id") or ""),
             "ptr": str(r.get("ptr") or "") or None,
             "priority": str(r.get("priority") or r.get("pri") or "P2")}
            for r in result.owed if (r.get("slug") or r.get("id"))]
    if not _save_obligations_checkpoint(transport, args.team, args.agent,
                                        as_of=now, open_rows=rows,
                                        seeded_by="corpus-fold",
                                        unknown_components=degraded):
        print("obligations seed: checkpoint write FAILED — nothing recorded",
              file=sys.stderr)
        return 3
    back = _load_obligations_checkpoint(transport, args.team, args.agent)
    if back is None or back["as_of"] != now:
        print("obligations seed: read-back mismatch — the checkpoint did not "
              "land; treat as not seeded", file=sys.stderr)
        return 3
    print(f"obligations seed: checkpoint written as of {now} — "
          f"{len(rows)} open obligation(s); the stream fold now answers "
          "completely from here forward")
    return 0


def cmd_obligations_repair(args: argparse.Namespace, transport: Any) -> int:
    """`obligations --repair-unknown`: re-probe ONLY the components the
    checkpoint carries as UNKNOWN, and clear the ones that now read.

    THE DEADLOCK THIS BREAKS. A carried UNKNOWN is sticky by construction: the
    stream fold copies ``unknown_components`` forward on every advance and
    nothing else writes it, so the ONLY way to clear one was a full
    ``--seed-checkpoint`` — a corpus walk over every component. When the stuck
    component is the one whose corpus probe cannot finish inside a budget
    (measured: the reviews leg covers 104 of 364 slugs at fold 300/briefing
    600), the only thing that could clear the UNKNOWN is the thing that cannot
    run. The UNKNOWN then reads as permanent when it is really unattempted, and
    those are different claims.

    Two properties make this safe to run without weakening the answer:

    * **It probes a SUBSET, so the stuck component gets the whole budget**
      instead of whatever survives six siblings. That is the measured shape of
      the failure — the review leg starved on a budget it inherited already
      drained — so a subset probe is not a smaller version of the same attempt.
    * **Clearing a component MERGES its owed rows into the open set.** Marking
      a surface covered while dropping the work it found would be a false clear
      manufactured by the repair itself, which is the exact failure class the
      checkpoint exists to prevent.

    It is off the wake path, like ``--seed-checkpoint``: a deliberate repair,
    never the per-wake answer. ``as_of`` is NOT advanced — this run folds no
    events and has no claim to make about time.
    """
    agent = _declared_identity(getattr(args, "agent", None))
    if not agent:
        print("obligations --repair-unknown: --agent or FULCRA_COORD_AGENT required",
              file=sys.stderr)
        return 2
    checkpoint = _load_obligations_checkpoint(transport, args.team, agent)
    if checkpoint is None:
        print("obligations --repair-unknown: no readable checkpoint — there is "
              "nothing to repair yet. Seed one with obligations "
              "--seed-checkpoint first.", file=sys.stderr)
        return 3
    unknown = sorted(set(checkpoint.get("unknown_components") or []))
    if not unknown:
        print("obligations --repair-unknown: nothing to repair — the checkpoint "
              "carries no UNKNOWN components")
        return 0

    now = _iso(_now())
    offered = _obligation_probes(transport, args.team, agent, now=now)
    by_name = {c.name: c for c in offered}
    probes = [by_name[n] for n in unknown if n in by_name]
    # A name the registry no longer offers cannot be probed here. Clearing it
    # would claim coverage we never established; it stays UNKNOWN and is
    # reported by a DIFFERENT sentence, because "not offered" and "read and
    # failed" have different remedies.
    unoffered = [n for n in unknown if n not in by_name]

    result = obligations_mod.fold(probes, expected=tuple(n for n in unknown
                                                         if n in by_name))
    cleared = sorted(set(result.consulted))
    still = sorted(set(unknown) - set(cleared))

    merged = {r["slug"]: r for r in checkpoint["open"]}
    added = 0
    for row in result.owed:
        slug = str(row.get("slug") or row.get("id") or "")
        if not slug or slug in merged:
            continue
        merged[slug] = {"slug": slug,
                        "ptr": str(row.get("ptr") or "") or None,
                        "priority": str(row.get("priority") or row.get("pri") or "P2")}
        added += 1

    print(f"obligations --repair-unknown: {len(unknown)} carried UNKNOWN "
          f"({', '.join(unknown)}) — cleared {len(cleared)}, still UNKNOWN "
          f"{len(still)}; {added} newly-open row(s) merged into the checkpoint")
    for name in cleared:
        print(f"  cleared: {name}")
    for name in still:
        detail = result.details.get(name) or "no detail"
        if name in unoffered:
            detail = ("component is not offered by this engine's probe "
                      "registry — cannot be repaired here")
        print(f"  ! still UNKNOWN: {name} — {detail}", file=sys.stderr)

    if not cleared and not added:
        print("obligations --repair-unknown: checkpoint UNCHANGED — nothing "
              "cleared and nothing merged, so it is not rewritten",
              file=sys.stderr)
        return 3
    if not _save_obligations_checkpoint(
            transport, args.team, agent, as_of=checkpoint["as_of"],
            open_rows=list(merged.values()), seeded_by="repair-unknown",
            unknown_components=still):
        print("obligations --repair-unknown: checkpoint write FAILED — nothing "
              "recorded, the carried UNKNOWN stands", file=sys.stderr)
        return 3
    back = _load_obligations_checkpoint(transport, args.team, agent)
    if back is None or sorted(back.get("unknown_components") or []) != still:
        print("obligations --repair-unknown: read-back mismatch — treat the "
              "repair as NOT applied", file=sys.stderr)
        return 3
    return 3 if still else 0


def cmd_obligations_dispatch(args: argparse.Namespace, transport: Any) -> int:
    """Route `obligations` to the corpus fold or the --stream path. A named
    function, not a lambda: the activity-classification pin requires every
    registered command to be classifiable by name."""
    if getattr(args, "repair_unknown", False):
        return cmd_obligations_repair(args, transport)
    if getattr(args, "seed_checkpoint", False):
        return cmd_obligations_seed(args, transport)
    if getattr(args, "stream", False):
        return cmd_obligations_stream(args, transport)
    return cmd_obligations(args, transport)


def cmd_obligations_stream(args: argparse.Namespace, transport: Any) -> int:
    """`obligations --stream`: follow the signal to the doc, never scan the corpus.

    THE POINT, and it is Ash's, repeated for six weeks before it was built: the
    bus payload has ALWAYS carried ``ptr``. The signal already names the exact
    document an obligation lives in. The default obligations path ignores it —
    it loads a reconcile-built summaries index and folds the whole fleet, so its
    cost is proportional to accumulated history rather than to what changed.
    Measured on this box 2026-08-29: the stream read is 8.3s and CLEAR while the
    corpus fold is 111.0s with 5 of 7 probes UNREADABLE, against 3,159 task docs
    and 950 review entries.

    This path instead: read my own channel forward from my own durable cursor,
    keep the events addressed to me, and open ONLY the documents their ``ptr``
    names. Cost is proportional to new events plus the handful of docs they
    point at.

    WHAT IT HONESTLY IS NOT: complete on its own. A windowed read answers for
    the window, so an obligation opened before it is invisible here. That is the
    checkpoint half of the design and it is NOT built yet — so anything earlier
    than the window is reported UNKNOWN, never clear. A fold that cannot see far
    enough must say so; that failure is the whole reason this work exists.
    """
    agent = _declared_identity(getattr(args, "agent", None))
    if not agent:
        print("obligations --stream: --agent or FULCRA_COORD_AGENT required",
              file=sys.stderr)
        return 2
    cfg, cfg_status = records.load_config_classified(transport, args.team)
    if cfg is None:
        print(f"obligations --stream: UNKNOWN — records config unreadable "
              f"({cfg_status})", file=sys.stderr)
        return 3

    checkpoint = _load_obligations_checkpoint(transport, args.team, agent)
    cursor = records.load_cursor(transport, args.team, agent)
    now = _now()
    lookback_h = int(getattr(args, "lookback_hours", 0) or 168)
    since = None
    if checkpoint:
        # The checkpoint is the authority for everything before its as_of, so
        # the window starts THERE — not at the queue cursor, which tracks a
        # different consumer (the delivery read) and may be ahead of the last
        # obligation fold. Reading from the older instant re-covers events the
        # queue consumed but this fold never saw.
        since = checkpoint["as_of"]
    elif cursor and cursor.get("last_read"):
        since = str(cursor["last_read"])
    floor = _iso(now - timedelta(hours=lookback_h))
    # THE CURSOR IS THE START, and the floor applies only when there is no
    # cursor. I first wrote this as `min(cursor, floor)` — copied from the mesh
    # sweep, where widening is the safe direction because that sweep is the only
    # reader and must never miss. Here it is the WRONG direction and it defeated
    # the whole point: it re-read a week of fleet events on every run and blew
    # past a 2-minute timeout. A cursor that cannot shrink the window is not a
    # cursor. What is older than the cursor is the checkpoint's job, and until
    # that exists this path says UNKNOWN about it rather than pretending.
    start = since or floor
    until = _iso(now)

    raw = transport.records(cfg["data_type"], start, until)
    if raw is None:
        print("obligations --stream: UNKNOWN — window unreadable, cursor untouched",
              file=sys.stderr)
        return 3
    events = records.events_for(raw, agent)
    if events is None:
        print("obligations --stream: UNKNOWN — events unparseable", file=sys.stderr)
        return 3

    # Fold the window: a directive opens, a response/verdict naming me closes.
    # An FYI opens NOTHING — measured 2026-08-21, most of 92 stream-only "opens"
    # were FYIs replayed as permanent obligations.
    opened: dict[str, dict[str, Any]] = {}
    carried: dict[str, dict[str, Any]] = {}
    if checkpoint:
        # Seed the fold with the past: the checkpoint open set as of its
        # instant. Window events then open and close on top of it.
        for row in checkpoint["open"]:
            opened[row["slug"]] = {"slug": row["slug"], "ptr": row["ptr"],
                                   "kind": "directive", "fyi": False}
            carried[row["slug"]] = row
    touched: set = set()
    for ev in events:
        slug, kind = ev.get("slug"), ev.get("kind")
        if not slug:
            continue
        if kind == "directive" and not ev.get("fyi"):
            opened.setdefault(slug, ev)
            touched.add(slug)
        elif kind in ("response", "verdict"):
            opened.pop(slug, None)
            touched.add(slug)

    # Follow the ptr — but ONLY for slugs the window touched. THIS is what
    # makes the run proportional to new events: a checkpoint row with no
    # window activity carries forward with its stored fields, unread. The
    # first live seeded run proved why (2026-08-29): re-verifying every
    # carried row read 225 docs in 136.6s — proportional to the open set,
    # not to what changed, which is the corpus fold's disease in miniature.
    # The trade is explicit: an obligation closed WITHOUT an emitted event
    # stays owed here until the periodic re-seed reconciles it. That is the
    # correct pressure — the missing close event is the defect, and hiding
    # it by re-reading everything every run would subsidize silent closers.
    owed, unresolved = [], []
    for slug, ev in opened.items():
        if slug in carried and slug not in touched:
            row = carried[slug]
            owed.append({"slug": slug, "ptr": row.get("ptr"),
                         "priority": row.get("priority", "P2"),
                         "blocked_on": row.get("blocked_on") or None,
                         "verified": "checkpoint"})
            continue
        ptr = ev.get("ptr") or f"task/{slug}.md"
        doc = transport.read(f"team/{args.team}/{ptr}")
        if doc is None:
            # Absent-or-unreadable is ambiguous and must not read as discharged.
            unresolved.append(slug)
            continue
        fm = okf.parse_frontmatter(doc) or {}
        if str(fm.get("status") or "") not in tasks.TERMINAL_STATUSES:
            owed.append({"slug": slug, "ptr": ptr,
                         "priority": str(fm.get("priority") or "P2"),
                         "blocked_on": str(fm.get("blocked_on") or "") or None,
                         "verified": "doc"})

    reads = sum(1 for r in owed if r.get("verified") == "doc") + len(unresolved)
    origin = ("checkpoint " + checkpoint["as_of"] + " + window") if checkpoint else "window only"
    print(f"obligations --stream: {len(owed)} owed from {len(events)} event(s) "
          f"in [{start}, {until}] ({origin}) — {len(opened)} open slug(s), "
          f"{reads} doc read(s)")
    for row in sorted(owed, key=lambda r: r["priority"]):
        blocked = f"  blocked_on={row['blocked_on']}" if row["blocked_on"] else ""
        print(f"  - [{row['priority']}] {row['slug']}{blocked}")
    for slug in unresolved:
        print(f"  ! {slug}: ptr unreadable — cannot claim discharged",
              file=sys.stderr)
    if checkpoint and checkpoint.get("unknown_components"):
        print("  ! UNKNOWN components carried from the seed: "
              + ", ".join(checkpoint["unknown_components"])
              + " — the corpus fold could not read these when the checkpoint "
              "was written; they are not covered by this answer", file=sys.stderr)
    if not checkpoint:
        print(f"  ! NO CHECKPOINT: anything opened before {start} is UNKNOWN "
              "here. Seed one deliberately with obligations --seed-checkpoint "
              "(runs the corpus fold once, off the wake path)", file=sys.stderr)
    elif not unresolved:
        # A clean fold ADVANCES the checkpoint, so the corpus walk never runs
        # again for this agent outside seed and repair. Advance only on clean:
        # an unresolved ptr means the open set is not fully known, and a
        # checkpoint that guesses converts one bad read into a durable lie.
        _save_obligations_checkpoint(
            transport, args.team, agent, as_of=until, open_rows=owed,
            seeded_by="stream-fold",
            unknown_components=checkpoint.get("unknown_components") or [])
    return 3 if unresolved else 0


def cmd_inbox(args: argparse.Namespace, transport: Any) -> int:
    agent = _identity(args.agent)
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
    # Contract 2 (OC2/OC3, ladder PR 2): the envelope seals first and rc is a
    # pure function of its health in BOTH modes — an unreadable summaries index
    # is UNKNOWN (rc 3), an unresolved role inbox is DEGRADED (rc 3); the old
    # unconditional rc 0 could not tell a clean-empty inbox from either.
    rows_out = got
    if not ok:
        rows_out = [_read_degraded_row(reason, marker="inbox-degraded")] + rows_out
    if unresolved_roles:
        rows_out = [_role_degraded_row(unresolved_roles)] + rows_out
    envelope, rc = class_a_envelope(rows_out, source_type="inbox-source")
    if args.json:
        jsonutil.print_json(envelope)
        return rc
    if not ok:
        _surface_read_degraded(reason, json_mode=False, marker="inbox-degraded")
    print(f"inbox — {agent}: {len(got)} item(s)")
    if unresolved_roles:  # always shown — an unknown role inbox must never hide
        print(_role_degraded_line(_role_degraded_row(unresolved_roles)))
    for r in got:
        print(_line(r))
    return rc


#: Text-mode counterpart to the JSON envelope's ``obligations: not-checked``.
#: A successfully served empty event window is real CLEAR for the event delta,
#: but says nothing about retained directives, tasks, or review obligations.
_QUEUE_EMPTY_IS_NOT_CLEAR = (
    "queue: 0 events — this is NOT proof that nothing is owed. Events are "
    "best-effort wake hints; a hint never written, or one older than this "
    "window, leaves a durable obligation unmentioned. For the actual answer: "
    "coord-engine obligations <team> --agent <you>  (rc 3 = UNKNOWN)"
)


def _warn_empty_queue_gap(
        events: list[Any], *, json_mode: bool,
        obligations: "Optional[dict[str, Any]]",
) -> None:
    """Disclose the retained-state gap on otherwise silent text CLEAR reads."""
    if not json_mode and not events and obligations is None:
        print(_QUEUE_EMPTY_IS_NOT_CLEAR, file=sys.stderr)


def _obligations_not_checked() -> dict[str, Any]:
    """The honest marker for a machine-readable envelope that did not fold.

    Fold-on-empty is OPT-IN (promise plan T3(b), 2026-08-02). A skipped fold
    must still be *stated*: an omitted key lets a consumer read CLEAR as
    "nothing owed", which is the precise false inference the fold exists to
    refuse. ``not-checked`` is deliberately not one of the fold's own states
    (CLEAR/DATA/UNKNOWN/INVALID) so no caller can map it to any of them.
    """
    return {"state": "not-checked"}


#: Without these an event has no meaning to deliver: `kind` is what it IS and
#: `slug` is what it is ABOUT. Everything else on the line is decoration that a
#: `?` renders honestly.
_REQUIRED_EVENT_FIELDS = ("kind", "slug")


def _poison_line(event: Any, reason: Any) -> str:
    """One unrenderable event, rendered LOUDLY rather than crashing or vanishing.

    Best-effort on every field independently, so a single bad value cannot take
    the rest of the line with it. Never raises: this is the last line of defence
    for the delivery path, and a formatter that can fail here is the whole bug.
    """
    def _bit(name: str) -> str:
        try:
            value = event.get(name) if isinstance(event, dict) else None
            return str(value) if value not in (None, "") else "?"
        except Exception:
            return "?"
    return (f"POISON {_bit('recorded_at')} {_bit('from')} kind={_bit('kind')} "
            f"slug={_bit('slug')} ptr={_bit('ptr')} "
            f"id={_bit('record_id')} writer={_bit('writer')} "
            f"[unrenderable: {reason}]")


def _classify_queue_events(
        events: list[Any]) -> "tuple[list[dict[str, Any]], list[tuple[Any, str]]]":
    """Split a window into (renderable, [(event, reason), ...]).

    ONE classification, consumed by BOTH output modes. Round 2 validated inside
    the TEXT renderer, which `cmd_queue` calls only when `json_mode` is false —
    so `--json`, the mode automation actually uses, skipped validation entirely,
    saved the cursor, and then raised inside the envelope on `event["kind"]`.
    The event was consumed and never appeared anywhere: a permanent wedge traded
    for silent loss (codex-reviewer, 600 r2). Third time in this PR that a rule
    landed on one path and its sibling went without, which is why the decision
    now lives in one function that neither mode can bypass.
    """
    ok: list[dict[str, Any]] = []
    bad: list[tuple[Any, str]] = []
    for event in events:
        if not isinstance(event, dict):
            bad.append((event, f"not an event object: {type(event).__name__}"))
            continue
        missing = [f for f in _REQUIRED_EVENT_FIELDS if not event.get(f)]
        if missing:
            bad.append((event, "missing required field(s): " + ", ".join(missing)))
            continue
        ok.append(event)
    return ok, bad


def _print_queue_events(events: list[dict[str, Any]], *, json_mode: bool) -> int:
    """Render a window. Returns the number of UNRENDERABLE events.

    PER-EVENT, and never fatal. `kind` and `slug` used to be direct subscripts
    while every neighbouring field went through `.get()` — so an event missing
    either raised KeyError out of here, out of `cmd_queue`, and past the cursor
    save, which never ran. At-least-once redelivery then returned the same
    window with the same poison event on the next read, and the one after that:
    a permanently wedged cursor with no error path that said so. Measured live
    on codex-reviewer's window, 9 events stuck from 08:03Z (coord-boss, 2026-08-10).

    Three properties, per coord-boss's constraints:
      * a poison event is RENDERED (as an explicit POISON line), never skipped —
        silence would trade a wedge for a disappearance;
      * it is COUNTED, so the caller can say the window was poisoned;
      * nothing here can raise, so the cursor save below is always reached.
    """
    ok, bad = _classify_queue_events(events)
    for event in ok:
        try:
            if json_mode:
                jsonutil.print_json(event)
            else:
                print(f"{event.get('recorded_at') or ''}"[:19].ljust(19)
                      + f" {event.get('from') or '?'}"
                      f" {event.get('kind')} {event.get('priority') or '-'}"
                      f" {event.get('slug')} {event.get('ptr') or '-'}")
        except Exception as e:      # noqa: BLE001 — deliberate: delivery > format
            bad.append((event, f"{type(e).__name__}: {e}"))
    for event, reason in bad:
        print(_poison_line(event, reason), file=sys.stderr)
    return len(bad)


def _queue_result_envelope(
        events: list[dict[str, Any]], *, cfg: dict[str, Any],
        cursor_path: str, advanced: bool,
        outcome_mix: Optional[dict[str, int]] = None,
        obligations: Optional[dict[str, Any]] = None,
        poisoned: "Optional[list[tuple[Any, str]]]" = None) -> dict[str, Any]:
    """The single-object ``--json`` success envelope for a queue read.

    Shares the ``type`` discriminator convention with the ``queue-error``
    failure envelope (:func:`_queue_failure`): automation switches on one
    field and never has to guess whether a line is an event or a verdict.
    ``state`` is terminal — DATA (events delivered) or CLEAR (a clean, fully
    read window with nothing new); the failure envelope owns INVALID and
    UNKNOWN, so CLEAR can never be a disguise for either.

    EVERY envelope carries an ``obligations`` key: the fold's report when it
    ran, otherwise ``{"state": "not-checked"}``. Not only CLEAR — round-2
    finding 1. Scoping the marker to CLEAR made its PRESENCE a proxy for "the
    window was empty" instead of for "the fold did not run", and left a
    default DATA read — which performs exactly zero fold ops — silent about
    coverage it never checked. A consumer must be able to test one key on one
    shape, so the marker is universal and the golden gains the key.
    """
    from . import __version__ as engine_version
    protocol = None
    if cfg.get("authority_mode") == "versioned":
        protocol = {
            "protocol_version": cfg["protocol_version"],
            "cursor_schema_version": cfg["cursor_schema_version"],
            "cursor_generation": cfg["cursor_generation"],
        }
    poisoned = poisoned or []
    envelope = {
        "type": "queue-result",
        "contract": 2,
        "state": "DATA" if (events or poisoned) else "CLEAR",
        "events": [{
            "id": event.get("record_id"),
            "ts": event.get("recorded_at"),
            "sender": event.get("from"),
            "to": event.get("to"),
            # `.get()`, not a subscript: this function must not be the thing
            # that raises on a malformed event. Callers pass VALIDATED events,
            # and this is the belt to that braces — a formatter on the delivery
            # path that can throw is the whole defect this PR exists to fix.
            "kind": event.get("kind"),
            "pri": event.get("priority"),
            "slug": event.get("slug"),
            "ptr": event.get("ptr"),
        } for event in events],
        "count": len(events),
        # POISON IS DELIVERED, in the machine-readable channel too. An event
        # this build cannot format is still an event the agent RECEIVED, so it
        # appears here with the reason rather than vanishing between a saved
        # cursor and an envelope that omitted it.
        "poison": [{
            "id": (e.get("record_id") if isinstance(e, dict) else None),
            "ts": (e.get("recorded_at") if isinstance(e, dict) else None),
            "sender": (e.get("from") if isinstance(e, dict) else None),
            "reason": reason,
        } for e, reason in poisoned],
        "poison_count": len(poisoned),
        # respec s7: outcome_mix is ADDITIVE under the cursor block (per the
        # deputy ruling) — the agent's own durable classification mix from v2
        # `handled` rows. Absent (legacy cursor / no rows) the key is omitted,
        # keeping the legacy envelope byte-identical for golden tests.
        "cursor": ({"path": cursor_path, "advanced": advanced,
                    "outcome_mix": outcome_mix}
                   if outcome_mix is not None
                   else {"path": cursor_path, "advanced": advanced}),
        "engine_version": engine_version,
        "protocol": protocol,
    }
    envelope["obligations"] = (
        obligations if obligations is not None else _obligations_not_checked())
    return envelope


def _bus_v3_migration_agents(
        transport: Any, team: str, explicit: list[str]
) -> tuple[list[str], Optional[str]]:
    """Discover legacy cursor owners without reading task or role state."""
    prefix = f"team/{team}/_coord/agents/"
    try:
        entries = transport.list_dir(prefix)
    except (TransportError, OSError) as exc:
        return [], f"agent-census-read-failed: {exc}"
    agents = set(explicit)
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str):
            return [], "agent-census-malformed"
        if entry.get("is_dir") or name.endswith("/"):
            agent = name.rstrip("/")
            if agent and "/" not in agent:
                agents.add(agent)
    return sorted(agents), None


def _bus_v3_migration_block_error(
        authority_class: str, cursor_rows: list[dict[str, Any]],
        census_error: Optional[str]) -> Optional[str]:
    """Return the stable machine reason for a pre-write migration block."""
    authority_errors = {
        "absent-blocks": "authority-absent",
        "malformed-blocks": "authority-malformed",
        "unsupported-blocks": "authority-unsupported",
        "unreadable-blocks": "authority-unreadable",
    }
    if authority_class in authority_errors:
        return authority_errors[authority_class]
    if census_error is not None:
        return census_error.split(":", 1)[0]
    cursor_errors = {
        "malformed-blocks": "cursor-malformed",
        "unreadable-blocks": "cursor-unreadable",
    }
    for row in cursor_rows:
        classification = row["classification"]
        if classification in cursor_errors:
            return cursor_errors[classification]
    return None


def cmd_bus_v3_migrate(args: argparse.Namespace, transport: Any) -> int:
    """Dry-run/apply the ruled s5 authority/cursor-only migration.

    Apply has exactly one possible write: the Bus authority document. Legacy
    cursors are classified but never seeded, repaired, or rewritten here;
    task and role paths are neither listed nor read.
    """
    mode = "apply" if args.apply else "dry-run"
    authority_path = records.config_path(args.team)
    reader = getattr(transport, "read_classified", None)
    if reader is None:
        raw, authority_read = None, "error"
    else:
        raw, authority_read = reader(authority_path)

    if authority_read == "error":
        authority_class = "unreadable-blocks"
        target = None
    elif raw is None:
        authority_class = "absent-blocks"
        target = None
    else:
        target, authority_class = records.schema1_authority_migration_target(raw)

    agents, census_error = _bus_v3_migration_agents(
        transport, args.team, list(args.agents or []))
    cursor_rows: list[dict[str, Any]] = []
    blocked = authority_class not in ("readable-legacy", "current")
    for agent in agents:
        _cursor, status = records.load_legacy_cursor_classified(
            transport, args.team, agent)
        classification = {
            "ok": "readable-legacy",
            "absent": "absent",
            "invalid": "malformed-blocks",
            "error": "unreadable-blocks",
        }[status]
        blocked = blocked or classification.endswith("blocks")
        cursor_rows.append({
            "agent": agent,
            "path": records.cursor_path(args.team, agent),
            "classification": classification,
        })
    if census_error is not None:
        blocked = True

    authority_write: Any = 0
    state = "BLOCKED" if blocked else "READY"
    rc = 3 if blocked else 0
    error_code = _bus_v3_migration_block_error(
        authority_class, cursor_rows, census_error)
    if args.apply and not blocked:
        if authority_class == "current":
            state = "CURRENT"
        else:
            rendered = records.render_authority_config(target or {})
            if not transport.write(authority_path, rendered):
                state, rc = "UNKNOWN", 2
                error_code = "authority-write-refused"
            else:
                # The write reached the transport. Until read-back proves the
                # target, report that fact without claiming either zero writes
                # or a verified mutation.
                authority_write = "ISSUED-BUT-UNPROVEN"
                verify_raw, verify_status = reader(authority_path)
                if verify_status != "ok" or verify_raw is None:
                    state, rc = "UNKNOWN", 3
                    error_code = "authority-verify-unreadable"
                else:
                    verified, verified_class = (
                        records.schema1_authority_migration_target(verify_raw))
                    if verified_class != "current" or verified != target:
                        state, rc = "UNKNOWN", 3
                        error_code = "authority-verify-mismatch"
                    else:
                        authority_write = 1
                        state = "APPLIED"
                        error_code = None

    envelope = {
        "type": "bus-v3-migration",
        "mode": mode,
        "state": state,
        "error_code": error_code,
        "authority": {
            "path": authority_path,
            "classification": authority_class,
            "target_schema": 1,
        },
        "cursors": cursor_rows,
        "agent_census_error": census_error,
        "writes": {
            "authority": authority_write,
            "legacy_cursors": 0,
            "tasks": 0,
            "roles": 0,
        },
        "rc": rc,
    }
    if args.json:
        jsonutil.print_json(envelope)
    else:
        print(f"bus-v3 migrate: {state} [{mode}] authority="
              f"{authority_class} cursors={len(cursor_rows)}")
        for row in cursor_rows:
            print(f"  {row['agent']}: {row['classification']}")
        if census_error:
            print(f"  census: {census_error}", file=sys.stderr)
        print("  writes: authority="
              f"{envelope['writes']['authority']} legacy-cursors=0 "
              "tasks=0 roles=0")
    return rc


def _write_consume_audit(transport: Any, team: str, *, caller: str,
                         target: str, cursor_path: str, observed_prior: Any,
                         intended_authority: dict[str, Any], ts: str) -> bool:
    """Durably record a deliberate ``--consume`` takeover BEFORE it happens.

    The consumption guard exists because a foreign-identity read silently ate
    another agent's pending directives (live incident 2026-07-28); the audit
    doc is what makes the deliberate override reconstructable after the fact.
    The fields record OBSERVATIONS and INTENT, never predictions — under
    concurrency this process cannot know what state its consuming read will
    actually overtake, only what it saw and what it meant to do:

    - ``observed_prior``: the target cursor's coverage claim as read at ``ts``
      (v2: authority generation + per-agent revision; legacy schema 1: its
      ``last_read``; or the bare classification ``absent``/``invalid``/
      ``error`` when there was no readable claim). A concurrent writer may
      advance the cursor between this observation and the consuming read.
    - ``intended_authority``: the cursor schema (and, for v2, the authority
      generation) the takeover intends to operate under. No predicted
      revision or timestamp: a staged delivery may never commit, a CAS loser
      adopts the winner's state, and the legacy save stamps its own clock.
      The actual transition is evidenced by the cursor document afterward.

    ORDERING INVARIANT: this audit document must land before any cursor
    MUTATION or consuming READ — the window/records query and every cursor
    write happen strictly after this returns True. Reading the target cursor
    to capture ``observed_prior`` is deliberately allowed beforehand: it is
    a plain observation read, not a mutation, and consumes nothing.

    Returns False when the document did not verifiably land — the caller then
    REFUSES the takeover, because an unauditable takeover is the silent
    consumption incident with a flag on it.
    """
    safe_time = ts.replace(":", "").replace("-", "").replace(".", "")
    path = records.consume_audit_path(
        team, stamp=safe_time,
        caller=tasks.agent_key(caller), target=tasks.agent_key(target))
    fm = {
        "type": "ConsumeAudit",
        "ts": ts,
        "caller": caller,
        "target": target,
        "cursor": cursor_path,
        "observed_prior": observed_prior,
        "intended_authority": intended_authority,
        "reason": (f"caller '{caller}' read the queue as '{target}' with "
                   "--consume — deliberate consumption-guard override"),
    }
    body = (okf.render_frontmatter(fm)
            + f"\n{caller} takes over {target}'s queue cursor.\n")
    return bool(transport.write(path, body))


def _print_v2_delivery(
        pending: dict[str, Any], *, cursor_revision: int, json_mode: bool,
        replay: bool, obligations: Optional[dict[str, Any]] = None,
) -> None:
    events = pending["events"]
    _warn_empty_queue_gap(
        events, json_mode=json_mode, obligations=obligations)
    _print_queue_events(events, json_mode=json_mode)
    envelope = {
        "type": "queue-delivery",
        "contract": 2,
        "token": pending["token"],
        "event_count": len(events),
        "event_ids": [event.get("record_id") for event in events],
        "window_start": pending["window_start"],
        "window_end": pending["window_end"],
        "cursor_revision": cursor_revision,
        "outcome": "replayed" if replay else "staged",
        # A DEFAULT transactional read folds nothing — it says so rather than
        # staying silent, so a consumer cannot read a delivery envelope as
        # "and nothing else is owed". An explicit ``--obligations`` is honored
        # here like anywhere else and supplies the real verdict instead.
        "obligations": (obligations if obligations is not None
                        else _obligations_not_checked()),
        "rc": 0,
    }
    if json_mode:
        jsonutil.print_json(envelope)
    else:
        print("queue: DELIVERY "
              f"token={pending['token']} events={len(events)} "
              f"revision={cursor_revision}; process the batch, then run "
              "`coord-engine queue commit <team> --agent <you> "
              f"--token {pending['token']} --result "
              "<record-id>=<outcome> ...`",
              file=sys.stderr)


def _cmd_queue_v2(
        args: argparse.Namespace, transport: Any, cfg: dict[str, Any],
        agent: str, *, peek: bool, engine_version: str
) -> int:
    """Transactional v2 read: stage first, advance only on explicit commit.

    ``--obligations`` is honored here on exactly the same terms as on the v1
    path (round-2 finding 2): explicit means fold, default means zero fold
    ops. It was previously accepted by the shared ``queue`` parser and then
    never read by this function — every v2 envelope claimed ``not-checked``
    even when the caller had asked for the fold, which is indistinguishable
    from a fold that ran and could say nothing. The fold is run at the LAST
    moment before each success envelope, never before the cursor work, so a
    read that fails still fails at its own cost.
    """
    generation = cfg["cursor_generation"]
    cursor, raw, status = records.load_v2_cursor_classified(
        transport, args.team, agent, generation)
    if status in ("error", "invalid"):
        return _queue_failure(
            args,
            state="INVALID" if status == "invalid" else "UNKNOWN",
            error_code=(
                "cursor-invalid" if status == "invalid"
                else "cursor-read-failed"
            ),
            message=(
                f"queue: DEGRADED — transactional cursor {status}; "
                "coverage untouched, retry"
            ),
            rc=3,
        )
    if cursor is None:
        # One-time dual-read migration. After the first successful v2 CAS the
        # isolated v2 document is authoritative forever.
        legacy, legacy_status = records.load_legacy_cursor_classified(
            transport, args.team, agent)
        if legacy_status in ("error", "invalid"):
            return _queue_failure(
                args,
                state="INVALID" if legacy_status == "invalid" else "UNKNOWN",
                error_code=(
                    "legacy-cursor-invalid"
                    if legacy_status == "invalid"
                    else "legacy-cursor-read-failed"
                ),
                message=(
                    "queue: DEGRADED — cannot safely seed cursor v2 because "
                    f"the legacy cursor is {legacy_status}; coverage untouched, "
                    "retry"
                ),
                rc=3,
            )
        cursor = records.initial_v2_cursor(generation, legacy)
        raw = None
    pending = cursor.get("pending")
    if isinstance(pending, dict):
        if peek:
            fragment = _requested_obligations(args, transport, agent)
            json_mode = bool(getattr(args, "json", False))
            _warn_empty_queue_gap(
                pending["events"], json_mode=json_mode,
                obligations=fragment)
            if json_mode:
                jsonutil.print_json(_queue_result_envelope(
                    pending["events"], cfg=cfg,
                    cursor_path=records.v2_cursor_path(
                        args.team, agent, generation),
                    advanced=False,
                    outcome_mix=records.outcome_mix(cursor),
                    obligations=fragment))
            else:
                _print_queue_events(pending["events"], json_mode=False)
            if pending["events"]:
                print("queue: peek — pending transactional batch shown; "
                      "token withheld and cursor untouched", file=sys.stderr)
            return 0
        _print_v2_delivery(
            pending, cursor_revision=cursor["revision"],
            json_mode=bool(getattr(args, "json", False)), replay=True,
            obligations=_requested_obligations(args, transport, agent))
        return 0

    if not peek:
        write_gate = records.compatibility(
            cfg, engine_version=engine_version, write_cursor=True)
        if not write_gate["ok"]:
            return _queue_failure(
                args,
                state="INCOMPATIBLE",
                error_code="engine-incompatible",
                message=(
                    f"queue: INCOMPATIBLE — {write_gate['reason']}; v2 cursor "
                    "untouched"
                ),
                rc=3,
            )
        if not records.v2_transport_ready(transport):
            return _queue_failure(
                args,
                state="INCOMPATIBLE",
                error_code="cas-unsupported",
                message=(
                    "queue: DEGRADED — active cursor v2 requires a proven "
                    "atomic compare-and-swap, but this transport does not "
                    "provide one; coverage untouched"
                ),
                rc=3,
            )

    committed = cursor["committed"]
    now = _now()
    last_read = committed.get("last_read")
    if last_read is None:
        since_dt = now - timedelta(seconds=records.DEFAULT_LOOKBACK_SECONDS)
    else:
        try:
            last = datetime.fromisoformat(last_read.replace("Z", "+00:00"))
        except ValueError:
            return _queue_failure(
                args,
                state="INVALID",
                error_code="cursor-invalid",
                message=(
                    "queue: DEGRADED — transactional cursor has invalid "
                    "committed time; coverage untouched, retry"
                ),
                rc=3,
            )
        since_dt = last - timedelta(seconds=records.CURSOR_SKEW_SECONDS)
    window_start, window_end = _iso(since_dt), _iso(now)
    window = transport.records(
        cfg["data_type"], window_start, window_end)
    events = records.events_for(window, agent)
    if events is None:
        return _queue_failure(
            args,
            state="UNKNOWN",
            error_code="window-unknown",
            message=(
                "queue: DEGRADED — window UNKNOWN, transactional cursor NOT "
                "staged or advanced"
            ),
            rc=3,
        )
    for warning in records.observed_version_warnings(window):
        print(f"queue: VERSION WARNING — {warning}", file=sys.stderr)
    for warning in records.invisible_writer_census(window):
        print(f"queue: DELIVERY WARNING — {warning}", file=sys.stderr)
    seen = set(committed["seen_ids"])
    fresh = [event for event in events
             if event.get("record_id") not in seen]
    if any(not isinstance(event.get("record_id"), str)
           or not event["record_id"] for event in fresh):
        # Bus data, not transport doubt: a record without a stable id will
        # not grow one on retry, so this is INVALID rather than UNKNOWN.
        return _queue_failure(
            args,
            state="INVALID",
            error_code="event-id-missing",
            message=(
                "queue: DEGRADED — recognized event lacks a stable record "
                "id; transactional batch not staged"
            ),
            rc=3,
        )
    if peek:
        fragment = _requested_obligations(args, transport, agent)
        json_mode = bool(getattr(args, "json", False))
        _warn_empty_queue_gap(
            fresh, json_mode=json_mode, obligations=fragment)
        if json_mode:
            jsonutil.print_json(_queue_result_envelope(
                fresh, cfg=cfg,
                cursor_path=records.v2_cursor_path(
                    args.team, agent, generation),
                advanced=False,
                outcome_mix=records.outcome_mix(cursor),
                obligations=fragment))
        else:
            _print_queue_events(fresh, json_mode=False)
        if fresh:
            print("queue: peek — transactional cursor NOT staged or advanced",
                  file=sys.stderr)
        return 0

    staged = records.stage_v2_delivery(
        transport, args.team, agent, generation,
        cursor=cursor, expected_raw=raw, staged_at=_iso(now),
        window_start=window_start, window_end=window_end, events=fresh)
    if staged["status"] == "unsupported":
        return _queue_failure(
            args,
            state="INCOMPATIBLE",
            error_code="cas-unsupported",
            message=(
                "queue: DEGRADED — active cursor v2 requires a proven atomic "
                "compare-and-swap, but this transport does not provide one; "
                "coverage untouched"
            ),
            rc=3,
        )
    if staged["status"] == "lost":
        winner, _winner_raw, winner_status = records.load_v2_cursor_classified(
            transport, args.team, agent, generation)
        if (winner_status != "ok" or winner is None
                or not isinstance(winner.get("pending"), dict)):
            return _queue_failure(
                args,
                state="UNKNOWN",
                error_code="stage-race-unverified",
                message=(
                    "queue: DEGRADED — concurrent stage lost and winner "
                    "could not be verified; coverage untouched, retry"
                ),
                rc=3,
            )
        _print_v2_delivery(
            winner["pending"], cursor_revision=winner["revision"],
            json_mode=bool(getattr(args, "json", False)), replay=True,
            obligations=_requested_obligations(args, transport, agent))
        return 0
    staged_cursor = staged["cursor"]
    _print_v2_delivery(
        staged_cursor["pending"], cursor_revision=staged_cursor["revision"],
        json_mode=bool(getattr(args, "json", False)), replay=False,
        obligations=_requested_obligations(args, transport, agent))
    return 0


def cmd_queue_commit(args: argparse.Namespace, transport: Any) -> int:
    team = getattr(args, "commit_team", None)
    agent = _declared_identity(getattr(args, "agent", None))
    token = getattr(args, "token", None)
    if not team or not agent or not token:
        return _queue_failure(
            args,
            state="REFUSED",
            error_code="usage",
            message="queue commit: TEAM, --agent, and --token are required",
            rc=2,
        )
    classifications: dict[str, str] = {}
    for value in getattr(args, "results", None) or []:
        record_id, separator, outcome = value.partition("=")
        if (not separator or not record_id
                or outcome not in records.DELIVERY_OUTCOMES
                or record_id in classifications):
            return _queue_failure(
                args,
                state="REFUSED",
                error_code="usage",
                message=(
                    "queue commit: each --result must be a unique "
                    "RECORD_ID=completed|blocked|superseded|ignored"
                ),
                rc=2,
            )
        classifications[record_id] = outcome
    cfg, cfg_status = records.load_config_classified(transport, team)
    if cfg is None or not records.v2_active(cfg):
        # One stderr line, three machine states: an unreadable authority is
        # retryable doubt, a malformed one is human-fixable, and a readable
        # non-v2 authority means this engine/verb pairing is not usable.
        state, error_code = {
            "error": ("UNKNOWN", "config-read-failed"),
            "invalid": ("INVALID", "config-invalid"),
        }.get(cfg_status, ("INCOMPATIBLE", "authority-not-v2"))
        return _queue_failure(
            args,
            state=state,
            error_code=error_code,
            message=(
                f"queue commit: INCOMPATIBLE — active cursor-v2 authority "
                f"required (config={cfg_status}); cursor untouched"
            ),
            rc=3,
        )
    from . import __version__ as engine_version
    # Same zero-transport check as the read side: a commit is a WRITE, so a
    # stale engine hears about itself before it stamps anything.
    currency = records.authority_currency(cfg, engine_version=engine_version)
    if currency:
        print(f"queue: ENGINE STALE — {currency}", file=sys.stderr)
    write_gate = records.compatibility(
        cfg, engine_version=engine_version, write_cursor=True)
    if not write_gate["ok"]:
        return _queue_failure(
            args,
            state="INCOMPATIBLE",
            error_code="engine-incompatible",
            message=(
                f"queue commit: INCOMPATIBLE — {write_gate['reason']}; "
                "cursor untouched"
            ),
            rc=3,
        )
    outcome = records.commit_v2_delivery(
        transport, team, agent, cfg["cursor_generation"], token=token,
        classifications=classifications)
    status = outcome["status"]
    if status in ("committed", "idempotent"):
        cursor = outcome["cursor"]
        payload = {
            "type": "queue-commit", "token": token, "outcome": status,
            "cursor_revision": cursor["revision"], "rc": 0,
        }
        if getattr(args, "json", False):
            jsonutil.print_json(payload)
        else:
            print(f"queue commit: {status} token={token} "
                  f"revision={cursor['revision']}")
        return 0
    if status == "stale":
        return _queue_failure(
            args,
            state="REFUSED",
            error_code="stale-token",
            message=(
                f"queue commit: STALE token rejected: {token}; cursor "
                "untouched"
            ),
            rc=3,
        )
    if status == "unsupported":
        return _queue_failure(
            args,
            state="INCOMPATIBLE",
            error_code="cas-unsupported",
            message=(
                "queue commit: DEGRADED — transport cannot prove atomic CAS; "
                "cursor untouched"
            ),
            rc=3,
        )
    if status == "unclassified":
        return _queue_failure(
            args,
            state="REFUSED",
            error_code="results-incomplete",
            message=(
                "queue commit: REFUSED — every staged event requires exactly "
                f"one --result; missing={outcome['missing']} "
                f"unexpected={outcome['unexpected']}; cursor untouched"
            ),
            rc=2,
        )
    if status == "invalid-events":
        return _queue_failure(
            args,
            state="INVALID",
            error_code="event-id-missing",
            message=(
                "queue commit: DEGRADED — pending batch contains an event "
                "without a stable record id; cursor untouched"
            ),
            rc=3,
        )
    # Remaining statuses come from the classified cursor read at commit time.
    fallback_state, fallback_code = {
        "invalid": ("INVALID", "cursor-invalid"),
        "error": ("UNKNOWN", "cursor-read-failed"),
    }.get(status, ("UNKNOWN", f"cursor-{status}"))
    return _queue_failure(
        args,
        state=fallback_state,
        error_code=fallback_code,
        message=(
            f"queue commit: DEGRADED — cursor {status}; cursor untouched, "
            "retry"
        ),
        rc=3,
    )


def _queue_failure(
        args: argparse.Namespace, *, state: str, error_code: str,
        message: str, rc: int,
        extra: "Optional[dict[str, Any]]" = None
) -> int:
    """Emit one stable queue failure for humans and JSONL automation.

    Exit status alone cannot distinguish the failure classes: rc 3 covers
    every fail-closed outcome and rc 2 every refusal.  In JSON mode stdout
    therefore carries a terminal envelope while stderr retains the actionable
    human diagnostic.  EVERY nonzero exit of the queue family (``queue`` and
    ``queue commit``, legacy and v2-active) routes through here — enforced by
    the AST completeness gate in tests/test_queue_terminal_states.py — so a
    ``--json`` consumer never sees a nonzero rc with empty stdout.  States:

    - ``UNKNOWN``       store/transport doubt; backoff and retry.
    - ``INVALID``       durable bytes exist but are malformed; human-fixable,
                        never auto-recreated over.
    - ``INCOMPATIBLE``  version/capability gate: engine below a floor,
                        unsupported schema, or no proven CAS transport.
    - ``ABSENT``        the store affirmatively has no records config.
    - ``REFUSED``       caller-side rejection: usage error, incomplete
                        ``--result`` set, stale token, unauditable takeover
                        is NOT here (that write failure is UNKNOWN — the doc
                        may or may not have landed).
    """
    print(message, file=sys.stderr)
    if bool(getattr(args, "json", False)):
        envelope = {
            "type": "queue-error",
            "contract": 2,
            "state": state,
            "error_code": error_code,
            "rc": rc,
        }
        if extra:
            # Nested diagnosis, still ONE object: a caller switching on `type`
            # sees queue-error and can drill into the cause without parsing a
            # second row.
            envelope.update(extra)
        jsonutil.print_json(envelope)
    return rc


def cmd_queue(args: argparse.Namespace, transport: Any) -> int:
    """THE bus v3 read: cursored event queue for one agent.

    Window = [cursor − skew, now]; no cursor ⇒ the default lookback. The
    cursor advances ONLY after a clean window whose events have been printed
    (delivery to stdout is the handoff); an UNKNOWN window (transport doubt,
    malformed line — ``transport.records()`` collapses all of it to None)
    prints DEGRADED, exits 3, and leaves coverage untouched, so the next read
    re-covers it. This is the window rule made automatic: an agent can be
    dark for a week and its next read covers the week.

    CONSUMPTION GUARD (live incident 2026-07-28): the cursor belongs to the
    agent's own duty loop — a read under another identity CONSUMED that
    agent's queue (operator diagnostics ate pending directives). Reading as
    an agent other than the caller's own ``$FULCRA_COORD_AGENT`` therefore
    defaults to PEEK (no cursor advance); ``--consume`` restores the old
    behavior for a deliberate takeover, and ``--peek`` forces a safe read
    even as yourself. Every ``--consume`` takeover writes a durable audit
    document under ``_coord/audit/consume/`` BEFORE the read proceeds; if
    that write fails the takeover is refused (an unauditable takeover does
    not happen). Under ``--json``, one ``queue-result`` object (state DATA
    or CLEAR) is the entire success stdout; failures keep the ``queue-error``
    envelope. Text-mode success output is unchanged.
    """
    if args.team == "commit":
        return cmd_queue_commit(args, transport)
    if getattr(args, "commit_team", None) is not None:
        return _queue_failure(
            args,
            state="REFUSED",
            error_code="usage",
            message="queue: unexpected second team argument",
            rc=2,
        )
    agent = _declared_identity(getattr(args, "agent", None))
    if not agent:
        return _queue_failure(
            args,
            state="REFUSED",
            error_code="usage",
            message="queue: --agent or FULCRA_COORD_AGENT required",
            rc=2,
        )
    own_identity = classifier.resolve_identity(environ=os.environ)
    peek = bool(getattr(args, "peek", False))
    # Guard fires ONLY when the caller HAS a declared identity that differs:
    # `--agent X` with no FULCRA_COORD_AGENT set is the normal automation
    # pattern (the flag IS the identity declaration) and must keep consuming,
    # or shipping this guard would silently freeze every fleet cursor.
    takeover = False
    if not peek and own_identity and agent != own_identity:
        if getattr(args, "consume", False):
            takeover = True  # deliberate override — audited before any read
        else:
            peek = True
            print(f"queue: reading as '{agent}' but this caller is "
                  f"'{own_identity}' — peek mode (cursor NOT advanced); pass "
                  "--consume to take over their queue deliberately",
                  file=sys.stderr)
    cfg, cfg_status = records.load_config_classified(transport, args.team)
    if cfg is None:
        if cfg_status == "error":
            # Unreadable is NOT missing: an expired-auth/offline host must
            # report DEGRADED (retryable), or its automation reads "config
            # missing" as a durable state and goes quietly deaf (2026-07-28).
            return _queue_failure(
                args,
                state="UNKNOWN",
                error_code="config-read-failed",
                message=(
                    "queue: DEGRADED — records config could not be read "
                    "(transport failure, not a missing config); window UNKNOWN, "
                    "cursor untouched — check auth/network and retry"
                ),
                rc=3,
            )
        if cfg_status == "invalid":
            return _queue_failure(
                args,
                state="INVALID",
                error_code="config-invalid",
                message=(
                    "queue: INCOMPATIBLE — bus-v3 authority is malformed or "
                    "partially versioned; cursor untouched"
                ),
                rc=3,
            )
        return _queue_failure(
            args,
            state="ABSENT",
            error_code="config-absent",
            message=(
                "queue: no bus-v3 records config "
                f"(team/{args.team}/{records.CONFIG_NAME} or "
                f"{records.ENV_DATA_TYPE}) — cannot read the record queue"
            ),
            rc=2,
        )
    from . import __version__ as engine_version
    # Zero transport: the currency check rides the config this read already
    # loaded, so a snapshot-restored engine learns it is stale at its FIRST
    # bus touch instead of writing events nobody can parse.
    currency = records.authority_currency(cfg, engine_version=engine_version)
    if currency:
        print(f"queue: ENGINE STALE — {currency}", file=sys.stderr)
    read_gate = records.compatibility(
        cfg, engine_version=engine_version, write_cursor=False)
    if not read_gate["ok"]:
        return _queue_failure(
            args,
            state="INCOMPATIBLE",
            error_code="engine-incompatible",
            message=(
                f"queue: INCOMPATIBLE — {read_gate['reason']}; cursor "
                "untouched"
            ),
            rc=3,
        )
    for warning in read_gate["warnings"]:
        print(f"queue: VERSION WARNING — {warning}", file=sys.stderr)
    v2 = records.v2_active(cfg)
    if takeover:
        # The audit doc lands BEFORE the takeover read touches anything: an
        # unauditable takeover does not happen (fail closed), and plain reads
        # and --peek never reach this write. Capturing observed_prior below
        # is a plain observation READ of the target cursor — allowed before
        # the audit lands (it mutates and consumes nothing); the window/
        # records query still runs only after the audit write succeeds. The
        # audit records what THIS process observed and intended, never a
        # prediction: a concurrent writer may move the cursor between the
        # observation and the consuming read, so the actual transition is
        # evidenced by the cursor document itself afterward.
        audit_ts = _iso(_now())
        if v2:
            generation = cfg["cursor_generation"]
            target_cursor = records.v2_cursor_path(args.team, agent, generation)
            prior_cursor, _prior_raw, prior_status = (
                records.load_v2_cursor_classified(
                    transport, args.team, agent, generation))
            if prior_status == "ok" and prior_cursor is not None:
                observed_prior: Any = {
                    "schema": 2, "generation": generation,
                    "revision": prior_cursor["revision"]}
            else:
                observed_prior = prior_status
            intended_authority = {"schema": 2, "generation": generation}
        else:
            target_cursor = records.cursor_path(args.team, agent)
            prior_cursor_v1, prior_status = records.load_cursor_classified(
                transport, args.team, agent)
            if prior_status == "ok" and prior_cursor_v1 is not None:
                observed_prior = {
                    "schema": 1, "last_read": prior_cursor_v1["last_read"]}
            else:
                observed_prior = prior_status
            intended_authority = {"schema": 1}
        if not _write_consume_audit(
                transport, args.team, caller=own_identity, target=agent,
                cursor_path=target_cursor, observed_prior=observed_prior,
                intended_authority=intended_authority, ts=audit_ts):
            return _queue_failure(
                args,
                state="UNKNOWN",
                error_code="consume-audit-failed",
                message=(
                    "queue: REFUSED — takeover audit record could not be "
                    f"written; --consume of '{agent}' aborted, cursor "
                    "untouched"
                ),
                rc=3,
            )
    if v2:
        return _cmd_queue_v2(
            args, transport, cfg, agent, peek=peek,
            engine_version=engine_version)
    if not peek:
        write_gate = records.compatibility(
            cfg, engine_version=engine_version, write_cursor=True)
        if not write_gate["ok"]:
            return _queue_failure(
                args,
                state="INCOMPATIBLE",
                error_code="engine-incompatible",
                message=(
                    f"queue: INCOMPATIBLE — {write_gate['reason']}; "
                    "refusing before cursor write"
                ),
                rc=3,
            )
        for warning in write_gate["warnings"]:
            if warning not in read_gate["warnings"]:
                print(f"queue: VERSION WARNING — {warning}", file=sys.stderr)
    now = _now()
    cursor, cursor_status = records.load_cursor_classified(
        transport, args.team, agent)
    if cursor_status in ("error", "invalid"):
        # INVALID is terminal and human-fixable: the cursor document exists
        # but does not parse, so guessing a lookback and then saving over the
        # corrupt bytes would auto-recreate the cursor and destroy the only
        # evidence of what went wrong. Same discrimination the v2 path makes.
        return _queue_failure(
            args,
            state="INVALID" if cursor_status == "invalid" else "UNKNOWN",
            error_code=(
                "cursor-invalid" if cursor_status == "invalid"
                else "cursor-read-failed"
            ),
            message=(
                f"queue: DEGRADED — cursor {cursor_status}; coverage "
                "untouched"
                + (", fix or remove "
                   f"{records.cursor_path(args.team, agent)} and retry"
                   if cursor_status == "invalid" else ", retry")
            ),
            rc=3,
        )
    if cursor is None:
        since_dt = now - timedelta(seconds=records.DEFAULT_LOOKBACK_SECONDS)
        seen: list[str] = []
    else:
        try:
            last = datetime.fromisoformat(cursor["last_read"].replace("Z", "+00:00"))
        except ValueError:
            return _queue_failure(
                args,
                state="INVALID",
                error_code="cursor-invalid",
                message=(
                    "queue: DEGRADED — cursor has an invalid last_read time; "
                    "coverage untouched, fix or remove "
                    f"{records.cursor_path(args.team, agent)} and retry"
                ),
                rc=3,
            )
        since_dt = last - timedelta(seconds=records.CURSOR_SKEW_SECONDS)
        seen = list(cursor["seen_ids"])
    window = transport.records(cfg["data_type"], _iso(since_dt), _iso(now))
    events = records.events_for(window, agent)
    if events is None:
        return _queue_failure(
            args,
            state="UNKNOWN",
            error_code="window-unknown",
            message=(
                "queue: DEGRADED — window UNKNOWN, cursor NOT advanced "
                "(re-covered next read)"
            ),
            rc=3,
        )
    for warning in records.observed_version_warnings(window):
        print(f"queue: VERSION WARNING — {warning}", file=sys.stderr)
    for warning in records.invisible_writer_census(window):
        print(f"queue: DELIVERY WARNING — {warning}", file=sys.stderr)
    seen_set = set(seen)
    fresh = [e for e in events if e.get("record_id") not in seen_set]
    json_mode = bool(getattr(args, "json", False))
    # CLASSIFIED ONCE, for BOTH modes. Doing it inside the text renderer meant
    # `--json` — the mode automation uses — skipped validation, saved the
    # cursor, and then raised inside the envelope: consumed and invisible
    # (codex-reviewer, 600 r2).
    renderable, poisoned = _classify_queue_events(fresh)
    poison = len(poisoned)
    # Computed BEFORE the guarded region so the `finally` below always has a
    # valid value to persist, even if a future edit raises above it. Poison is
    # included: it appears in the output of BOTH modes, so consuming it honours
    # at-least-once rather than dropping it.
    new_seen = seen + [e["record_id"] for e in fresh
                       if isinstance(e, dict) and isinstance(e.get("record_id"), str)]
    try:
        if not json_mode:
            # Text mode stays byte-identical for shell consumers; the JSON
            # envelope is emitted once below, after the cursor outcome is known.
            poison = _print_queue_events(fresh, json_mode=False)
    finally:
        # THE INVARIANT, enforced structurally rather than by remembering.
        # Once a window has been READ, coverage is a fact about what this
        # process received — not contingent on our ability to render, summarise
        # or fold anything afterwards. The wedge existed because an exception
        # between the read and the save skipped it, and at-least-once
        # redelivery then returned the same poison forever. A `finally` holds
        # even for exceptions a later edit introduces; a careful ordering does
        # not. `peek` is the ONE exit that legitimately does not advance, and it
        # is below this block, so it cannot be reached without passing here.
        if not peek:
            advanced = records.save_cursor(transport, args.team, agent,
                                           last_read=_iso(now),
                                           seen_ids=new_seen)
            if not advanced:
                print("queue: cursor save failed — coverage unadvanced, next "
                      "read re-covers this window", file=sys.stderr)
    if peek:
        obligations_fragment = _requested_obligations(args, transport, agent)
        _warn_empty_queue_gap(
            fresh, json_mode=json_mode, obligations=obligations_fragment)
        if json_mode:
            jsonutil.print_json(_queue_result_envelope(
                renderable, cfg=cfg,
                cursor_path=records.cursor_path(args.team, agent),
                advanced=False, obligations=obligations_fragment,
                poisoned=poisoned))
        if fresh:
            print(f"queue: peek — {len(fresh)} event(s) shown, cursor NOT "
                  "advanced (the owning agent still receives them)",
                  file=sys.stderr)
        return 0
    if poison:
        # LOUD, and after the save: a poisoned window is a delivery-integrity
        # event someone must look at, but it is no longer a reason to stop
        # receiving mail.
        print(f"queue: {poison} unrenderable event(s) in this window — shown as "
              "POISON lines above and CONSUMED; coverage advanced. Report the "
              "POISON lines verbatim: they name a writer this build cannot "
              "format.", file=sys.stderr)
    obligations_fragment = _requested_obligations(args, transport, agent)
    _warn_empty_queue_gap(
        fresh, json_mode=json_mode, obligations=obligations_fragment)
    if json_mode:
        envelope = _queue_result_envelope(
            renderable, cfg=cfg,
            cursor_path=records.cursor_path(args.team, agent),
            advanced=advanced, obligations=obligations_fragment,
            poisoned=poisoned)
        jsonutil.print_json(envelope)
    return 0


def _requested_obligations(
        args: argparse.Namespace, transport: Any,
        agent: str) -> "Optional[dict[str, Any]]":
    """Run the obligation fold for a queue read, IF the caller asked for it.

    One rule, applied at every queue success envelope: an explicit
    ``--obligations`` always folds; anything else never does. Returning None
    is what makes the envelope say ``{"state": "not-checked"}``.

    A queue read establishes what EVENTS arrived in the window. It does not
    establish what is owed — that is the r2 spec item-3 distinction, and the
    fold is the only thing that closes it. That gap does not close just
    because the window happened to deliver something, which is why the flag is
    no longer gated on an empty read (round-2 findings 1/2): a flag accepted
    and then quietly dropped hands the caller silence they cannot tell apart
    from a real verdict.

    OPT-IN since the 2026-08-02 promise plan (T3(b)), reversing the 2026-07-30
    default-ON ruling. It costs a task-index listing, a review listing and a
    roles listing per wake; the measured evidence is that at the default
    budget it reaches no component and can only answer UNKNOWN, so every
    default wake paid that bill for no information. The DEFAULT path performs
    zero fold ops on any window — that is the cost contract, and it is
    untouched. Pass ``--obligations`` to buy the answer when it is worth
    buying; when it is skipped the envelope says ``{"state": "not-checked"}``
    rather than nothing, so the skip stays visible to automation.

    The fragment reports fold UNKNOWN/INVALID inside a successful queue result.
    rc 3 is reserved for a degraded event window; the standalone ``obligations``
    verb retains its own nonzero UNKNOWN/INVALID contract.
    """
    if not getattr(args, "obligations", False):
        return None
    result = obligations_mod.fold(
        _obligation_probes(transport, args.team, agent, now=_iso(_now())),
        expected=obligations_mod.OBLIGATION_COMPONENTS)
    fragment = {
        "state": result.state.value,
        "owed_count": len(result.owed),
        "consulted": result.consulted,
        "degraded": result.degraded,
        "malformed": result.malformed,
        "reason": result.reason(),
    }
    if not getattr(args, "json", False):
        print(f"queue: obligations {result.state.value} — {result.reason()}",
              file=sys.stderr)
    return fragment


def cmd_respond(args: argparse.Namespace, transport: Any) -> int:
    agent = _identity(args.agent)
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
    shard = _response_path(args.team, args.name, stamp)
    # T1 (coord-boss scope addition, 2026-08-08): `transport.write` returns False
    # on a transport failure rather than raising, so this used to be able to lose
    # the response, print success and exit 0 — the responder believes they
    # answered and the asker never hears anything at all. Same family as PR 583.
    if not transport.write(shard, okf.render_frontmatter(fm)
                           + f"\n{args.evidence or args.outcome}\n"):
        print(f"respond: response NOT recorded for {args.name} — the shard write "
              f"failed (transport). NOTHING was closed; retry.", file=sys.stderr)
        return 3
    rc = 0
    try:
        out = tasks.apply_update(doc, now=now, status="done",
                                 evidence=f"{args.outcome} (respond by {agent})")
        if transport.write(path, out):
            print(f"responded {args.name}: {args.outcome} (closed)")
        else:
            # apply_update succeeding is NOT the close landing.
            print(f"responded {args.name}: {args.outcome} (response recorded; "
                  f"not closed: the task write failed — the directive is still "
                  f"OPEN)", file=sys.stderr)
            rc = 3
    except tasks.TaskError as e:
        # NOT an error rc: the status machine legitimately refuses some closes
        # (already done, illegal transition), and the response IS recorded. Only
        # a failed WRITE — a response or close that never landed — is non-zero.
        print(f"responded {args.name}: {args.outcome} (response recorded; not closed: {e})")
    # THE REPLY LEG. This printed an unconditional "the owner's queue surfaces
    # it" while emitting nothing — the queue reads events and a shard cannot
    # reach it. The line was believed, so a responded-to directive was re-asked
    # twice at rising priority (2026-08-08). Say only what happened.
    owner = str((okf.parse_frontmatter(doc) or {}).get("owner") or "")
    delivered = _emit_response_companion(
        transport, args.team, slug=args.name, owner=owner, responder=agent,
        shard_ptr=shard.split("/", 2)[-1])
    if delivered:
        print("response recorded and delivered — the owner's queue surfaces it")
    else:
        print("response recorded (durable) — NOT delivered to the owner's queue; "
              "they see it only by reading the task doc")
    return rc


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
    candidates: set[str] = set()
    for n in sorted(names):
        if n == "index.md":
            continue
        if n.endswith(".md"):
            candidates.add(n[:-3])
        elif "." not in n.rstrip("/"):
            # A role lease directory can outlive or precede its role document.
            # It is still a candidate whose state must be classified, not an
            # absence that licenses "nothing to park".
            candidates.add(n.rstrip("/"))
    for role in sorted(candidates):
        holders, ok = _role_fresh_holders(
            transport, team, role, now=now, listing_cache=cache)
        if not ok:
            ok_all = False  # this role's state is unknown; do not read it as "not held"
            continue
        if agent in holders:
            held.append(role)
    return held, ok_all


def cmd_continuity_park(args: argparse.Namespace, transport: Any) -> int:
    """Session-exit checkpoint for every held role, or one selected role."""
    agent = _identity(args.agent)
    now = _iso(_now())
    if args.role:
        holders, ok = _role_fresh_holders(
            transport, args.team, args.role, now=now)
        held = [args.role] if ok and agent in holders else []
    else:
        held, ok = _held_roles(transport, args.team, agent)
    if not ok:
        # UNKNOWN is not "nothing to park". Refusing here is the whole point: a
        # session runs park as it exits, so a silent no-op discards the checkpoint
        # the NEXT session resumes from, and nobody is watching to notice. Say the
        # checkpoint was not written, loudly and non-zero, while the operator can
        # still retry with the context still alive.
        scope = f"role {args.role}" if args.role else f"which roles {agent} holds"
        print(f"park: could not determine {scope} in team/{args.team} "
              f"(role state unreadable, not empty) — "
              f"CHECKPOINT NOT WRITTEN. Nothing was parked; retry before ending "
              f"the session.", file=sys.stderr)
        return 1
    if not held:
        holding = (f"does not hold fresh role {args.role}"
                   if args.role else "holds no fresh roles")
        print(f"park: {agent} {holding} in team/{args.team} — "
              f"CHECKPOINT NOT WRITTEN because there was nothing to park",
              file=sys.stderr)
        return 2
    def _build(role: str) -> "tuple[str, dict]":
        task_slug = f"role-{tasks.slugify(role)}"
        return task_slug, continuity.build_snapshot(
            agent=agent, task=task_slug,
            objective=args.objective or f"parked role {role} at session exit",
            now=now, next_actions=args.next or [],
            open_questions=args.open_question or [],
            decisions=getattr(args, "decision", None) or [],
            artifacts=getattr(args, "artifact", None) or [],
        )

    if getattr(args, "handoff", False):
        # Validate BEFORE writing anything. The sections are identical across
        # held roles, so one check covers the batch — and a half-written batch
        # is the worst outcome available here: some roles pointing at a
        # checkpoint the successor was told to trust, others not.
        _, probe = _build(held[0])
        findings = handoff.validate(
            probe, resolve=handoff.store_resolver(transport, args.team))
        if findings:
            print(handoff.format_findings(findings), file=sys.stderr)
            return 3

    failed = False
    for role in held:
        task_slug, snap = _build(role)
        path = _continuity_path(args.team, agent, task_slug)
        if not transport.write(path, json.dumps(snap, indent=2)):
            print(f"park: snapshot write FAILED for {role}; checkpoint_ref left unchanged",
                  file=sys.stderr)
            failed = True
            continue
        # The save landed, so the moment is owed — emitted BEFORE the
        # checkpoint_ref update because the two answer different questions. The
        # ref is role bookkeeping; the moment records that this agent saved
        # state at this instant, which is true whether or not the role doc
        # accepts a pointer to it.
        _checkpoint_moment(transport, args.team, snap, path)
        if not _set_role_field(transport, args.team, role, "checkpoint_ref", path):
            print(f"park: checkpoint_ref update FAILED for {role}", file=sys.stderr)
            failed = True
            continue
        print(f"parked {role} -> {path}")
    return 1 if failed else 0


def cmd_briefing(args: argparse.Namespace, transport: Any) -> int:
    """One-call session-start bundle. Every section tolerates absent add-ons."""
    agent = _identity(args.agent)
    now = _iso(_now())
    out: dict[str, Any] = {"schema": "coord.teams.briefing.v1", "team": args.team,
                           "agent": agent, "at": now}
    # Public-read failure contract (see _read_degraded_row): the CORE task fold is
    # not an add-on — an UNKNOWN summaries index must surface as the shared marker,
    # never a silently-empty board/inbox/needs-me that reads as "all clear". The
    # bundle stays tolerant (rc 0); the marker + stderr notice make it loud.
    doc_sink: list[Any] = []
    feed_sink: list[Any] = []
    rows, rows_ok, rows_reason = _load_rows_status(
        transport, args.team, doc_sink=doc_sink, feed_sink=feed_sink,
        feed_section_key=projection_mod.NEEDS_ME_KEY)
    agg_doc = doc_sink[0] if doc_sink else None
    feed_evidence = feed_sink[0] if feed_sink else None
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
        # BRIEFING MEASURES WORK (coord-boss ruling, 2026-08-09): it feeds
        # dispatch decisions, and a false nudge is the one thing it must never
        # emit. Bounded by the SAME add-on deadline as the shard read, and a
        # scan that runs out of budget reports PARTIAL — which withholds the
        # nudge rather than reverting to it. The continuity audit deliberately
        # does NOT measure; see its own call site.
        work_index, work_ok = _work_evidence_index(
            transport, args.team, deadline=add_on.instant)
        out["presence"] = presence.roster(
            shards, now=now, work_index=work_index,
            work_scan=(presence.WORK_SCAN_COMPLETE if work_ok
                       else presence.WORK_SCAN_PARTIAL))
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
    role_resolution: dict[str, tuple[list[str], bool]] = {}
    try:
        # The role fold sits BETWEEN add_on's open (above, consumed by presence)
        # and its later consumers (pending-reviews, forge, the resume check).
        # add_on is an ABSOLUTE deadline, so a fold that ignores it does not
        # merely overrun its own cap — it burns the shared window and starves the
        # sections that DO respect it, while the comment below asserts the
        # add-on stack is bounded. Keep the fold's own cap AND the shared
        # ceiling: whichever arrives first wins, the composition pattern the
        # overlay at _load_rows already uses.
        _role_dl = _role_fold_budget()
        _shared_left = add_on.remaining()
        held_roles, unresolved_roles = _held_roles_for_rows(
            transport, args.team, agent, rows, now=now,
            deadline_seconds=(_role_dl if _shared_left is None
                              else min(_role_dl, _shared_left)),
            resolution_sink=role_resolution)
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
            transport, args.team, agent, rows=rows, deadline=add_on.instant,
            aggregate_doc=agg_doc, feed_evidence=feed_evidence,
            role_resolution=role_resolution)
    except Exception as e:
        print(f"briefing: pending_reviews section unavailable ({type(e).__name__})", file=sys.stderr)
        out["pending_reviews"] = []
    try:
        out["forge_feedback"] = _forge_feedback_for(
            transport, args.team, agent, deadline=add_on.instant,
            aggregate_doc=agg_doc, feed_evidence=feed_evidence)
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
    # Same treatment as needs-me, for the same reason: briefing's degraded markers
    # sit INSIDE per-section lists that scale with the store, so a truncating
    # reader loses them while the header survives. briefing always returns 0 — the
    # envelope's degraded count is therefore the ONLY machine signal it has, which
    # makes this more load-bearing here, not less.
    _brief_degraded = sum(
        1 for section in ("presence", "inbox", "needs_me", "pending_reviews",
                          "forge_feedback")
        for r in (out.get(section) or [])
        if _is_degraded_row(r)
    ) + (1 if out.get("read_degraded") else 0) + (
        1 if out.get("role_degraded") else 0)
    emit_envelope(
        "briefing", count=len(out.get("needs_me") or []), rc=0,
        inbox=len(out.get("inbox") or []),
        reviews=len(out.get("pending_reviews") or []),
        degraded=_brief_degraded,
    )
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
        "review-orphan-degraded", "review-role-degraded", "review-source")
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
    forge_fb = [r for r in forge_rows
                if r.get("type") not in ("forge-degraded", "forge-source")]
    forge_deg = [r for r in forge_rows if r.get("type") == "forge-degraded"]
    forge_src = [r for r in forge_rows if r.get("type") == "forge-source"]
    print(f"  forge feedback: {len(forge_fb)} PR(s)")
    for r in forge_fb[:5]:
        print(_forge_feedback_line(r))
    for r in forge_deg:  # always shown — a degraded fold must never hide
        print(_forge_degraded_line(r))
    for r in forge_src:  # the fold's source disclosure (projection vs raw scan)
        print(_source_line("forge", r))
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
    agent = _identity(args.agent)
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
        "engine": records.engine_stamp(),
    }
    if engagement_obj is not None:
        fm["engagement"] = engagement_obj
    body = f"\n# Presence: {agent}\n"
    transport.write(shard_path, okf.render_frontmatter(fm) + body)
    print(f"beat {agent} ({slug}.md)")
    return 0


#: Directories whose entries are dated WORK ARTIFACTS attributable to an agent.
#: Each is a positive finding: a file here means that agent did something at
#: that time. Nothing here can prove the converse — an agent missing from the
#: scan is UNKNOWN, never idle.
def _work_evidence_index(
    transport: Any, team: str, *, deadline: Optional[float] = None
) -> tuple[dict[str, str], bool]:
    """Newest work-artifact timestamp per agent, READ-DERIVED (coord-boss's
    third guardrail: measured mtimes, never inference).

    Scans the two places an agent's work lands WITHOUT passing through a verb,
    which is exactly the blind spot a beat-only signal has:

      - ``review/<slug>/verdicts/<head>--<reviewer>.md`` — filing a verdict is
        not a verb at all; `review request` prints the path and the reviewer
        writes the shard itself.
      - ``_coord/agents/<agent>/reports/*`` — report docs, likewise written
        straight to the store.

    Returns ``(index, ok)``. Every entry found is a true positive regardless, but
    ``ok`` is what licenses the ABSENCE reading: only a scan that completed can
    say "this agent has no recent artifact" rather than "we did not see one".
    A listing that raises or returns ``None`` sets ``ok=False`` — `list_dir`
    cannot tell a real empty directory from an unreadable one, so a partial scan
    that silently reported absence would nudge working agents all over again,
    one layer down.
    """
    newest: dict[str, str] = {}
    ok = True
    if _PUBLIC_READ_CONTEXT.get() is not None:
        # Work-artifact mtimes are not part of the Unit-4 generation.  Mixing a
        # later raw review/report listing into a generation-backed roster would
        # create a second freshness authority.  PARTIAL preserves the existing
        # annotation field while withholding any negative/nudge inference.
        return {}, False

    def _note(agent: str, mtime: Any) -> None:
        # Store mtimes render on a TWELVE-HOUR clock ("2026-08-09 01:14AM UTC"),
        # so comparing them as strings inverts the midnight hour — 12AM sorts
        # after every other hour, and a "newest" picked that way can be older
        # than what it beat. Normalize to a UTC ISO instant FIRST, then compare;
        # `aggregate._parse_store_mtime` is the existing parser and stays the
        # single implementation.
        if not agent or not isinstance(mtime, str):
            return
        dt = aggregate._parse_store_mtime(mtime)
        if dt is None:
            return
        iso = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if agent not in newest or iso > newest[agent]:
            newest[agent] = iso

    def _entries(path: str) -> list[dict[str, Any]]:
        nonlocal ok
        if deadline is not None and time.monotonic() >= deadline:
            ok = False          # out of budget: PARTIAL, never a false absence
            return []
        try:
            rows = transport.list_dir(path)
        except TransportError:
            ok = False
            return []
        if rows is None:       # UNKNOWN, not empty — same ambiguity, no raise
            ok = False
            return []
        return rows

    # CHEAP HALF FIRST, and the order is load-bearing rather than tidy.
    #
    # Measured on the live store 2026-08-09, immediately after 591 shipped:
    # 35 agent directories vs 438 review directories, one listing each. With the
    # review sweep running first it consumed the ENTIRE budget every time — at
    # 120s, six times the shipped budget, the scan was still incomplete and had
    # attributed work to three agents. Because PARTIAL withholds the nudge, that
    # turned the signal off fleet-wide: a fix for false nudges that produced no
    # nudges at all.
    #
    # Agent reports are ~1/12th the listings and cover every agent that writes a
    # report, so doing them first makes the common case COMPLETE instead of
    # always-partial. The review sweep then spends whatever budget remains, and
    # anything it does not reach is still reported honestly as PARTIAL.
    for row in _entries(f"team/{team}/_coord/agents"):
        name = str(row.get("name") or "").rstrip("/")
        if not name:
            continue
        for r in _entries(f"team/{team}/_coord/agents/{name}/reports"):
            _note(name, r.get("mtime"))

    for row in _entries(f"team/{team}/review"):
        name = str(row.get("name") or "")
        if not name.endswith("/"):
            continue
        for v in _entries(f"team/{team}/review/{name.rstrip('/')}/verdicts"):
            vn = str(v.get("name") or "")
            # `<40-hex head>--<reviewer>.md` — the reviewer is the attribution.
            if "--" in vn and vn.endswith(".md"):
                _note(vn.rsplit("--", 1)[1][:-3], v.get("mtime"))

    return newest, ok


def cmd_presence_show(args: argparse.Namespace, transport: Any) -> int:
    # BOUNDED (codex-reviewer, 591 r3): unbounded, this listed every review's
    # verdicts dir — 435 on the live store — synchronously, on a command whose
    # whole job is to print a roster. Its own deadline, since a direct command
    # has no add-on stack to share one with; expiry degrades to PARTIAL, which
    # withholds the nudge rather than inventing an absence.
    work_deadline = Deadline.open(_presence_work_budget())
    work_index, work_ok = _work_evidence_index(
        transport, args.team, deadline=work_deadline.instant)
    ros = presence.roster(
        _presence_shards(transport, args.team), now=_iso(_now()),
        work_index=work_index,
        work_scan=(presence.WORK_SCAN_COMPLETE if work_ok
                   else presence.WORK_SCAN_PARTIAL))
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


def _engagement_defaults_path(team: str) -> str:
    return f"{router.router_prefix(team)}engagement-defaults.json"


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
        entries = transport.list_dir(router.router_prefix(team))
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
    """Claim a role lease.

    THE 2026-09-04 OUTAGE IS WHY THIS READS `read_classified` AND CHECKS THE
    WRITE. For about an hour the store returned HTTP 500 on every path three
    levels deep or more. `transport.read` collapses "absent" and "unreadable"
    into None, and `transport.write` returns False that nobody looked at, so
    this verb — measured, not reasoned about — did three wrong things at once:

    * printed "role 'coord-boss' has no registered role doc" while the doc sat
      in the store at 1390 bytes, untouched since 2026-08-03. The message is
      load-bearing: it tells the reader that dormancy suppression and review
      role-routing are OFF for the role;
    * printed "claimed coord-boss" and exited 0 for a write that never landed —
      the shard's version history runs 01:14:22Z straight to 02:14:45Z;
    * updated the LOCAL nonce state to that phantom write's nonce, so the next
      real claim raised "another session last claimed as coord-boss" — an alarm
      whose own text invites the reader to hunt an intruder that never existed.

    An unreadable read is UNKNOWN, never absent; an unverified write is not a
    claim. Both are the same rule, applied on the two planes.
    """
    agent = _identity(args.agent)
    slug = tasks.agent_key(agent)
    doc_raw, doc_status = transport.read_classified(_role_doc_path(args.team, args.role))
    if doc_status == "error":
        print(f"WARNING: could not READ the role doc for {args.role!r} — this is "
              f"UNKNOWN, not absence. Status folds and review role-routing may be "
              f"running on defaults, and a dormant_until on this role cannot be "
              f"seen right now. Re-check before acting on anything that depends "
              f"on the role doc.", file=sys.stderr)
    elif okf.parse_frontmatter(doc_raw) is None:
        print(f"note: role {args.role!r} has no registered role doc — status folds fall back "
              f"to defaults and review role-routing will NOT match this role's holders; "
              f"create team/{args.team}/roles/{args.role}.md", file=sys.stderr)
    shard_path = f"{_leases_prefix(args.team, args.role)}{slug}.md"
    state = _nonce_state_path(args.team, args.role, slug)
    # Same-id double-acting check: leases can't distinguish two sessions sharing one
    # id (same shard file), so compare the shard's nonce to the one THIS session wrote.
    shard_raw, shard_status = transport.read_classified(shard_path)
    if shard_status == "error":
        # An unreadable shard has no nonce to compare, and silently skipping the
        # comparison would present "check disabled" as "check passed".
        print(f"note: the lease shard for {agent} could not be read — the "
              f"same-id double-acting check did NOT run this pass; a second "
              f"session claiming this role would not have been detected.",
              file=sys.stderr)
    existing = okf.parse_frontmatter(shard_raw) or {}
    try:
        stored = state.read_text().strip() if state.exists() else None
    except OSError:
        stored = None
    shard_nonce = existing.get("nonce")  # absent for pre-nonce shards: overwrites by
    # old-engine sessions are undetectable by design — nothing to compare against.
    if stored and shard_nonce and shard_nonce != stored:
        # Surface the AGE of the mismatching write: a 17-day-dead predecessor
        # lineage and a live intruder produce the same nonce mismatch, and only
        # the shard timestamp (already in hand) tells them apart.
        age = presence._ago_label(existing.get("timestamp"), _iso(_now()))
        when = "at an UNKNOWN time" if age == "unknown" else f"{age} ago"
        print(f"WARNING: nonce mismatch on {slug}.md — another session last claimed "
              f"as {agent} {when} (same-id double-acting). A large age means a dead "
              f"predecessor lineage, not a live intruder — check it before escalating. "
              f"Give each session its own FULCRA_COORD_AGENT identity, or stop one.",
              file=sys.stderr)
    elif stored is None and shard_nonce:
        print(f"note: taking over an existing lease shard for {agent} written by another "
              f"session (no local nonce state to compare)", file=sys.stderr)
    nonce = secrets.token_hex(8)
    fm = {"type": "Lease", "title": f"{args.role} lease — {agent}", "agent": agent,
          "timestamp": _iso(_now()), "nonce": nonce,
          "summary": args.summary or ""}
    if not transport.write(shard_path,
                           okf.render_frontmatter(fm) + f"\nHolding {args.role}.\n"):
        # The write did not land. Saying "claimed" here is the failure that lets
        # a lease lapse while every log line says it was renewed — and writing
        # the nonce locally would poison the NEXT claim's double-acting check
        # with a nonce the store never saw.
        print(f"roles claim: WRITE FAILED for {slug}.md — {args.role} is NOT "
              f"claimed and the local nonce state is left untouched. The lease "
              f"stands at whatever the store last accepted; re-run when the "
              f"transport recovers.", file=sys.stderr)
        return 3
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(nonce + "\n")
    except OSError as e:
        print(f"note: could not persist nonce state (double-acting check disabled "
              f"until it can be written): {e}", file=sys.stderr)
    print(f"claimed {args.role} as {agent} ({slug}.md; refresh by re-running)")
    return 0


def cmd_roles_release(args: argparse.Namespace, transport: Any) -> int:
    agent = _identity(args.agent)
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


# --- router (feed-first decision plane + host-local executor) ---

def _shadow_window_active(transport: Any, team: str) -> bool:
    """True iff a W7 shadow window is armed — the `shadow-window.json` marker
    exists and carries a `started_at`. One cheap read; absent/unreadable/malformed
    ⇒ off, so the delivery probes stay silent outside the window (and default-off
    on any doubt). Checked only when there is something to probe.

    Lives here (not with a delivery path) because both surviving callers are the
    router's adapter legs: `_router_pass`'s cloud-adapter delivery and the
    host-local executor's. It was written alongside the `listen` tick's
    `path="listener"` probe; that verb retired with bus v3 and was removed, but
    the window gate itself is live and `router.SHADOW_EVIDENCE_PATHS` still
    carries `"listener"` so historical evidence stays readable."""
    raw = transport.read(router.router_prefix(team) + "shadow-window.json")
    if not raw:
        return False
    try:
        doc = json.loads(raw)
    except ValueError:
        return False
    # codex #470 P2: a PARSEABLE started_at, not mere truthiness — a malformed
    # marker ({"started_at":"bogus"}) is doubt ⇒ off, matching this fn's own
    # contract and `shadow status`.
    return isinstance(doc, dict) and router.parse_iso(doc.get("started_at")) is not None


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


def _router_state_prefix(
    args: argparse.Namespace,
) -> "tuple[Optional[str], Optional[str]]":
    """Resolve the router state-prefix override → ``(name, error)``.

    Precedence: the ``--state-prefix`` flag beats the ``COORD_ROUTER_STATE_PREFIX``
    env fallback (launchd-friendly); absent both ⇒ ``(None, None)`` = the
    canonical default (byte-identical to today). A bad-charset name from EITHER
    source returns ``(None, message)`` so the command can exit 2 — the flag and
    the env are validated the same way, and an invalid name can never compose a
    path (it never reaches `router.router_prefix`).

    An explicitly EMPTY value from either source is invalid, not canonical: a
    launch script expanding an unset variable (``--state-prefix "$P"``, or a
    plist setting the env to "") must fail rc 2 rather than silently run a
    shadow pass against the live ``router/`` cursor — the exact cursor-sharing
    wake-loss class this override exists to prevent. Canonical requires the
    flag ABSENT and the env UNSET."""
    name = getattr(args, "state_prefix", None)
    source = "--state-prefix"
    if name is None:
        name = os.environ.get("COORD_ROUTER_STATE_PREFIX")
        source = "COORD_ROUTER_STATE_PREFIX"
    if name is None:
        return None, None
    if not router.STATE_PREFIX_RE.match(name):
        return None, (f"{source} {name!r} is not a valid router state prefix "
                      f"(must match {router.STATE_PREFIX_RE.pattern})")
    return name, None


def _router_pass(args: argparse.Namespace, transport: Any) -> int:
    team = args.team
    state, state_err = _router_state_prefix(args)
    if state_err is not None:
        print(f"router: {state_err}", file=sys.stderr)
        return 2
    prefix = router.router_prefix(team, state=state)
    # config.json is SHARED enablement policy — always read from the canonical
    # prefix so a namespaced pass decides against the same config the live
    # router uses (the override moves only the router's OWN cursor-tracked state).
    config_prefix = router.router_prefix(team)
    task_prefix = f"team/{team}/task/"
    now = _now()

    cursor, cursor_reason = router.parse_cursor(transport.read(prefix + "cursor.json"))
    observe = cursor is None
    shadow = bool(getattr(args, "shadow", False))
    # Blocking (c): a SHADOW pass under a --state-prefix override reads the
    # delivered view from the CANONICAL prefix. The shadow plane never delivers,
    # so its own namespaced delivered/ is eternally empty; reading it would feed
    # empty last_delivered_at into decide() and inflate policy-divergent forever
    # (debounce/lapsed-checkin never see live delivery recency). The read is safe
    # by construction — shadow writes nothing to the canonical plane — and the
    # pass SKIPS the delivered.json refold (it maintains no view it will reuse).
    # Predicate is exactly (shadow AND override active); default and the
    # live-namespaced pair (run --state-prefix X, no shadow) keep today's
    # namespaced delivered/ behavior, where it is correct.
    shadow_override = shadow and state is not None
    delivered_prefix = config_prefix if shadow_override else prefix
    if observe:
        print(f"router: OBSERVE-ONLY pass — {cursor_reason}; decisions are "
              f"logged, nothing is enqueued, and a fresh cursor is written at "
              f"the end of this pass to arm the next one", file=sys.stderr)
    elif shadow:
        # W7 read-only shadow: cursor-tracked like a live pass (each directed
        # item decided ONCE, watermark + ledger advance), but a decision is
        # logged AND persisted per item and NOTHING is enqueued or executed.
        print("router: SHADOW pass — W7 read-only acceptance measurement; a "
              "decision is logged + persisted per directed item, nothing "
              "enqueued or executed", file=sys.stderr)

    raw_config = transport.read(config_prefix + "config.json")
    agents_cfg, _executors, cfg_errors = router.validate_config(raw_config)
    # Rate caps come from the SAME authoritative document as enablement, read
    # once so the two can never disagree about which config they saw.
    caps, caps_err = router.validate_caps(raw_config)
    if caps_err:
        print(f"router: rate cap {caps_err} — holding at the failsafe "
              f"{caps}", file=sys.stderr)
    measurement_failed = bool(shadow and cfg_errors)
    if "_config" in cfg_errors:
        print(f"router: {cfg_errors['_config']} — every agent reads "
              f"unconfigured (observe-only) until config.json is fixed",
              file=sys.stderr)
    agent_errors = {k: v for k, v in cfg_errors.items() if k != "_config"}
    for agent, problem in sorted(agent_errors.items()):
        print(f"router: config invalid for {agent}: {problem}", file=sys.stderr)

    # delivered view — decision-plane-owned fold over the delivery records
    delivered_shards: list[dict] = []
    delivered_listing_ok = True
    try:
        dl_entries = transport.list_dir(delivered_prefix + "delivered/")
    except TransportError as e:
        # codex #460 fix: a listing ERROR is NOT "genuinely empty". Fold no
        # shards this pass, but do NOT refold delivered.json (that would
        # overwrite the populated view with {} and feed empty last_delivered_at
        # into decide() for the whole pass). Skip the refold, stay fail-visible.
        dl_entries = []
        delivered_listing_ok = False
        measurement_failed = bool(shadow)
        print(f"router: delivered/ listing degraded ({e}) — skipping "
              f"delivered.json refold this pass (the populated view is "
              f"preserved; it regenerates next pass)", file=sys.stderr)
    # codex 554 r3: a per-shard failure is EVIDENCE LOSS. For the delivered.json
    # view it remains mere bookkeeping loss, but for the rate cap it means the
    # window count is measured from an incomplete set — and a count that looks
    # measured while part of the evidence was dropped is exactly the fail-open
    # this cap exists to prevent.
    shard_evidence_ok = True
    for e in dl_entries:
        name = e.get("name") or ""
        if e.get("is_dir") or not name.endswith(".json"):
            continue
        raw = transport.read(delivered_prefix + "delivered/" + name)
        if raw:
            try:
                delivered_shards.append(json.loads(raw))
            except ValueError:
                shard_evidence_ok = False
                pass  # a corrupt record shard is bookkeeping loss, not a stop
        else:
            # unreadable OR genuinely empty — the transport cannot tell us
            # which, so it counts as evidence we do not have
            shard_evidence_ok = False
    if delivered_listing_ok:
        delivered_view = router.fold_delivered(delivered_shards)
    else:
        # codex #466: the delivered/ shard listing failed. DECIDE from the
        # last-known-good persisted delivered.json so last_delivered_at is
        # honored per-agent (no early lapsed check-ins / weakened debounce).
        # A valid mapping — INCLUDING {} — is KNOWN history and proceeds. But if
        # delivered.json is ALSO unavailable (None), malformed, or not a
        # mapping, delivery history is UNKNOWN (not empty): FAIL THE PASS CLOSED
        # — do not enqueue or advance the cursor from unknown history (that
        # recreates the premature-checkin/weakened-debounce bug). Retries next
        # pass once either read recovers.
        delivered_view = None
        prior_raw = transport.read(delivered_prefix + "delivered.json")
        if prior_raw is not None:
            try:
                loaded = json.loads(prior_raw)
                if isinstance(loaded, dict):
                    delivered_view = loaded
            except ValueError:
                pass
        if delivered_view is None:
            print("router: delivered/ listing degraded AND delivered.json "
                  "unavailable/malformed — delivery history is UNKNOWN; failing "
                  "the pass closed (nothing enqueued, cursor not advanced; "
                  "retries next pass)", file=sys.stderr)
            return 1

    # Windowed wake counts for the rate cap, folded from the SAME durable
    # delivery shards as delivered_view. When the listing degraded we hold only
    # the persisted per-agent view, which carries no per-delivery timestamps —
    # so the last hour is UNKNOWN, not zero, and the cap fails closed per item.
    wake_counts, global_wakes, counts_complete = router.count_wakes_last_hour(
        delivered_shards, now=now)
    # Three independent ways the window can be unknown, and ALL of them must
    # clear the flag: the directory listing failed, an individual shard was
    # unreadable or malformed, or a shard parsed but could not be classified.
    # r3 set this from the listing alone, so per-file loss ran the cap on
    # incomplete evidence while reporting a confident number.
    counts_known = delivered_listing_ok and shard_evidence_ok and counts_complete
    if not counts_known:
        why = ("listing degraded" if not delivered_listing_ok
               else "a delivery shard was unreadable or malformed"
               if not shard_evidence_ok
               else "a delivery shard could not be classified")
        print(f"router: {why} — last-hour wake counts are UNKNOWN; the rate "
              f"cap fails CLOSED (items defer to the next window rather than "
              f"ride an unmeasurable cap)", file=sys.stderr)

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

    watermark_dt = router.parse_iso(cursor["watermark"]) if not observe else None
    processed = dict(cursor["processed"]) if not observe else {}
    max_seen = watermark_dt
    candidates: list = []

    # E3 (addendum §3.3): FEED-FIRST candidate source. The router's candidates
    # are `uploaded` events under task/ from the data-updates feed (server-
    # written, second-granular — subsumes the minute-granularity tie). The task-
    # directory LISTING stays as the fail-closed fallback, taken on ANY feed
    # doubt (feed unsupported/error, or cursor missing/corrupt so no window).
    # The cursor/ledger/decide seams are UNCHANGED: both sources yield (ts, name)
    # candidates into the same loop below; the inclusive `>= watermark` rescan +
    # processed ledger stay as defense in depth. When Fulcra webhooks ship, the
    # receiver replaces this feed poll and nothing downstream moves.
    feed = None if observe else _team_updates(
        transport, team, since=cursor["watermark"], now=router.iso(now))
    feed_usable = feed is not None
    if feed_usable:
        feed_candidates: list = []
        for change in feed:
            if change.get("state") != "uploaded":
                continue
            path = change.get("path") or ""
            if not path.startswith(task_prefix):
                continue
            name = path[len(task_prefix):]
            if ("/" in name or not name.endswith(".md")
                    or name in ("index.md", "log.md")):
                continue
            ts = router.parse_iso(change.get("uploaded_at"))
            if ts is None:
                # Addendum principle 2: malformed feed data is DOUBT, never a
                # skip. A relevant task upload with an unparseable timestamp,
                # silently dropped, would be LOST FOREVER — the watermark
                # advances via other candidates, the shard is never ledgered,
                # and a later listing pass excludes it (mtime < the advanced
                # watermark). Abandon the partial feed and take the full listing
                # fallback; the watermark does not advance from the doubtful feed.
                print("router: feed task event has an unparseable uploaded_at — "
                      "feed doubtful this pass; using the listing fallback (no "
                      "watermark advance from the partial feed)", file=sys.stderr)
                feed_usable = False
                break
            if watermark_dt is not None and ts < watermark_dt:
                continue
            feed_candidates.append((ts, name))
        if feed_usable:
            candidates = feed_candidates
    if not feed_usable:
        # fail-closed fallback: the full task-directory listing (the W4 source).
        try:
            entries = transport.list_dir(task_prefix)
        except TransportError as e:
            print(f"router: scan degraded (feed unavailable, task listing "
                  f"failed): {e}, retry next pass", file=sys.stderr)
            return 1
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
            caps=caps,
            agent_wakes_last_hour=wake_counts.get(assignee, 0),
            global_wakes_last_hour=global_wakes,
            counts_known=counts_known,
        )
        counts[decision] += 1
        suffix = ""
        if (observe or shadow) and decision in ("interrupt", "defer", "checkin"):
            suffix = (" [shadow: not enqueued]" if shadow
                      else " [observe-only: not enqueued]")
        detail_stream = sys.stderr if args.json else sys.stdout
        print(f"decision {assignee} {shard_id} -> {decision} ({reason}){suffix}",
              file=detail_stream)
        if decision == "unroutable":
            # fail-visible lane: never a silent drop
            print(f"router: wake unroutable for {assignee} — {reason}; item "
                  f"{shard_id} batches to the digest until config is fixed",
                  file=detail_stream)
        if shadow:
            # W7: persist the decision for EVERY directed item (the report's
            # comparison population) so it can be correlated with delivery-probe
            # evidence on the idempotency key. Enqueue/execute nothing. A failed
            # shard write is a measurement gap, logged, never fatal.
            if not transport.write(
                    prefix + router.SHADOW_DECISIONS_SUBPATH
                    + router.shadow_evidence_filename(assignee, key),
                    json.dumps(router.shadow_decision_record(
                        key=key, agent=assignee, decision=decision,
                        reason=reason, priority=priority,
                        decided_at=router.iso(now)), sort_keys=True) + "\n"):
                print(f"router: shadow decision write failed for {key} "
                      f"(measurement gap, non-fatal)", file=sys.stderr)
                measurement_failed = True
        elif not observe and decision in ("interrupt", "defer", "checkin"):
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

    if not observe and delivered_listing_ok and not shadow_override:
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

    # A duty sample certifies a COMPLETED shadow measurement pass, never merely
    # that the resident process woke. Persist the exact observed minute slot
    # only after all decision/population work and the cursor checkpoint landed.
    if shadow:
        if measurement_failed:
            pass_failed = True
        else:
            mark_path = (prefix + router.SHADOW_MARKS_SUBPATH
                         + router.shadow_mark_bucket(now))
            try:
                raw_mark = transport.read(mark_path)
                existing_mark = json.loads(raw_mark) if raw_mark else None
            except (TransportError, ValueError) as e:
                print(f"router: shadow mark {mark_path} is unreadable ({e}) — "
                      "pass measurement UNKNOWN; refusing to overwrite",
                      file=sys.stderr)
                pass_failed = True
            else:
                mark = router.shadow_mark_record(
                    existing_mark, at=router.iso(now))
                if not transport.write(
                        mark_path, json.dumps(mark, sort_keys=True) + "\n"):
                    print("router: shadow pass-mark write failed — pass "
                          "measurement UNKNOWN", file=sys.stderr)
                    pass_failed = True

    if args.json:
        result = {"observe_only": observe, "scanned": len(candidates),
                  "enqueued": enqueued, "decisions": counts,
                  "pass_failed": pass_failed}
        if getattr(args, "_defer_json", False):
            args._router_pass_result = result
        else:
            jsonutil.print_json(result)
        return 1 if pass_failed else 0
    summary = ", ".join(f"{d}={counts[d]}" for d in router.DECISIONS if counts[d])
    print(f"router pass: {len(candidates)} candidate(s), {enqueued} enqueued"
          + (f" — {summary}" if summary else "")
          + (" [observe-only]" if observe else "")
          + (" [PASS FAILED — retrying next pass]" if pass_failed else ""))
    return 1 if pass_failed else 0


def _default_adapter_invoke(inv: dict[str, Any]) -> "tuple[str, str]":
    """Default decision-plane adapter invoker → (status, detail), status one of
    "delivered" | "failed" | "unconfigured".

    This is the host-side integration SEAM. The real cloud-adapter clients plug
    in here: `managed-agents-message` needs a Managed Agents client + creds
    (fail-closed secrets — never in team paths); `routine-align`'s alignment
    mechanism is W6. Until a client is wired on THIS host, the default reports
    `unconfigured` — the wake stays VISIBLY QUEUED (never silently dropped, never
    a burned retry), which is the plan's fail-visible degradation mode. Tests
    inject a fake invoker to exercise the delivered/failed paths."""
    return ("unconfigured",
            f"no host-side client wired for adapter "
            f"{inv.get('adapter')!r} on this decision plane yet")


def _router_execute_cloud(args: argparse.Namespace, transport: Any,
                          invoke: Any = None, *, emit: bool = True) -> "dict[str, int]":
    """Drain the decision-plane-owned queue entries (cloud-reachable adapters):
    claim → invoke → delivered/dead-letter with BOUNDED cross-pass retry. Only
    `executor == decision-plane` entries are touched; host-local entries are
    left for the W5.5 thin executor. Delivery is at-least-once — safe by the
    adapter content rule (`router.adapter_invocation`)."""
    invoke = invoke or _default_adapter_invoke
    team = args.team
    state, state_err = _router_state_prefix(args)
    counts = {"delivered": 0, "dead_lettered": 0, "retried": 0,
              "unconfigured": 0, "skipped": 0, "deferred": 0}
    if state_err is not None:
        # defensive: the command entry (cmd_router_run) validates first, so this
        # is unreachable in normal flow — never compose a path from a bad name.
        print(f"router: {state_err}", file=sys.stderr)
        return counts
    prefix = router.router_prefix(team, state=state)
    # config.json (SHARED policy) and shadow-evidence/ (the LIVE delivery paths'
    # canonical correlation surface) stay at the canonical prefix; only the
    # router's own queue/delivered/dead-letter state moves under an override.
    canon_prefix = router.router_prefix(team)
    now = _now()

    try:
        q_entries = transport.list_dir(prefix + "queue/")
    except TransportError as e:
        print(f"router: queue listing degraded ({e}) — no execution this pass, "
              f"entries stay queued and retry next pass", file=sys.stderr)
        return counts

    agents_cfg, _executors, _errs = router.validate_config(
        transport.read(canon_prefix + "config.json"))

    for e in q_entries:
        name = e.get("name") or ""
        if e.get("is_dir") or not name.endswith(".json"):
            continue
        raw = transport.read(prefix + "queue/" + name)
        try:
            entry = json.loads(raw) if raw else None
        except ValueError:
            continue
        if not isinstance(entry, dict) or not router.is_decision_plane_entry(entry):
            continue  # host-local (or malformed) — W5.5's, never fired here
        # deferred wakes wait for their idle boundary
        nb = router.parse_iso(entry.get("not_before"))
        if nb is not None and nb > now:
            counts["deferred"] += 1
            continue
        # respect a fresh FOREIGN claim (another decision-plane process mid-flight)
        if router.claim_is_skippable(entry, router.DECISION_PLANE, now):
            counts["skipped"] += 1
            continue

        key = router.idempotency_key(str(entry.get("source_shard")),
                                     str(entry.get("agent")))
        cfg = agents_cfg.get(entry.get("agent")) or {}
        qpath = prefix + "queue/" + name

        def _terminate(sub: str, record: dict, tally: str) -> bool:
            """Write a delivered/dead-letter record, then remove the queue entry
            ONLY if that write landed. A failed record write must NOT be followed
            by the delete — else the wake is lost (same class as the W4 P1). Left
            queued, it retries next pass (at-least-once, safe by the content
            rule). Returns True iff the record landed and the entry was cleared."""
            if transport.write(prefix + sub + router.record_filename(key),
                               json.dumps(record, sort_keys=True) + "\n"):
                transport.delete(qpath)
                counts[tally] += 1
                return True
            print(f"router execute: {sub.rstrip('/')} record write failed "
                  f"for {key} — entry stays queued, retries next pass",
                  file=sys.stderr)
            counts["retried"] += 1
            return False

        try:
            inv = router.adapter_invocation(entry, cfg.get("adapter_args"))
        except ValueError as ve:
            # unroutable at execution — dead-letter immediately (never succeeds)
            _terminate("dead-letter/", router.dead_letter_record(
                entry, attempts=int(entry.get("attempts", 0)) + 1,
                last_error=f"unroutable: {ve}", gave_up_at=router.iso(now)),
                "dead_lettered")
            continue

        status, detail = invoke(inv)
        if status == "delivered":
            if _terminate("delivered/",
                          router.delivery_record(entry, router.iso(now)),
                          "delivered"):
                # W7 delivery probe (path=adapter): a GENUINE cloud-adapter
                # delivery (record landed). While a window is armed, record
                # evidence — guarded so a probe failure never affects execution.
                # Quiet during a shadow window (the shadow runner executes
                # nothing); meaningful when a live router runs during a window.
                try:
                    if _shadow_window_active(transport, team):
                        transport.write(
                            canon_prefix + router.SHADOW_EVIDENCE_SUBPATH
                            + router.shadow_evidence_filename(
                                str(entry.get("agent")), key),
                            json.dumps(router.shadow_evidence_record(
                                key=key, agent=str(entry.get("agent")),
                                delivered_at=router.iso(now), path="adapter"),
                                sort_keys=True) + "\n")
                except Exception as e:
                    print(f"router execute: W7 adapter probe skipped "
                          f"({type(e).__name__}: {e})", file=sys.stderr)
        elif status == "unconfigured":
            # visibly queued, not a burned retry — awaits a wired host client
            counts["unconfigured"] += 1
        else:  # "failed" — bounded cross-pass retry, then dead-letter
            attempts = int(entry.get("attempts", 0)) + 1
            if attempts >= router.MAX_DELIVERY_ATTEMPTS:
                _terminate("dead-letter/", router.dead_letter_record(
                    entry, attempts=attempts, last_error=detail,
                    gave_up_at=router.iso(now)), "dead_lettered")
            else:
                retried = router.claim_stamp(entry, router.DECISION_PLANE, now)
                retried["attempts"] = attempts
                retried["last_error"] = detail
                if not transport.write(qpath,
                                       json.dumps(retried, sort_keys=True) + "\n"):
                    print(f"router execute: retry-state write failed for {key} "
                          f"— entry unchanged, retries next pass", file=sys.stderr)
                counts["retried"] += 1

    active = {k: v for k, v in counts.items() if v}
    if active and emit:
        print("router execute: "
              + ", ".join(f"{k}={v}" for k, v in active.items()))
    return counts


def _run_fixed_rate(pass_fn: Any, *, label: str,
                    period_s: float = router.ROUTER_POLL_SECONDS,
                    clock: Any = None, sleeper: Any = None) -> None:
    """Run ``pass_fn`` on an anchored cadence without burst catch-up."""
    monotonic = clock or time.monotonic
    sleep = sleeper or time.sleep
    next_tick = monotonic()
    while True:
        pass_fn()
        next_tick += period_s
        now = monotonic()
        if now > next_tick:
            late_by = now - next_tick
            # Advance to the first anchor at or after ``now``.  ``floor + 1``
            # incorrectly skips an anchor when lateness is an exact multiple
            # of the period (for example, a 120s pass on a 60s cadence).
            skipped = math.ceil(late_by / period_s)
            next_tick += skipped * period_s
            print(f"{label}: cadence overrun by {late_by:.3f}s — skipped "
                  f"{skipped} tick(s), no burst catch-up", file=sys.stderr)
        sleep(max(0.0, next_tick - now))


def cmd_router_run(args: argparse.Namespace, transport: Any) -> int:
    # W7 shadow mode is READ-ONLY: log + persist decisions, never enqueue OR
    # execute (the pass suppresses the enqueue; the executor is skipped here).
    _, state_err = _router_state_prefix(args)
    if state_err is not None:
        print(f"router run: {state_err}", file=sys.stderr)
        return 2
    shadow = bool(getattr(args, "shadow", False))
    json_mode = bool(getattr(args, "json", False))
    if json_mode and not getattr(args, "once", False):
        print("router run: --json requires --once", file=sys.stderr)
        return 2
    if json_mode:
        args._defer_json = True
    def _pass() -> tuple[int, Optional[dict[str, int]]]:
        pass_rc = _router_pass(args, transport)
        counts = None
        if not shadow:
            counts = _router_execute_cloud(
                args, transport, emit=not json_mode)
        return pass_rc, counts

    if not getattr(args, "once", False):
        _run_fixed_rate(lambda: _pass(), label="router run")
        raise AssertionError("resident router loop returned")

    rc, execute_counts = _pass()
    if json_mode:
        jsonutil.print_json({
            "pass": getattr(args, "_router_pass_result", None),
            "execute": execute_counts,
        })
    return rc


def cmd_router_shadow_arm(args: argparse.Namespace, transport: Any) -> int:
    """Write the W7 shadow-window marker — records `started_at` and activates
    the fleet-wide delivery probes. Idempotent: a re-arm reports the existing
    start (never resets the clock) unless the marker is missing/malformed."""
    prefix = router.router_prefix(args.team)
    path = prefix + "shadow-window.json"
    existing = transport.read(path)
    if existing:
        try:
            doc = json.loads(existing)
            if isinstance(doc, dict) and doc.get("started_at"):
                print(f"shadow window already armed at {doc['started_at']} "
                      f"(min_hours={doc.get('min_hours')}) — not resetting the "
                      f"clock")
                return 0
        except ValueError:
            pass  # malformed marker -> re-arm cleanly
    mh = getattr(args, "min_hours", 48)
    min_hours = 48 if mh is None else int(mh)  # NOT `or 48` — that maps 0 -> 48
    # codex #470 P1: the normative window is >=48h — refuse to arm a shorter (or
    # negative) one that `shadow status` would then treat as the acceptance
    # minimum, letting the gate pass early.
    if min_hours < 48:
        print(f"shadow arm: --min-hours {min_hours} is below the normative "
              f"minimum of 48 — refusing to arm a window that could pass "
              f"acceptance early", file=sys.stderr)
        return 1
    started_at = router.iso(_now())
    doc = {"started_at": started_at, "min_hours": min_hours,
           "poll_seconds": router.ROUTER_POLL_SECONDS}
    if not transport.write(path, json.dumps(doc, sort_keys=True) + "\n"):
        print("shadow arm: marker write failed — window NOT armed", file=sys.stderr)
        return 1
    print(f"shadow window ARMED at {started_at} (>= {doc['min_hours']}h, poll "
          f"{doc['poll_seconds']}s). Delivery probes now record to "
          f"{prefix}{router.SHADOW_EVIDENCE_SUBPATH}; run `router run --shadow "
          f"{args.team}` for the read-only decision log.")
    return 0


def cmd_router_shadow_status(args: argparse.Namespace, transport: Any) -> int:
    """Report the shadow-window marker: armed?, start, and elapsed vs min_hours."""
    prefix = router.router_prefix(args.team)
    raw = transport.read(prefix + "shadow-window.json")
    if not raw:
        print("shadow window: NOT armed")
        return 0
    try:
        doc = json.loads(raw)
    except ValueError:
        print("shadow window: marker present but MALFORMED (re-arm to fix)",
              file=sys.stderr)
        return 1
    started = router.parse_iso(doc.get("started_at")) if isinstance(doc, dict) else None
    if started is None:
        print("shadow window: marker present but has no valid started_at",
              file=sys.stderr)
        return 1
    elapsed_h = (_now() - started).total_seconds() / 3600.0
    min_h = doc.get("min_hours", 48)
    done = "MET" if elapsed_h >= min_h else "not yet"
    print(f"shadow window ARMED at {doc['started_at']}: {elapsed_h:.1f}h "
          f"elapsed of >= {min_h}h ({done})")
    return 0


def cmd_router_shadow_report(args: argparse.Namespace, transport: Any) -> int:
    """Fold the armed W7 window into one fail-closed acceptance report.

    Under a ``--state-prefix`` override the router's own decision state
    (shadow-decisions/, shadow-marks/) is read from the sibling namespace, while
    the window marker and the delivery evidence stay CANONICAL: evidence is
    produced by the live delivery paths (the `listen` tick and cloud-adapter
    execution), which carry no override, so a namespaced shadow measurement
    correlates its own decisions against the fleet's canonical evidence."""
    state, state_err = _router_state_prefix(args)
    if state_err is not None:
        print(f"shadow report: {state_err}", file=sys.stderr)
        return 2
    prefix = router.router_prefix(args.team, state=state)      # own decision state
    canon_prefix = router.router_prefix(args.team)             # shared window + evidence
    unknown: list[str] = []

    def _rows(subpath: str, *, timestamp: Optional[str],
              required: tuple[str, ...],
              base: Optional[str] = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        root = base if base is not None else prefix
        try:
            entries = transport.list_dir(root + subpath)
        except TransportError as e:
            unknown.append(f"{subpath} listing unreadable: {e}")
            return rows
        for entry in entries:
            name = entry.get("name") or ""
            if entry.get("is_dir") or not name.endswith(".json"):
                continue
            try:
                raw = transport.read(root + subpath + name)
                doc = json.loads(raw) if raw else None
            except (TransportError, ValueError) as e:
                unknown.append(f"{subpath}{name} unreadable: {e}")
                continue
            if (isinstance(doc, dict)
                    and all(doc.get(field) is not None for field in required)
                    and (timestamp is None
                         or router.parse_iso(doc.get(timestamp)) is not None)):
                rows.append(doc)
            else:
                unknown.append(f"{subpath}{name} malformed required fields")
        return rows

    try:
        raw = transport.read(canon_prefix + "shadow-window.json")
        window = json.loads(raw) if raw else None
    except (TransportError, ValueError) as e:
        window = None
        unknown.append(f"shadow-window unreadable: {e}")
    start = window.get("started_at") if isinstance(window, dict) else None
    started = router.parse_iso(start)
    try:
        min_hours = int(window.get("min_hours", 48))
    except (AttributeError, TypeError, ValueError):
        min_hours = 0
    if started is None or min_hours < 48:
        unknown.append("shadow-window marker missing or invalid")

    decisions = _rows(
        router.SHADOW_DECISIONS_SUBPATH, timestamp="decided_at",
        required=("key", "agent", "decision", "decided_at"))
    evidence = _rows(
        router.SHADOW_EVIDENCE_SUBPATH, timestamp="delivered_at",
        required=("key", "agent", "path", "delivered_at"),
        base=canon_prefix)
    for row in decisions:
        if (not isinstance(row.get("key"), str)
                or not isinstance(row.get("agent"), str)
                or row.get("decision") not in router.DECISIONS):
            unknown.append("shadow-decisions contains malformed record")
    for row in evidence:
        if (not isinstance(row.get("key"), str)
                or not isinstance(row.get("agent"), str)
                or row.get("path") not in router.SHADOW_EVIDENCE_PATHS):
            unknown.append("shadow-evidence contains malformed record")
    mark_rows = _rows(
        router.SHADOW_MARKS_SUBPATH, timestamp=None,
        required=("bucket", "slots"))
    pass_marks: list[str] = []
    for mark in mark_rows:
        slots = mark.get("slots")
        if (not isinstance(mark.get("bucket"), str)
                or not isinstance(slots, list) or len(slots) > 60
                or any(not isinstance(slot, str)
                       or router.parse_iso(slot) is None for slot in slots)):
            unknown.append(f"{router.SHADOW_MARKS_SUBPATH}"
                           f"{mark.get('bucket')} has invalid observed slots")
            continue
        pass_marks.extend(slots)
    if not mark_rows:
        unknown.append("shadow-marks population missing")

    store_keys: set[str] = set()
    try:
        task_entries = transport.list_dir(f"team/{args.team}/task/")
    except TransportError as e:
        task_entries = []
        unknown.append(f"task population unreadable: {e}")
    for entry in task_entries:
        name = entry.get("name") or ""
        if entry.get("is_dir") or not name.endswith(".md"):
            continue
        try:
            fm = okf.parse_frontmatter(
                transport.read(f"team/{args.team}/task/{name}"))
        except TransportError as e:
            unknown.append(f"task/{name} unreadable: {e}")
            continue
        assignee = str((fm or {}).get("assignee") or "").strip()
        if assignee and assignee != "*":
            store_keys.add(router.idempotency_key(name[:-3], assignee))

    end = router.iso(_now())
    ended = router.parse_iso(end)
    feed_slugs: set[str] = set()
    updates_fn = getattr(transport, "updates", None)
    if started is not None and ended is not None and updates_fn is not None:
        seconds = max(0, int((ended - started).total_seconds()) + 120)
        try:
            try:
                changes = updates_fn(f"{seconds} seconds", team=args.team)
            except TypeError:
                changes = updates_fn(f"{seconds} seconds")
        except Exception:
            changes = None
        if not isinstance(changes, list):
            unknown.append("data-updates task population unreadable")
        else:
            task_prefix = f"team/{args.team}/task/"
            for change in changes:
                if not isinstance(change, dict):
                    unknown.append("data-updates contains malformed change")
                    continue
                path = change.get("path", change.get("full_name"))
                if (isinstance(path, str) and path.startswith(task_prefix)
                        and path.endswith(".md")):
                    lifecycle = [
                        router.parse_iso(change.get(field))
                        for field in ("uploaded_at", "archived_at", "deleted_at")
                    ]
                    lifecycle = [ts for ts in lifecycle if ts is not None]
                    if not lifecycle:
                        unknown.append(
                            f"data-updates task change for {path} has no "
                            "valid lifecycle timestamp")
                    elif any(started <= ts <= ended for ts in lifecycle):
                        feed_slugs.add(path[len(task_prefix):-3])
    else:
        unknown.append("data-updates task population unavailable")

    # A deleted shard cannot supply its assignee at report time. Decisions do:
    # union keys whose source slug appeared in the append-only feed window.
    for decision in decisions:
        key, agent = decision.get("key"), decision.get("agent")
        suffix = f":{agent}"
        if isinstance(key, str) and key.endswith(suffix):
            source = key[:-len(suffix)]
            if source in feed_slugs:
                store_keys.add(key)

    if unknown:
        report: dict[str, Any] = {
            "pass": False, "verdict": "UNKNOWN", "unknown": unknown,
            "counts": {}, "window": {"start": start, "end": end},
        }
        rc = 1
    else:
        report = router.shadow_report(
            decisions, evidence, store_keys=store_keys,
            pass_marks=pass_marks,
            window_start=start, window_end=end)
        elapsed_hours = ((ended - started).total_seconds() / 3600.0
                         if ended is not None and started is not None else 0.0)
        report["duration"] = {
            "elapsed_hours": round(elapsed_hours, 4),
            "min_hours": min_hours,
        }
        report["gates"]["window_duration"] = elapsed_hours >= min_hours
        report["pass"] = all(report["gates"].values())
        report["verdict"] = "PASS" if report["pass"] else "FAIL"
        rc = 0 if report["pass"] else 1
    if getattr(args, "json", False):
        jsonutil.print_json(report)
    else:
        counts = ", ".join(f"{k}={v}" for k, v in sorted(
            report.get("counts", {}).items())) or "none"
        print(f"shadow report: {report['verdict']} — {counts}")
        for reason in report.get("unknown", []):
            print(f"  UNKNOWN: {reason}", file=sys.stderr)
    return rc


# --- W5.5: thin host executor -----------------------------------------------
#
# The SOLE executor for host-local adapters (plan §W5/W5.5). Policy-free and
# config-authority-free: it makes NO wake decisions (W4's `router run` did that
# and stamped `executor`/`adapter` into the queue entry — the trusted routing
# source) and re-runs NO policy. It drains exactly the queue entries whose
# `executor` matches its own host id, fires the sanctioned host-local adapter
# through the one invoker seam, and records the outcome. A flaky desktop delays
# only its own adapters' wakes — visibly, in the durable queue (plan §2.5).
# Delivery is at-least-once and safe by `router.adapter_invocation`'s keyed-nudge
# content rule; deployment (wiring the real adapter scripts, scheduling the
# poller on a host) is a separate Ash-gated step — this command wakes nothing.

def _default_host_adapter_invoke(inv: dict[str, Any]) -> "tuple[str, str]":
    """Default host-local adapter invoker → (status, detail), status one of
    "delivered" | "failed" | "unconfigured".

    The host-side integration SEAM. A sanctioned host-local adapter is a small
    host-provisioned SCRIPT at `$COORD_WAKE_ADAPTER_DIR/<adapter>.sh`; the first
    one in-tree is `macos-notify` (posts a desktop notification and spawns
    nothing). The rest of `router.ADAPTERS_HOST_LOCAL` still reports
    `unconfigured` until its script lands (W6).

    Three properties hold at this seam (see `wake_adapters.run_script_adapter`):
    NUDGE-ONLY — the adapter receives the agent id, the idempotency key and a
    STATIC reason constant, never a per-event command/URL/payload, so
    at-least-once delivery converges to one bus check; BOUNDED — the script runs
    under a hard timeout with its whole process group killed at the bound, so a
    hung adapter cannot wedge the executor (reported `failed`); FAIL-VISIBLE —
    an un-provisioned host (env unset, or no script) reports `unconfigured` and
    the wake stays VISIBLY QUEUED, never dropped and never a burned retry.

    `COORD_WAKE_ADAPTER_DIR` is unset by default, so an engine that has merely
    been installed wakes nothing; tests inject a fake invoker or a stub script.
    """
    return wake_adapters.run_script_adapter(inv)


def _claim_owner(executor_id: str) -> str:
    """This process's claim identity: the host executor id plus its pid.

    SELECTION and delivered/dead-letter OWNERSHIP stay keyed to the bare host
    executor id (`entry.executor`); the claim additionally names the CLAIMING
    PROCESS. That distinction is what lets `claim_is_skippable` bound duplicates
    across two processes sharing one host id: a sibling process has a different
    owner token, so it reads a fresh claim as FOREIGN and skips (the local
    pidfile is the primary single-process guard — this is defense-in-depth). The
    token is STABLE across a resident process's passes (pid is constant), so the
    process's own at-least-once retry is never blocked by its own fresh claim."""
    return f"{executor_id}#pid{os.getpid()}"


#: Bounded fan-out for queue-entry prefetch. Small on purpose: enough to hide
#: per-request latency, low enough not to hammer the store or burn file handles.
QUEUE_PREFETCH_WORKERS = 8


def _read_queue_entries(transport: Any, prefix: str,
                        q_entries: "list[dict]") -> "dict[str, Optional[str]]":
    """Prefetch every candidate queue entry body CONCURRENTLY.

    Why: the executor read each entry SERIALLY, one network round trip apiece,
    on every pass — including the entries belonging to other executors, which it
    then skipped. Measured 2026-08-05 against the live store: 112 queue entries
    at ~0.7-0.96s per read = 78-107s for a pass on a 60s cadence, which is
    exactly the `cadence overrun by ~22s — skipped 1 tick(s)` the VPS executor
    logged continuously. The cost scaled with FLEET-WIDE queue depth, not with
    this host's share of it, so a busy fleet silently halved every executor's
    delivery rate.

    These reads are independent and side-effect-free, so only the READ phase is
    parallel; claim/invoke/write stays strictly serial and in listing order, and
    the per-entry semantics are unchanged — a read returning None still means
    UNKNOWN and is skipped by the caller, never executed on.
    """
    paths = [prefix + "queue/" + (e.get("name") or "")
             for e in q_entries
             if not e.get("is_dir") and (e.get("name") or "").endswith(".json")]
    if not paths:
        return {}
    out: "dict[str, Optional[str]]" = {}
    first_error: "list[BaseException]" = []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(QUEUE_PREFETCH_WORKERS, len(paths))) as pool:
        futures = {pool.submit(transport.read, p): p for p in paths}
        for fut in concurrent.futures.as_completed(futures):
            path = futures[fut]
            try:
                out[path] = fut.result()
            except BaseException as e:            # noqa: BLE001
                # Preserve serial behaviour: a raising read is not swallowed
                # into a false "nothing here". Record it and re-raise after the
                # pool drains so the caller's degraded path still fires.
                out[path] = None
                if not first_error:
                    first_error.append(e)
    if first_error:
        raise first_error[0]
    return out


def _router_execute_host(args: argparse.Namespace, transport: Any,
                         invoke: Any = None,
                         claim_owner: Optional[str] = None, *,
                         emit: bool = True) -> "dict[str, int]":
    """Drain the queue entries resolved to THIS host's executor id: select →
    idempotency-skip → skip-fresh-foreign-claim → CLAIM → invoke →
    delivered/dead-letter with BOUNDED cross-pass retry. Only
    `executor == <this host id>` entries are touched; everything else (other
    hosts, the decision plane, malformed) is left untouched. Policy-free: an
    entry present in the queue executes regardless of priority — W4 already
    decided.

    Claim-then-invoke (plan §2 duplicate bound): the advisory claim is PERSISTED
    to the queue entry BEFORE the adapter is invoked, so the side-effect window
    is always claimed and the claim-holder owns the delivered/dead-letter
    transition. If the claim write does not land, the adapter is NOT invoked and
    the entry stays visibly queued for the next tick (`claim_unpersisted`,
    loud) — never a wake without a persisted claim. The claim is advisory, not a
    lock: a stale own/foreign claim stays retryable, so nothing wedges.

    Read-contract (plan §2, and the bug that hit W1–W3): a queue OR delivered/
    listing that RAISES is UNKNOWN-degraded — reported loudly, `degraded` set, no
    execution this pass, wakes stay VISIBLY queued (a dead/blind executor never
    reports a clean "0 delivered"). A per-entry read that is None/unparseable is
    SKIPPED (never invoke an adapter on an UNKNOWN entry — a failed read must
    cause no wake and no delivery-record). The delivered/ existence check that
    drives idempotency confirms via the RAISING `list_dir`, never a falsy read."""
    invoke = invoke or _default_host_adapter_invoke
    executor_id = getattr(args, "host", None) or _host()
    claim_owner = claim_owner or _claim_owner(executor_id)
    dry_run = getattr(args, "dry_run", False)
    counts = {"delivered": 0, "dead_lettered": 0, "retried": 0,
              "unconfigured": 0, "skipped": 0, "deferred": 0,
              "already_delivered": 0, "would_execute": 0,
              "claim_unpersisted": 0, "degraded": 0}
    state, state_err = _router_state_prefix(args)
    if state_err is not None:
        # defensive: cmd_router_execute validates first (clean rc 2).
        print(f"router execute [{executor_id}]: {state_err}", file=sys.stderr)
        counts["degraded"] = 1
        return counts
    prefix = router.router_prefix(args.team, state=state)
    # config.json (SHARED) is read for the host-resolved adapter_args target only
    # — always canonical; the queue/delivered state moves under an override.
    canon_prefix = router.router_prefix(args.team)
    now = _now()

    try:
        q_entries = transport.list_dir(prefix + "queue/")
    except TransportError as e:
        print(f"router execute [{executor_id}]: queue listing degraded ({e}) — "
              f"UNKNOWN, no execution this pass; wakes stay visibly queued (a "
              f"dead executor leaves wakes queued, never a silent 0 delivered)",
              file=sys.stderr)
        counts["degraded"] = 1
        return counts

    # Idempotency source: the delivered/ record set. Existence is confirmed via
    # the RAISING list_dir — a listing that raises is degraded (cannot rule out a
    # re-fire we shouldn't make), never read as "nothing delivered yet".
    try:
        delivered_names = {e.get("name")
                           for e in transport.list_dir(prefix + "delivered/")
                           if not e.get("is_dir")}
    except TransportError as e:
        print(f"router execute [{executor_id}]: delivered/ listing degraded "
              f"({e}) — cannot confirm idempotency, no execution this pass; "
              f"wakes stay visibly queued", file=sys.stderr)
        counts["degraded"] = 1
        return counts

    # config.json is read ONLY for the host-resolved adapter_args routing target
    # (thread_id/endpoint_name) — not for policy. No config authority here.
    agents_cfg, _executors, _errs = router.validate_config(
        transport.read(canon_prefix + "config.json"))

    # Prefetch bodies concurrently; the pass then walks them in listing order.
    # Serial reads made pass time scale with FLEET-WIDE queue depth and pushed
    # the resident executor past its own cadence (see _read_queue_entries).
    try:
        bodies = _read_queue_entries(transport, prefix, q_entries)
    except TransportError as e:
        print(f"router execute [{executor_id}]: queue entry prefetch degraded "
              f"({e}) — UNKNOWN, no execution this pass; wakes stay visibly "
              f"queued", file=sys.stderr)
        counts["degraded"] = 1
        return counts

    for e in q_entries:
        name = e.get("name") or ""
        if e.get("is_dir") or not name.endswith(".json"):
            continue
        raw = bodies.get(prefix + "queue/" + name)
        try:
            entry = json.loads(raw) if raw else None
        except ValueError:
            entry = None
        if not isinstance(entry, dict):
            # read None / unparseable → UNKNOWN entry: SKIP, never invoke on it
            counts["skipped"] += 1
            continue
        if entry.get("executor") != executor_id:
            continue  # another host's, the decision plane's, or malformed routing
        # deferred wakes wait for their idle boundary (W4 stamped not_before)
        nb = router.parse_iso(entry.get("not_before"))
        if nb is not None and nb > now:
            counts["deferred"] += 1
            continue

        key = router.idempotency_key(str(entry.get("source_shard")),
                                     str(entry.get("agent")))
        qpath = prefix + "queue/" + name

        # step 2: a delivery-record already exists for this key → SKIP, never
        # re-invoke (existence confirmed via the RAISING list_dir above).
        if router.record_filename(key) in delivered_names:
            counts["already_delivered"] += 1
            continue

        # respect a FRESH FOREIGN claim (a DIFFERENT process mid-flight — the
        # owner token names the process, not just the host id). Our own claim and
        # a STALE foreign claim are retryable — at-least-once again, safe by the
        # content rule; a stale claim never wedges an entry forever.
        if router.claim_is_skippable(entry, claim_owner, now):
            counts["skipped"] += 1
            continue

        if dry_run:
            counts["would_execute"] += 1        # select + report only
            continue

        cfg = agents_cfg.get(entry.get("agent")) or {}

        def _terminate(sub: str, record: dict, tally: str) -> bool:
            """Write a delivered/dead-letter record, then remove the queue entry
            ONLY if that write landed. A failed record write must NOT be followed
            by the delete — else the wake is lost. Left queued, it retries next
            pass (at-least-once, safe by the content rule). Returns True iff the
            record landed and the entry was cleared."""
            if transport.write(prefix + sub + router.record_filename(key),
                               json.dumps(record, sort_keys=True) + "\n"):
                transport.delete(qpath)
                counts[tally] += 1
                return True
            print(f"router execute [{executor_id}]: {sub.rstrip('/')} "
                  f"record write failed for {key} — entry stays queued, "
                  f"retries next pass", file=sys.stderr)
            counts["retried"] += 1
            return False

        # only the four host-local adapters are executable on a host executor; a
        # cloud adapter mis-resolved here (or anything unknown) is a per-entry
        # error → dead-letter, never a silent skip or mis-fire.
        adapter = entry.get("adapter")
        if adapter not in router.ADAPTERS_HOST_LOCAL:
            _terminate("dead-letter/", router.dead_letter_record(
                entry, attempts=int(entry.get("attempts", 0)) + 1,
                last_error=(f"adapter {adapter!r} is not a host-local adapter "
                            f"({sorted(router.ADAPTERS_HOST_LOCAL)}) — not "
                            f"executable on this host executor"),
                gave_up_at=router.iso(now)), "dead_lettered")
            continue

        try:
            inv = router.adapter_invocation(entry, cfg.get("adapter_args"))
        except ValueError as ve:
            _terminate("dead-letter/", router.dead_letter_record(
                entry, attempts=int(entry.get("attempts", 0)) + 1,
                last_error=f"unroutable: {ve}", gave_up_at=router.iso(now)),
                "dead_lettered")
            continue

        # CLAIM-THEN-INVOKE: persist this process's claim to the queue entry
        # BEFORE the side effect, so the fire window is never unclaimed and a
        # concurrent sibling reads the fresh claim and skips. If the claim does
        # NOT land (write False or raises), do NOT invoke — the entry stays
        # visibly queued for the next tick. No wake without a persisted claim.
        entry = router.claim_stamp(entry, claim_owner, now)
        try:
            claim_ok = transport.write(
                qpath, json.dumps(entry, sort_keys=True) + "\n")
        except TransportError:
            claim_ok = False
        if not claim_ok:
            print(f"router execute [{executor_id}]: claim write failed for "
                  f"{key} — adapter NOT invoked, entry stays visibly queued, "
                  f"retries next pass", file=sys.stderr)
            counts["claim_unpersisted"] += 1
            continue

        status, detail = invoke(inv)
        if status == "delivered":
            if _terminate("delivered/",
                          router.delivery_record(entry, router.iso(now)),
                          "delivered"):
                # W7 delivery probe (path=adapter): a GENUINE host-local adapter
                # delivery (record landed). Host-local adapters are a real
                # delivery plane — their deliveries must enter the W7 evidence
                # population (codex #470 r2). Window-gated + guarded: a probe
                # failure never affects execution.
                try:
                    if _shadow_window_active(transport, args.team):
                        # W7 evidence is CANONICAL by design (like the cloud
                        # executor): only cursor/queue/delivered/dead-letter/marks
                        # move under a --state-prefix override. Writing evidence to
                        # the namespaced sibling would hide it from the canonical
                        # shadow report → the delivery reads no-probe-evidence/missed.
                        transport.write(
                            canon_prefix + router.SHADOW_EVIDENCE_SUBPATH
                            + router.shadow_evidence_filename(
                                str(entry.get("agent")), key),
                            json.dumps(router.shadow_evidence_record(
                                key=key, agent=str(entry.get("agent")),
                                delivered_at=router.iso(now), path="adapter"),
                                sort_keys=True) + "\n")
                except Exception as e:
                    print(f"router execute [{executor_id}]: W7 adapter probe "
                          f"skipped ({type(e).__name__}: {e})", file=sys.stderr)
        elif status == "unconfigured":
            # visibly queued, not a burned retry — awaits a wired host script
            counts["unconfigured"] += 1
        else:  # "failed" — bounded cross-pass retry, then dead-letter
            attempts = int(entry.get("attempts", 0)) + 1
            if attempts >= router.MAX_DELIVERY_ATTEMPTS:
                _terminate("dead-letter/", router.dead_letter_record(
                    entry, attempts=attempts, last_error=detail,
                    gave_up_at=router.iso(now)), "dead_lettered")
            else:
                # Build on the already-claimed entry (refresh claimed_at, same
                # owner) and add the retry audit — one consistent claim, no
                # conflicting stamp. Advisory: it only suppresses a concurrent
                # fresh foreign fire; correctness rests on the content rule.
                retried = router.claim_stamp(entry, claim_owner, now)
                retried["attempts"] = attempts
                retried["last_error"] = detail
                if not transport.write(qpath,
                                       json.dumps(retried, sort_keys=True) + "\n"):
                    print(f"router execute [{executor_id}]: retry-state write "
                          f"failed for {key} — entry unchanged, retries next "
                          f"pass", file=sys.stderr)
                counts["retried"] += 1

    active = {k: v for k, v in counts.items() if v}
    if active and emit:
        print(f"router execute [{executor_id}]: "
              + ", ".join(f"{k}={v}" for k, v in active.items())
              + (" [dry-run]" if dry_run else ""))
    return counts


def cmd_router_execute(args: argparse.Namespace, transport: Any) -> int:
    _, state_err = _router_state_prefix(args)
    if state_err is not None:
        print(f"router execute: {state_err}", file=sys.stderr)
        return 2
    json_mode = bool(getattr(args, "json", False))
    if json_mode and not (
            getattr(args, "once", False) or getattr(args, "dry_run", False)):
        print("router execute: --json requires --once or --dry-run",
              file=sys.stderr)
        return 2
    def _pass() -> dict[str, int]:
        return _router_execute_host(args, transport, emit=not json_mode)

    if not (getattr(args, "once", False)
            or getattr(args, "dry_run", False)):
        _run_fixed_rate(_pass, label="router execute")
        raise AssertionError("resident router executor loop returned")

    counts = _pass()
    if json_mode:
        jsonutil.print_json(counts)
    return 1 if counts.get("degraded") else 0


# --- stash (fulcra-agent-durable-state) ---

def _stash_prefix(team: str, agent: str) -> str:
    # Raw agent id, not agent_key: the stash path is a documented convention
    # (SKILL + pre-existing stashes) that agents also address with plain
    # `fulcra-api file` commands, so the engine must not remap it.
    return f"team/{team}/_coord/agents/{agent}/stash/"


def cmd_stash_push(args: argparse.Namespace, transport: Any) -> int:
    agent = _identity(args.agent)
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
    agent = _identity(args.agent)
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
    agent = _identity(args.agent)
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
    unknown_snapshot_agents: list[str] = []
    # DELIBERATELY UNMEASURED (coord-boss ruling, 2026-08-09). This audit's
    # product is CHECKPOINT staleness, not activity: "this agent is working but
    # not snapshotting" is exactly the finding it exists to make, and folding
    # work evidence in here would mask it. `presence show` and `briefing` do
    # measure; this asymmetry is chosen, not an omission.
    for r in presence.roster(_presence_shards(transport, args.team), now=_iso(now_dt)):
        pts = roles._parse(r.get("last_seen"))
        if pts is None:
            continue
        pres_rows.append({"agent": r["agent"], "ts": pts})
        # ONE read per agent via the LATEST pointer. The previous shape read
        # EVERY snapshot document of every agent to find the newest, because a
        # directory listing carries no mtime (upstream register U8) — measured
        # at 203 reads / ~149s for three agents, which is why this verb was
        # killed at 240s and again at 590s on a live store.
        raw = transport.read(_continuity_latest_path(args.team, r["agent"]))
        sts = None
        if raw is not None:
            try:
                sts = continuity._parse_created_at(json.loads(raw).get("created_at"))
            except (ValueError, TypeError, AttributeError):
                sts = None
        if sts is not None:
            snap_rows.append({"agent": r["agent"], "ts": sts})
            continue
        # No usable pointer. Two very different cases hide here, and collapsing
        # them is how this audit would start lying:
        #
        #   - the agent has NEVER checkpointed -> a real finding, and the one
        #     this audit exists for;
        #   - the agent HAS snapshots this build cannot date (pre-pointer
        #     history, or a pointer write that failed) -> UNKNOWN, and flagging
        #     it would manufacture a false accusation against someone who is
        #     working.
        #
        # ONE listing separates them, with no reads. It is paid only for agents
        # without a pointer, so the common path stays one read per agent.
        try:
            has_any = any(e.get("is_dir") for e in
                          transport.list_dir(_continuity_prefix(args.team, r["agent"])))
        except TransportError:
            has_any = True   # cannot prove absence -> UNKNOWN, never a finding
        if has_any:
            unknown_snapshot_agents.append(r["agent"])
            pres_rows.pop()  # undatable: it must not reach the staleness fold

    flagged_agents = continuity_audit.stale_agents(pres_rows, snap_rows, now=now_dt)
    # Coverage, stated. "No stale agents" and "we could not tell for N of them"
    # must not look the same — that equivalence is behind most of the incidents
    # this layer has logged.
    if unknown_snapshot_agents:
        view["continuity_unknown"] = sorted(unknown_snapshot_agents)
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


def writer_present() -> bool:
    """Is the ``fulcra_common`` annotation writer importable RIGHT HERE?

    Deliberately imported in the running interpreter rather than probed from
    outside. ``uv tool install`` gives the engine its own venv, so a
    system-python import proves nothing in either direction — and when doctor
    itself is the engine entry point, "here" is exactly the environment the
    annotate/digest legs will run in.
    """
    try:
        import fulcra_common  # noqa: F401
    except Exception:
        return False
    return True


def _report_writer_presence() -> None:
    """One doctor line for writer presence. WARN, never unhealthy.

    A bare ``uv tool install coord-engine`` silently drops ``fulcra_common``,
    and the legs that need it — ``annotate project``, ``digest
    --emit-timeline`` — swallow the ImportError and exit 0 (see
    commands_annotate._emit, whose contract is "returns False, never an
    exception"). That combination is why the task digest went dark on
    2026-08-04 with nothing failing anywhere.

    Absence is a WARN and not a failure ON PURPOSE: most hosts never run those
    legs, and flipping doctor to unhealthy fleet-wide for a capability they do
    not use would train agents to ignore the exit code — which costs more than
    it buys. The line carries the CONSEQUENCE, not just the state, so a reader
    who does run those legs knows immediately what it means for them.
    """
    if writer_present():
        print("  ✓ fulcra_common writer present (annotate/digest legs can emit)")
    else:
        print("  ! fulcra_common writer MISSING — annotate/digest legs will "
              "SILENTLY NO-OP (they exit 0 without it). Re-run the store "
              "adopt-latest.sh, or install with "
              "--with 'git+…#subdirectory=packages/fulcra-common'")


def _report_pin_currency(transport: Any, team: Optional[str]) -> None:
    """One doctor line for engine-vs-fleet-pin currency. WARN, never unhealthy.

    See :mod:`coord_engine.pin_currency` for why this exists and why an
    unprovable answer must print as a warning rather than a tick. Wrapped
    defensively: a preflight that CRASHES teaches even less than one that lied,
    and no currency question is worth taking doctor down over.
    """
    try:
        print(pin_currency.report(transport, team))
    except Exception as e:  # pragma: no cover - defense in depth
        print(f"  ! fleet pin currency check failed to run "
              f"({type(e).__name__}) — currency unknown")


_UNSET = object()
_REMOTE_TIPS: Any = _UNSET


def _remote_ref_tips() -> "Optional[set]":
    """SHAs advertised by ``git ls-remote origin``, or None if it could not run.

    Cached for the process: ONE network call, not one per register entry.
    GitHub advertises ``refs/pull/*/head`` here — 553 of 863 refs on this repo —
    which is precisely why a PR head missing from a local clone is still
    visible at the source.
    """
    global _REMOTE_TIPS
    if _REMOTE_TIPS is not _UNSET:
        return _REMOTE_TIPS
    import shutil as _shutil
    import subprocess as _subprocess
    git = _shutil.which("git")
    root = handoff.repo_root()
    tips = None
    if git and root is not None:
        try:
            cp = _subprocess.run([git, "ls-remote", "origin"], cwd=str(root),
                                 capture_output=True, text=True, timeout=120)
            if cp.returncode == 0:
                tips = {ln.split()[0] for ln in cp.stdout.splitlines() if ln.strip()}
        except Exception:
            tips = None
    _REMOTE_TIPS = tips
    return tips


def _forge_head_exists(sha: str) -> "Optional[bool]":
    """Authoritative existence check via the forge API, or None if unavailable.

    The ONLY source permitted to answer False, because it is the only one that
    distinguishes "does not exist" from "I cannot see it".
    """
    import re as _re
    import shutil as _shutil
    import subprocess as _subprocess
    gh = _shutil.which("gh")
    root = handoff.repo_root()
    if not gh or root is None:
        return None
    try:
        url = _subprocess.run(["git", "remote", "get-url", "origin"],
                              cwd=str(root), capture_output=True, text=True,
                              timeout=30)
        if url.returncode != 0:
            return None
        # THE ORIGIN MUST BE GITHUB. An earlier round parsed owner/repo out of
        # ANY host and then asked github.com about it — so a self-hosted or
        # GitLab origin got answered by a same-named GitHub repo, or by a 404
        # meaning "no such repo here" that read as "commit is gone"
        # (codex-reviewer, round 1). A wrong authority is worse than no answer.
        raw = (url.stdout or "").strip()
        m = _re.match(
            r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
            r"([^/]+)/([^/]+?)(?:\.git)?$", raw)
        if not m:
            return None
        cp = _subprocess.run(
            [gh, "api", f"repos/{m.group(1)}/{m.group(2)}/commits/{sha}",
             "--jq", ".sha"], cwd=str(root), capture_output=True, text=True,
            timeout=60)
    except Exception:
        return None
    if cp.returncode == 0 and (cp.stdout or "").strip():
        return True
    # ABSENCE IS EXACTLY ONE RESPONSE. Measured against the live API:
    #   missing commit      -> 422 "No commit found for SHA: <sha>"
    #   inaccessible repo   -> 404 "Not Found"
    # A bare status match would read the second as absence, which is this whole
    # P0 one layer down: inability to SEE becoming proof of ABSENCE. So match
    # the commit endpoint's own no-commit message and nothing else. Auth
    # failure, rate limit, network error, 404: all UNKNOWN.
    err = (cp.stderr or "").lower()
    if "no commit found for sha" in err:
        return False
    return None


def _fetch_probe_head_exists(sha: str) -> "Optional[bool]":
    """Ask the remote whether it can serve this object. PRESENCE ONLY.

    Forge-agnostic, and measured against GitHub:

        full sha, alive       -> rc 0                       -> True
        full sha, fabricated  -> "upload-pack: not our ref"
        ABBREVIATED sha       -> "couldn't find remote ref"  -- ALWAYS, either way

    TWO THINGS THIS DELIBERATELY DOES NOT DO, both from coord-boss on round 3:

    1. **It never answers False.** "not our ref" is only absence if ``origin``
       is the CANONICAL repository. In a fork checkout origin is the fork, and
       an upstream ``refs/pull`` head returns exactly that string while being
       perfectly alive. Since this layer runs FIRST, a False here would
       short-circuit the origin-verified forge path that would have answered
       correctly -- the same wrong-authority shape round 2 existed to fix,
       reintroduced one layer above it. So presence is the whole contribution;
       absence stays with the authority that proves which repo it is asking.

    2. **It runs in a THROWAWAY repository**, because ``--dry-run`` DOES write
       to the object store. Verified: fetching a reachable non-tip into a
       ``--depth 1`` clone with ``--dry-run`` left the object present and took
       .git from 4.0M to 12M. Probing in the working repo would therefore
       mutate it, pay a pack per unknown head, and make the classifier
       HISTORY-DEPENDENT -- a second run answering True locally from what the
       first downloaded. An earlier docstring here claimed the opposite, which
       is worse than saying nothing: the next person would have trusted it.
    """
    if len(sha or "") != 40:
        return None
    import shutil as _shutil
    import subprocess as _subprocess
    import tempfile as _tempfile
    git = _shutil.which("git")
    root = handoff.repo_root()
    if not git or root is None:
        return None
    try:
        url = _subprocess.run([git, "remote", "get-url", "origin"], cwd=str(root),
                              capture_output=True, text=True, timeout=30)
        if url.returncode != 0 or not (url.stdout or "").strip():
            return None
        origin = url.stdout.strip()
        with _tempfile.TemporaryDirectory() as td:
            for args in (["init", "-q"], ["remote", "add", "origin", origin]):
                if _subprocess.run([git, *args], cwd=td, capture_output=True,
                                   timeout=30).returncode != 0:
                    return None
            cp = _subprocess.run(
                [git, "fetch", "--dry-run", "--depth=1", "origin", sha],
                cwd=td, capture_output=True, text=True, timeout=120)
    except Exception:
        return None
    return True if cp.returncode == 0 else None


def _remote_head_exists(sha: str) -> "Optional[bool]":
    """Does this sha exist AT THE SOURCE? True / False / None(unknown).

    Layered, and the layering is MEASURED rather than assumed:

    * an advertised ref tip proves PRESENCE — cheap, one cached call;
    * a MISS proves nothing. ``ls-remote`` sees ref TIPS only, and 2 of the 6
      live heads in this register are reachable-but-not-tips; treating a miss
      as absence would still have destroyed them;
    * so a miss falls through to the forge API, the only authoritative answer;
    * and when that cannot speak, the answer is None.
    """
    tips = _remote_ref_tips()
    if tips and any(t.startswith(sha) or sha.startswith(t) for t in tips):
        return True
    # A tips MISS proves nothing (ref tips only). Ask the remote directly --
    # forge-agnostic, so a GitLab or self-hosted origin gets a real answer
    # instead of the None the GitHub-only path is obliged to return.
    fetched = _fetch_probe_head_exists(sha)
    if fetched is not None:
        return fetched
    return _forge_head_exists(sha)


def _git_head_probe() -> "Callable[[str], Optional[bool]]":
    """``sha -> True | False | None`` via ``git cat-file -e``.

    None whenever the answer is not TRUSTWORTHY: no git, no repository, a
    non-sha argument, or any error. Only ``git`` speaking clearly about an
    object it looked for produces a bool — the whole gc contract rests on this
    function refusing to guess.
    """
    import re as _re
    import shutil as _shutil
    import subprocess as _subprocess

    sha_re = _re.compile(r"^[0-9a-fA-F]{7,64}$")
    git = _shutil.which("git")
    root = handoff.repo_root()

    def _git_out(*args: str) -> Optional[str]:
        """stdout of a successful git call, else None."""
        if not git or root is None:
            return None
        try:
            cp = _subprocess.run([git, *args], cwd=str(root),
                                 capture_output=True, timeout=15)
        except Exception:
            return None
        if cp.returncode != 0:
            return None
        return (cp.stdout or b"").decode("utf-8", "replace").strip()

    def _can_prove_absence() -> bool:
        """Is THIS repository able to say an object does not exist?

        `git cat-file -e` answers "is this object HERE", not "does this object
        EXIST". In a clone that legitimately does not hold all history those are
        different questions, and the gc treats a False as authoritative grounds
        to retire a review. So absence is trustworthy only in a repository that
        has everything:

        * **shallow** — history truncated by `--depth`; live commits outside the
          cut are simply not present. Verified live: `git cat-file -e` on a
          merged, current-main commit in a `--depth 1` clone exits 128 with
          "fatal: Not a valid object name", which the classifier below reads as
          FALSE. That is a live head reported affirmatively dead.
        * **partial / promisor** — objects are fetched lazily, so absence is
          "not fetched yet", not "gone".

        Anything we cannot determine counts as cannot-prove. This is the
        destructive path; the cost of an unnecessary None is a review that stays
        open one more cycle, and the cost of a wrong False is a review destroyed.
        """
        if _git_out("rev-parse", "--is-shallow-repository") != "false":
            return False
        # PARTIAL CLONE, detected as a CAPABILITY rather than at one remote.
        # An earlier round of this fix checked `remote.origin.*` only; git does
        # not require the promisor remote to be named origin, so a clone whose
        # promisor is `upstream` passed the guard and kept the destructive path
        # open (codex-reviewer, round 1 — verified with
        # remote.upstream.promisor=true). Checking one instance of a thing
        # instead of the thing itself is the same mistake this whole P0 is
        # about: asking "is it HERE" rather than "does it EXIST".
        if _git_out("config", "--get", "extensions.partialclone"):
            return False
        if _git_out("config", "--get-regexp",
                    r"^remote\..*\.(promisor|partialclonefilter)$"):
            return False
        return True

    absence_is_trustworthy = _can_prove_absence()

    def _probe(sha: str) -> Optional[bool]:
        if not git or root is None or not sha_re.match(sha or ""):
            return None
        try:
            cp = _subprocess.run(
                [git, "cat-file", "-e", f"{sha}^{{commit}}"],
                cwd=str(root), capture_output=True, timeout=15)
        except Exception:
            return None
        if cp.returncode == 0:
            return True
        # git says "not a valid object name" / "could not get object info" on a
        # genuinely absent object. Any OTHER stderr is an error we cannot read
        # as absence — a broken repo must not retire a review.
        err = (cp.stderr or b"").decode("utf-8", "replace").lower()
        if "not a valid object" in err or "could not get object" in err or not err:
            # LOCAL ABSENCE IS NOT ABSENCE. A standard clone fetches branches,
            # not refs/pull/*, and a review register is full of PR heads.
            # Measured on this register: 6 of 6 entries this classifier called
            # dead were ALIVE at the source, from a full, non-shallow,
            # non-partial clone. So the question moves to the REMOTE, and this
            # branch never returns False on its own again.
            if not absence_is_trustworthy:
                return None
            return _remote_head_exists(sha)
        return None

    # Published so callers on the DESTRUCTIVE path can tell "found nothing"
    # from "could not look". Without it a blind run is indistinguishable from
    # a clean register — safe, but silently wrong to a reader.
    _probe.absence_is_trustworthy = absence_is_trustworthy  # type: ignore[attr-defined]
    return _probe


def _local_repo_identity() -> "Optional[str]":
    """``owner/repo`` for the checkout we are standing in, or None.

    None is a real answer and the safe one: an unidentifiable vantage point
    cannot witness the absence of anything.
    """
    import subprocess as _subprocess
    try:
        cp = _subprocess.run(["git", "remote", "get-url", "origin"],
                             capture_output=True, timeout=10)
    except (OSError, _subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    url = (cp.stdout or b"").decode("utf-8", "replace").strip()
    found = review_gc.repos_from_of(url)
    return sorted(found)[0] if len(found) == 1 else None


def _gc_entries(transport: Any, team: str) -> "tuple[list, list[str]]":
    """Read the register into :class:`review_gc.Entry` values.

    Returns ``(entries, unreadable_slugs)``. A slug whose doc cannot be read is
    NOT an entry: it is reported and skipped, because an unreadable doc is the
    one case where we know least and could destroy the most.
    """
    entries: list = []
    unreadable: list[str] = []
    prefix = f"team/{team}/review/"
    for row in transport.list_dir(prefix):
        name = row.get("name") or ""
        if not name.endswith(".md"):
            continue
        slug = name[:-3]
        raw = transport.read(prefix + name)
        if raw is None:
            unreadable.append(slug)
            continue
        fm = okf.parse_frontmatter(raw) or {}
        vprefix = _verdicts_prefix(team, slug)
        try:
            vnames = {e.get("name") for e in transport.list_dir(vprefix)}
        except Exception:
            unreadable.append(slug)
            continue
        entries.append(review_gc.Entry(
            slug=slug,
            # v2 docs carry `head:`; v1 docs hide it in the `of:` prose.
            head=(review.normalize_head(fm.get("head"))
                  or review_gc.head_from_prose(fm.get("of"))),
            superseded_by=(fm.get(review_gc.SUPERSEDED_KEY) or None),
            settled=SETTLED_MARKER in vnames,
            gc_closed=review_gc.is_terminal(vnames),
            repos=review_gc.repos_from_of(fm.get("of")),
        ))
    return entries, unreadable


def cmd_review_gc(args: argparse.Namespace, transport: Any) -> int:
    """Retire register entries that can never settle. DRY RUN unless --apply."""
    try:
        entries, unreadable = _gc_entries(transport, args.team)
    except TransportError as e:
        print(f"review gc: register unreadable ({e}) — nothing scanned, "
              f"nothing retired", file=sys.stderr)
        return 2
    probe = _git_head_probe()
    # BLINDNESS MUST BE LOUD. With the shallow/partial guard in place gc is
    # SAFE in such a clone -- every head reads UNKNOWN, so nothing is
    # classified dead and nothing is retired. It is also SILENT: the run
    # reports "retired 0", which reads exactly like a clean register. That is
    # the same failure this whole P0 is about, one layer up -- absence of a
    # finding standing in for a finding of absence.
    blind = not getattr(probe, "absence_is_trustworthy", True)
    if blind:
        print("review gc: this checkout CANNOT PROVE ABSENCE (shallow or "
              "partial clone), so no entry can be classified dead here. A "
              "'nothing retirable' result from this clone is BLIND, not clean. "
              "Re-run from a full checkout (git fetch --unshallow).",
              file=sys.stderr)
        if args.apply:
            print("review gc: refusing --apply from a clone that cannot prove "
                  "absence -- nothing was written", file=sys.stderr)
            return 2
    local_repo = getattr(args, "repo", None) or _local_repo_identity()
    if not local_repo:
        print("review gc: cannot identify this checkout's repository (no origin "
              "remote, or an unparseable one), so no head can be witnessed here "
              "— every entry reads UNKNOWN. This result is BLIND, not clean. "
              "Pass --repo owner/repo to assert it.", file=sys.stderr)
    else:
        print(f"review gc: witnessing from {local_repo} — reviews whose head "
              f"lives in another repository are UNKNOWN here, never dead.",
              file=sys.stderr)
    verdicts = review_gc.plan(entries, head_exists=probe, local_repo=local_repo)
    print(review_gc.render_plan(verdicts, applying=bool(args.apply)))
    for slug in unreadable:
        print(f"  keep {slug} — UNREADABLE: doc or verdicts dir could not be "
              f"read; skipped", file=sys.stderr)
    if not args.apply:
        return 0
    now = _iso(_now())
    by = _identity(getattr(args, "sender", None))
    failed = 0
    for v in verdicts:
        if not v.retirable:
            continue
        path = _verdicts_prefix(args.team, v.slug) + review_gc.GC_MARKER
        if not transport.write(path, review_gc.marker_body(v, now=now, by=by)):
            print(f"review gc: marker write FAILED for {v.slug} — entry stays "
                  f"live", file=sys.stderr)
            failed += 1
    retired = sum(1 for v in verdicts if v.retirable) - failed
    print(f"review gc: retired {retired} entr(ies)"
          + (f", {failed} write(s) failed" if failed else ""))
    return 1 if failed else 0


def cmd_doctor(args: argparse.Namespace, transport: Any) -> int:
    """Local preflight: tooling on PATH + store reachable. Exit 0 = healthy."""
    if getattr(args, "self", False):
        return _doctor_self(args, transport)
    if getattr(args, "delivery", False):
        return _doctor_delivery(args, transport)
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
    _report_writer_presence()
    _report_pin_currency(transport, args.team)
    if args.team:
        cfg, cfg_status = records.load_config_classified(transport, args.team)
        if cfg_status == "error":
            print("  ✗ Bus V3 authority UNKNOWN (store read failed)",
                  file=sys.stderr)
            ok = False
        elif cfg_status == "invalid":
            print("  ✗ Bus V3 authority malformed or partially versioned",
                  file=sys.stderr)
            ok = False
        elif cfg is None:
            print("  ! Bus V3 authority absent or malformed; fleet version "
                  "census unavailable")
        else:
            gate = records.compatibility(
                cfg, engine_version=_v, write_cursor=True)
            if not gate["ok"]:
                print(f"  ✗ Bus V3 version gate: {gate['reason']}",
                      file=sys.stderr)
                ok = False
            for warning in gate["warnings"]:
                print(f"  ! Bus V3 version warning: {warning}")
            if records.v2_transport_ready(transport):
                print("  ✓ Bus V3 cursor CAS transport available")
            elif records.v2_active(cfg):
                print("  ✗ Bus V3 cursor v2 is active but this transport "
                      "cannot prove atomic CAS", file=sys.stderr)
                ok = False
            else:
                print("  ! Bus V3 cursor CAS transport unavailable; schema v2 "
                      "activation remains blocked")
            record_rows = None
            try:
                record_rows = transport.records(
                    cfg["data_type"],
                    _iso(_now() - timedelta(days=30)), _iso(_now()))
            except Exception:
                record_rows = None
            now_iso = _iso(_now())
            fresh_presence = [
                shard for shard in _presence_shards(transport, args.team)
                if presence.classify(
                    shard.get("timestamp") if isinstance(shard, dict) else None,
                    now=now_iso) != "stale"
            ]
            census = records.fleet_version_census(
                fresh_presence, record_rows)
            if census["record_evidence_unknown"]:
                print("  ! Fleet census: adoption-claim evidence UNKNOWN")
            if not census["agents"]:
                print("  ! Fleet census: no adoption/presence version evidence")
            for row in census["agents"]:
                running = row["running_engine_version"] or "UNKNOWN"
                adopted = row["adopted_engine_version"] or "UNKNOWN"
                print(f"  {'!' if running == 'UNKNOWN' else '✓'} "
                      f"{row['agent']}: running={running} "
                      f"(protocol={row['running_protocol_version']}, "
                      f"cursor={row['running_cursor_schema_version']}); "
                      f"adopted={adopted}")
            if census["mixed"]:
                print("  ! Fleet census: MIXED/UNKNOWN versions; v2 cursor "
                      "activation is unsafe")
            # respec s7: supersession-adoption metric (deputy-corrected
            # definition). Classification evidence exists only in v2 cursor
            # `handled` rows, so pre-activation windows honestly read UNKNOWN
            # — never 0% — and an empty denominator reads n/a, never 100%.
            # `outcomes` stays None (UNKNOWN) until at least one cursor READS
            # ok: activation alone proves nothing was read, an empty census
            # has no sources, and absent/invalid/error reads are unreadable
            # evidence, not an empty classification set (pr-503 round 1).
            fleet_ev = records.fleet_events(record_rows)
            outcomes = None
            if (fleet_ev is not None and cfg is not None
                    and records.v2_active(cfg)):
                for row in census["agents"]:
                    cur, _raw, status = records.load_v2_cursor_classified(
                        transport, args.team, row["agent"],
                        cfg["cursor_generation"])
                    if status == "ok" and cur is not None:
                        if outcomes is None:
                            outcomes = {}
                        for h in cur["committed"]["handled"]:
                            outcomes[h["record_id"]] = h["outcome"]
            # s7 verb-channel link: gather `task supersede --record` evidence
            # from HOT task docs (frontmatter `superseded_record_id`). Gated
            # with the outcomes read — explicit ids refine a measured window
            # and never override the UNKNOWN precedence, so scanning a
            # pre-activation fleet would buy nothing. A failed listing/read
            # degrades LOUDLY to stream-only evidence — affected
            # supersessions land in the unknown bucket, never a fabricated
            # count and never silence.
            explicit_ids: Optional[set] = None
            if outcomes is not None:
                task_scan_degraded = False
                try:
                    task_fms = []
                    task_prefix = f"team/{args.team}/task/"
                    for entry in transport.list_dir(task_prefix):
                        name = entry.get("name") or ""
                        if entry.get("is_dir") or not name.endswith(".md"):
                            continue
                        raw = transport.read(task_prefix + name)
                        if raw is None:
                            task_scan_degraded = True
                            continue
                        task_fms.append(okf.parse_frontmatter(raw))
                    explicit_ids = records.explicit_supersessions(task_fms)
                except Exception:
                    task_scan_degraded = True
                if task_scan_degraded:
                    print("  ! Supersession adoption: task-verb evidence "
                          "unreadable (explicit `task supersede --record` "
                          "channel degraded to stream-only this run)")
            adoption = records.supersession_adoption(
                fleet_ev or [], outcomes, explicit_ids=explicit_ids)
            if adoption["status"] == "unknown":
                print("  ! Supersession adoption: UNKNOWN (no v2 "
                      "classification evidence this window — not 0%)")
            elif adoption["counted"] == 0:
                print(f"  ✓ Supersession adoption: n/a (0 candidates; "
                      f"{adoption['unknown']} unmeasurable)")
            else:
                print(f"  ✓ Supersession adoption: "
                      f"{adoption['superseded']}/{adoption['counted']} "
                      f"({adoption['ratio']:.0%}; "
                      f"{adoption['unknown']} unmeasurable)")
    print("doctor: healthy" if ok else "doctor: PROBLEMS FOUND")
    return 0 if ok else 1


def _doctor_self(args: argparse.Namespace, transport: Any) -> int:
    """Am I the engine the fleet expects? rc 0 current, 3 stale, 2 unknown.

    The one-command replacement for the unconditional restore-and-adopt
    preamble: a wake runs this and only pays for repair when it is nonzero.
    Tri-state on purpose — rc 0 is claimed ONLY when the pin exists, parses
    and this engine meets it. An unreadable config, an authority with no pin,
    and a malformed pin are all rc 2 with the reason named, because
    "comparison impossible" reported as "current" is the failure mode a
    self-check exists to prevent.
    """
    from . import __version__ as engine_version
    if not args.team:
        print("doctor --self: team is required (the authority lives per team)",
              file=sys.stderr)
        return 2
    cfg, cfg_status = records.load_config_classified(transport, args.team)
    if cfg is None:
        detail = {
            "error": "the records config could not be READ (transport "
                     "failure) — retry when the store is reachable",
            "invalid": "the records config is malformed — human-fixable, the "
                       "bytes are the evidence",
        }.get(cfg_status,
              f"no bus-v3 records config for team {args.team}")
        print(f"self: UNKNOWN — {detail}", file=sys.stderr)
        return 2
    state, detail = records.authority_currency_state(
        cfg, engine_version=engine_version)
    if state == "current":
        print(f"self: CURRENT — {detail}")
        return 0
    if state == "stale":
        print(f"self: STALE — {detail}", file=sys.stderr)
        print("  run the store's adopt-latest.sh, then re-run "
              "`coord-engine doctor <team> --self`", file=sys.stderr)
        return 3
    print(f"self: UNKNOWN — {detail}", file=sys.stderr)
    return 2


def _doctor_delivery(args: argparse.Namespace, transport: Any) -> int:
    """Write and read one probe through the production typed-record seams."""
    agent = _declared_identity(args.agent)
    if not args.team:
        print("doctor --delivery: team is required", file=sys.stderr)
        return 2
    if not agent:
        print("doctor --delivery: no agent identity", file=sys.stderr)
        return 2
    cfg = records.load_config(transport, args.team)
    if cfg is None:
        print("doctor --delivery: no records config — cannot write",
              file=sys.stderr)
        return 2

    nonce = uuid.uuid4().hex[:8]
    slug = f"delivery-probe-{nonce}"
    # Build once through the public probe helper as a local contract check;
    # emit_event below is the normal event path used by reminders/directives.
    payload = records.roundtrip_probe_payload(agent, nonce)
    if records.parse_payload(payload) is None:  # pragma: no cover - invariant
        print("doctor --delivery: probe payload is not parseable",
              file=sys.stderr)
        return 2
    started = _now()
    written = records.emit_event(
        transport, cfg, sender=agent, to=f"{agent}-probe", kind="claim",
        priority="P3", slug=slug, team=args.team)
    if not written:
        print("doctor --delivery: probe write REFUSED", file=sys.stderr)
        return 2

    deadline = time.monotonic() + args.deadline
    while True:
        now = _now()
        window = transport.records(
            cfg["data_type"], _iso(started - timedelta(minutes=2)), _iso(now))
        if window is not None:
            for rec in window:
                if not isinstance(rec, dict):
                    continue
                parsed = records.parse_payload(rec.get("note"))
                if parsed is None or parsed.get("slug") != slug:
                    continue
                stamp = parsed.get("writer") or {}
                from . import __version__
                if stamp.get("engine_version") != __version__:
                    print(
                        "delivery: probe readable but writer stamp is "
                        f"{stamp.get('engine_version')!r}, engine is "
                        f"{__version__} — TWO engines are writing as this "
                        "identity",
                        file=sys.stderr,
                    )
                    return 3
                lag = max(0.0, (now - started).total_seconds())
                print("delivery: PROVEN — probe written, ingested and parsed "
                      f"in {lag:.0f}s (payload v1, stamped {__version__})")
                return 0
            for rec in window:
                if not isinstance(rec, dict):
                    continue
                note = rec.get("note")
                if (isinstance(note, str) and slug in note
                        and records.parse_payload(note) is None):
                    print(
                        "delivery: probe written but NOT readable — this "
                        "engine wrote a non-v1 note (legacy/rolled-back "
                        "writer). Run adopt-latest and retry.",
                        file=sys.stderr,
                    )
                    return 3
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(5.0, remaining))
    print("delivery: UNKNOWN — probe not readable within "
          f"{args.deadline:.0f}s (ingest lag or store failure); NOT proof of "
          "delivery", file=sys.stderr)
    return 3


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
    if _digest_persists(args):
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


def _is_self_addressed_vacancy(maintainer: str, leases: Any) -> bool:
    """Would this role's vacancy notice be addressed to the party who lapsed?

    A closed loop with no exit: the notice about someone's absence lands in the
    absent one's own unread bucket. Observed live — three daily ROLE VACANT
    directives for a role whose registered `maintainer:` was its own retired
    holder.

    The predicate is deliberately NARROW: the maintainer is one of THIS role's
    lease holders. Decidable from data already in hand, with no judgement about
    whether an identity is alive. The obvious wider rule — "refuse to address a
    notice to anyone who looks stale in presence" — would be wrong AND dangerous
    here: presence staleness is not death (a reviewer on this bus read 5 days
    stale while filing verdicts hourly), so it would misroute alarms for
    live-but-quiet maintainers. That is this function's own failure, inverted.

    REPORTS, does not redirect. My first version rerouted to ``_human()`` and an
    existing test caught it doing harm: a role legitimately maintained by the
    human operator, who also appears as a lease agent, got its notice moved off
    a real person onto the bare ``"human"`` default — an address nobody reads.
    The engine cannot know a better addressee than the registry does, and a
    silent rewrite to a worse one is the same class of bug as the loop itself.
    Same rule we just agreed for alias resolution: warn and let a human fix the
    field, never silently rewrite the destination.
    """
    # No carve-out for the human operator. I tried one — exempting `_human()` —
    # and it does not survive contact: `_human()` is whatever
    # FULCRA_COORD_HUMAN happens to say, so a role naming a person the engine
    # has not been told is the operator looks exactly like the ArcBot case, and
    # a role naming the configured human would be exempted without the engine
    # knowing anything more than the string matched. FLAGGING is safe in a way
    # REROUTING was not: nothing moves, we only say what we see, and a human who
    # really is reachable loses nothing but one accurate stderr line.
    return maintainer in {str(l.get("agent") or "") for l in (leases or [])}


def _emit_escalation_event(transport: Any, team: str, *, to: str,
                           slug: str, ptr: str) -> Optional[bool]:
    """Publish an engine-minted vacancy directive to the event plane.

    TRI-STATE, and the distinction is load-bearing:
      True  — the record landed.
      False — there IS an event plane and the write to it FAILED. A real
              delivery failure: retry it, and fail the sweep closed.
      None  — this team has NO bus-v3 config at all. Nothing was delivered
              because there is nowhere to deliver to. That is a deployment
              shape, not an incident, and it must NOT count as undelivered:
              a team running file-plane-only would otherwise take rc 3 on
              every sweep forever, and an alarm that fires on every run is
              worth exactly as much as no alarm (the same reasoning this
              module already applies to the attendance count cap).

    Never raises and never fails the sweep: the DOCUMENT is the durable
    obligation and the event is delivery, so a bus that is down must not stop a
    vacancy from being recorded.
    """
    cfg = records.load_config(transport, team)
    if cfg is None:
        print("escalate: no bus-v3 records config — the vacancy directive rides "
              "the file plane only and will NOT appear in any stream fold",
              file=sys.stderr)
        return None
    try:
        ok = records.emit_event(
            transport, cfg, sender=_host(), to=to, kind="directive",
            priority="P1", slug=slug, ptr=ptr, team=team)
    except Exception as e:  # a bus write must never lose the vacancy record
        print(f"escalate: vacancy directive event failed to emit ({e}) — the "
              f"document stands, but {to}'s stream fold will not open it",
              file=sys.stderr)
        return False
    if not ok:
        print(f"escalate: vacancy directive event did NOT land — the document "
              f"stands, but {to}'s stream fold will not open it", file=sys.stderr)
    return bool(ok)


def _vacancy_slug_candidates(role: str, today: str, sla: float) -> list[str]:
    """Both titles cmd_escalate can mint for a role on a day, as slugs.

    Needed only for markers written BEFORE delivery state existed, which carry
    no slug. The title branches on `attended`, so a day has two possible slugs
    and exactly one of them has a document on disk. Probing two paths is cheaper
    and far more honest than guessing one.
    """
    titles = [
        f"ROLE VACANT {today}: {role} UNATTENDED past {sla:g}h SLA "
        f"— no holder work found",
        f"ROLE VACANT {today}: {role} lease lapsed past {sla:g}h SLA "
        f"(attendance UNVERIFIED)",
    ]
    return [tasks.slugify(t) for t in titles]


def _resolve_vacancy(transport: Any, team: str, role: str, today: str,
                     sla: float) -> tuple[Optional[str], Optional[str], str]:
    """Today's vacancy for this role as (slug, assignee), from the DOCUMENT.

    THE single place routing facts are derived. Seven rounds of this fix each
    proved a SUBSET of what "this marker is proof" requires — the boolean, then
    the field types, then that the slug referenced something real, then that it
    was this role's vacancy — and each time the field left unchecked was the next
    hole. `to` was the last one (codex-reviewer, 682 r6): a marker carrying the
    REAL vacancy slug with `to: mallory` was accepted as proof that Alice's
    vacancy had reached the stream.

    So confirmation is no longer a list of field checks that can be incomplete.
    Both the suppression predicate and the redelivery path resolve the vacancy
    HERE, and a marker is proof only if it equals what this function computes
    right now. There is no field left to forget, because nothing is read off the
    marker at all.

    Returns ``(slug, assignee, state)`` where state is one of:
      "live"       — a real, still-open vacancy that owes delivery
      "terminal"   — the day's vacancy exists but was already ANSWERED
      "unresolved" — nothing readable; UNKNOWN, so redelivery is attempted

    TERMINAL IS NOT A FAILURE, and conflating them was the r1 defect here
    (codex-reviewer, 685 r1 P2): returning a bare False for a completed
    obligation made the caller report "redelivery FAILED", increment
    `undelivered` and return rc 3 on EVERY future sweep — a permanent false
    alarm created by correctly refusing to resurrect. A completed obligation is
    not an undelivered live one, so the distinction rides in the return rather
    than in the caller's guesswork.
    """
    # UNKNOWN OUTRANKS TERMINAL (codex-reviewer, 685 r2). r1 remembered a
    # terminal candidate but not that ANOTHER candidate document EXISTED and
    # could not be resolved. With one candidate `done` and the other corrupt,
    # the loop fell through to "already answered", recorded no failure and
    # exited rc 0 — while the unreadable one may still be a live P1. A document
    # we could not read is not evidence that nothing is owed; it is the absence
    # of evidence, and it must fail closed into redelivery rather than be
    # outvoted by its answered sibling.
    terminal: Optional[str] = None
    unreadable = False
    for cand in _vacancy_slug_candidates(role, today, sla):
        doc = transport.read(_task_path(team, cand))
        if doc is None:
            continue  # never existed — not evidence of anything
        fm = okf.parse_frontmatter(doc)
        if fm is None:
            unreadable = True  # EXISTS but unreadable: UNKNOWN, not absent
            continue
        # NEVER RESURRECT A TERMINAL OBLIGATION (coord-maintainer, 2026-08-23).
        # 2.0.5 made opens emit, and redelivery then replayed opens for
        # documents that had ALREADY been answered. During the broken window the
        # open never reached the stream, so when the agent closed the row their
        # close had nothing to answer and emitted nothing. Redelivery now
        # replays the open into a fold that has never seen a close for it: the
        # obligation is terminal in the document and OPEN in the stream,
        # permanently.
        #
        # The `abandoned` case is strictly worse and is why this checks ANY
        # terminal state rather than emitting a compensating close for `done`:
        # `abandoned -> done` is an illegal transition, so a resurrected
        # abandoned row cannot be discharged by ANY action its holder can take.
        # It is a P1 they are required to carry and forbidden to answer.
        #
        # A terminal document is not a live obligation, so there is nothing to
        # deliver. Skip it and keep looking — the day's other candidate may be
        # the live one.
        # Normalised the same way cli.py already reads a doc status elsewhere:
        # these documents are hand-editable, so case and stray whitespace are
        # real, and a status that fails to match here fails OPEN into a
        # resurrection.
        if str(fm.get("status") or "").strip().lower() in tasks.TERMINAL_STATUSES:
            terminal = cand
            continue
        assignee = fm.get("assignee")
        if isinstance(assignee, str) and assignee:
            return cand, assignee, "live"
        # Non-terminal document with no readable assignee: it exists and may be
        # live, but nothing here can say who for. UNKNOWN, same as unreadable.
        unreadable = True
    if unreadable:
        return None, None, "unresolved"
    if terminal is not None:
        return terminal, None, "terminal"
    return None, None, "unresolved"


def _delivery_confirmed(transport: Any, team: str,
                        dstate: Optional[dict[str, Any]], role: str,
                        today: str, sla: float) -> bool:
    """Is this marker PROOF that today's vacancy reached the stream?

    ONE predicate, used by every consumer, because the bug it closes is
    consumers diverging (codex-reviewer, 682 r5). r5 fixed the redelivery path
    to derive its own candidate set, and left the SUPPRESSION fast path trusting
    `dstate.get("delivered")` by itself — so a well-typed marker
    {"delivered": true, "slug": "unrelated-real-task", "to": "mallory"} still
    short-circuited, reported "event delivered", emitted nothing, and left the
    real vacancy permanently absent. Same unrelated-task marker, opposite
    boolean, same permanent loss.

    Confirmation requires the marker to be claiming delivery OF THIS ROLE'S
    VACANCY: `delivered` a real boolean True, and `slug` one of the two
    candidates derived for this role/date/SLA. Anything else is UNKNOWN, which
    routes into the derived redelivery path — the safe direction.
    """
    if not dstate or dstate.get("delivered") is not True:
        return False
    slug, to, state = _resolve_vacancy(transport, team, role, today, sla)
    if state != "live":
        return False
    return dstate.get("slug") == slug and dstate.get("to") == to


def _redeliver_escalation(transport: Any, team: str, role: str, today: str,
                          sla: float, *, dstate: Optional[dict[str, Any]],
                          maintainer: str) -> Any:
    """Re-emit today's vacancy event for a role whose delivery never landed.

    Returns whether the event is now on the stream. Idempotent by slug: the
    fold keys `open` on the slug, so a duplicate event re-opens the SAME
    obligation rather than creating a second one — which is why retrying is
    safe and dropping the retry was not.
    """
    # THE MARKER IS A HINT; THE DOCUMENT IS THE AUTHORITY (codex-reviewer, 682
    # r3). Round three validated the marker's SHAPE and then still routed on its
    # contents: a well-typed
    # {"delivered": false, "slug": "role-vacant-nonexistent", "to": "mallory"}
    # made the sweep emit a directive to mallory pointing at a task that does
    # not exist, report success, and write a delivered marker that suppressed
    # every retry — while the real vacancy sat under its own slug, still absent
    # from the stream of the agent it was actually for.
    #
    # Type-checking a claim is not verifying it. Routing evidence is now taken
    # from the TASK DOCUMENT — the durable obligation — and the marker only
    # proposes which document to look at. A proposal that does not resolve to a
    # real document is discarded, not followed.
    # THE CANDIDATE SET IS DERIVED, NEVER PROPOSED (codex-reviewer, 682 r4).
    # r4 probed the marker's slug first and verified only that it resolved to a
    # real task with a readable assignee — not that it was THIS ROLE'S vacancy.
    # So a stale or corrupt marker naming any valid task routed THAT task to its
    # assignee, recorded delivery success, and left the real vacancy unannounced.
    #
    # The marker's slug was only ever an optimisation: the day's candidate set is
    # already deterministic and bounded (two titles, branching on `attended`), so
    # probing it finds the same document without trusting anything. Every round
    # of this fix that tried to VALIDATE the proposed slug found another hole —
    # nonexistent, then wrong-recipient, then valid-but-unrelated. Not selecting
    # on it removes the class instead of the instance. The marker now carries
    # delivery STATE only; routing comes from (role, date, sla) and the document.
    slug, to, state = _resolve_vacancy(transport, team, role, today, sla)
    if state == "terminal":
        # Already answered. Nothing is owed, so this is not a delivery failure
        # and must not be counted as one — see _resolve_vacancy's docstring.
        print(f"escalate: {role}'s vacancy for today was already answered "
              f"({slug}) — nothing to deliver, not resurrecting it",
              file=sys.stderr)
        return "terminal"
    if state != "live" or to is None:
        print(f"escalate: {role} has today's marker but no vacancy document "
              f"whose assignee can be read — cannot redeliver, and NOT recording "
              f"a delivery that did not happen; state UNKNOWN", file=sys.stderr)
        return False
    if dstate and dstate.get("to") and dstate.get("to") != to:
        print(f"escalate: {role} delivery marker names recipient "
              f"{dstate.get('to')!r} but the vacancy document assigns "
              f"{to!r} — routing on the document", file=sys.stderr)
    if dstate and dstate.get("slug") and dstate.get("slug") != slug:
        print(f"escalate: {role} delivery marker names slug "
              f"{dstate.get('slug')!r}, which is not a vacancy slug derived for "
              f"this role and date — ignored, using {slug!r}", file=sys.stderr)
    ok = _emit_escalation_event(transport, team, to=to, slug=slug,
                                ptr=f"task/{slug}.md")
    if ok is not None:
        _write_escalation_delivery(transport, team, role, today,
                                   slug=slug, to=to, delivered=bool(ok))
    return ok


def cmd_escalate(args: argparse.Namespace, transport: Any) -> int:
    """Role-vacancy sweep: for every role doc, if vacancy past SLA and no marker
    today, write the marker + a P1 directive to the role's maintainer.
    Heartbeat-safe (idempotent per day)."""
    now = _iso(_now()); today = _now().strftime("%Y-%m-%d")
    escalated = checked = undelivered = 0
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
    # ONE shared verdict-activity scan for the whole sweep, bounded by wall-clock
    # as well as count. Built here rather than per role: see
    # `_verdict_activity_index` for the measurement that motivated it.
    att_dl = Deadline.open(_attendance_scan_budget())
    att_index = _verdict_activity_index(transport, args.team, deadline=att_dl)
    _att_scanned, _att_total, _att_cut = att_index[1], att_index[2], att_index[4]

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
            # NAME THE REASON. This branch used to `continue` in total silence,
            # and `escalated=0` was then the only thing an operator saw. Those
            # are three different worlds — already escalated today, lease still
            # fresh, nothing due — and one integer could not tell them apart.
            # coord-maintainer, 2026-08-23: ran the sweep against a role 44h
            # past its SLA, saw a clean rc 0 and `0 escalated`, and reasonably
            # concluded the alarm had gone quiet on a real vacancy. It had not;
            # the role was escalated at 00:41:50Z and today's marker was
            # suppressing the repeat. A silent skip that produces a confident
            # wrong reading in a careful reader is a defect in the OUTPUT.
            if marker_exists:
                # REDELIVERY, before the suppression takes effect. The repeat is
                # suppressed; the DELIVERY is not, because they are different
                # questions and only one of them was ever answered here
                # (codex-reviewer, 682 r1). A vacancy whose event never landed
                # is retried on every sweep until it does — otherwise the first
                # failure is permanent, since the mint branch below can never
                # run again for this role today.
                dstate = _read_escalation_delivery(transport, args.team, role, today)
                if _delivery_confirmed(transport, args.team, dstate, role, today, sla):
                    print(f"escalate: {role} already escalated today "
                          f"(marker {today}, event delivered) — repeat "
                          f"suppressed", file=sys.stderr)
                else:
                    redelivered = _redeliver_escalation(
                        transport, args.team, role, today, sla,
                        dstate=dstate, maintainer=str(reg.get("maintainer")
                                                      or _human()))
                    if redelivered == "terminal":
                        pass  # answered already; the note is printed by the helper
                    elif redelivered is None:
                        print(f"escalate: {role} already escalated today "
                              f"(marker {today}); this team has no event plane, "
                              f"so the vacancy lives on the file plane only",
                              file=sys.stderr)
                    else:
                        print(f"escalate: {role} already escalated today "
                              f"(marker {today}) but its event had NOT been "
                              f"delivered — redelivery "
                              + ("SUCCEEDED, the vacancy is now on the stream"
                                 if redelivered else
                                 "FAILED, it remains invisible to every fold"),
                              file=sys.stderr)
                        if not redelivered:
                            undelivered += 1
            else:
                print(f"escalate: {role} not due — lease is fresh inside its "
                      f"{sla:g}h SLA", file=sys.stderr)
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
        # ATTENDANCE on the ACTING path, answered from the ONE shared scan built
        # before this loop. It used to rebuild that scan per role — 41 sequential
        # listings each — so a sweep with N acting roles paid N x ~24s. The cost
        # is now constant in the number of roles.
        #
        # Wiring this into `roles status` alone was the round-1 defect: that
        # improved what an operator READS while the sweep kept emitting the same
        # false "unattended" P1s. The diagnostic is not the actuator.
        anchor = roles._parse(now)
        attended, a_scanned, a_total = None, 0, 0
        if anchor is not None:
            attended, a_scanned, a_total = _role_attended(
                transport, args.team,
                [str(l.get("agent")) for l in (leases or [])],
                since=anchor - timedelta(hours=sla), index=att_index)
        # THE PER-ROLE VERDICT (coord-maintainer's ask, 2026-08-23). `attended`
        # is a THREE-valued answer and the sweep used to publish only its
        # aggregate effect. FOUND and NOT-FOUND are conclusions; UNKNOWN is the
        # honest "I could not see far enough", and it is the one that must never
        # be read as either. Printing it per role is what lets a reader tell an
        # attending fleet from a scan that ran out of budget — the distinction
        # `escalated=0` silently collapsed.
        print(f"escalate: {role} attendance "
              + ("FOUND" if attended is True else
                 "NOT-FOUND" if attended is False else
                 "UNKNOWN-within-budget")
              + f" (scanned {a_scanned}/{a_total}, sla {sla:g}h)",
              file=sys.stderr)
        if not roles.escalation_due(leases, now=now, sla_hours=sla,
                                    marker_exists_today=marker_exists,
                                    attended=attended):
            print(f"escalate: {role} vacancy explained — a holder filed a verdict "
                  f"within {sla:g}h (scanned {a_scanned}/{a_total}); the LEASE "
                  f"lapsed, the job did not. Escalation suppressed — ask for a "
                  f"lease renewal.", file=sys.stderr)
            continue
        # The `ROLE VACANT ...` slug family is a CONTRACT (dedupe key, existing
        # queries, day-over-day re-notify). Keep it; change only the claim made
        # after it, which is the part that was false.
        if attended is False:
            title = (f"ROLE VACANT {today}: {role} UNATTENDED past {sla:g}h SLA "
                     f"— no holder work found")
            evidence = (f"A COMPLETE verdict sweep ({a_scanned}/{a_total}) found no "
                        f"work by any holder inside the window.")
        else:
            title = (f"ROLE VACANT {today}: {role} lease lapsed past {sla:g}h SLA "
                     f"(attendance UNVERIFIED)")
            evidence = (f"Attendance could NOT be established (scanned "
                        f"{a_scanned}/{a_total}). This says the LEASE lapsed — NOT "
                        f"that nobody is working. Verify before treating it as absence.")
        maintainer = str(reg.get("maintainer") or _human())
        self_addressed = _is_self_addressed_vacancy(maintainer, leases)
        if not self_addressed:
            # The daily marker is a SUPPRESSOR: it stops tomorrow's sweep from
            # re-notifying. Writing it for a notice that reached nobody would
            # record a delivery that did not happen and then silence the only
            # mechanism that would try again (codex-reviewer, 577 r1). A
            # closed-loop role therefore keeps re-surfacing every sweep until
            # its `maintainer:` field is fixed. It does not spam: the directive
            # write below is guarded on the task not already existing, so the
            # repeat is one stderr line and one "suppressed" note, not a new
            # document per run.
            transport.write(marker_path, okf.render_frontmatter(
                {"type": "Escalation", "role": role, "timestamp": now})
                + "\nescalated\n")
        slug, content = tasks.new_task_doc(
            title,
            now=now, status="proposed", priority="P1", owner=_host(),
            assignee=maintainer, kind="directive",
            summary=f"Role {role} in team/{args.team} has no fresh lease past its SLA. "
                    f"{evidence} "
                    + (f"CLOSED LOOP: this notice is addressed to {maintainer}, "
                       f"who is also this role's lapsed holder — an alarm about "
                       f"an absence, delivered to the absent party. It was NOT "
                       f"rerouted, because nothing here knows a better addressee "
                       f"than the registry does. Fix the `maintainer:` field in "
                       f"the role doc. " if self_addressed else "")
                    + f"Claim it (coord-engine roles claim {args.team} {role}) or reassign.",
        )
        # Counted OUTSIDE the write branch. codex-reviewer, 577 r1: the first
        # sweep was correctly degraded, and the SECOND one went clean — run 2
        # found the existing directive, skipped the whole branch, and reported
        # undelivered=0, degraded=0, rc 0 while the notice was still sitting
        # undeliverable in the absent party's bucket. The delivery failure is a
        # property of WHO the notice is addressed to, not of whether this
        # particular sweep wrote a new document, so a retry must not launder it.
        if self_addressed:
            undelivered += 1
        dst = _task_path(args.team, slug)
        # ABSENT, not merely unreadable: this is the per-day idempotency guard
        # for the vacancy mint, and it both WRITES and EMITS. Read a 500 as
        # "not minted yet" and an outage turns the daily pass into duplicate
        # P1 vacancy rows plus duplicate bus events, fleet-wide — the alarm
        # bloat mechanism running at machine speed.
        existing_row, row_status = transport.read_classified(dst)
        if row_status == "error":
            print(f"escalate: {slug} — cannot READ the day's row to tell whether "
                  f"it was already minted; skipping rather than risk a duplicate "
                  f"row and a duplicate event. This role is UNKNOWN this pass, "
                  f"not clear.", file=sys.stderr)
            continue
        if existing_row is None:
            transport.write(dst, content)
            escalated += 1
            # PUBLISH IT TO THE EVENT PLANE. Until 2.0.5 this branch wrote the
            # document and told the bus NOTHING: the only "emit" anywhere in
            # cmd_escalate is emit_envelope, which is a stdout summary line, not
            # a record. Under the FILE plane that was invisible, because
            # `needs-me` enumerated task docs and therefore found engine-written
            # ones. Under the STREAM plane a fold's `open` set is built ONLY
            # from directive events, so a P1 minted here entered NOBODY's fold —
            # not the assignee's, not anyone's. coord-maintainer hit this at
            # 2026-08-23T06:50Z: they tested for the presence of a ROLE VACANT
            # row by reading their owed fold, saw silence, and concluded the
            # alarm had gone quiet. The row existed. The obligation had simply
            # never been published to the plane they were reading.
            #
            # Mirror of the retention gap (reconcile._run_retention archives a
            # quiet `proposed` task and likewise emits nothing, so an obligation
            # silently stops being dischargeable). One path never OPENS on the
            # stream, the other never CLOSES. Same root cause: engine-written
            # obligations bypassed the event plane, which only became
            # load-bearing when folds went forward-only at the cutover.
            emitted = _emit_escalation_event(
                transport, args.team, to=maintainer,
                slug=slug, ptr=f"task/{slug}.md")
            # Delivery is recorded whether or not it succeeded, so a later sweep
            # knows there is something to retry AND which slug to retry with.
            # The slug is not recomputable later: its title depends on the
            # `attended` value at mint time, which varies between sweeps.
            if emitted is not None:
                _write_escalation_delivery(transport, args.team, role, today,
                                           slug=slug, to=maintainer,
                                           delivered=bool(emitted))
            print(f"escalated {role} -> {maintainer}"
                  + (" (UNDELIVERED: closed loop)" if self_addressed else ""))
            if self_addressed:
                print(f"escalate: {role}'s maintainer ({maintainer}) IS its own "
                      f"lapsed holder — this notice lands in the absent party's "
                      f"bucket and has no exit. NOT rerouted: the engine does "
                      f"not know a better addressee, and a silent rewrite to a "
                      f"worse one is the same bug pointed the other way. Fix "
                      f"the role doc's `maintainer:` field. The daily marker "
                      f"was NOT written, so this re-surfaces every sweep until "
                      f"it is — an undelivered notice must not be recorded as "
                      f"a delivery.", file=sys.stderr)
        else:
            # Same redelivery duty as the marker branch above, and this is the
            # ONLY path a self-addressed vacancy ever takes: it writes no daily
            # marker by design, so it arrives here every sweep. Without this the
            # closed-loop case had the permanent-loss hole too.
            # ROUTED THROUGH THE SAME TERMINAL-AWARE RESOLVER as the marker
            # branch (codex-reviewer, 685 r1 P1). This branch used to emit the
            # `slug` computed further up, bypassing the resolver entirely — so a
            # self-addressed vacancy, which writes no daily marker by design and
            # therefore lands here EVERY sweep, republished its own terminal task
            # as a fresh open. Exactly the permanently-undischargeable row this
            # PR exists to prevent, recreated by the one path that skipped the
            # guard. One retry, one resolver.
            dstate = _read_escalation_delivery(transport, args.team, role, today)
            if not _delivery_confirmed(transport, args.team, dstate, role, today, sla):
                r_slug, r_to, r_state = _resolve_vacancy(
                    transport, args.team, role, today, sla)
                if r_state == "terminal":
                    print(f"escalate: {role}'s vacancy for today was already "
                          f"answered ({r_slug}) — nothing to deliver, not "
                          f"resurrecting it", file=sys.stderr)
                elif r_state == "live" and r_to:
                    ok = _emit_escalation_event(transport, args.team, to=r_to,
                                                slug=r_slug,
                                                ptr=f"task/{r_slug}.md")
                    if ok is not None:
                        _write_escalation_delivery(transport, args.team, role,
                                                   today, slug=r_slug, to=r_to,
                                                   delivered=bool(ok))
                        print(f"escalate: {role}'s existing directive had NOT been "
                              f"delivered to the stream — redelivery "
                              + ("SUCCEEDED" if ok else "FAILED"), file=sys.stderr)
            print(f"re-escalation suppressed for {role} (today's directive already exists)")
            if self_addressed:
                print(f"escalate: {role} still has an UNDELIVERED notice — its "
                      f"maintainer ({maintainer}) is its own lapsed holder, and "
                      f"today's directive is sitting in that unread bucket. The "
                      f"retry is suppressed because the document exists, NOT "
                      f"because anyone received it. Fix the role doc's "
                      f"`maintainer:` field.", file=sys.stderr)
    # The verdict, on stderr, so a vacancy check that could not finish is not
    # mistaken for one that found nothing. escalate returned rc 0 after a 98s
    # run that printed 136 bytes — indistinguishable from a clean sweep, which is
    # exactly what let a DEGRADED watchdog read as "0 escalated" for 12 hours.
    # An INCOMPLETE attendance scan means every "unattended" call this sweep made
    # was made on partial evidence, so it fails closed with rc 3.
    # COVERAGE IS ALWAYS PARTIAL BY DESIGN and must not read as an incident: the
    # register holds 412 review dirs on the live store and the scan is capped at
    # `budget` (40), because a complete fan-out is ~243s of transport. So rc 3 is
    # reserved for the scan being cut by its WALL-CLOCK deadline — a real anomaly
    # — and the ordinary count cap is reported as coverage, not as degradation.
    # An alarm that fires on every run is worth exactly as much as no alarm.
    att_cut = _att_cut
    # UNDELIVERED is degradation in the same register as a cut scan: the sweep
    # ran, but a notice it counted reached nobody. rc 0 there would report a
    # clean vacancy check while an alarm sat in an absent party's bucket —
    # stderr is transient observability, not durable delivery, and an unattended
    # caller keys on the rc (codex-reviewer, 577 r1).
    rc = 3 if (att_cut or undelivered) else 0
    emit_envelope("escalate", count=checked, rc=rc, escalated=escalated,
                  undelivered=undelivered,
                  attendance=f"{_att_scanned}/{_att_total}",
                  degraded=1 if (att_cut or undelivered) else 0)
    print(f"escalate: {checked} role(s) checked, {escalated} escalated"
          + (f", {undelivered} UNDELIVERED (closed loop)" if undelivered else ""))
    if att_cut:
        print(f"escalate: DEGRADED — the attendance scan was cut by its "
              f"wall-clock budget (COORD_ATTENDANCE_SCAN_BUDGET) after "
              f"{_att_scanned}/{_att_total} review dirs, so every vacancy call "
              f"above rests on less evidence than even the count cap allows. "
              f"UNKNOWN, not clear.", file=sys.stderr)
    return rc


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
    agent = _identity(args.agent)
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

_ASK_FIELD_WIDTH = 140


def _clip(text: str, width: int = _ASK_FIELD_WIDTH) -> str:
    """Clip to WIDTH, marking the cut. A silently-truncated ask reads as a
    complete one, so the operator cannot tell 'that is the whole question' from
    'the rest is in the store'."""
    text = str(text or "").strip()
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def _derived_unlock(row: dict[str, Any]) -> bool:
    """True when `unlock` is the echo `cmd_task_block` synthesises for --on-user
    asks (`answer from <who>`) rather than an unlock the author wrote. Printing
    that back adds a line and no information."""
    blocked_on = str(row.get("blocked_on") or "").strip()
    who = blocked_on[len(query._USER_PREFIX):] if blocked_on.startswith(query._USER_PREFIX) else blocked_on
    return str(row.get("unlock") or "").strip() == f"answer from {who}".strip()


def cmd_asks(args: argparse.Namespace, transport: Any) -> int:
    # Public-read failure contract (see _read_degraded_row): an UNKNOWN index must
    # not read as "nothing waiting on the human".
    rows, ok, reason = _load_rows_status(transport, args.team)
    got = query.asks(rows, now=_iso(_now()), human=args.human or _human())
    # Contract 2 (OC2/OC3, ladder PR 3): envelope seals first, rc follows its
    # health in BOTH modes — an unreadable index is UNKNOWN rc 3, never a
    # clean-empty "nothing waiting on the human" at rc 0.
    out = [_read_degraded_row(reason)] + got if not ok else got
    envelope, rc = class_a_envelope(out, source_type="asks-source")
    if args.json:
        jsonutil.print_json(envelope)
        return rc
    if not ok:
        _surface_read_degraded(reason, json_mode=False)
    print(f"asks — {len(got)} waiting on {args.human or _human()} (oldest first)")
    for r in got:
        age = "?" if r.get("age_hours") is None else f"{r['age_hours']:g}h"
        print(f"  [{age:>6}] [{r.get('priority')}] {r.get('title')}")
        ask = str(r.get('blocked_on') or r.get('next_action') or '').strip()
        if ask:
            print(f"           ask: {_clip(ask)}")
        # `unlock` gets its OWN line rather than joining the `or` chain above:
        # `task block` requires blocked_on, so it is never falsy on a blocked row
        # and an `ask or unlock` fallback would be unreachable for exactly the
        # rows it is meant to rescue. The two answer different questions — who it
        # waits on vs what clears it — so the operator needs both.
        unlock = str(r.get('unlock') or '').strip()
        if unlock and not _derived_unlock(r):
            print(f"           unlock: {_clip(unlock)}")
        print(f"           slug: {r.get('name')}  owner: {r.get('owner')}")
    return rc


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


# --- bus-v3 tag provisioning (timeline identity) ----------------------------


def _tag_recipe(dimension: str, name: str) -> str:
    """The exact commands a human runs when the engine cannot create a tag.

    Printed, never guessed at: an agent that cannot provision must be able to
    hand a person something that works verbatim, and then record the result
    with the matching ``--tag-id-<dimension>``.
    """
    return "\n".join([
        f"  # {dimension}: create the tag "
        "(409 means it already exists — list and reuse):",
        "  TOKEN=$(fulcra-api auth print-access-token)",
        "  curl -sS -X POST https://api.fulcradynamics.com/user/v1alpha1/tag \\",
        "    -H \"Authorization: Bearer $TOKEN\" -H 'Content-Type: application/json' \\",
        f"    -d '{{\"name\": \"{name}\"}}'",
        "  # (list them all: curl -sS "
        "https://api.fulcradynamics.com/user/v1alpha1/tag \\",
        "  #    -H \"Authorization: Bearer $TOKEN\")",
        "  # then record the uuid it returns:",
        "  coord-engine bus-v3 tag-provision <team> --agent <name> "
        f"--tag-id-{dimension} <uuid>",
    ])


def _tag_declarations(args: argparse.Namespace, agent: str,
                      entry: dict) -> "list[tuple[str, Optional[str], Optional[str]]]":
    """Which dimensions this invocation is provisioning.

    Each item is ``(dimension, declared_value, explicit_uuid)``. ``agent`` is
    always in play (it is the identity); the other three appear only when the
    caller declares them or supplies a uuid for them. That is what makes a
    model switch ``--model <new>`` and nothing else: undeclared dimensions are
    left exactly as the registry already has them, never blanked.
    """
    out = []
    for dim in bus_tags.DIMENSIONS:
        explicit = getattr(args, f"tag_id_{dim}", None)
        declared = agent if dim == "agent" else getattr(args, dim, None)
        if dim == "agent" and not explicit and entry.get("agent"):
            continue  # already recorded; re-resolving would just cost a call
        if explicit or (declared and (dim != "agent" or not entry.get("agent"))):
            out.append((dim, declared, explicit))
    return out


def cmd_bus_v3_tag_provision(args: argparse.Namespace, transport: Any) -> int:
    """Register an identity's timeline tags — agent, platform, harness, model.

    rc 0 when every requested dimension is recorded (or already was), 2
    otherwise. Partial progress is still WRITTEN before a nonzero exit: a tag
    that exists but is unrecorded is the one state that leads to a duplicate
    tag on the retry, so recording what resolved is strictly safer than
    discarding it.

    The registry itself is NEVER created or repaired here: an absent one means
    the team has not adopted tagging (a cutover decision, made once by a human,
    documented in docs/coord/BUS-V3.md), and a malformed one is evidence a
    person must read. Both print what to do and refuse — an engine that writes
    over durable bytes it could not parse destroys the only copy of the
    mistake.

    MODEL IS A DECLARATION. The engine cannot see which model drives it, so
    ``--model`` is taken on trust; a wrong one is a presence-integrity bug and
    the fix is to re-provision, which is cheap and rewrites only that
    dimension.
    """
    agent = _declared_identity(args.agent)
    if not agent:
        print("tag-provision: no agent identity (--agent or "
              "FULCRA_COORD_AGENT)", file=sys.stderr)
        return 2
    path = bus_tags.tags_path(args.team)
    registry, status = bus_tags.load_registry(transport, args.team,
                                              use_cache=False)
    if status == "error":
        print(f"tag-provision: UNKNOWN — {path} could not be read; retry when "
              "the store is reachable", file=sys.stderr)
        return 2
    if status == "absent":
        print(f"tag-provision: ABSENT — {path} does not exist. This team has "
              "not adopted identity tagging; seed the registry first (see "
              "docs/coord/BUS-V3.md, \"Setup (once per account)\"), then "
              "re-run.", file=sys.stderr)
        return 2
    if status != "ok" or registry is None:
        print(f"tag-provision: INVALID — {path} exists but does not parse as "
              f"{bus_tags.SCHEMA}. A human must fix the bytes; this verb will "
              "not recreate them.", file=sys.stderr)
        return 2

    agents = {name: dict(entry) for name, entry in registry["agents"].items()}
    entry = agents.get(agent, {})
    wanted = _tag_declarations(args, agent, entry)
    if not wanted:
        have = ", ".join(f"{d}={entry[d]}" for d in bus_tags.DIMENSIONS
                         if d in entry)
        print(f"tag-provision: {agent} already registered ({have}); declare "
              "--platform/--harness/--model to add or update a dimension")
        return 0

    # VALIDATE EVERY EXPLICIT UUID BEFORE CREATING ANYTHING. Rejecting a bad
    # --tag-id mid-loop would abandon tags that earlier iterations had already
    # created on the account: they exist, nothing records them, and the retry
    # mints duplicates. An argument error is knowable with zero side effects,
    # so it is settled before the first side effect.
    for dim, _declared, explicit in wanted:
        if explicit and not bus_tags.is_uuid(explicit):
            print(f"tag-provision: --tag-id-{dim} {explicit!r} is not a uuid "
                  "(record tags are uuids, never names); nothing was created",
                  file=sys.stderr)
            return 2

    ensure = getattr(transport, "tag_ensure", None)
    resolved: dict[str, str] = {}
    failures: list[str] = []
    for dim, declared, explicit in wanted:
        if explicit:
            resolved[dim] = explicit.strip()
            continue
        name = bus_tags.tag_name(dim, declared)
        tag_id = ensure(name) if callable(ensure) else None
        if not tag_id or not bus_tags.is_uuid(tag_id):
            failures.append(dim)
            print(f"tag-provision: cannot create the tag {name!r} from here. "
                  "Run this, then record the uuid:", file=sys.stderr)
            print(_tag_recipe(dim, name), file=sys.stderr)
            continue
        resolved[dim] = tag_id.strip()

    if resolved:
        entry.update(resolved)
        agents[agent] = entry
        if not transport.write(
                path, bus_tags.render_registry(registry["base"], agents)):
            hint = " ".join(f"--tag-id-{d} {t}" for d, t in resolved.items())
            print(f"tag-provision: the registry write to {path} did not land "
                  f"— the tags exist but are NOT recorded; re-run with {hint}",
                  file=sys.stderr)
            return 2
        bus_tags.cache_clear()
        print(f"tag-provision: {agent} -> "
              + ", ".join(f"{d}={resolved[d]}" for d in bus_tags.DIMENSIONS
                          if d in resolved)
              + f" (channel tag {registry['base']} rides every event)")

    missing = [d for d in bus_tags.DIMENSIONS if d not in entry]
    if missing and not failures:
        print(f"tag-provision: {agent} has no {'/'.join(missing)} tag — its "
              "events stay filterable by the dimensions it does have; declare "
              "the rest whenever you like")
    return 2 if failures else 0


def cmd_bus_v3_send(args: argparse.Namespace, transport: Any) -> int:
    """Write ONE bus event — the supported hand-send, tagged like every other.

    WHY THIS VERB EXISTS. ``tell``/``respond``/``remind`` cover the directive
    workflow, but the bus also carries bare events (a `claim` announcing you
    are on the bus, a `verdict`, a demo `directive`), and the documentation
    taught those as a raw ``fulcra-api record`` pipe. A raw pipe cannot read
    ``tags.json``, so every hand-sent event stayed untagged no matter how
    carefully its sender had provisioned — the documented path defeated the
    feature. This is that same write, through ``records.emit_event``, which is
    where tagging lives.

    Fail-closed on the stream: no records config means the event has nowhere
    to go that is certainly right, and guessing a stream is worse than not
    writing. rc 0 written, 2 otherwise.
    """
    sender = _declared_identity(args.sender)
    if not sender:
        print("send: no agent identity (--from or FULCRA_COORD_AGENT)",
              file=sys.stderr)
        return 2
    if args.kind == "directive":
        print("send: NOTE - a hand-sent directive creates NO task row, so it "
              "will NOT appear in the recipient needs-me and no obligation "
              "is tracked. Use tell if you are genuinely asking for work.",
              file=sys.stderr)
    cfg, cfg_status = records.load_config_classified(transport, args.team)
    if cfg is None:
        detail = {
            "error": "the records config could not be READ (transport "
                     "failure) — retry when the store is reachable",
            "invalid": "the records config is malformed — a human fixes the "
                       "bytes; retrying will not",
        }.get(cfg_status,
              f"no bus-v3 records config for team {args.team} "
              f"(team/{args.team}/{records.CONFIG_NAME})")
        print(f"send: {detail}", file=sys.stderr)
        return 2
    try:
        written = records.emit_event(
            transport, cfg, sender=sender, to=args.to, kind=args.kind,
            priority=args.priority, slug=args.slug, ptr=args.ptr,
            team=args.team)
    except ValueError as e:   # unknown kind/priority — fails AT the write
        print(f"send: {e}", file=sys.stderr)
        return 2
    if not written:
        print("send: the record did NOT land", file=sys.stderr)
        return 2
    print(f"send: {args.kind} {args.slug} -> {args.to} "
          f"(from {sender}; readable in their queue in ~20s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coord-engine", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def add_json(sp):
        sp.add_argument("--json", action="store_true", help="emit JSON")

    r = sub.add_parser(
        "reconcile",
        help="feed-fold + heal a team's task views (full-scan fallback)",
    )
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
    nm.add_argument("--envelope-only", action="store_true",
                    help="print ONLY the stderr verdict envelope (count, fold "
                         "sources, degraded count, rc) and no records — for a "
                         "harness whose context truncates an unbounded payload "
                         "before its trailing markers")
    add_json(nm)
    nm.set_defaults(func=cmd_needs_me)

    ow = sub.add_parser(
        "owed", help="open obligations folded from the annotation stream "
                     "(forward from a durable cursor; zero enumeration)")
    ow.add_argument("team")
    ow.add_argument("--agent", "-a")
    ow.add_argument("--json", action="store_true")
    ow.set_defaults(func=cmd_owed)

    ob = sub.add_parser("obligations",
                        help="terminal answer: does this agent owe work?")
    ob.add_argument("team"); ob.add_argument("--agent", required=True)
    ob.add_argument("--seed-partial", action="store_true",
                    help="allow --seed-checkpoint to proceed when stream-served "
                         "components are degraded, writing their names into the "
                         "checkpoint as permanently-surfaced UNKNOWNs")
    ob.add_argument("--seed-checkpoint", action="store_true",
                    help="run the corpus fold ONCE and write its open set as "
                         "the checkpoint the --stream fold starts from; the "
                         "sanctioned use of the enumerating fold")
    ob.add_argument("--repair-unknown", action="store_true",
                    help="re-probe ONLY the components the checkpoint carries "
                         "as UNKNOWN and clear the ones that now read, merging "
                         "their owed rows in; breaks the deadlock where the "
                         "only way to clear an UNKNOWN was the corpus walk "
                         "that cannot finish. Off the wake path.")
    ob.add_argument("--stream", action="store_true",
                    help="follow the signal to the doc: read this agent's "
                         "channel forward from its cursor and open only the "
                         "documents the events' ptr names, instead of folding "
                         "the whole task corpus")
    ob.add_argument("--lookback-hours", type=int, default=168,
                    help="window floor for --stream (default 168h); the window "
                         "is the OLDER of the cursor and this floor")
    add_json(ob)
    ob.set_defaults(func=cmd_obligations_dispatch)

    sc = sub.add_parser("search", help="substring search over tasks")
    sc.add_argument("team"); sc.add_argument("query"); add_json(sc)
    sc.add_argument("--archived", action="store_true", help="also search the cold archive")
    sc.set_defaults(func=cmd_search)

    rl = sub.add_parser("roles", help="role status fold (fulcra-agent-roles)")
    rlsub = rl.add_subparsers(dest="roles_command", required=True)
    rst = rlsub.add_parser("status", help="HELD/VACANT/CONTESTED + escalation-due")
    rst.add_argument("team"); rst.add_argument("role"); add_json(rst)
    rst.add_argument("--check-attendance", action="store_true",
                     help="scan verdicts for holder work inside the SLA window; a "
                          "lapsed lease alone never proves a role is unattended "
                          "(opt-in: it costs one listing per review)")
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
        sp.add_argument("--fyi", action="store_true",
                        help="this message asks for NOTHING: deliver it, but do "
                             "not open an obligation the recipient can never "
                             "discharge (reports, acks, FYIs)")

    tl = sub.add_parser("tell", help="direct work at an agent (directive = task w/ assignee)")
    tl.add_argument("team"); tl.add_argument("assignee"); tl.add_argument("title")
    tl.add_argument("--closes", metavar="SLUG",
                    help="the directive this reply ANSWERS — closes it with this "
                         "reply as the artifact (exact hash-suffixed slug)")
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
    qu = sub.add_parser("queue", help="bus v3 transactional event delivery (`queue TEAM` stages; `queue commit TEAM --token TOKEN` advances)")
    qu.add_argument("team", help="team name, or literal `commit`")
    qu.add_argument("commit_team", nargs="?",
                    help=argparse.SUPPRESS)
    qu.add_argument("--agent", "-a")
    qu.add_argument("--token",
                    help="delivery token (required by `queue commit TEAM`)")
    qu.add_argument(
        "--result", dest="results", action="append",
        help="commit classification RECORD_ID=completed|blocked|superseded|ignored (repeat for every staged event)")
    qu.add_argument("--peek", action="store_true",
                    help="show events without advancing the cursor (safe diagnostic read)")
    qu.add_argument("--consume", action="store_true",
                    help="advance another agent's cursor deliberately (reading as a non-self identity peeks by default)")
    # DEFAULT OFF — OPT-IN (promise plan T3(b), 2026-08-02), openly reversing
    # the 2026-07-30 default-ON ruling on measured grounds: at the default
    # budget the fold reaches 0/7 components in production, so the default
    # could only ever answer UNKNOWN while charging 3+ listings on every empty
    # wake fleet-wide — a signal with no information at a positive price.
    # The skip is never silent: EVERY machine-readable success envelope carries
    # "obligations": {"state": "not-checked"}. --no-obligations is retained as
    # an accepted no-op alias so existing callers keep parsing. Default-ON
    # returns if an aggregate-fold rewrite ever makes CLEAR reachable.
    # Passing it explicitly ALWAYS folds (round-2 findings 1/2) — an eventful
    # window is not a reason to drop a flag the caller paid for.
    qu.add_argument("--obligations", action=argparse.BooleanOptionalAction,
                    default=False,
                    help="reconcile durable obligations alongside the read "
                         "(an empty queue is not proof nothing is owed); "
                         "OFF by default, and a default read folds nothing — "
                         "the verdict rides the success envelope, it never "
                         "changes the exit code. --no-obligations is the "
                         "(default) no-op alias")
    add_json(qu)
    qu.set_defaults(func=cmd_queue)
    bv = sub.add_parser(
        "bus-v3",
        help="Bus V3 administration: authority migration, tag registry, tagged send")
    bvsub = bv.add_subparsers(dest="bus_v3_command", required=True)
    bvm = bvsub.add_parser(
        "migrate",
        help="classify legacy cursors and upgrade the authority to schema v1",
    )
    bvm.add_argument("team")
    modes = bvm.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true",
                       help="classify and print the plan; write nothing")
    modes.add_argument("--apply", action="store_true",
                       help="apply the schema-v1 authority upgrade")
    bvm.add_argument(
        "--agent", dest="agents", action="append",
        help="include a known agent in cursor proof (repeatable; discovered "
             "agent directories are always included)")
    add_json(bvm)
    bvm.set_defaults(func=cmd_bus_v3_migrate)
    ib = sub.add_parser("inbox", help="open directives for an agent (--ack <slug> to ack)")
    ib.add_argument("team"); ib.add_argument("--agent", "-a"); ib.add_argument("--ack")
    ib.add_argument("--all", action="store_true",
                    help="include acknowledged, closed, future, and @backlog history")
    add_json(ib)
    ib.set_defaults(func=cmd_inbox)
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
    dr.add_argument(
        "--self", action="store_true",
        help="engine currency only: rc 0 current, 3 stale (adopt latest), "
             "2 unknown (no config, or no/malformed authority pin)")
    dr.add_argument(
        "--delivery", action="store_true",
        help="write/read a probe event and prove fleet-readable delivery")
    dr.add_argument("--agent", help="identity for --delivery (or $FULCRA_COORD_AGENT)")
    dr.add_argument(
        "--deadline", type=float, default=90.0,
        help="seconds to wait for the delivery probe (default 90)")
    dr.set_defaults(func=cmd_doctor)

    ac = sub.add_parser(
        "acceptance", help="production acceptance probes across agent identities")
    acsub = ac.add_subparsers(dest="acceptance_command", required=True)
    acp = acsub.add_parser(
        "pair", help="prove delivery, nonce round-trip, park/resume, and join")
    acp.add_argument("team")
    acp.add_argument("--agent", required=True, help="initiating identity A")
    acp.add_argument("--peer", required=True, help="responding identity B")
    acp.add_argument(
        "--timeout", type=float, default=90.0,
        help="seconds allowed for each delivery/queue hop (default 90)")
    acp.add_argument("--nonce", help=argparse.SUPPRESS)
    acp.set_defaults(func=cmd_acceptance_pair)

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
    tdn.add_argument("--agent", "-a", help="closing identity for the close event")
    tdn.set_defaults(func=cmd_task_done)
    tbl = tksub.add_parser("block", help="mark blocked (requires --unlock; sets blocked_on; --on-user routes to a human)")
    tbl.add_argument("team"); tbl.add_argument("name")
    tbl.add_argument("--blocked-on", dest="blocked_on")
    tbl.add_argument("--on-user", dest="on_user", help="human-facing ask; assigns to FULCRA_COORD_HUMAN/human + tags needs:human")
    tbl.add_argument("--unlock", help="REQUIRED: what specifically unblocks this (the concrete unlock, not just the blocker)")
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
    tsp = tksub.add_parser("supersede", help="close a re-dispatched task's origin copy, naming its successor (legal from any live state)")
    tsp.add_argument("team"); tsp.add_argument("name")
    tsp.add_argument("--by", required=True, help="the successor task slug (or PR/artifact) that replaces this copy")
    tsp.add_argument("--record", help="EVENT record id of the superseded predecessor dispatch — joins this supersession to the bus stream so the doctor supersession-adoption metric counts the explicit verb directly")
    tsp.add_argument("--reason", "-r")
    tsp.set_defaults(func=cmd_task_supersede, verb="supersede")

    rv = sub.add_parser("review", help="review verdict tally (fulcra-agent-review)")
    rvsub = rv.add_subparsers(dest="review_command", required=True)
    rvq = rvsub.add_parser("request", help="open a review with required reviewers (durable obligation)")
    rvq.add_argument("team"); rvq.add_argument("name", help="slug or title")
    rvq.add_argument("--of", required=True, help="artifact under review (PR url or description)")
    rvq.add_argument(
        "--head",
        help="exact 40- or 64-hex commit SHA; re-requesting the same PR slug "
             "with a new head advances its append-only review round",
    )
    rvq.add_argument("--reviewer", action="append", required=True,
                     help="required reviewer (role preferred); repeat for many")
    rvq.add_argument("--from", dest="sender", help="requesting agent (defaults to host)")
    rvq.set_defaults(func=cmd_review_request)
    rvs = rvsub.add_parser("status", help="APPROVED/CHANGES/PENDING from reviewers' verdicts")
    rvs.add_argument("team"); rvs.add_argument("slug"); add_json(rvs)
    rvs.set_defaults(func=cmd_review_status)
    rvg = rvsub.add_parser("gc", help="retire register entries that can never settle (DRY RUN by default)")
    rvg.add_argument("team")
    rvg.add_argument("--apply", action="store_true",
                     help="actually write the .gc-closed markers; without it "
                          "gc only prints what it would retire")
    rvg.add_argument("--repo", default=None, metavar="OWNER/REPO",
                     help="assert which repository this checkout speaks for. "
                          "Only reviews whose head lives in THIS repo can be "
                          "witnessed absent; everything else is UNKNOWN and is "
                          "never retired. Defaults to the origin remote.")
    rvg.add_argument("--from", dest="sender", help="acting agent (for the marker)")
    rvg.set_defaults(func=cmd_review_gc)
    rvw = rvsub.add_parser(
        "residue",
        help="close review-request rows whose review already reached a "
             "terminal state (DRY RUN by default)")
    rvw.add_argument("team")
    rvw.add_argument("--apply", action="store_true",
                     help="actually close the rows; without it sweep only "
                          "prints what it would close")
    rvw.add_argument("--agent", "-a", default=None,
                     help="closing identity for the close events")
    rvw.set_defaults(func=cmd_review_residue)
    rvv = rvsub.add_parser(
        "verdict", help="file YOUR verdict for a review round (writes the same "
                        "canonical shard `review request` prints)")
    rvv.add_argument("team")
    rvv.add_argument("name", help="review slug")
    rvv.add_argument("--head", help="exact head this verdict is pinned to")
    rvv.add_argument("--verdict", required=True,
                     help="approve | changes")
    rvv.add_argument("--note", help="the body of the verdict")
    rvv.add_argument("--from", dest="sender", help="reviewer identity")
    rvv.set_defaults(func=cmd_review_verdict)

    rvc = rvsub.add_parser("close", help="close a review because its PR MERGED (evidence, not inference)")
    rvc.add_argument("team"); rvc.add_argument("slug")
    rvc.add_argument("--merge-sha", required=True,
                     help="the FULL 40- or 64-hex merge commit sha — closure "
                          "carries evidence; an abbreviation is an assertion")
    rvc.add_argument("--merged-at", help="ISO timestamp of the merge (defaults to now)")
    rvc.add_argument("--reason", help="why this row is closed")
    rvc.add_argument("--from", dest="sender", help="acting agent (for the marker)")
    rvc.set_defaults(func=cmd_review_close)
    rvn = rvsub.add_parser(
        "conclude",
        help="mark a reviewed row terminal when no merge evidence exists (NOT .settled)")
    rvn.add_argument("team"); rvn.add_argument("slug")
    rvn.add_argument("--reason", help="why this row concluded without merge evidence")
    rvn.add_argument("--from", dest="sender", help="acting agent (for the marker)")
    rvn.set_defaults(func=cmd_review_conclude)
    rvr = rvsub.add_parser("restore", help="move an archived settled-single review back to the hot path")
    rvr.add_argument("team"); rvr.add_argument("slug")
    rvr.set_defaults(func=cmd_review_restore)

    ro = sub.add_parser(
        "router",
        help="wake-router feed-first decision plane + host-local executor",
    )
    rosub = ro.add_subparsers(dest="router_command", required=True)
    ror = rosub.add_parser(
        "run",
        help=("fold feed changes by cursor, execute cloud adapters, and enqueue "
              "host-local wakes (fixed 60s cadence; --once for one pass)"),
    )
    ror.add_argument("team")
    ror.add_argument(
        "--once", action="store_true",
        help="one pass then exit; resident mode uses an anchored fixed-rate "
             "cadence compatible with the duty gate, while externally "
             "scheduled --once inherits that scheduler's throttle semantics")
    ror.add_argument("--shadow", action="store_true", help="W7 read-only shadow mode: log + persist a decision per directed item, enqueue and execute NOTHING (the >=48h acceptance measurement)")
    ror.add_argument("--state-prefix", default=None, metavar="NAME", help="relocate the router's own cursor-tracked state to the sibling team/<team>/_coord/router-<NAME>/ (default: canonical router/; env COORD_ROUTER_STATE_PREFIX is the fallback). Lets one host run live delivery and a shadow measurement in parallel without a shared-cursor collision. Config stays shared/canonical. Charset [A-Za-z0-9_.-]+.")
    add_json(ror)
    ror.set_defaults(func=cmd_router_run)
    rosh = rosub.add_parser("shadow", help="W7 shadow-window control (arm/status the >=48h read-only acceptance window)")
    roshsub = rosh.add_subparsers(dest="shadow_command", required=True)
    rosha = roshsub.add_parser("arm", help="write the shadow-window marker (records started_at; activates the fleet-wide delivery probes)")
    rosha.add_argument("team")
    rosha.add_argument("--min-hours", type=int, default=48, help="minimum window length before acceptance (default 48)")
    rosha.set_defaults(func=cmd_router_shadow_arm)
    roshs = roshsub.add_parser("status", help="report the shadow-window marker (armed?, started_at, elapsed vs min_hours)")
    roshs.add_argument("team")
    roshs.set_defaults(func=cmd_router_shadow_status)
    roshr = roshsub.add_parser(
        "report",
        help="fold the armed W7 window into a fail-closed acceptance verdict")
    roshr.add_argument("team")
    roshr.add_argument("--state-prefix", default=None, metavar="NAME", help="read the router's own decision state (shadow-decisions/, shadow-marks/) from the sibling router-<NAME>/ namespace; the window marker and delivery evidence stay canonical (env COORD_ROUTER_STATE_PREFIX is the fallback)")
    add_json(roshr)
    roshr.set_defaults(func=cmd_router_shadow_report)
    roe = rosub.add_parser("execute", help="W5.5 thin host executor: fire host-local adapters for queue entries resolved to THIS host (policy-free; --once for one pass)")
    roe.add_argument("team")
    roe.add_argument("--host", default=None, help="executor id to drain (default: this host's own id)")
    roe.add_argument("--once", action="store_true", help="one pass then exit (default: resident loop)")
    roe.add_argument("--dry-run", action="store_true", help="select + report only; invoke nothing, write nothing")
    roe.add_argument("--state-prefix", default=None, metavar="NAME", help="drain the queue from the sibling router-<NAME>/ namespace (pairs with a namespaced `router run`; env COORD_ROUTER_STATE_PREFIX is the fallback). Config stays shared/canonical.")
    add_json(roe)
    roe.set_defaults(func=cmd_router_execute)

    wk = sub.add_parser(
        "wake", help="zero-model wake adapter utilities (queued file / SessionStart)")
    wksub = wk.add_subparsers(dest="wake_command", required=True)
    wkq = wksub.add_parser(
        "queue-file", help="write an idempotency-keyed local SessionStart nudge")
    wkq.add_argument("team"); wkq.add_argument("--agent", required=True)
    wkq.add_argument("--key", required=True)
    wkq.set_defaults(func=cmd_wake_queue_file)
    wkc = wksub.add_parser(
        "consume", help="consume this identity's queued SessionStart nudges")
    wkc.add_argument("team"); wkc.add_argument("--agent", required=True)
    wkc.set_defaults(func=cmd_wake_consume)

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
    ctp = ctsub.add_parser("park", help="session-exit: snapshot held roles + set checkpoint_refs")
    ctp.add_argument("team"); ctp.add_argument("--agent", "-a"); ctp.add_argument("--objective")
    ctp.add_argument("--role", help="snapshot only this role (must have a fresh lease)")
    ctp.add_argument("--next", action="append"); ctp.add_argument("--open-question", action="append", dest="open_question")
    ctp.add_argument("--decision", action="append", dest="decision")
    ctp.add_argument("--artifact", action="append", dest="artifact",
                     help="cold-start reading list entry; 'path#anchor' or "
                          "'path (Anchor section)' has its anchor checked too")
    ctp.add_argument("--handoff", action="store_true",
                     help="enforce the five-section handoff form "
                          "(docs/coord/CHECKPOINT-HANDOFF.md) and REFUSE to "
                          "write anything if it is not resumable")
    ctp.set_defaults(func=cmd_continuity_park)

    ctr = ctsub.add_parser("resume", help="print a resume brief from the latest snapshot")
    ctr.add_argument("team"); ctr.add_argument("agent"); ctr.add_argument("task", nargs="?")
    ctr.add_argument("--max-age", metavar="DURATION",
                     help="exit 2 unless checkpoint age is at most DURATION (for example 30m, 12h, 2d)")
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

    # tag-provision/send attach to the bus-v3 subparser created above (a second
    # add_parser("bus-v3") is an argparse ArgumentError — PRs 515+524 each
    # created one green in isolation and broke build_parser on their union)
    bvt = bvsub.add_parser(
        "tag-provision",
        help="register an identity's timeline tags (agent/platform/harness/"
             "model) in _coord/bus-v3/tags.json")
    bvt.add_argument("team")
    bvt.add_argument("--agent", "-a",
                     help="identity to provision (default FULCRA_COORD_AGENT)")
    bvt.add_argument("--platform",
                     help="platform declaration, e.g. claude-code")
    bvt.add_argument("--harness", help="harness declaration, e.g. ccr")
    bvt.add_argument("--model",
                     help="model declaration, e.g. opus-5 — DECLARED, not "
                          "detectable; re-provision when it changes")
    for _dim in ("agent", "platform", "harness", "model"):
        bvt.add_argument(f"--tag-id-{_dim}", dest=f"tag_id_{_dim}",
                         help=f"record an EXTERNALLY created {_dim} tag uuid "
                              "instead of creating one")
    bvt.set_defaults(func=cmd_bus_v3_tag_provision)
    bvs = bvsub.add_parser(
        "send",
        help="write ONE bus event (the supported hand-send — identity-tagged, "
             "unlike a raw `fulcra-api record` pipe)")
    bvs.add_argument("team")
    bvs.add_argument("--to", required=True,
                     help="recipient agent name, or `all`")
    bvs.add_argument("--kind", required=True, choices=list(records.KINDS))
    bvs.add_argument("--slug", required=True,
                     help="short kebab-case identity for the exchange")
    bvs.add_argument("--priority", "-p", default="P2",
                     choices=["P0", "P1", "P2", "P3"])
    bvs.add_argument("--ptr",
                     help="team-relative File Store path of the document, "
                          "when there is a body worth reading")
    bvs.add_argument("--from", dest="sender",
                     help="sending identity (default FULCRA_COORD_AGENT)")
    bvs.set_defaults(func=cmd_bus_v3_send)
    return p


# --- W1.5 activity-implies-liveness -----------------------------------------
#
# Every engine bus WRITE verb refreshes the ACTOR's presence timestamp at the
# single dispatch chokepoint below, so no verb can be missed and none has to
# opt in. The set is keyed on the command FUNCTIONS themselves (not on parsed
# subcommand strings). See AGENTS.md, "Activity implies liveness".
# THIS IS A DENYLIST, and it must stay one. It was an ALLOWLIST of thirteen
# functions, which cannot keep the promise the paragraph above makes: a verb
# added later is simply absent, silently, and absence here is indistinguishable
# from "this agent is not working". Twenty write verbs had accumulated outside
# it — `review close`, `escalate`, `continuity snapshot`/`park`, `roles claim`/
# `release`, `answer`, `bus-v3 send` and `stash push` among them.
#
# Measured cost (2026-08-09, live store): codex-reviewer rendered
# "stale 6d — nudge" having filed a verdict 3.5h earlier; coord-opus-worker
# rendered "stale 42h — nudge" having filed a report 4.8h earlier. An agent
# whose job IS reviewing — file a verdict, close a review, claim its role, save
# continuity — performs none of the thirteen blessed verbs, so it rendered dark
# while working. The roster attaches an IMPERATIVE to that judgement ("nudge"),
# so the failure does not merely mislabel: it dispatches people.
#
# Inverting makes the default SAFE. A new write verb counts as activity without
# anyone remembering; a new READ verb has to be named here, which is a decision
# someone makes on purpose rather than an omission nobody notices. Reads must
# stay out — looking at the board is not evidence that any work happened.
#: Read verbs DEFINED IN THIS MODULE. The set is completed at module end, once
#: the extracted command modules are imported — see the note down there; that
#: ordering is load-bearing, not cosmetic.
_ACTIVITY_READ_FUNCS: frozenset = frozenset({
    cmd_obligations_dispatch, cmd_obligations_stream,
    # NOT cmd_obligations_repair: it exists to MUTATE the checkpoint, so
    # running it is activity. The stream fold's checkpoint advance is
    # incidental to a read; this verb's write is the whole point.
    cmd_status, cmd_board, cmd_search, cmd_needs_me, cmd_owed, cmd_briefing,
    cmd_presence_show, cmd_review_status, cmd_health, cmd_doctor,
    cmd_obligations, cmd_roles_status, cmd_continuity_resume,
    cmd_agents, cmd_asks, cmd_engagement_gate, cmd_stash_list,
    cmd_router_shadow_status,
    # `presence beat` is W1's own write of this very shard; routing it through
    # the activity path would double-write and let the throttle memo suppress a
    # deliberate beat.
    cmd_presence_beat,
})


#: Handlers that serve BOTH a read and a write operation, so the function alone
#: cannot classify the invocation. Each maps to a predicate over the PARSED
#: ARGS: true when this particular invocation wrote something.
#:
#: codex-reviewer, 590 r2: classification keyed only by handler silently
#: mis-answered three real commands — `queue commit` (a durable classification
#: record) did not count as activity, while `inbox` and `digest` refreshed
#: presence merely by being VIEWED. Both directions wrong, in the same table,
#: for the same structural reason: one function object, two operations.
#:
#: `queue --consume` is here too, and codex did not name it. It deliberately
#: advances ANOTHER agent's cursor — a mutation of someone else's state, not
#: bookkeeping of your own read — so it belongs on the write side. Shipping the
#: three that were reported while leaving its neighbour is the exact habit that
#: produced this review round.
_MIXED_MODE_ACTIVITY: dict[Any, Any] = {
    # `queue TEAM` reads (its own cursor advance is bookkeeping of that read);
    # `queue commit TEAM` records classifications; `--consume` moves another
    # agent's cursor.
    cmd_queue: lambda a: (getattr(a, "commit_team", None) is not None
                          or bool(getattr(a, "consume", False))),
    # `inbox TEAM` views; `inbox TEAM --ack SLUG` acknowledges.
    cmd_inbox: lambda a: bool(getattr(a, "ack", None)),
    # `digest TEAM` views; `--store` and `--emit-timeline` BOTH persist, so this
    # defers to the same `_digest_persists` the command branches on.
    cmd_digest: lambda a: _digest_persists(a),
}


def _digest_persists(args: Any) -> bool:
    """Does this `digest` invocation enter the PERSISTENT branch?

    ONE definition, called by `cmd_digest` itself and by the activity
    classifier. It was two: the command branched on `store or emit_timeline`
    while `_MIXED_MODE_ACTIVITY` checked only `store`, so `digest --emit-timeline`
    wrote the digest marker (and possibly the emitted marker) while classifying
    as a READ — the actor did durable work and rendered dark for it
    (codex-reviewer, 590 r3). Two copies of one condition is how that drift
    happened, so there is now one copy and both callers read it.
    """
    return bool(getattr(args, "store", False)
                or getattr(args, "emit_timeline", False))


def record_activity_artifact(args: Any, path: str) -> None:
    """Tell the chokepoint which document this invocation wrote.

    Attached to the PARSED ARGS, not a module-global map keyed by ``id(args)``.
    That map leaked on the exception path — `main` returns from its handler
    before any cleanup, so a failed command left its entry behind and a later
    object at the same id could inherit the wrong artifact path
    (codex-reviewer, 594 r1). State that lives on the object dies with it; there
    is no cleanup to forget.
    """
    if path:
        setattr(args, "_activity_artifact_path", path)


#: Per-agent work EVENTS. One immutable file per event, never overwritten.
#:
#: This was a single mutable `LATEST-work.json` with a read-compare-write
#: "monotonic" guard, and codex-reviewer reproduced two races against it (594
#: r1): two hosts both read the old value and the OLDER timestamp could land
#: last; worse, the failed-write branch deleted the shared path unconditionally
#: and could erase a NEWER pointer another host had just written. The store
#: offers no conditional or versioned write, so there is no safe way to mutate
#: one shared path — the fix is not to have one.
#:
#: Immutable events have no race to lose: a writer only ever CREATES its own
#: event, a failed write leaves every prior event intact and still true, and
#: "newest" is a deterministic fold over names rather than a value someone must
#: defend. The filename leads with the ISO instant, so lexical max IS newest
#: (our own naming, so unlike the store's 12-hour mtimes it sorts correctly).
WORK_EVENTS_DIR = "work"

#: Keep the newest few events per agent; older ones are unreachable by the fold.
#: Pruning only ever removes entries STRICTLY OLDER than one just written, which
#: is safe concurrently — the worst case is that another host already removed it.
WORK_EVENTS_KEEP = 5


def _work_events_prefix(team: str, agent: str) -> str:
    """Events live under the RAW agent name — NOT `tasks.agent_key()`, which
    appends a hash (`coord-maintainer` -> `coord-maintainer-f68406`). That is
    right for the PRESENCE namespace and wrong here: `_coord/agents/<agent>/`
    uses raw names on the live store. Keying by the hashed form would file every
    event where no reader lists — a silent no-op that fixtures written with the
    same helper would happily agree with."""
    return f"team/{team}/_coord/agents/{agent}/{WORK_EVENTS_DIR}/"


def _work_event_name(now_iso: str, kind: str, path: str) -> str:
    """`<iso>-<digest>.json`. The digest makes concurrent DISTINCT events at the
    same instant distinct files, while two hosts recording the SAME event write
    the same name with the same bytes — idempotent rather than conflicting."""
    digest = hashlib.sha1(f"{kind}|{path}".encode()).hexdigest()[:8]
    return f"{now_iso}-{digest}.json"


def _read_work_pointer(transport: Any, team: str, agent: str) -> Optional[dict]:
    """Newest work event for one agent, or ``None`` meaning UNKNOWN.

    UNKNOWN covers no events yet (the whole fleet on day one), an unreadable
    listing, and a corrupt newest event. None of them licenses "this agent did
    nothing" — the caller falls back to the sweep. Never raises: one bad agent
    must not break the fold for the rest.
    """
    try:
        rows = transport.list_dir(_work_events_prefix(team, agent))
    except TransportError:
        return None
    names = sorted(str(r.get("name") or "") for r in (rows or [])
                   if str(r.get("name") or "").endswith(".json"))
    if not names:
        return None
    try:
        raw = transport.read(_work_events_prefix(team, agent) + names[-1])
    except TransportError:
        return None
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return doc if isinstance(doc, dict) and doc.get("ts") else None


def _prune_work_events(transport: Any, team: str, agent: str) -> None:
    """Best-effort: drop all but the newest ``WORK_EVENTS_KEEP`` events.

    Only ever deletes entries strictly older than ones being kept, so a
    concurrent writer's newest event is never at risk. Failure is ignored —
    stale extra events cost storage, never correctness.
    """
    try:
        rows = transport.list_dir(_work_events_prefix(team, agent)) or []
        names = sorted(str(r.get("name") or "") for r in rows
                       if str(r.get("name") or "").endswith(".json"))
        for stale in names[:-WORK_EVENTS_KEEP]:
            transport.delete(_work_events_prefix(team, agent) + stale)
    except Exception:
        pass


def _stamp_work_pointer(transport: Any, team: str, agent: str, *, kind: str,
                        path: str, now_iso: str) -> bool:
    """Record one work event. True iff it persisted.

    Called only after the command succeeded (588): an event for work that did
    not land would be a lie. There is NO delete-on-failure — the old design
    removed a shared mutable pointer and could erase another host's newer one.
    A failed write here simply leaves the previous events, which are all still
    true; the reader sees slightly stale rather than wrong, and that is the
    right way to be wrong.
    """
    prefix = _work_events_prefix(team, agent)
    body = json.dumps({"schema": "work-event/v1", "agent": agent,
                       "kind": kind, "path": path, "ts": now_iso},
                      sort_keys=True)
    try:
        wrote = transport.write(prefix + _work_event_name(now_iso, kind, path),
                                body)
    except TransportError:
        wrote = False
    if not wrote:
        print(f"work event not recorded for {agent} — earlier events stand and "
              f"remain true; the reader will read slightly stale, never wrong",
              file=sys.stderr)
        return False
    _prune_work_events(transport, team, agent)
    return True


def _is_activity_invocation(args: Any) -> bool:
    """Does THIS invocation count as evidence the actor is working?

    Classification is per-OPERATION, not per-function: a mixed handler is
    resolved by its parsed args before the read/write default applies. The
    default remains "counts", because its failure mode is an agent rendered dark
    while doing exactly its job — but a declared read, or the read branch of a
    mixed command, must never manufacture liveness out of looking at a view.
    """
    func = getattr(args, "func", None)
    if func is None:
        return False
    predicate = _MIXED_MODE_ACTIVITY.get(func)
    if predicate is not None:
        return bool(predicate(args))
    return func not in _ACTIVITY_READ_FUNCS


def _is_activity_refresh_func(func: Any) -> bool:
    """Function-only view of the rule, for callers with no parsed args.

    UNSAFE for a mixed handler — it cannot see which operation ran — so it
    answers False for those rather than guessing a direction.
    """
    if func is None or func in _MIXED_MODE_ACTIVITY:
        return False
    return func not in _ACTIVITY_READ_FUNCS


class _ActivityRefreshFuncs:
    """Membership seam for the coverage regression, so a test can ask "does this
    verb refresh?" without reaching into dispatch. Membership is COMPUTED from
    the denylist rather than curated — a curated answer here would re-create the
    very bug the test exists to catch."""

    def __contains__(self, func: Any) -> bool:
        return _is_activity_refresh_func(func)


ACTIVITY_REFRESH_FUNCS = _ActivityRefreshFuncs()

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
              "agent": actor, "timestamp": now_iso,
              "engine": records.engine_stamp()}
        transport.write(shard_path, okf.render_frontmatter(fm) + f"\n# Presence: {actor}\n")
    except Exception as e:
        print(f"presence activity-refresh failed: {e}", file=sys.stderr)


def _v2_public_read_sections(func: Any) -> Optional[tuple[str, ...]]:
    """Required generation sections for each migrated public fold.

    The tuple is documentation as executable policy: every function in the
    Unit 5 migration list enters one shared read before its domain renderer.
    Empty/``None`` are distinct — ``None`` means this is not a public fold.
    """
    return {
        cmd_status: ("tasks",),
        cmd_board: ("tasks",),
        cmd_needs_me: ("tasks", "reviews", "forge", "roles", "presence"),
        cmd_search: ("tasks",),
        cmd_inbox: ("tasks", "roles"),
        cmd_digest: ("tasks", "presence"),
        cmd_asks: ("tasks",),
        cmd_review_status: ("reviews",),
        cmd_roles_status: ("roles", "presence"),
        cmd_presence_show: ("presence",),
        cmd_briefing: generation.REQUIRED_SECTIONS,
    }.get(func)


def _begin_v2_public_read(
    args: argparse.Namespace, transport: Any,
) -> Optional[public_read.PublicReadResult]:
    """Enter Unit 5 only when the transport explicitly activates v2 reads.

    Mixed-fleet/test transports without the capability keep the established v1
    compatibility path. The deployed transport declares this capability; Unit
    6 owns flipping its fleet-verified epsilon to true during activation.
    """
    sections = _v2_public_read_sections(getattr(args, "func", None))
    if sections is None or getattr(transport, "public_read_v2_enabled", None) is not True:
        return None
    result = public_read.read_current(
        transport,
        args.team,
        now=_now(),
        epsilon_seconds=getattr(transport, "public_read_epsilon_seconds", None),
        epsilon_verified=getattr(transport, "public_read_epsilon_verified", False),
        deadline=Deadline.open(getattr(transport, "timeout", 30.0)),
    )
    # Structural section validation happens centrally, but keep the command's
    # declared dependency explicit so a future partial-generation schema cannot
    # accidentally license a fold whose section is absent.
    if result.rc == 0 and any(not isinstance(result.section(name), Mapping)
                              for name in sections):
        return public_read.PublicReadResult(
            outcome_mod.OutcomeState.UNKNOWN,
            result.coverage + (outcome_mod.SurfaceCoverage(
                "required-sections", outcome_mod.CoverageState.UNKNOWN,
                reason="required public-read section absent"),),
            result.sections,
            result.coverage_horizon,
            result.generation,
            result.watermark,
            result.applied_update_ids,
        )
    return result


def _run_v2_public_handler(
    args: argparse.Namespace,
    transport: Any,
    authority: public_read.PublicReadResult,
) -> int:
    """Render domain output and authority metadata from one sealed decision."""
    if authority.rc != 0:
        if getattr(args, "json", False):
            print(authority.render_json(result=None))
        else:
            print(authority.render_text_metadata())
            for item in authority.coverage:
                if item.state in (outcome_mod.CoverageState.UNKNOWN,
                                  outcome_mod.CoverageState.NOT_RUN):
                    print(f"  {item.surface}: {item.state.value}"
                          + (f" — {item.reason}" if item.reason else ""),
                          file=sys.stderr)
        return authority.rc

    def with_domain_result(rc: int) -> public_read.PublicReadResult:
        if rc == 0:
            return authority
        coverage = tuple(sorted(
            authority.coverage + (outcome_mod.SurfaceCoverage(
                "domain-result",
                outcome_mod.CoverageState.UNKNOWN,
                reason=f"domain renderer exited {rc}",
            ),),
            key=lambda item: item.surface,
        ))
        return public_read.PublicReadResult(
            outcome_mod.OutcomeState.UNKNOWN,
            coverage,
            authority.sections,
            authority.coverage_horizon,
            authority.generation,
            authority.watermark,
            authority.applied_update_ids,
        )

    token = _PUBLIC_READ_CONTEXT.set(authority)
    sealed_transport = public_read.SealedGenerationTransport(
        transport, args.team, authority,
    )
    try:
        if getattr(args, "json", False):
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                rc = args.func(args, sealed_transport)
            raw = captured.getvalue()
            try:
                domain = json.loads(raw) if raw.strip() else None
            except ValueError as exc:
                raise RuntimeError(
                    "public JSON renderer emitted more than one value or prose"
                ) from exc
            rendered = with_domain_result(rc)
            print(rendered.render_json(result=domain))
            return rc
        rc = args.func(args, sealed_transport)
        rendered = with_domain_result(rc)
        print(rendered.render_text_metadata())
        if rc != 0:
            domain_coverage = rendered.coverage_by_surface("domain-result")
            print(
                f"  domain-result: {domain_coverage.state.value}"
                + (f" — {domain_coverage.reason}" if domain_coverage.reason else ""),
                file=sys.stderr,
            )
        return rc
    finally:
        _PUBLIC_READ_CONTEXT.reset(token)


def main(argv: Optional[list[str]] = None, transport: Any = None) -> int:
    args = build_parser().parse_args(argv)
    transport = transport if transport is not None else FulcraFileTransport()
    try:
        authority = _begin_v2_public_read(args, transport)
        rc = (_run_v2_public_handler(args, transport, authority)
              if authority is not None else args.func(args, transport))
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
    if rc == 0 and _is_activity_invocation(args):
        actor = _known_sender(args)
        team = getattr(args, "team", None)
        if actor and team:
            now_iso = _iso(_now())
            _refresh_activity_presence(
                transport, team, actor,
                now_monotonic=_now_monotonic(), now_iso=now_iso)
            # ONE WRITE SITE for the work pointer (coord-boss constraint 1,
            # 2026-08-09). Stamped HERE rather than beside each artifact write,
            # so pointer coverage INHERITS the classification the chokepoint
            # already computes: a newly added write verb stamps by default, and
            # "someone forgot the stamp" is designed out instead of tested for.
            # Same argument that made the classifier a denylist.
            #
            # rc == 0 is the persistence gate (588): we are past the command,
            # and it succeeded. Best-effort, exactly like the presence bump —
            # it can never change rc.
            try:
                _stamp_work_pointer(
                    transport, team, actor,
                    kind=str(getattr(args, "command", "") or "write"),
                    path=str(getattr(args, "_activity_artifact_path", "") or ""),
                    now_iso=now_iso)
            except Exception as exc:                      # never break a write
                print(f"work pointer stamp failed: {exc}", file=sys.stderr)
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

from . import commands_acceptance  # noqa: E402

cmd_acceptance_pair = commands_acceptance.cmd_acceptance_pair


# --- activity denylist: the EXTRACTED read verbs -----------------------------
#
# Completed HERE, after the extracted command modules are bound, because a
# denylist assembled earlier in this file can only name what `cli.py` itself
# defines. That was the hole codex-reviewer found (590 r1): `headroom`, `route`,
# `atc report`, `annotate status` and `threads` live in extracted modules, so the
# default-true predicate treated them as activity and merely READING one of those
# views refreshed the reader's presence. Manufacturing liveness out of someone
# looking at a dashboard is the worse direction to be wrong in — it suppresses
# the nudge for an agent who really is gone.
_ACTIVITY_READ_FUNCS = _ACTIVITY_READ_FUNCS | frozenset({
    cmd_headroom, cmd_route, cmd_atc_report,   # commands_atc
    cmd_dash,                                  # commands_atc — serves a view
    cmd_annotate_status,                       # commands_annotate
    cmd_threads,                               # commands_threads
})


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))


def _sweep_one(transport: Any, team: str, slug: str) -> "review_gc.Disposition":
    """Disposition for ONE review slug. Transport failure is UNKNOWN, not empty."""
    try:
        entries = transport.list_dir(_verdicts_prefix(team, slug))
    except TransportError:
        return review_gc.Disposition(
            review_gc.RESIDUE_UNKNOWN, "listing-raised",
            "verdicts listing raised — closes nothing")
    names = {(e.get("name") or "") for e in (entries or [])}
    fm: Any = None
    if SETTLED_MARKER in names:
        try:
            fm = okf.parse_frontmatter(
                transport.read(_settled_marker_path(team, slug))) or {}
        except TransportError:
            return review_gc.Disposition(
                review_gc.RESIDUE_UNKNOWN, "marker-unreadable",
                "settle marker unreadable — closes nothing")
    return review_gc.residue_disposition(
        names, settled_marker=SETTLED_MARKER, marker_fm=fm)


def cmd_review_residue(args: argparse.Namespace, transport: Any) -> int:
    """Close review-request rows whose review already reached a terminal state.

    DRY RUN unless ``--apply``. This is not a one-time backfill: a review that
    settles through the fold's APPROVED cache is settled by a pure build path
    that must not mutate task state, so nothing closes those rows at settle
    time and new ones appear for as long as reviews get approved.
    """
    from . import model

    rows, ok, reason = _load_rows_status(transport, args.team)
    if not ok:
        # A partial view of the rows cannot tell a closed row from an unread
        # one, and this verb closes things. UNKNOWN is not empty.
        print(f"review residue: rows fold DEGRADED ({reason}) — refusing to "
              f"close anything on a partial view", file=sys.stderr)
        return 2

    buckets: "dict[str, list[tuple[str, str, str]]]" = {}
    by_answer: "dict[str, int]" = {}
    scanned = 0
    for r in rows or []:
        if r.get("status") not in model.OPEN_STATUSES:
            continue
        title = str(r.get("title") or "")
        if not title.startswith(_REVIEW_REQUEST_TITLE_PREFIX):
            continue
        slug = title[len(_REVIEW_REQUEST_TITLE_PREFIX):].strip()
        if not slug:
            continue
        scanned += 1
        d = _sweep_one(transport, args.team, slug)
        buckets.setdefault(d.kind, []).append(
            (str(r.get("id") or ""), slug, d.why))
        if d.kind == review_gc.RESIDUE_CLOSE:
            by_answer[d.answer] = by_answer.get(d.answer, 0) + 1

    closable = buckets.get(review_gc.RESIDUE_CLOSE, [])
    print(f"review residue [{'APPLY' if args.apply else 'DRY RUN'}] team={args.team}")
    # A MEASURED POPULATION, EVERY RUN. A sweep with nothing to close and a
    # sweep that has stopped detecting print the same `closed 0` otherwise, and
    # this fleet has shipped that shape three times in one week. The scanned
    # count is the quantity that separates them.
    print(f"  scanned             : {scanned} open review-request row(s)")
    print(f"  closable            : {len(closable)}")
    if by_answer:
        detail = "  ".join(f"{k}={v}" for k, v in sorted(by_answer.items()))
        print(f"      by answer       : {detail}")
    for key, label in ((review_gc.RESIDUE_UNRESOLVED, "unresolved-marker"),
                       (review_gc.RESIDUE_UNKNOWN_PROVENANCE, "unknown-provenance"),
                       (review_gc.RESIDUE_UNKNOWN, "UNKNOWN")):
        items = buckets.get(key, [])
        # NAMED, NEVER COUNTED. A shrinking count is indistinguishable from a
        # detector that stopped detecting; a named list is auditable.
        print(f"  {label:20s}: {len(items)}")
        for _rid, slug, why in sorted(items, key=lambda t: t[1]):
            print(f"      - {slug} — {why}")
    print(f"  genuinely open      : {len(buckets.get(review_gc.RESIDUE_OPEN, []))}")

    if not args.apply:
        for _rid, slug, why in sorted(closable, key=lambda t: t[1]):
            print(f"  would close: {slug} — {why}")
        return 0

    failed = 0
    for rid, slug, why in sorted(closable, key=lambda t: t[1]):
        ns = argparse.Namespace(
            team=args.team, name=rid,
            evidence=f"residue sweep: {why}",
            agent=getattr(args, "agent", None))
        # Reuse `task done` rather than reimplementing it: it refuses a ghost
        # close on a failed write and emits the close companion, and a sweep
        # that skipped either would be a bulk version of both bugs.
        if cmd_task_done(ns, transport) != 0:
            failed += 1
    print(f"  closed {len(closable) - failed}, FAILED {failed}")
    return 1 if failed else 0
