"""The bus-v4 cutover switch: which plane SERVES an agent's obligations.

Until now every agent has acted on the FILE plane (bus-v3 task documents): `needs-me` and `obligations` fold
those, while coord-fold folds the annotation stream in parallel and `compare-to-fold` proves the two agree.
This module is the flip. One durable document on the bus,

    team/<team>/_coord/bus-v4/cutover.json   {"v": 1, "serve": "fold" | "files", "at": ISO, "by": ..., "reason": ...}

decides, fleet-wide, what `needs-me` and `obligations` answer from:

* absent / unreadable / malformed / any value but "fold"  -> "files": the old plane stays authoritative. The
  switch fails SAFE toward today's behaviour; nothing is served from a fold nobody proved.
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
    """-> ("fold" | "files", why). Only a well-formed document saying "fold" serves from the fold."""
    try:
        body, state = transport.read_classified(switch_path(team))
    except Exception as exc:                                     # a transport that raises is "files", loudly
        return "files", f"cutover switch unreadable ({exc}); serving from files"
    if state == "absent":
        return "files", "no cutover switch on the bus; serving from files"
    if state != "ok" or not body:
        return "files", f"cutover switch {state}; serving from files"
    try:
        doc = json.loads(body)
    except ValueError:
        return "files", "cutover switch is not JSON; serving from files"
    if not isinstance(doc, dict) or doc.get("v") != SWITCH_VERSION or doc.get("serve") not in SERVE_VALUES:
        return "files", "cutover switch malformed; serving from files"
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


def fold_rows(transport: Any, team: str, agent: str) -> tuple[Optional[list[dict[str, Any]]], str]:
    """The agent's open obligations FROM ITS COORD-FOLD CHECKPOINT, in needs-me row shape, plus the needs-me
    source row disclosing the fold's cursor. (None, why) when the checkpoint cannot be the answer."""
    try:
        body, state = transport.read_classified(checkpoint_path(team, agent))
    except Exception as exc:
        return None, f"coord-fold checkpoint for {agent} unreadable ({exc})"
    if state == "absent":
        return None, f"coord-fold checkpoint for {agent} is absent — this identity has not seeded its fold"
    if state != "ok" or not body:
        return None, f"coord-fold checkpoint for {agent} is {state}"
    try:
        ckpt = json.loads(body)
    except ValueError:
        return None, f"coord-fold checkpoint for {agent} is not JSON"
    if not isinstance(ckpt, dict) or ckpt.get("v") != 1 or not isinstance(ckpt.get("open"), dict):
        return None, f"coord-fold checkpoint for {agent} is malformed"
    rows: list[dict[str, Any]] = []
    for slug, row in ckpt["open"].items():
        if not isinstance(row, dict):
            return None, f"coord-fold checkpoint for {agent} carries a non-object row at {slug!r}"
        rows.append({
            "id": slug, "name": slug, "slug": slug, "title": slug,
            "priority": str(row.get("pri") or "?"), "status": "open", "kind": "task",
            "owner": row.get("from"), "assignee": row.get("to"), "ptr": row.get("ptr"),
            "opened_at": row.get("at"), "claimed_by": row.get("claimed_by"),
            "served_from": "coord-fold",
        })
    rows.sort(key=lambda r: (r["priority"], str(r.get("opened_at") or ""), r["id"]))
    rows.append({"type": "needs-me-source", "source": "projection", "as_of": ckpt.get("cursor"),
                 "fold": "coord-fold", "generation": ckpt.get("generation"), "writer": ckpt.get("writer")})
    return rows, f"served from coord-fold checkpoint (cursor {ckpt.get('cursor')}, generation {ckpt.get('generation')})"


def degraded_row(reason: str) -> dict[str, Any]:
    return {"type": FOLD_DEGRADED, "reason": reason}
