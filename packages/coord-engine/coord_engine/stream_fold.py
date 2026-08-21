"""needs-me folded from the annotation stream, forward from a durable cursor.

The architecture (operator-ordered, rule doc
``_coord/rules/2026-08-21-stream-is-the-architecture.md``): comms are the
annotation stream; folds read it FORWARD FROM A DURABLE CURSOR with cost
proportional to new events; files are content the stream points at, never a
thing enumerated to discover work; ``data-updates`` answers only "is there
anything new".

This module NEVER lists a directory. Its inputs are one bounded stream read
after the cursor and its own saved state; its outputs are the open-obligation
fold and an advanced cursor, written together in one document so they cannot
disagree.

Event semantics (see :mod:`coord_engine.records`):

- ``kind=directive`` addressed to the agent (or broadcast) OPENS the slug —
  unless the payload carries ``fyi`` (a notification opens nothing, 2026-08-11).
- ``kind=response`` for the same slug CLOSES it — but ONLY the responder's own
  copy: closes are attributed via the record's ``sources`` and a close by bob
  never discharges alice's copy of a broadcast (codex-coder round 2, 2026-08-21).
  Known limit, stated not hidden: a directed obligation closed by someone OTHER
  than its assignee (e.g. the owner marking it done) stays open in the
  assignee's fold until they close it themselves.
- Everything else on the track is data, not control plane, and is skipped by
  :func:`records.parse_payload` returning None.

Honesty: a failed stream read is UNKNOWN and nonzero — the saved fold is never
served as fresh. A served fold carries its ``as_of`` horizon so staleness is
declared, not hidden.

Bootstrap: the stream cannot retroactively contain history older than its
retention, so a state document may be SEEDED once at cutover from the last
file-plane fold the fleet ever runs (``seed_state``). After that the cursor
only moves forward.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import records
from .outcome import CommandOutcome, CoverageState, SurfaceCoverage

STATE_VERSION = 1
#: Overlap re-read window; seen-id suppression collapses the repeats. Mirrors
#: records.CURSOR_SKEW_SECONDS in spirit but wider: the fold replays cheaply.
OVERLAP_SECONDS = 600
#: First-run lookback when no state exists (matches the queue's bootstrap).
DEFAULT_LOOKBACK_SECONDS = records.DEFAULT_LOOKBACK_SECONDS
SEEN_CAP = 4000


def state_path(team: str, agent: str) -> str:
    return f"team/{team}/_coord/agents/{agent}/stream-fold.json"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def empty_state(now: datetime) -> dict[str, Any]:
    return {"v": STATE_VERSION,
            "cursor": _iso(now - timedelta(seconds=DEFAULT_LOOKBACK_SECONDS)),
            "seen": [], "open": {}}


def load_state(transport: Any, team: str, agent: str,
               now: datetime) -> tuple[dict[str, Any], str]:
    """(state, source) where source is one of seeded|fresh|invalid."""
    raw = transport.read(state_path(team, agent))
    if raw is None:
        return empty_state(now), "fresh"
    try:
        doc = json.loads(raw)
    except ValueError:
        return empty_state(now), "invalid"
    if (not isinstance(doc, dict) or doc.get("v") != STATE_VERSION
            or _parse_iso(str(doc.get("cursor") or "")) is None
            or not isinstance(doc.get("open"), dict)
            or not isinstance(doc.get("seen"), list)):
        # Typed INVALID: the caller must fail closed and leave the document
        # for forensics. Resetting to a fresh lookback here silently discarded
        # every obligation older than the default window (round-2 finding 1).
        return doc if isinstance(doc, dict) else {}, "invalid"
    return doc, "seeded"


def apply_events(state: dict[str, Any], rows: list[dict[str, Any]],
                 agent: str) -> int:
    """Fold new stream records into state; returns how many were new.

    Pure: no transport, no clock. Rows may arrive in any order; they are
    applied in ``recorded_at`` order so a same-batch open+close lands closed.
    """
    seen = set(state["seen"])
    fresh = 0
    for rec in sorted(rows or [], key=lambda r: str(r.get("recorded_at") or "")):
        rid = rec.get("id")
        if not isinstance(rid, str) or not rid or rid in seen:
            continue
        seen.add(rid)
        fresh += 1
        ev = records.parse_payload(rec.get("note"))
        if ev is None:
            continue
        slug = ev["slug"]
        if ev["kind"] == "directive" and ev["to"] in (agent, records.BROADCAST):
            if ev.get("fyi"):
                continue  # a notification opens nothing
            state["open"][slug] = {
                "pri": ev.get("pri") or "P3",
                "at": str(rec.get("recorded_at") or "")[:19],
                "ptr": ev.get("ptr"),
            }
        elif ev["kind"] == "response":
            # Close ONLY the responder's own copy: a response is attributed via
            # the record's sources, and bob answering a broadcast must not
            # discharge alice's obligation (round-2 finding 2).
            if records.sender_of(rec) == agent:
                state["open"].pop(slug, None)
    state["seen"] = sorted(seen)[-SEEN_CAP:]
    return fresh


def fold(transport: Any, team: str, agent: str, *,
         now: Optional[datetime] = None) -> CommandOutcome:
    """One pass: read forward, apply, persist, report. Zero enumeration."""
    now = now or datetime.now(timezone.utc)
    cfg, status = records.load_config_classified(transport, team)
    if not cfg:
        return CommandOutcome.from_surfaces(rows=(), coverage=(
            SurfaceCoverage("stream", CoverageState.UNKNOWN,
                            reason=f"records config {status}"),))
    state, source = load_state(transport, team, agent, now)
    if source == "invalid":
        return CommandOutcome.from_surfaces(rows=(), coverage=(
            SurfaceCoverage(
                "stream", CoverageState.UNKNOWN,
                reason="persisted fold state is corrupt or incompatible — left "
                       "untouched for forensics; repair or reseed it explicitly "
                       "(resetting silently would discard old obligations)"),))
    cur = _parse_iso(state["cursor"])
    start = _iso(cur - timedelta(seconds=OVERLAP_SECONDS))
    rows = transport.records(cfg["data_type"], start, _iso(now))
    if rows is None:
        return CommandOutcome.from_surfaces(rows=(), coverage=(
            SurfaceCoverage(
                "stream", CoverageState.UNKNOWN,
                reason=f"stream read failed; last good fold as of "
                       f"{state['cursor']} not served as fresh"),))
    fresh = apply_events(state, rows, agent)
    state["cursor"] = _iso(now)
    persisted = bool(transport.write(
        state_path(team, agent), json.dumps(state, sort_keys=True)))
    out_rows = tuple(
        {"slug": slug, **meta}
        for slug, meta in sorted(state["open"].items(),
                                 key=lambda kv: (kv[1].get("pri") or "P3",
                                                 kv[1].get("at") or "")))
    if not persisted:
        # The architecture's claim IS the durable cursor; a fold that cannot
        # persist it has not checkpointed and must not exit 0 (round-2
        # finding 4). Rows are still shown — they are true — under UNKNOWN.
        return CommandOutcome.from_surfaces(rows=out_rows, coverage=(
            SurfaceCoverage(
                "stream", CoverageState.UNKNOWN,
                reason=f"as-of {state['cursor']}; +{fresh} events; STATE WRITE "
                       f"FAILED — cursor not advanced durably, this pass is "
                       f"not a checkpoint"),))
    return CommandOutcome.from_surfaces(rows=out_rows, coverage=(
        SurfaceCoverage("stream",
                        CoverageState.DATA if out_rows else CoverageState.CLEAR,
                        reason=f"as-of {state['cursor']}; +{fresh} events; "
                               f"state {source}"),))
