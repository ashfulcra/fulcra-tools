"""The bus-v4 cutover switch: which plane SERVES an agent's obligations.

Until now every agent has acted on the FILE plane (bus-v3 task documents): `needs-me` and `obligations` fold
those, while coord-fold folds the annotation stream in parallel and `compare-to-fold` proves the two agree.
This module is the flip. One durable document on the bus,

    team/<team>/_coord/bus-v4/cutover.json   {"v": 1, "serve": "fold" | "files", "at": ISO, "by": ..., "reason": ...}

decides, fleet-wide, what `needs-me` and `obligations` answer from:

* absent -> "files": the old plane stays authoritative until a switch exists. A well-formed "files" also serves files.
* unreadable / not JSON / malformed -> "unknown": authority cannot be established, so `needs-me` and `obligations`
  answer UNKNOWN (rc 3) and consult NOTHING — a host that cannot read the switch after the flip must not answer
  from files and report clean (split-brain; codex-reviewer P0 on engine-ship-gate-6445cde8).
* "fold" -> the agent's coord-fold checkpoint (team/<team>/member/<agent>/fold/checkpoint.json) is the answer.
  A checkpoint that is absent, unreadable or corrupt is UNKNOWN (rc 3) — never CLEAR: an identity that has not
  seeded its fold does not thereby owe nothing (Ash, 2026-09-06: the identities woken after the flip are exactly
  the ones with no seed yet).

Rollback is one write: `coord-engine cutover set <team> --serve files --reason ...`. Every read happens on the
next verb invocation; there is no cached copy of the switch anywhere.

Cost: one pointed read of the switch and one of the checkpoint per invocation. Nothing here lists a directory.
"""
from __future__ import annotations

import json
from typing import Any, Optional

SWITCH_VERSION = 1
SWITCH = "team/{team}/_coord/bus-v4/cutover.json"
CHECKPOINT = "team/{team}/member/{agent}/fold/checkpoint.json"
SERVE_VALUES = ("fold", "files")
FOLD_DEGRADED = "fold-degraded"          # endswith "degraded": the envelope reads it as UNKNOWN, rc 3


def switch_path(team: str) -> str:
    return SWITCH.format(team=team)


def checkpoint_path(team: str, agent: str) -> str:
    return CHECKPOINT.format(team=team, agent=agent)


def read_switch(transport: Any, team: str) -> tuple[str, str]:
    """-> ("fold" | "files" | "unknown", why).

    Three states, because two were a split-brain (codex-reviewer, engine-ship-gate-6445cde8 P0): once the fleet
    has flipped, a host that CANNOT READ the switch must not answer from files and report a clean result.
    * "files"   — the switch is ABSENT (pre-cutover) or a well-formed document says files.
    * "fold"    — a well-formed document says fold.
    * "unknown" — the read failed, raised, or returned bytes that are not a switch. Authority cannot be
                  established, so the public reads answer UNKNOWN (rc 3) and consult nothing.
    """
    try:
        body, state = transport.read_classified(switch_path(team))
    except Exception as exc:
        return "unknown", f"cutover switch unreadable ({exc}); authority unknown"
    if state == "absent":
        return "files", "no cutover switch on the bus; serving from files"
    if state != "ok" or not body:
        return "unknown", f"cutover switch read state {state!r}; authority unknown"
    try:
        doc = json.loads(body)
    except ValueError:
        return "unknown", "cutover switch is not JSON; authority unknown"
    if not isinstance(doc, dict) or doc.get("v") != SWITCH_VERSION or doc.get("serve") not in SERVE_VALUES:
        return "unknown", "cutover switch is malformed; authority unknown"
    if doc.get("serve") == "fold":
        return "fold", f"cutover switch says fold (set {doc.get('at')} by {doc.get('by')})"
    return "files", f"cutover switch says files (set {doc.get('at')} by {doc.get('by')})"


def switch_doc(*, serve: str, by: str, reason: str, at: str) -> str:
    if serve not in SERVE_VALUES:
        raise ValueError(f"serve must be one of {SERVE_VALUES}")
    if not reason.strip():
        raise ValueError("a reason is required")
    return json.dumps({"v": SWITCH_VERSION, "serve": serve, "at": at, "by": by, "reason": reason.strip()},
                      sort_keys=True, indent=1) + "\n"


