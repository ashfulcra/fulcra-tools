"""One pass: read forward from the cursor, apply, re-read, persist, report (spec §3.3). O(new events).

Ruling 1 (G26): the cursor is the recorded_at of the LAST APPLIED event, never now.
Ruling 2 (G27): re-read before write; a moved generation is refused BY NAME, never overwritten.
Ruling 4 (G25): hitting max_events is a remainder (rc 0); zero progress with events present is the only error.
G31 (codex-coder r7): the cursor PASSES observed-irrelevant records; a gap is an unapplied RELEVANT event.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, NamedTuple

from . import channel, checkpoint as cp, events, pointers
from .transport import PointerTransport, TransportUnavailable

OVERLAP_SECONDS = 5
_BROADCAST = "all"
_EPOCH = "1970-01-01T00:00:00Z"
_OWN_ACTIONS = ("claim", "release", "close")   # the assignee's own state changes on an obligation it holds (never `open`/`note`)


class FoldOutcome(NamedTuple):
    state: dict[str, Any]
    source: str
    applied: int
    unread: int
    rc: int


class FoldRefused(RuntimeError):
    pass


class FoldContended(RuntimeError):
    """The checkpoint generation moved under this pass: this agent is acting twice."""


def _minus_overlap(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (dt - timedelta(seconds=OVERLAP_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(reader: PointerTransport, writer: Any, team: str, agent: str, *, now: str, writer_id: str,
        max_events: int = 5000, verify_pointers: bool = False, rebuild: bool = False) -> FoldOutcome:
    cfg = channel.resolve(reader, team)
    state, source = cp.load(reader, team, agent)
    if source == "corrupt":
        raise FoldRefused("checkpoint is corrupt — left untouched for forensics; repair or reseed it explicitly")
    if source == "error":
        raise TransportUnavailable("checkpoint unreadable")
    if source == "fresh":
        state = cp.empty(_EPOCH)
    elif rebuild:
        # Recompute the derived open set from the stream under the CURRENT relevance rule, keeping the
        # generation/writer so lost-update detection (G27) still guards the write. Events are never deleted (G28);
        # this replays them. Needed once when the rule changed (2026-09-05: sender events no longer open).
        fresh = cp.empty(_EPOCH)
        fresh["generation"], fresh["writer"] = state.get("generation", 0), state.get("writer", "")
        state, source = fresh, "rebuild"
    generation = int(state.get("generation", 0))
    applied = unread = 0
    last_observed = state["cursor"]          # G31: advances past irrelevant records until the first UNAPPLIED relevant one
    for rec in reader.read_events(cfg["data_type"], _minus_overlap(state["cursor"])):
        at = rec.get("recorded_at") or last_observed
        ev = events.parse_event(rec)
        # RULING (2026-09-05, coord-boss blocker a0927018): an obligation belongs to its ASSIGNEE — the addressed `to`,
        # or everyone for a broadcast. A sender's waiting is bookkeeping, not an obligation: an `open` the agent SENT
        # never opens for the sender. The earlier unconditional `or ev["from"] == agent` made every seed leak into its
        # senders' folds (coord-boss saw 54 of coord-maintainer's opens; coord-maintainer saw 137 of coord-boss's), so
        # no coordinator could ever AGREE. What DOES apply from the sender's side is the agent's own ACTIONS on an
        # obligation it holds — claim/release/close are addressed to the open's sender but performed by the assignee,
        # and the assignee's fold must see them or a closed row would stay open forever.
        # 6f8121fc class A (coord-boss, 2026-09-05): a broadcast opens for every agent EXCEPT its sender — under the
        # assignee ruling the sender of a broadcast owes nothing on it (coord-boss's own fleet P0 had become his open).
        if ev is None or not (ev["to"] == agent
                              or (ev["to"] == _BROADCAST and ev["from"] != agent)
                              or (ev["from"] == agent and ev["kind"] in _OWN_ACTIONS)):
            if not unread:
                last_observed = at
            continue
        if applied >= max_events:
            unread += 1
            continue
        cp.apply(state, ev)
        applied += 1
        last_observed = at
    if applied == 0 and unread:
        raise FoldRefused(f"no progress: {unread} events present and none applied (max_events={max_events})")
    state["cursor"] = last_observed
    state["unread_events"] = unread
    state["unreadable_pointers"] = []
    if verify_pointers:
        for slug, row in state["open"].items():
            _body, st = reader.read_classified(pointers.qualify(team, row["ptr"]))   # team-relative ptr -> team root
            if st != "ok":
                state["unreadable_pointers"].append(slug)
    again, src2 = cp.load(reader, team, agent)
    if src2 == "error":
        raise TransportUnavailable("checkpoint re-read before write did not answer; not writing")
    if src2 == "ok" and int(again.get("generation", 0)) != generation:
        raise FoldContended(f"{agent} is acting twice (two hosts or a duplicated cron): checkpoint generation moved "
                            f"{generation} -> {again.get('generation')} by writer {again.get('writer')!r} under this pass; not overwriting")
    state["generation"] = generation + 1
    state["writer"] = writer_id
    if not cp.save(writer, team, agent, state):
        raise TransportUnavailable("checkpoint save failed")
    return FoldOutcome(state, source, applied, unread, 3 if state["unreadable_pointers"] else 0)
