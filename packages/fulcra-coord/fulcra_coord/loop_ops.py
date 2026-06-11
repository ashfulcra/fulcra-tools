"""Loop return-leg I/O: response + evidence sub-logs + the `respond` command.

The CLOSED-LOOP GUARANTEE's write half (spec 2026-06-09): an outcome exists
ONLY as a bus response event under ``directives/<id>/responses/`` — one shard
per response (append-only, concurrent responders never clobber; same pattern
as directives.append_directive_route). The LWW snapshot's `outcome`/`state` is
a best-effort CACHE of the sub-log fold, never the truth.

The EVIDENCE sub-log (``directives/<id>/evidence/``) sits beside it: forge-
mirrored signals, force-stamped ``source=forge-mirror``, consumed only by
detection (out-of-band flags) — never by ``fold_loop`` (see the invariant
block there): mirrored evidence can never close a loop.

Layering: imports schema/remote/log/loops — never cli/views/lifecycle/inbox
(fitness-pinned like directives.py).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import remote, loops
from . import log as ops_log
from .output import info as _info, warn as _warn, print_json as _print_json
# The bus clock format ("UTC, microsecond precision, trailing Z") has ONE home:
# timeutil — a pure stdlib leaf, so binding it here costs no layering edge (the
# loop_ops import pin forbids only up-layer modules). Bound under the local
# historical name; this replaced an inlined duplicate of the same function.
from .timeutil import now_iso as _now_z


def append_loop_response(
    directive_id: str, response: dict[str, Any], *,
    backend: Optional[list[str]] = None,
) -> bool:
    """Append ONE response event to a loop's response sub-log (append-only shard).

    Stamps ``at`` and an ``event_id`` if absent. Each response lands as its OWN
    file so multi-holder audiences (e.g. ``@reviewer`` fan-out) responding
    concurrently never overwrite one another. Best-effort: True on a confirmed
    upload, False on any failure (never raises)."""
    try:
        event = dict(response)
        event.setdefault("at", _now_z())
        event_id = event.get("event_id") or uuid.uuid4().hex
        event["event_id"] = str(event_id)
        return bool(remote.upload_json(
            event, remote.directive_response_path(directive_id, event["event_id"]),
            backend=backend))
    except Exception:
        return False


def read_loop_responses(
    directive_id: str, *, backend: Optional[list[str]] = None
) -> list[dict[str, Any]]:
    """Every response shard for a loop, sorted by (at, event id) — machine-
    agnostic stable order, mirroring read_directive_routing. Best-effort: []."""
    try:
        records = remote.list_json(remote.directive_responses_prefix(directive_id),
                                   backend=backend)
    except Exception:
        return []
    events: list[tuple[str, str, dict[str, Any]]] = []
    for path, rec in records:
        if isinstance(rec, dict):
            events.append((rec.get("at", "") or "", Path(path).stem, rec))
    events.sort(key=lambda t: (t[0], t[1]))
    return [rec for _at, _eid, rec in events]


def append_loop_evidence(
    directive_id: str, evidence: dict[str, Any], *,
    backend: Optional[list[str]] = None,
) -> bool:
    """Append ONE mirrored-evidence event to a loop's evidence sub-log
    (append-only shard, ``directives/<id>/evidence/``).

    Same shard discipline as ``append_loop_response``: stamps ``at`` and an
    ``event_id`` if absent; each event lands as its OWN file so concurrent
    mirror sweeps never overwrite one another; best-effort — True on a
    confirmed upload, False on any failure (never raises).

    THE one deliberate difference: ``source`` is FORCE-set to ``forge-mirror``
    unconditionally — callers cannot forge first-party-ness. Every event in
    this sub-log is, by construction, a mirrored off-bus signal; nothing that
    reads it can ever mistake mirrored evidence for a bus-native response."""
    try:
        event = dict(evidence)
        event["source"] = "forge-mirror"   # forced — see docstring
        event.setdefault("at", _now_z())
        event_id = event.get("event_id") or uuid.uuid4().hex
        event["event_id"] = str(event_id)
        return bool(remote.upload_json(
            event, remote.directive_evidence_path(directive_id, event["event_id"]),
            backend=backend))
    except Exception:
        return False


def read_loop_evidence(
    directive_id: str, *, backend: Optional[list[str]] = None
) -> list[dict[str, Any]]:
    """Every evidence shard for a loop, sorted by (at, event id) — the same
    machine-agnostic stable order as read_loop_responses. Best-effort: [].
    Consumed by DETECTION only (out-of-band flags) — never by the closure
    fold (see the invariant block on fold_loop)."""
    try:
        records = remote.list_json(remote.directive_evidence_prefix(directive_id),
                                   backend=backend)
    except Exception:
        return []
    events: list[tuple[str, str, dict[str, Any]]] = []
    for path, rec in records:
        if isinstance(rec, dict):
            events.append((rec.get("at", "") or "", Path(path).stem, rec))
    events.sort(key=lambda t: (t[0], t[1]))
    return [rec for _at, _eid, rec in events]


def load_loop_records(
    *, backend: Optional[list[str]] = None
) -> list[dict[str, Any]]:
    """Every TOP-LEVEL loop record on the bus — one paths-only listing of the
    directives prefix, the top-level-only filter applied to the PATHS, then a
    pooled download of ONLY the surviving records.

    FILTER BEFORE DOWNLOAD (PERF, 2026-06-10 measured pass): this used to ride
    ``remote.list_json``, which downloads EVERY ``.json`` under the prefix —
    ack/routing/response/evidence shards included — and only then threw the
    shards away. Each shard download is one ~1.3s subprocess, and this sweep
    runs on every listener notify tick, every board render, every digest, and
    every reconcile health fold, so the bus paid for its whole sub-log volume
    over and over. Filtering the paths first (the ``_directive_parity_check``
    idiom) makes the cost O(top-level records), not O(all shards).

    THE TOP-LEVEL-ONLY FILTER (load-bearing — the single home for this rule,
    consumed by the loop health check, the board, the digest section, the
    forge-mirror sweep, and the inbox overdue suffix):
    ``remote.directives_prefix()`` holds SUB-LOG SUBTREES (``<id>/acks/``,
    ``<id>/routing/``, ``<id>/responses/``, ``<id>/evidence/``) beside the
    top-level ``directives/<id>.json`` loop records. Only a path that, after
    stripping the prefix, has NO further ``/`` and ends in ``.json`` is a loop
    record — a shard counted as a record would inflate every count.

    Deliberately NOT best-effort: a broken prefix LISTING raises so each
    surface keeps its own error discipline (health/board swallow to ``[]`` at
    the call site; the digest lets its caller omit the whole section). A
    single failed record download, by contrast, is skipped — one unreadable
    record must not blank every board."""
    import concurrent.futures
    prefix = remote.directives_prefix()
    paths: list[str] = []
    for path in remote.list_files(prefix, backend=backend):
        # TOP-LEVEL-ONLY FILTER (see docstring): reject sub-log shards by PATH,
        # before any download is paid for.
        rel = path[len(prefix):] if path.startswith(prefix) else path
        if "/" in rel:
            continue  # ack/routing/response/evidence shard — never a loop record
        if not rel.endswith(".json"):
            continue
        paths.append(path)
    if not paths:
        return []
    # Pooled download of the surviving top-level records (the remote.list_json
    # pool shape): independent subprocesses, no shared state. Results keep the
    # listing's path order so callers see a stable order for a given bus.
    results: dict[str, dict[str, Any]] = {}
    workers = min(8, max(2, len(paths)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(remote.download_json, p, backend=backend): p
            for p in paths
        }
        for fut in concurrent.futures.as_completed(futures):
            path = futures[fut]
            try:
                rec = fut.result()
            except Exception:
                rec = None  # per-record isolation: skip, never blank the sweep
            if isinstance(rec, dict):
                results[path] = rec
    return [results[p] for p in paths if p in results]


def evidence_ids_for(
    me: str, records: list[dict[str, Any]], *, now: datetime,
    backend: Optional[list[str]] = None,
) -> set[str]:
    """The subset of MY awaiting_others loop ids whose evidence sub-log is
    nonempty — the detection input that flags a loop ◈ out-of-band (a forge-
    mirrored answer exists OFF the bus; mirrored evidence never closes
    anything — fold_loop's invariant).

    Probes are BOUNDED: awaiting_others is folded first WITHOUT evidence to
    get the candidate ids (my own open asks — a small set), then each
    candidate's evidence prefix is listed. Never one list per directive on
    the bus. Each probe is individually best-effort (a failed list reads as
    "no evidence", never an error)."""
    evidence_ids: set[str] = set()
    for s in loops.awaiting_others(me, records, now=now):
        lid = s.get("id")
        if not lid:
            continue
        try:
            if remote.list_json(remote.directive_evidence_prefix(lid),
                                backend=backend):
                evidence_ids.add(lid)
        except Exception:
            continue   # best-effort: an unreadable prefix is "no evidence"
    return evidence_ids


def _walk_to_terminal(kind: str, state: str) -> str:
    """Nearest terminal state reachable from ``state`` via legal transitions.

    BFS with sorted-neighbor order so the result is deterministic across
    machines (the bus has no global clock; two hosts folding the same loop must
    land on the same state). WHY a walk and not a single closed-hop: some
    machines (dispatch) put several legal hops between a response and closure
    (assigned -> accepted -> delivered -> closed), and the fold must replay the
    machine rather than teleport through an illegal edge. Returns ``state``
    unchanged when no terminal is reachable — the fold never raises and never
    invents an illegal transition (data off the bus can be anything)."""
    if kind not in loops.KINDS:
        return state
    terminals = loops.terminal_states(kind)
    if state in terminals:
        return state
    seen = {state}
    frontier = [state]
    while frontier:
        nxt_frontier: list[str] = []
        for s in frontier:
            for nxt in sorted(loops.KINDS[kind]["transitions"].get(s, set())):
                if nxt in terminals:
                    return nxt
                if nxt not in seen:
                    seen.add(nxt)
                    nxt_frontier.append(nxt)
        frontier = nxt_frontier
    return state


# CRITICAL INVARIANT: fold_loop reads ONLY the responses sub-log,
# NEVER the evidence sub-log. Mirrored evidence (source=forge-mirror, written
# by append_loop_evidence) is detection input — it flags a loop out-of-band on
# the board, and NOTHING more. If this fold ever consumed the evidence prefix,
# a forge comment could silently close a loop, resurrecting the exact
# out-of-band-verdict bug the loop substrate exists to kill. Closure is
# bus-response-only: the requester closes a flagged loop EXPLICITLY (respond),
# citing the evidence. Pinned by
# tests/test_loop_conformance.py::test_mirrored_evidence_never_closes_a_loop.
def fold_loop(
    record: dict[str, Any], *, backend: Optional[list[str]] = None,
    responses: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """The loop with its response sub-log folded in — outcome + closure derived
    ONLY from bus response events (the guarantee's read side). The latest
    OUTCOME-CARRYING response wins for `outcome` (S4: outcome-less shards are
    inert — they neither null a verdict nor close anything); such a response
    moves an expecting loop to its terminal closure by replaying legal hops
    (preferring the kind's response hop — responded/delivered/answered — then
    walking the machine to the nearest terminal, so the snapshot never
    teleports through an illegal edge). Pure given its inputs; the only I/O is
    the sub-log read.

    ``responses`` (F9, 2026-06-11 wave): a caller that ALREADY read the
    response sub-log — and, crucially, already distinguished a read FAILURE
    from a genuinely empty log, which this function's best-effort self-read
    cannot — passes the (at, event_id)-sorted records here so the fold never
    re-reads (and never silently re-introduces the failure-reads-as-empty
    collapse). None keeps the self-loading behaviour for every existing
    caller."""
    folded = dict(record)
    if responses is None:
        responses = read_loop_responses(record.get("id") or "", backend=backend)
    # 2026-06-11 bug hunt S4: take the LAST response that actually CARRIES a
    # non-empty outcome — not blindly responses[-1]. A trailing outcome-less
    # shard (a malformed/partial responder) used to null a real verdict AND
    # still close the loop: a terminal state with outcome=None is closure
    # without a verdict, the exact out-of-band-verdict bug class this
    # substrate exists to kill. The deliberate choice: an outcome-less
    # response NEVER advances state to terminal (the loop stays visibly open
    # until a real verdict lands), because a stuck-open loop is recoverable
    # while a silently-verdict-less closed one is not.
    last = next((r for r in reversed(responses) if r.get("outcome")), None)
    if last is None:
        return folded
    folded["outcome"] = last.get("outcome")
    kind = loops.loop_kind_of(folded)
    state = loops.loop_state_of(folded)
    for hop in ("responded", "delivered", "answered"):
        if loops.can_transition(kind, state, hop):
            state = hop
            break
    folded["state"] = _walk_to_terminal(kind, state)
    folded["updated_at"] = last.get("at") or folded.get("updated_at")
    return folded


def cmd_respond(args: Any, backend: Optional[list[str]] = None) -> int:
    """Close (or answer) a coordination loop ON THE BUS — the generic return leg.

    ``respond <loop-id> --outcome <verdict/result> [--evidence ...]``. Writes the
    response shard FIRST (durable truth), then best-effort refreshes the LWW
    snapshot from the fold so readers that only look at the snapshot see closure
    too. One command from any state — loop closure must never need a lifecycle
    dance (the waiting->done friction bug class)."""
    from . import identity  # lazy: keep module import light
    loop_id = getattr(args, "loop_id", None)
    if not loop_id:
        _warn("respond: loop id required")
        return 1
    record = remote.download_json(remote.directive_remote_path(loop_id),
                                  backend=backend)
    if not isinstance(record, dict):
        _warn(f"respond: no loop record found for {loop_id}")
        return 1
    me = identity.resolve_agent(getattr(args, "agent", None))
    outcome: dict[str, Any] = {"verdict": getattr(args, "outcome", "") or "done"}
    evidence = getattr(args, "evidence", "") or ""
    if evidence:
        outcome["evidence"] = evidence
    ok = append_loop_response(loop_id, {"by": me, "outcome": outcome},
                              backend=backend)
    if not ok:
        _warn(f"respond: response write FAILED for {loop_id} — loop still open")
        try:
            ops_log.log_op("respond", loop_id, status="response_write_failed")
        except Exception:
            pass
        return 1
    # Best-effort snapshot refresh — the shard above is already the truth.
    try:
        # 2026-06-11 bug hunt C6: fold onto a FRESH download, never the body
        # read at command start. That early copy can be arbitrarily stale by
        # the time the shard append lands; folding the sub-log onto it and
        # re-uploading silently REVERTED any concurrent snapshot write (an
        # ack, a summary edit) that arrived mid-command. Re-downloading just
        # before the refresh means the only thing this write changes is the
        # fold result; a failed re-download falls back to the original body
        # (no worse than the pre-fix behavior, and still best-effort).
        fresh = remote.download_json(remote.directive_remote_path(loop_id),
                                     backend=backend)
        base = fresh if isinstance(fresh, dict) else record
        folded = fold_loop(base, backend=backend)
        remote.upload_json(folded, remote.directive_remote_path(loop_id),
                           backend=backend)
    except Exception:
        pass
    if getattr(args, "format", "table") == "json":
        _print_json({"loop": loop_id, "responded_by": me, "outcome": outcome})
    else:
        _info(f"Loop {loop_id} responded by {me}: {outcome.get('verdict')}")
    return 0
