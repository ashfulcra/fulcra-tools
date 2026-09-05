"""Signal-plane payload v1 (spec §3.1)."""
from __future__ import annotations

import json
from typing import Any

PAYLOAD_VERSION = 1
KINDS = ("open", "close", "claim", "release", "note")
PRIORITIES = ("P0", "P1", "P2", "P3")
_PTR_REQUIRED = ("open", "close")


def build_payload(*, at: str, sender: str, to: str, kind: str, slug: str, pri: str, ptr: str | None) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    if pri not in PRIORITIES:
        raise ValueError(f"pri must be one of {PRIORITIES}, got {pri!r}")
    if not slug:
        raise ValueError("slug is required")
    if ptr is None and kind in _PTR_REQUIRED:
        raise ValueError(f"ptr is required on {kind}")
    if ptr is not None and (not ptr or ptr.endswith("/") or "*" in ptr or "?" in ptr):
        raise ValueError("ptr must be one file path — never a directory or a glob")
    return {"v": PAYLOAD_VERSION, "at": at, "from": sender, "to": to, "kind": kind, "slug": slug, "pri": pri, "ptr": ptr}


def parse_event(record: dict[str, Any]) -> dict[str, Any] | None:
    note = record.get("note")
    if not isinstance(note, str):
        return None
    try:
        p = json.loads(note)
    except json.JSONDecodeError:
        return None
    if not isinstance(p, dict) or p.get("v") != PAYLOAD_VERSION or p.get("kind") not in KINDS or not p.get("slug"):
        return None
    out = dict(p)
    out["record_id"] = record.get("id")
    out["recorded_at"] = record.get("recorded_at")
    return out
