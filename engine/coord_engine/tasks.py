"""Task lifecycle — the deterministic core of the fulcra-agent-tasks skill.

Writing OKF Task frontmatter and enforcing the status machine are code (a wrong
transition or malformed frontmatter is a correctness bug); composing the human
body note is prose. Pure functions here; the I/O wrapper + CLI live in ``cli.py``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

from . import okf
from .model import (
    DEFAULT_PRIORITY,
    DEFAULT_STATUS,
    VALID_PRIORITIES,
    VALID_STATUSES,
    is_valid_transition,
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class TaskError(ValueError):
    pass


def slugify(title: str) -> str:
    return _SLUG_RE.sub("-", (title or "").lower()).strip("-") or "task"


def agent_key(agent: str) -> str:
    """Collision-safe filename key for an agent id. ``slugify`` is lossy
    (``a:b`` and ``a/b`` collide), so distinct ids sharing a shard file would
    silently clobber each other (presence loss, lease merge -> CONTESTED
    blindness). Suffix a short hash of the raw id to make the key injective."""
    a = agent or "agent"
    return f"{slugify(a)[:48]}-{hashlib.sha1(a.encode()).hexdigest()[:6]}"


def new_task_doc(
    title: str,
    *,
    now: str,
    workstream: Optional[str] = None,
    status: str = DEFAULT_STATUS,
    priority: str = DEFAULT_PRIORITY,
    owner: Optional[str] = None,
    assignee: Optional[str] = None,
    summary: str = "",
    next_action: Optional[str] = None,
    kind: Optional[str] = None,
    not_before: Optional[str] = None,
) -> tuple[str, str]:
    """Return ``(slug, content)`` for a new OKF Task doc. Raises on bad enums."""
    if status not in VALID_STATUSES:
        raise TaskError(f"invalid status {status!r}")
    if priority not in VALID_PRIORITIES:
        raise TaskError(f"invalid priority {priority!r}")
    slug = slugify(title)
    tags = []
    if workstream:
        tags.append(f"workstream:{workstream}")
    if kind:
        tags.append(f"kind:{kind}")
    fm = {
        "type": "Task", "title": title, "description": summary or "", "timestamp": now,
        "tags": tags, "id": slug, "status": status, "priority": priority,
        "owner": owner, "assignee": assignee, "next_action": next_action,
        "not_before": not_before,
    }
    return slug, okf.render_frontmatter(fm) + f"\n\n# {title}\n"


def apply_update(
    existing: Optional[str],
    *,
    now: str,
    status: Optional[str] = None,
    summary: Optional[str] = None,
    next_action: Optional[str] = None,
    assignee: Optional[str] = None,
    blocked_on: Optional[str] = None,
    priority: Optional[str] = None,
    evidence: Optional[str] = None,
    add_tags: Optional[list[str]] = None,
    checkpoint_ref: Optional[str] = None,
    remove_tags: Optional[list[str]] = None,
) -> str:
    """Read-modify-write a task doc, enforcing the status machine. Raises
    ``TaskError`` on a missing doc, unparseable frontmatter, or illegal transition."""
    fm = okf.parse_frontmatter(existing)
    if fm is None:
        raise TaskError("task doc missing or has no parseable frontmatter")
    split = okf.split_frontmatter(existing or "")
    body = split[1] if split else ""
    old_status = fm.get("status") or DEFAULT_STATUS
    if status is not None:
        if status not in VALID_STATUSES:
            raise TaskError(f"invalid status {status!r}")
        if not is_valid_transition(old_status, status):
            raise TaskError(f"illegal transition {old_status} -> {status}")
        # Enforce "done requires evidence" HERE so it holds through every entry
        # point (`task update --status done`, not only `task done`).
        if status == "done" and not evidence:
            raise TaskError("done requires evidence")
        fm["status"] = status
    if priority is not None:
        if priority not in VALID_PRIORITIES:
            raise TaskError(f"invalid priority {priority!r}")
        fm["priority"] = priority
    if summary is not None:
        fm["description"] = summary
    if next_action is not None:
        fm["next_action"] = next_action
    if assignee is not None:
        fm["assignee"] = assignee
    if blocked_on is not None:
        fm["blocked_on"] = blocked_on
    if checkpoint_ref is not None:
        fm["checkpoint_ref"] = checkpoint_ref
    if add_tags:
        cur = fm.get("tags") or []
        if not isinstance(cur, list):
            cur = [str(cur)]
        fm["tags"] = cur + [t for t in add_tags if t not in cur]
    if remove_tags:
        cur = fm.get("tags") or []
        if not isinstance(cur, list):
            cur = [str(cur)]
        remove = set(remove_tags)
        fm["tags"] = [t for t in cur if t not in remove]
    fm["timestamp"] = now
    note = f"{now}: {old_status} → {fm['status']}" if status else f"{now}: updated"
    if evidence:
        label = "reason" if status == "abandoned" else "evidence"
        note += f" ({label}: {evidence})"
    tail = (body.rstrip() + f"\n\n- {note}\n") if body.strip() else f"- {note}\n"
    return okf.render_frontmatter(fm) + "\n\n" + tail.lstrip("\n")


def mark_done(existing: Optional[str], *, now: str, evidence: str) -> str:
    """Transition to ``done`` — thin wrapper; ``apply_update`` enforces evidence."""
    return apply_update(existing, now=now, status="done", evidence=evidence)
