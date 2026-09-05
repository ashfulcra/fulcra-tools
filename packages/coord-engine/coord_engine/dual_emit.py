"""Dual-emit: mirror every bus-v3 write onto bus-v4, the annotation-native plane coord-fold folds.

Plan Task 13 (docs/superpowers/plans/2026-09-04-coord-fold.md; directive 65761fbd). Called ONCE at the end of
``records.emit_event`` — the engine's single write chokepoint — so no verb has to opt in and none can be missed.

Contract, each clause a failure it prevents:
* No bus-v4 config on the store -> no-op (returns False). A hardcoded fallback channel would let a host mirror
  onto a plane nobody folds; the config document is the only authority (G15).
* Mirror failure NEVER fails the v3 write: the v3 record is the delivery the fleet relies on today; the mirror is
  the parallel-bus evidence. This function does not raise.
* Kind map is total over the kinds that carry obligations; anything else is not mirrored:
  directive->open, response->close (of the slug it closes), claim->claim, verdict->note.
* The payload is the eight-field coord-fold event, written literally (v, at, from, to, kind, slug, pri, ptr).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

V4_CONFIG = "team/{team}/_coord/bus-v4/records.json"
KIND_MAP = {"directive": "open", "response": "close", "claim": "claim", "verdict": "note"}
PAYLOAD_VERSION = 1
PRIORITIES = ("P0", "P1", "P2", "P3")


def v4_config(transport: Any, team: str) -> Optional[dict[str, str]]:
    """The bus-v4 channel, or None when absent/unreadable/malformed (all three are 'do not mirror')."""
    try:
        body, state = transport.read_classified(V4_CONFIG.format(team=team))
    except Exception:
        return None
    if state != "ok" or not body:
        return None
    try:
        cfg = json.loads(body)
    except ValueError:
        return None
    if not isinstance(cfg, dict) or not cfg.get("data_type") or not cfg.get("api_version"):
        return None
    return {"data_type": str(cfg["data_type"]), "api_version": str(cfg["api_version"])}


def payload(*, at: str, sender: str, to: str, kind: str, slug: str, pri: str, ptr: Optional[str]) -> dict[str, Any]:
    return {"v": PAYLOAD_VERSION, "at": at, "from": sender, "to": to, "kind": kind, "slug": slug, "pri": pri, "ptr": ptr}


def mirror(transport: Any, team: Optional[str], *, sender: str, to: str, kind: str, priority: str, slug: str,
           ptr: Optional[str] = None, recorded_at: Optional[str] = None) -> bool:
    """Mirror one v3 event onto bus-v4. True only if the v4 record write confirmed. Never raises."""
    try:
        if not team or kind not in KIND_MAP or priority not in PRIORITIES or not slug:
            return False
        cfg = v4_config(transport, team)
        if cfg is None:
            return False
        at = recorded_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        note = json.dumps(payload(at=at, sender=sender, to=to, kind=KIND_MAP[kind], slug=slug, pri=priority, ptr=ptr),
                          sort_keys=True)
        return bool(transport.record_write(cfg["data_type"], cfg["api_version"], note, sender, recorded_at=recorded_at))
    except Exception:
        return False
