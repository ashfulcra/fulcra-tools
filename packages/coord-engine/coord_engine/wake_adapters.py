"""Zero-model W6 wake-adapter legs.

The queued-file leg is host-local and intentionally carries only a keyed
``check the bus`` nudge.  SessionStart consumes it; it never interprets event
content.  The Routine leg records alignment under the router-owned namespace;
it explicitly does *not* create or target a cloud session.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_KEY = re.compile(r"^[A-Za-z0-9_./:-]{1,512}$")


def _slug(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip("-") or "identity"
    return f"{stem}-{hashlib.sha256(value.encode()).hexdigest()[:8]}"


def default_wake_root() -> Path:
    configured = os.environ.get("COORD_WAKE_DIR")
    if configured:
        return Path(configured).expanduser()
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state).expanduser() if state else Path.home() / ".local" / "state"
    return base / "coord-engine" / "wakes"


def wake_agent_dir(team: str, agent: str, *, root: Optional[Path] = None) -> Path:
    return (root or default_wake_root()) / _slug(team) / _slug(agent)


def _invocation(inv: dict[str, Any], adapter: str) -> tuple[str, str]:
    if inv.get("adapter") != adapter:
        raise ValueError(f"expected {adapter!r} invocation")
    agent, key = inv.get("agent"), inv.get("idempotency_key")
    if not isinstance(agent, str) or not agent:
        raise ValueError("wake invocation has no agent")
    if not isinstance(key, str) or not key:
        raise ValueError("wake invocation has no idempotency_key")
    if not _KEY.fullmatch(key):
        raise ValueError("wake invocation has an unsafe idempotency_key")
    return agent, key


def queue_wake_file(
    team: str,
    inv: dict[str, Any],
    *,
    root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Path:
    """Atomically write one idempotency-keyed host-local nudge.

    Duplicate at-least-once deliveries self-overwrite the same filename.  Raw
    event prose is deliberately not persisted: the consumer only needs the key
    to know that an authoritative bus check is due.
    """
    agent, key = _invocation(inv, "queued-wake-file")
    directory = wake_agent_dir(team, agent, root=root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{hashlib.sha256(key.encode()).hexdigest()}.json"
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    doc = {
        "agent": agent,
        "key": key,
        "queued_at": stamp.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "team": team,
        "type": "coord-queued-wake",
    }
    fd, temporary = tempfile.mkstemp(prefix=".wake-", dir=directory)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(doc, handle, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def consume_wake_files(
    team: str,
    agent: str,
    *,
    root: Optional[Path] = None,
    limit: int = 64,
) -> dict[str, Any]:
    """Consume valid exact-identity nudges once and return bounded hook context.

    Invalid/cross-identity files remain on disk and are reported, never silently
    discarded.  The stored event message is not replayed into model context.
    """
    directory = wake_agent_dir(team, agent, root=root)
    keys: list[str] = []
    errors: list[str] = []
    if directory.exists():
        for path in sorted(directory.glob("*.json"))[:limit]:
            try:
                doc = json.loads(path.read_text())
            except (OSError, ValueError):
                errors.append(f"{path.name}: invalid JSON")
                continue
            if not isinstance(doc, dict) or doc.get("type") != "coord-queued-wake":
                errors.append(f"{path.name}: invalid wake shape")
                continue
            if doc.get("team") != team or doc.get("agent") != agent:
                errors.append(f"{path.name}: identity mismatch")
                continue
            key = doc.get("key")
            if not isinstance(key, str) or not _KEY.fullmatch(key):
                errors.append(f"{path.name}: invalid key")
                continue
            keys.append(key)
            path.unlink()
    context = ""
    if keys:
        context = (
            f"coord wake nudge: {len(keys)} queued wake(s) were consumed. "
            "Check the coordination bus with the authoritative briefing; "
            "no action is encoded in these wake files. Keys: "
            + ", ".join(keys[:8])
        )
    if errors:
        degraded = (
            f"coord queued-wake degraded: {len(errors)} unreadable wake file(s) "
            "remain for operator inspection."
        )
        context = f"{context}\n{degraded}".strip()
    return {"context": context[:2000], "count": len(keys), "errors": errors,
            "keys": keys}


def alignment_filename(key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", key).strip("-") or "alignment"
    return f"{safe[:96]}-{hashlib.sha256(key.encode()).hexdigest()[:8]}.json"


def align_routine(
    transport: Any,
    team: str,
    inv: dict[str, Any],
    *,
    eligible_at: str,
    aligned_at: Optional[str] = None,
) -> str:
    """Record work for an agent's *existing, self-armed* cloud Routine.

    This is alignment/bookkeeping, not an exact-session wake.  The marker lives
    solely in the router-owned namespace and says so explicitly.
    """
    agent, key = _invocation(inv, "routine-align")
    aligned_at = aligned_at or datetime.now(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    record = {
        "agent": agent,
        "aligned_at": aligned_at,
        "eligible_at": eligible_at,
        "key": key,
        "mode": "self-armed-routine",
        "no_session_created": True,
    }
    path = (
        f"team/{team}/_coord/router/routine-align/"
        f"{alignment_filename(key)}"
    )
    if transport.write(path, json.dumps(record, sort_keys=True) + "\n") is False:
        raise RuntimeError(f"routine alignment write failed: {path}")
    return path
