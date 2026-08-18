"""Mesh envelope: bus-v3 v1 notes gain ``to_user`` alongside ``to``.

Addressing across accounts needs one more field than addressing within one, and
the plan (v1.1 §b1) is explicit that the existing envelope stays readable: a
mesh reader filters ``to_user == my uid``; a bus-v3 reader that has never heard
of the mesh still sees a well-formed v1 note and ignores the extra key.

Consent and addressing are SEPARATE (v1.1 §b2, corrected from the M1 findings):
this envelope says WHERE a message is going, and says nothing about what the
recipient is allowed to see. The datashare is the consent object, and only
where no broader grant already exists.
"""
import json
from typing import Any, Optional

from . import safety

V = 1
KINDS = ("directive", "response", "verdict", "claim")
PRIORITIES = ("P0", "P1", "P2", "P3")

#: Broadcast on the event plane. Distinct from the task plane's "*".
BROADCAST = "all"


def build(*, to_user: str, kind: str, slug: str, to: str = BROADCAST,
          priority: str = "P2", ptr: Optional[str] = None) -> dict[str, Any]:
    """Build a mesh-addressed v1 note. Raises on a bad shape rather than
    emitting something a peer will silently drop.

    ``to_user`` goes through the named-uid rail: an event addressed at a name
    instead of a uid would be delivered to nobody and look sent.
    """
    safety.require_named_uid(to_user)
    if kind not in KINDS:
        raise ValueError(f"kind {kind!r} not in {KINDS}")
    if priority not in PRIORITIES:
        raise ValueError(f"priority {priority!r} not in {PRIORITIES}")
    if not (slug or "").strip():
        raise ValueError("slug is required — an event with no slug cannot be answered")

    note: dict[str, Any] = {
        "v": V, "to": to, "to_user": to_user,
        "kind": kind, "pri": priority, "slug": slug,
    }
    # The content rule: claims and pure acks may be envelope-only, but anything
    # substantive carries a pointer to a durable doc. Events are pointers; the
    # obligation rides the doc (Leif demo learning).
    if ptr:
        note["ptr"] = ptr
    return note


def encode(note: dict[str, Any]) -> str:
    """The note field is a JSON *string*, not an object — see wire.F_NOTE."""
    return json.dumps(note, separators=(",", ":"), sort_keys=True)


def parse(text: Optional[str]) -> Optional[dict[str, Any]]:
    """Parse a note payload, or None if it is not a v1 envelope.

    None means "not mesh traffic" — legacy prose notes share the channel and
    must be skipped without raising.
    """
    if not text:
        return None
    try:
        note = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(note, dict) or note.get("v") != V:
        return None
    return note


def addressed_to(note: dict[str, Any], my_uid: str) -> bool:
    """Is this note for me?

    A note with no ``to_user`` is same-account bus traffic, not mesh traffic,
    and is NOT mine to consume off a peer's outbox — returning True there would
    make every peer read swallow that peer's internal bus.
    """
    tu = note.get("to_user")
    if not tu:
        return False
    return str(tu) == str(my_uid) or str(tu) == BROADCAST
