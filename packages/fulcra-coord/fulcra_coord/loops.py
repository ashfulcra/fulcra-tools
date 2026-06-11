"""Coordination-loop kind registry + per-kind lifecycle machines — PURE.

Spec: docs/superpowers/specs/2026-06-09-coordination-loops-design.md. The
Directive family is the loop record; this module owns what each `kind` MEANS:
its state machine, initial/terminal states, and whether it expects a bus-native
response. Everything here is a pure function over record dicts — no I/O, no
clock reads (callers inject `now`), importing ONLY schema + stdlib, so the
reducer stays testable and the layering fitness test can pin it low.

WHY a registry (dict) and not classes: adding a work-type must be a REGISTRY
ENTRY, not a schema family or module (anti-creep rule in the spec). The entry
declares states/transitions/terminals/expects_response default/SLA default;
question+signoff are registered now but only review/dispatch/idea get wired
(YAGNI — the registry is the extension point).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

# kind -> lifecycle declaration.
#   states:       every legal state
#   initial:      where a new loop starts
#   transitions:  {from_state: {to_state, ...}}
#   terminal:     states with no outbound edges (loop closed/settled)
#   expects_response: default for new loops of this kind
#   sla_hours:    default overdue horizon (None = no SLA)
KINDS: dict[str, dict[str, Any]] = {
    # Legacy/FYI: every pre-loop record reads as a `tell`. One-way by default —
    # no response expected, so it is never an OPEN loop unless explicitly
    # marked expects_response at creation.
    "tell": {
        "initial": "sent",
        "transitions": {"sent": {"acked", "closed"}, "acked": {"closed"},
                        "closed": set()},
        "expects_response": False,
        "sla_hours": None,
    },
    "review": {
        "initial": "requested",
        "transitions": {
            "requested": {"acked", "in_review", "responded"},
            "acked": {"in_review", "responded"},
            "in_review": {"responded"},
            "responded": {"closed"},
            "closed": set(),
        },
        "expects_response": True,
        "sla_hours": 24,
    },
    "dispatch": {
        "initial": "assigned",
        "transitions": {
            "assigned": {"accepted", "declined"},
            "accepted": {"in_progress", "delivered"},
            "declined": {"assigned", "closed"},   # reassign or give up
            "in_progress": {"delivered"},
            "delivered": {"closed"},
            "closed": set(),
        },
        "expects_response": True,
        "sla_hours": 72,
    },
    "idea": {
        "initial": "captured",
        "transitions": {
            "captured": {"maturing", "viable", "dropped"},
            "maturing": {"viable", "dropped"},
            "viable": {"routed", "dropped"},
            "routed": {"active", "dropped"},
            "active": {"done", "dropped"},
            "done": set(),
            "dropped": set(),
        },
        "expects_response": False,   # a pipeline, not an ask
        "sla_hours": None,
    },
    # Registered for the validator + future wiring; not built yet (YAGNI).
    "question": {
        "initial": "asked",
        "transitions": {"asked": {"answered"}, "answered": {"closed"},
                        "closed": set()},
        "expects_response": True,
        "sla_hours": 48,
    },
    "signoff": {
        "initial": "asked",
        "transitions": {"asked": {"answered"}, "answered": {"closed"},
                        "closed": set()},
        "expects_response": True,
        "sla_hours": 48,
    },
}


def states_of(kind: str) -> set[str]:
    return set(KINDS[kind]["transitions"].keys())


def initial_state(kind: str) -> str:
    return KINDS[kind]["initial"]


def terminal_states(kind: str) -> set[str]:
    t = KINDS[kind]["transitions"]
    return {s for s, outs in t.items() if not outs}


def can_transition(kind: str, from_state: str, to_state: str) -> bool:
    """True iff kind's machine allows from_state -> to_state. Unknown states
    are False (never raise on data read off the bus)."""
    if kind not in KINDS:
        return False
    return to_state in KINDS[kind]["transitions"].get(from_state, set())


def closure_reachable(kind: str, state: str) -> bool:
    """Spec invariant: from EVERY state some terminal is reachable — a
    lifecycle must never strand a loop. (BFS over the transition graph.)"""
    terminals = terminal_states(kind)
    seen, frontier = {state}, [state]
    while frontier:
        s = frontier.pop()
        if s in terminals:
            return True
        for nxt in KINDS[kind]["transitions"].get(s, set()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return False


def loop_kind_of(record: dict[str, Any]) -> str:
    """A record's loop kind; records written before the loop fields existed
    (no/None `kind`) read as the legacy `tell` kind — the spec's mixed-fleet
    floor ("old records read as kind:tell with the legacy lifecycle")."""
    kind = record.get("kind") or "tell"
    return kind if kind in KINDS else "tell"


def _has_unknown_kind(record: dict[str, Any]) -> bool:
    kind = record.get("kind")
    return kind is not None and kind not in KINDS


# Legacy directive status -> tell-lifecycle state, for records that predate the
# `state` field. acked/acted directives have been received; expired/terminal
# statuses read as closed; proposed/delivered are merely sent.
_LEGACY_STATUS_TO_STATE = {
    "proposed": "sent", "delivered": "sent",
    "acked": "acked", "acted": "acked",
    "expired": "closed",
}


def loop_state_of(record: dict[str, Any]) -> str:
    kind = loop_kind_of(record)
    state = record.get("state")
    if state:
        return state
    if kind == "tell":
        return _LEGACY_STATUS_TO_STATE.get(record.get("status") or "", "sent")
    return initial_state(kind)


def expects_response(record: dict[str, Any]) -> bool:
    """Whether this loop must stay open until a bus response arrives. Explicit
    field wins; absent (legacy) falls back to the kind default — which for the
    legacy `tell` kind is False, so old records are never retro-opened."""
    if _has_unknown_kind(record):
        return False
    val = record.get("expects_response")
    if val is not None:
        return bool(val)
    return bool(KINDS[loop_kind_of(record)]["expects_response"])


def is_open_loop(record: dict[str, Any]) -> bool:
    """OPEN = expects a response and is not in a terminal state. This is the
    closed-loop guarantee's read side: nothing but a recorded terminal
    transition (driven by a bus response event) makes an expecting loop
    not-open."""
    if not expects_response(record):
        return False
    kind = loop_kind_of(record)
    return loop_state_of(record) not in terminal_states(kind)


# ---------------------------------------------------------------------------
# Detection folds + board projection (spec 2026-06-09 Task 5) — the symmetric
# counterpart to the undelivered-directive check (#127): that catches sends
# that never ARRIVED; these catch loops never ANSWERED (per-kind SLA) and
# loops awaiting ME. Still pure: `now` is injected, the only inputs are
# record dicts — the cli wiring does the I/O and passes the caller's id.
# ---------------------------------------------------------------------------


def _hours_old(record: dict[str, Any], now) -> Optional[float]:
    raw = record.get("created_at") or ""
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if created.tzinfo is None and getattr(now, "tzinfo", None) is not None:
            created = created.replace(tzinfo=now.tzinfo)
        return (now - created).total_seconds() / 3600.0
    except Exception:
        return None


def _is_overdue(record: dict[str, Any], now) -> bool:
    """Record SLA wins; a None/absent record SLA falls back to the kind's
    default horizon (review 24h, dispatch 72h, ...). A kind with no default
    (idea, tell) — or an unparseable created_at — is never overdue."""
    sla = record.get("sla_hours")
    if sla is None:
        sla = KINDS[loop_kind_of(record)]["sla_hours"]
    if sla is None:
        return False
    age = _hours_old(record, now)
    try:
        sla_float = float(sla)
    except (TypeError, ValueError):
        return False
    return age is not None and age > sla_float


def _summary(record: dict[str, Any], now) -> dict[str, Any]:
    return {
        "id": record.get("id"), "kind": loop_kind_of(record),
        "state": loop_state_of(record), "title": record.get("title"),
        "from": record.get("from"), "audience": record.get("audience"),
        "overdue": _is_overdue(record, now),
    }


def awaiting_me(me: str, records: list[dict[str, Any]], *, now) -> list[dict[str, Any]]:
    """Open loops DIRECTED AT me that I haven't closed — my side of the ledger.
    Exact-audience match here; @role/broadcast resolution happens at the inbox
    layer (views.inbox_for), which is the delivery surface — this fold is the
    accounting surface."""
    return [_summary(r, now) for r in records
            if is_open_loop(r) and r.get("audience") == me]


def awaiting_others(
    me: str, records: list[dict[str, Any]], *, now,
    evidence_ids: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """Open loops I OPENED that nobody has answered — with overdue flags. The
    symmetric counterpart to the undelivered-directive check (#127): that
    catches sends that never arrived; this catches sends never ANSWERED.

    ``evidence_ids`` is the set of loop ids whose evidence sub-log
    is nonempty — the CALLER does that I/O (this fold stays pure); each
    summary gains ``out_of_band``: True iff this open loop's id is in the set.
    The flag is the REQUESTER's signal ("an answer exists off the bus — close
    your loop explicitly, citing it"), which is why only awaiting_others
    carries it: awaiting_me lists loops where I'm the responder, and a
    mirrored verdict changes nothing about what I owe. Default None ⇒ every
    summary reads False (back-compat: existing callers unchanged)."""
    out: list[dict[str, Any]] = []
    for r in records:
        if is_open_loop(r) and r.get("from") == me:
            s = _summary(r, now)
            s["out_of_band"] = bool(evidence_ids and s.get("id") in evidence_ids)
            out.append(s)
    return out


def loop_board(
    me: str, records: list[dict[str, Any]], *, now,
    evidence_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    """The coordination board projection (core, projection-side only).
    ``evidence_ids`` threads through to awaiting_others (out-of-band flags);
    see its docstring."""
    in_flight: dict[str, int] = {}
    ideas: dict[str, int] = {}
    for r in records:
        kind = loop_kind_of(r)
        if kind == "idea":
            state = loop_state_of(r)
            if state not in terminal_states("idea"):
                ideas[state] = ideas.get(state, 0) + 1
            continue
        if is_open_loop(r):
            in_flight[kind] = in_flight.get(kind, 0) + 1
    return {
        "awaiting_me": awaiting_me(me, records, now=now),
        "awaiting_others": awaiting_others(me, records, now=now,
                                           evidence_ids=evidence_ids),
        "in_flight_by_kind": in_flight,
        "ideas_pipeline": ideas,
    }
