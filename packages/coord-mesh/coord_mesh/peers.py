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


def get_cursor(reg: dict[str, Any], space_id: str, peer_uid: str):
    """This peer's position: ``{"t": <iso>, "ids": [...]}``, a legacy id string,
    or None.

    WHY A WATERMARK AND A LEDGER, not a single record id (codex-coder on
    54458d81). A lone id only works if the sort key never admits a NEW row
    *before* it. Sorting by ``(recorded_at, id)`` is deterministic but NOT
    append-monotonic: a record that arrives later carrying the same timestamp
    and a lexically smaller id sorts before the stored cursor, lands in the
    already-seen slice, and is silently skipped. That is the original
    silent-loss defect once more, narrowed from a whole window to a tied group.

    So the position is a WATERMARK (the newest instant consumed) plus the LEDGER
    of ids seen AT that instant. A row is new when it is later than the
    watermark, or shares the watermark and is not in the ledger — a test that
    cannot be fooled by id ordering, because it never relies on id ordering.
    Same shape coord-engine's E1 fold settled on: watermark plus processed
    ledger.
    """
    return ((reg.get("spaces") or {}).get(space_id, {})
            .get("cursors", {}).get(peer_uid))


def cursor_parts(cursor):
    """Normalize a stored cursor into ``(watermark_or_None, ids_set)``.

    A legacy string cursor carries no time, so it yields ``(None, {id})`` and
    the caller falls back to locating it positionally — the pre-watermark
    behaviour — rather than being silently reinterpreted as "seen nothing",
    which would replay the peer's whole window.
    """
    if not cursor:
        return None, set()
    if isinstance(cursor, str):
        return None, {cursor}
    return cursor.get("t"), set(cursor.get("ids") or [])


def set_cursor(reg: dict[str, Any], space_id: str, peer_uid: str,
               watermark, ids=None) -> None:
    """Advance a cursor to a watermark + the ids seen at it. Empty is REFUSED.

    Writing an empty cursor would silently reset the peer to "never read" and
    replay their whole outbox on the next poll — the at-least-once discipline
    tolerates a repeat, not a full replay.
    """
    if ids is None:                    # legacy single-id form, still accepted
        if not watermark:
            raise ValueError(
                "refusing to write an empty cursor: an unidentifiable record "
                "leaves the queue position UNKNOWN, and resetting would replay "
                "the outbox")
        upsert_space(reg, space_id)["cursors"][peer_uid] = watermark
        return
    if not watermark or not ids:
        raise ValueError(
            "refusing to write an empty cursor: a watermark with no ids leaves "
            "the queue position UNKNOWN, and resetting would replay the outbox")
    upsert_space(reg, space_id)["cursors"][peer_uid] = {
        "t": watermark, "ids": sorted(ids)}