def fold_rows(transport: Any, team: str, agent: str
              ) -> tuple[Optional[list[dict[str, Any]]], str, Optional[str]]:
    """The agent's open obligations FROM ITS COORD-FOLD CHECKPOINT, in needs-me row shape, plus the needs-me
    source row disclosing the fold's cursor.

    -> (rows, why, unhealthy). ``rows`` is None when the checkpoint cannot be the answer at all. ``unhealthy`` is
    a reason when the checkpoint IS readable but its own health fields say the fold is incomplete — coord-fold's
    contract (codex-reviewer P0, engine-ship-gate-c4a8410a): ``unread_events > 0`` means opens may be unapplied,
    ``unreadable_pointers`` non-empty means rows the fold could not classify; either is UNKNOWN (rc 3) at the
    serving boundary, with the known rows retained as PARTIAL data. Both fields are type-checked; malformed
    health metadata is unhealthy, never healthy output."""
    try:
        body, state = transport.read_classified(checkpoint_path(team, agent))
    except Exception as exc:
        return None, f"coord-fold checkpoint for {agent} unreadable ({exc})", None
    if state == "absent":
        # Two situations produce this one absent read, and a POINTED read cannot tell them apart: an identity
        # that is real but has not seeded a fold, and a name that is simply mistyped. Both are UNKNOWN, so
        # neither is unsafe — but the old wording asserted the first, and a mistyped name then reads as a live
        # fleet regression ("this agent stopped folding") rather than as a typo. Separating them would cost a
        # directory listing, which this module does not do (see the module docstring). So state the measurement
        # and name both readings instead of picking one.
        return None, (f"coord-fold checkpoint for {agent} is absent — no fold is seeded under this exact name. "
                      f"Identity names are exact; a mistyped name is indistinguishable from an unseeded "
                      f"identity at this cost, so check the name before concluding the fold broke"), None
    if state != "ok" or not body:
        return None, f"coord-fold checkpoint for {agent} is {state}", None
    try:
        ckpt = json.loads(body)
    except ValueError:
        return None, f"coord-fold checkpoint for {agent} is not JSON", None
    if not isinstance(ckpt, dict) or ckpt.get("v") != 1 or not isinstance(ckpt.get("open"), dict):
        return None, f"coord-fold checkpoint for {agent} is malformed", None
    rows: list[dict[str, Any]] = []
    for slug, row in ckpt["open"].items():
        if not isinstance(row, dict):
            return None, f"coord-fold checkpoint for {agent} carries a non-object row at {slug!r}", None
        rows.append({
            "id": slug, "name": slug, "slug": slug, "title": slug,
            "priority": str(row.get("pri") or "?"), "status": "open",
            "kind": "review" if slug.startswith("review-request-") else "task",
            "owner": row.get("from"), "assignee": row.get("to"), "ptr": row.get("ptr"),
            "opened_at": row.get("at"), "claimed_by": row.get("claimed_by"),
            "served_from": "coord-fold",
        })
    rows.sort(key=lambda r: (r["priority"], str(r.get("opened_at") or ""), r["id"]))
    rows.append({"type": "needs-me-source", "source": "projection", "as_of": ckpt.get("cursor"),
                 "fold": "coord-fold", "generation": ckpt.get("generation"), "writer": ckpt.get("writer")})
    unhealthy = checkpoint_health_reason(ckpt, agent)
    return rows, f"served from coord-fold checkpoint (cursor {ckpt.get('cursor')}, generation {ckpt.get('generation')})", unhealthy


def checkpoint_health_reason(ckpt: dict[str, Any], agent: str) -> Optional[str]:
    """None when the checkpoint's own health fields say the fold is complete; otherwise why it is not."""
    unread = ckpt.get("unread_events")
    if not isinstance(unread, int) or isinstance(unread, bool) or unread < 0:
        return f"coord-fold checkpoint for {agent}: unread_events is not a non-negative integer ({unread!r})"
    pointers = ckpt.get("unreadable_pointers")
    if not isinstance(pointers, list) or not all(isinstance(x, str) for x in pointers):
        return f"coord-fold checkpoint for {agent}: unreadable_pointers is not a list of strings ({pointers!r})"
    if unread > 0:
        return f"coord-fold checkpoint for {agent} has {unread} unread event(s) — opens may be unapplied"
    if pointers:
        return f"coord-fold checkpoint for {agent} has {len(pointers)} unreadable pointer(s): {pointers[:3]}"
    return None


def degraded_row(reason: str) -> dict[str, Any]:
    return {"type": FOLD_DEGRADED, "reason": reason}
