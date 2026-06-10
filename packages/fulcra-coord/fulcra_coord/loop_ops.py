"""Loop return-leg I/O: response sub-log + the `respond` command.

The CLOSED-LOOP GUARANTEE's write half (spec 2026-06-09): an outcome exists
ONLY as a bus response event under ``directives/<id>/responses/`` — one shard
per response (append-only, concurrent responders never clobber; same pattern
as directives.append_directive_route). The LWW snapshot's `outcome`/`state` is
a best-effort CACHE of the sub-log fold, never the truth.

Layering: imports schema/remote/log/loops — never cli/views/lifecycle/inbox
(fitness-pinned like directives.py).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import remote, loops
from . import log as ops_log
from .output import info as _info, warn as _warn, print_json as _print_json


def _now_z() -> str:
    """Current UTC instant as an ISO-8601 ``...Z`` stamp (the bus's clock
    format). Inlined like directives._now_z to keep the low-layer import
    surface minimal."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z")


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


def fold_loop(
    record: dict[str, Any], *, backend: Optional[list[str]] = None
) -> dict[str, Any]:
    """The loop with its response sub-log folded in — outcome + closure derived
    ONLY from bus response events (the guarantee's read side). Latest response
    wins for `outcome`; any response moves an expecting loop to its terminal
    closure by replaying legal hops (preferring the kind's response hop —
    responded/delivered/answered — then walking the machine to the nearest
    terminal, so the snapshot never teleports through an illegal edge). Pure
    given its inputs; the only I/O is the sub-log read."""
    folded = dict(record)
    responses = read_loop_responses(record.get("id") or "", backend=backend)
    if not responses:
        return folded
    last = responses[-1]
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
        folded = fold_loop(record, backend=backend)
        remote.upload_json(folded, remote.directive_remote_path(loop_id),
                           backend=backend)
    except Exception:
        pass
    if getattr(args, "format", "table") == "json":
        _print_json({"loop": loop_id, "responded_by": me, "outcome": outcome})
    else:
        _info(f"Loop {loop_id} responded by {me}: {outcome.get('verdict')}")
    return 0
