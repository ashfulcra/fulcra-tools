"""Structured continuity snapshots — the core of fulcra-agent-continuity.

Teams' ``member/<agent>/progress.md`` is freeform; this gives a *structured*,
resumable snapshot (objective / decisions / next actions / open questions /
artifacts) with a deterministic resume brief. Building the schema + folding many
snapshots to the latest is code; the prose is when/whether to snapshot.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import Any, Optional

SCHEMA = "coord.teams.continuity.v1"
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?|\.\d+)([smhd])$", re.IGNORECASE)
_MAX_DURATION_SECONDS = 999_999_999 * 86400


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


def build_snapshot(
    *,
    agent: str,
    task: str,
    objective: str,
    now: str,
    decisions: Optional[list[str]] = None,
    next_actions: Optional[list[str]] = None,
    open_questions: Optional[list[str]] = None,
    artifacts: Optional[list[str]] = None,
    context_used_percent: Optional[float] = None,
    transcript_path: Optional[str] = None,
) -> dict[str, Any]:
    """A structured snapshot (all list fields normalized, never None)."""
    return {
        "schema": SCHEMA,
        "checkpoint_id": f"CHK-{now}-{task}",
        "agent": agent,
        "task": task,
        "objective": objective,
        "decisions": _as_list(decisions),
        "next_actions": _as_list(next_actions),
        "open_questions": _as_list(open_questions),
        "artifacts": _as_list(artifacts),
        "context_used_percent": context_used_percent,
        "transcript_path": transcript_path,
        "created_at": now,
    }


def _parse_created_at(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def latest(snapshots: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Fold many snapshots to the newest by valid ISO ``created_at``.

    Corrupt hand-written snapshots are ignored so one bad timestamp cannot shadow
    resumable state. Equal timestamps break ties deterministically by task/id.
    """
    valid = [
        (dt, str(s.get("task") or ""), str(s.get("checkpoint_id") or ""), s)
        for s in snapshots
        if isinstance(s, dict)
        for dt in [_parse_created_at(s.get("created_at"))]
        if dt is not None
    ]
    if not valid:
        return None
    return max(valid, key=lambda item: item[:3])[3]


def checkpoint_age_seconds(snapshot: Optional[dict[str, Any]], *, now: datetime) -> Optional[float]:
    """Return a checkpoint's age, or ``None`` when invalid/unknowable.

    A future ``created_at`` is invalid evidence, not a zero-age checkpoint:
    treating an impossible timestamp as maximally fresh would let it pass every
    freshness gate until wall time caught up.
    """
    if not snapshot:
        return None
    created_at = _parse_created_at(snapshot.get("created_at"))
    if created_at is None:
        return None
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    age = (current.astimezone(timezone.utc) - created_at).total_seconds()
    if age < 0:
        return None
    return round(age, 3)


def parse_duration_seconds(value: str) -> Optional[float]:
    """Parse a non-negative ``s``/``m``/``h``/``d`` duration into seconds."""
    match = _DURATION_RE.fullmatch((value or "").strip())
    if not match:
        return None
    amount = float(match.group(1))
    if not math.isfinite(amount):
        return None
    unit = match.group(2).lower()
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return seconds if math.isfinite(seconds) and seconds <= _MAX_DURATION_SECONDS else None


def format_age(seconds: Optional[float]) -> str:
    """Compact human age with exact seconds retained for mechanical inspection."""
    if seconds is None:
        return "unknown"
    exact = f"{seconds:.3f}".rstrip("0").rstrip(".") + "s"
    if seconds >= 86400:
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        return f"{days}d {hours}h ({exact})"
    for unit, scale in (("h", 3600), ("m", 60)):
        if seconds >= scale and seconds % scale == 0:
            return f"{seconds / scale:g}{unit} ({exact})"
    return exact


def render_resume(snapshot: Optional[dict[str, Any]]) -> str:
    """Deterministic resume brief from a snapshot (or a 'no snapshot' line)."""
    if not snapshot:
        return "No continuity snapshot found."
    lines = [
        f"Resume: {snapshot.get('task')} (as of {snapshot.get('created_at')})",
        f"  agent: {snapshot.get('agent')}",
        f"  objective: {snapshot.get('objective')}",
    ]
    cu = snapshot.get("context_used_percent")
    if cu is not None:
        lines.append(f"  context used at snapshot: {cu}%")
    for label, key in (
        ("next actions", "next_actions"),
        ("open questions", "open_questions"),
        ("recent decisions", "decisions"),
        ("artifacts", "artifacts"),
    ):
        items = snapshot.get(key) or []
        if items:
            lines.append(f"  {label}:")
            lines.extend(f"    - {x}" for x in items)
    return "\n".join(lines)
