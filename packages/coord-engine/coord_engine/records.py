"""Coord v3 — the typed-record control plane.

The bus began as files, and every read walks a directory tree that now holds
~1,500 task docs. Folds compensate with aggregate indexes, overlays and time
budgets, and they still lose: the fleet listener degrades on roughly nine ticks
in ten. All of that machinery exists because the file store has no index.

The typed-record API *is* an index — time-ordered, filterable by type,
answerable in one range query. This module carries coordination EVENTS on it.
Documents stay files; a record that needs a body points at one.

Measured 2026-07-27: a record is readable ~20s after write (one observation),
comfortably inside the router's 60s cadence, so records can serve as a trigger
and not merely as an index.

Payload contract
----------------
Only sanctioned annotation fields survive a write — we lost structured payload
fields silently before learning that — so the payload rides as compact JSON in
``note``, with the sender in ``sources`` where it is queryable:

    sources: ["coord-boss"]
    note:    {"v":1,"to":"codex-coder","kind":"directive","pri":"P0",
              "slug":"...","ptr":"task/....md"}

``ptr`` is present only when there is a body worth reading. Most events have no
body and cost no file read at all.
"""
from __future__ import annotations

import json
from typing import Any, Optional

#: Payload schema version. Bump only for incompatible shape changes; readers
#: must ignore payloads whose version they do not know rather than guess.
PAYLOAD_VERSION = 1

#: Event classes carried on the control plane. One data type carries all of
#: them, discriminated by ``kind``: ``get-records`` filters by type, so one type
#: plus a kind field costs one query where five types would cost five.
KINDS = ("directive", "response", "verdict", "claim")

#: Prefix identifying records the engine's own projection wrote. Records from
#: any other source are data, not control-plane events.
PROJECTION_SOURCE_PREFIX = "com.fulcradynamics.fulcra-coord"


def build_payload(*, to: str, kind: str, priority: str, slug: str,
                  ptr: Optional[str] = None) -> str:
    """Serialize a control-plane payload for the ``note`` field.

    Raises ``ValueError`` on an unknown ``kind`` — a mistyped event class must
    fail at the write, not decay into an event nobody routes.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    payload: dict[str, Any] = {
        "v": PAYLOAD_VERSION, "to": to, "kind": kind,
        "pri": priority, "slug": slug,
    }
    if ptr:
        payload["ptr"] = ptr
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def parse_payload(note: Any) -> Optional[dict[str, Any]]:
    """Parse a ``note`` into a control-plane payload, or None if it isn't one.

    None means "not a control-plane event" — free-text notes on the same track
    are ordinary annotations and must be skipped silently, not treated as
    malformed. A payload carrying an unknown ``v`` also returns None: a reader
    that guesses at a shape it does not know is worse than one that waits.
    """
    if not isinstance(note, str) or not note.startswith("{"):
        return None
    try:
        obj = json.loads(note)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("v") != PAYLOAD_VERSION:
        return None
    kind, to, slug = obj.get("kind"), obj.get("to"), obj.get("slug")
    if kind not in KINDS:
        return None
    if not isinstance(to, str) or not isinstance(slug, str) or not slug:
        return None
    return {
        "to": to, "kind": kind, "slug": slug,
        "pri": obj.get("pri"), "ptr": obj.get("ptr"),
    }


def sender_of(record: dict[str, Any]) -> Optional[str]:
    """The authoring agent from ``sources``, or None if unattributed.

    Attribution is self-declared today, exactly as it is in the file bus. That
    is fine inside one account and stops being fine at a share boundary, which
    is why platform-attested authorship is a MESH prerequisite rather than
    something this module pretends to solve.
    """
    for src in record.get("sources") or []:
        if isinstance(src, str) and src and not src.startswith("com.fulcradynamics."):
            return src
    return None


#: Recipient value that addresses every agent on the bus. Readers keep events
#: whose ``to`` is their own name OR this value; a reader that matches only its
#: literal name silently drops fleet-wide directives.
BROADCAST = "all"


def events_for(records: Optional[list], agent: str) -> Optional[list[dict[str, Any]]]:
    """Control-plane events addressed to ``agent`` (or broadcast), newest last.

    ``records is None`` propagates as None — an UNKNOWN window must never be
    presented as an empty one. That is the same fail-closed rule the file folds
    follow, and it matters more here: the caller advances a cursor on success.

    Duplicate records (same id) collapse to one event — the record API can
    return the same record more than once (observed live 2026-07-27).
    """
    if records is None:
        return None
    out: list[dict[str, Any]] = []
    seen_ids: set = set()
    for rec in records:
        if not isinstance(rec, dict):
            return None
        payload = parse_payload(rec.get("note"))
        if payload is None:
            continue  # ordinary annotation on the same track
        if payload["to"] not in (agent, BROADCAST):
            continue
        rec_id = rec.get("id")
        if rec_id is not None:
            if rec_id in seen_ids:
                continue
            seen_ids.add(rec_id)
        out.append({
            "slug": payload["slug"],
            "kind": payload["kind"],
            "priority": payload["pri"],
            "ptr": payload["ptr"],
            "from": sender_of(rec),
            "recorded_at": rec.get("recorded_at"),
            "record_id": rec.get("id"),
        })
    out.sort(key=lambda e: str(e.get("recorded_at") or ""))
    return out


def compare_to_file_fold(record_events: Optional[list[dict[str, Any]]],
                         file_slugs: set[str]) -> dict[str, Any]:
    """Shadow-comparison for the migration's read-path cutover.

    Returns ``{status, only_in_records, only_in_files}``. ``status`` is
    ``"unknown"`` when the record window was UNKNOWN — never ``"match"``, since
    an unknown window trivially "agrees" with anything and would green-light a
    cutover on no evidence. That mistake is the whole reason this function
    exists instead of a bare set comparison at the call site.
    """
    if record_events is None:
        return {"status": "unknown", "only_in_records": [], "only_in_files": []}
    rec_slugs = {e["slug"] for e in record_events}
    only_rec = sorted(rec_slugs - file_slugs)
    only_file = sorted(file_slugs - rec_slugs)
    return {
        "status": "match" if not only_rec and not only_file else "divergent",
        "only_in_records": only_rec,
        "only_in_files": only_file,
    }
