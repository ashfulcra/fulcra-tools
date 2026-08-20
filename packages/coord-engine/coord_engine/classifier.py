"""Shared identity resolution and conservative canonical-read classification.

Both seams preserve uncertainty.  An exported session identity is a stronger
fact than local state or a machine name, while a failed read is never evidence
of absence.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Union


_IDENTITY_SAFE = re.compile(r"[^A-Za-z0-9:_.-]+")


class CanonicalState(str, Enum):
    PRESENT = "PRESENT"
    EMPTY = "EMPTY"
    TOMBSTONED = "TOMBSTONED"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class CanonicalRead:
    state: CanonicalState
    documents: tuple[str, ...] = ()


def sanitize_hostname(raw: str) -> tuple[str, bool]:
    """Return an injective safe hostname, or an empty string when none is usable."""
    collapsed = _IDENTITY_SAFE.sub("-", raw).strip("-.:_")
    if collapsed == raw:
        return raw, False
    if not collapsed:
        return "", True
    digest = hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()[:6]
    return f"{collapsed}-{digest}", True


def resolve_identity(
    explicit: Optional[str] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    persisted: Union[Optional[str], Callable[[], Optional[str]]] = None,
    hostname: Optional[Callable[[], str]] = None,
    on_hostname_rewritten: Optional[Callable[[str, str], None]] = None,
) -> Optional[str]:
    """Resolve exactly ``explicit > env > persisted > sanitized host``.

    ``hostname`` is deliberately lazy so a declared identity never touches the
    host minting path.  This keeps two sessions on one host separate.
    """
    if explicit:
        return explicit
    env = environ or {}
    declared = env.get("FULCRA_COORD_AGENT")
    if declared:
        return declared
    saved = persisted() if callable(persisted) else persisted
    if saved:
        return saved
    if hostname is None:
        return None
    raw = hostname()
    safe, rewritten = sanitize_hostname(raw)
    if not safe:
        raise RuntimeError(
            "cannot derive a fleet identity: this host's name contains no "
            "usable characters (" + repr(raw) + "). Set FULCRA_COORD_AGENT to "
            "an explicit identity before writing to the coordination store."
        )
    if rewritten and on_hostname_rewritten is not None:
        on_hostname_rewritten(raw, safe)
    return f"coord-reconcile:{safe}"


def canonical_read(transport: Any, prefix: str) -> CanonicalRead:
    """Read a canonical directory without collapsing distinct negative facts.

    A listing is positive evidence of empty only when it completed.  A listed
    document is read through the transport's classified seam; any unreadable
    document makes the whole result UNKNOWN.  Lifecycle records are tombstones
    only when their explicit state proves deletion/archive, and malformed entry
    shapes remain unsupported rather than becoming an empty listing.
    """
    try:
        listing = transport.list_dir(prefix)
    except Exception:
        return CanonicalRead(CanonicalState.UNKNOWN)
    if not isinstance(listing, list):
        return CanonicalRead(CanonicalState.UNSUPPORTED)
    if not listing:
        return CanonicalRead(CanonicalState.EMPTY)

    documents: list[str] = []
    tombstones = 0
    for entry in listing:
        if not isinstance(entry, dict):
            return CanonicalRead(CanonicalState.UNSUPPORTED)
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            return CanonicalRead(CanonicalState.UNSUPPORTED)
        lifecycle = entry.get("state")
        if lifecycle in ("archived", "deleted", "tombstoned"):
            tombstones += 1
            continue
        if lifecycle is not None and lifecycle not in ("uploaded", "present"):
            return CanonicalRead(CanonicalState.UNSUPPORTED)
        path = name if name.startswith(prefix) else prefix + name
        reader = getattr(transport, "read_classified", None)
        if reader is None:
            return CanonicalRead(CanonicalState.UNSUPPORTED)
        try:
            content, status = reader(path)
        except Exception:
            return CanonicalRead(CanonicalState.UNKNOWN)
        if status in ("error", "absent"):
            return CanonicalRead(CanonicalState.UNKNOWN)
        if status != "ok" or not isinstance(content, str):
            return CanonicalRead(CanonicalState.UNSUPPORTED)
        documents.append(content)

    if documents:
        return CanonicalRead(CanonicalState.PRESENT, tuple(documents))
    if tombstones:
        return CanonicalRead(CanonicalState.TOMBSTONED)
    return CanonicalRead(CanonicalState.UNSUPPORTED)
