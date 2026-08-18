"""Peer registry + per-peer cursors, keyed by an opaque ``space``.

The plan's abstraction rule (v1.1 §d): "peer registry keyed by an opaque
``space`` concept so groups slot in as a space kind without breaking pairwise."
Groups are imminent in the platform, and a registry that bakes in pairwise-ness
would have to be rewritten the day they land. So a peer is reached *through* a
space, and a space today is always kind ``pair`` — kind ``group`` slots in
beside it without touching callers.

Cursors live in MY store, never the peer's (v1.1 §b3). A peer cannot be asked
to remember what I have read, and must never be writable by me.
"""
import json
import os
from typing import Any, Optional

KIND_PAIR = "pair"
KIND_GROUP = "group"          # reserved; groups GA is the v2 substrate
KINDS = (KIND_PAIR, KIND_GROUP)

_DEFAULT_PATH = "~/.coord-mesh/peers.json"


def registry_path() -> str:
    return os.path.expanduser(os.environ.get("COORD_MESH_PEERS", _DEFAULT_PATH))


def _empty() -> dict[str, Any]:
    return {"version": 1, "spaces": {}}


def load(path: Optional[str] = None) -> dict[str, Any]:
    """Load the registry. A missing file is an empty registry (first run).

    A CORRUPT file is NOT: it raises, because silently starting from empty would
    reset every cursor and replay every peer's whole outbox.
    """
    p = path or registry_path()
    if not os.path.exists(p):
        return _empty()
    with open(p, "r", encoding="utf-8") as fh:
        raw = fh.read()
    if not raw.strip():
        return _empty()
    data = json.loads(raw)   # ValueError propagates on purpose
    if not isinstance(data, dict) or "spaces" not in data:
        raise ValueError(f"peer registry at {p} is not a registry document")
    return data


def save(reg: dict[str, Any], path: Optional[str] = None) -> str:
    p = path or registry_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(reg, fh, indent=2, sort_keys=True)
    os.replace(tmp, p)        # atomic: a crash mid-write cannot truncate cursors
    return p


def upsert_space(reg: dict[str, Any], space_id: str, *, kind: str = KIND_PAIR,
                 name: Optional[str] = None, members: Optional[list] = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"space kind {kind!r} not in {KINDS}")
    sp = reg.setdefault("spaces", {}).setdefault(space_id, {})
    sp["kind"] = kind
    if name:
        sp["name"] = name
    if members is not None:
        sp["members"] = list(members)
    sp.setdefault("cursors", {})
    return sp


def get_cursor(reg: dict[str, Any], space_id: str, peer_uid: str) -> Optional[str]:
    """Last record id consumed from this peer in this space, or None."""
    return ((reg.get("spaces") or {}).get(space_id, {})
            .get("cursors", {}).get(peer_uid))


def set_cursor(reg: dict[str, Any], space_id: str, peer_uid: str,
               record_id: Optional[str]) -> None:
    """Advance a cursor. A None/empty id is REFUSED.

    Writing an empty cursor would silently reset the peer to "never read" and
    replay their whole outbox on the next poll — the at-least-once discipline
    tolerates a repeat, not a full replay.
    """
    if not record_id:
        raise ValueError(
            "refusing to write an empty cursor: an unidentifiable record leaves "
            "the queue position UNKNOWN, and resetting would replay the outbox"
        )
    upsert_space(reg, space_id)["cursors"][peer_uid] = record_id
