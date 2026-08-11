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
    TERMINAL_STATUSES,
    VALID_PRIORITIES,
    VALID_STATUSES,
    is_valid_transition,
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class TaskError(ValueError):
    pass


MAX_SLUG_LEN = 80


def slugify(title: str) -> str:
    """Filename-safe slug, capped at ``MAX_SLUG_LEN`` — an unbounded slug from a
    long title produced a ~600-char filename in the migration dry-run."""
    s = _SLUG_RE.sub("-", (title or "").lower()).strip("-") or "task"
    if len(s) > MAX_SLUG_LEN:
        s = s[:MAX_SLUG_LEN].rstrip("-")
    return s


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
    slug: Optional[str] = None,
    evidence: Optional[str] = None,
) -> tuple[str, str]:
    """Return ``(slug, content)`` for a new OKF Task doc. Raises on bad enums.

    ``slug`` overrides the title-derived slug (used for both the filename and the
    ``id`` frontmatter field) — the directive path passes a payload-hash-suffixed
    slug so identical messages dedupe onto one id and distinct messages get
    distinct, non-racing ids.

    ``evidence`` is REQUIRED when a doc is created directly in a terminal state.
    ``apply_update`` has always enforced "done requires evidence", but that rule
    lived only on the update path — a doc could be BORN terminal carrying no
    reason at all, and nothing said so. Creating terminal rows is exactly what
    the notification path does, so the rule has to hold at both entry points or
    it does not hold.
    """
    if status not in VALID_STATUSES:
        raise TaskError(f"invalid status {status!r}")
    if priority not in VALID_PRIORITIES:
        raise TaskError(f"invalid priority {priority!r}")
    if status in TERMINAL_STATUSES and not (evidence or "").strip():
        label = "reason" if status == "abandoned" else "evidence"
        raise TaskError(f"a doc created as {status} requires {label}")
    slug = slug or slugify(title)
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
    body = f"\n\n# {title}\n"
    if status in TERMINAL_STATUSES:
        label = "reason" if status == "abandoned" else "evidence"
        body += f"\n- {now}: created {status} ({label}: {evidence})\n"
    return slug, okf.render_frontmatter(fm) + body


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
    unlock: Optional[str] = None,
    superseded_by: Optional[str] = None,
    superseded_record_id: Optional[str] = None,
) -> str:
    """Read-modify-write a task doc, enforcing the status machine. Raises
    ``TaskError`` on a missing doc, unparseable frontmatter, or illegal transition."""
    fm = okf.parse_frontmatter(existing)
    if fm is None:
        raise TaskError("task doc missing or has no parseable frontmatter")
    split = okf.split_frontmatter(existing or "")
    body = split[1] if split else ""
    old_status = fm.get("status") or DEFAULT_STATUS
    if superseded_by and old_status in ("done", "abandoned"):
        # Terminal dispositions are immutable (pr-491 round 1): supersession
        # closes LIVE work; it must never rewrite already-closed history —
        # including the done->done "no-op" path, which would append a fresh
        # completion record onto settled work.
        raise TaskError(
            f"cannot supersede a terminal task (status {old_status})")
    if status is not None:
        if status not in VALID_STATUSES:
            raise TaskError(f"invalid status {status!r}")
        if not is_valid_transition(old_status, status):
            # Supersession (D3, respec 2026-07-28) is a legal close from any
            # LIVE state — its whole point is closing a copy that was never
            # driven through the normal lifecycle (reassigned-away work).
            # Terminal states stay immutable: a superseded record must never
            # rewrite an existing terminal disposition (pr-491 round 1).
            _live = {"proposed", "active", "waiting", "blocked"}
            if not (status == "done" and superseded_by
                    and old_status in _live):
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
    if unlock is not None:
        fm["unlock"] = unlock
    if superseded_by is not None:
        fm["superseded_by"] = superseded_by
    if superseded_record_id is not None:
        # s7 verb-channel link: the superseded predecessor's EVENT record id,
        # the join the supersession-adoption fold's explicit channel keys on.
        # Optional — a supersession with no known event id stays legal and
        # reads as unmeasured, never guessed.
        fm["superseded_record_id"] = superseded_record_id
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


def apply_answer(existing: Optional[str], *, now: str, answer: str,
                 relayer: Optional[str] = None, human: str = "human") -> tuple[str, str]:
    """The operator return-leg (fulcra-agent-operator): validate the task is a
    waiting-for-operator ask, then in ONE write: record the answer, unblock
    (blocked -> active), hand the task back to its OWNER (so it lands in their
    inbox and their listener fires), and strip the needs:human marker.
    Returns (new_doc, owner). Raises TaskError on a non-ask or missing owner."""
    if not answer or not answer.strip():
        raise TaskError("answer requires text")
    fm = okf.parse_frontmatter(existing)
    if fm is None:
        raise TaskError("task doc missing or has no parseable frontmatter")
    tags = fm.get("tags") or []
    if not isinstance(tags, list):
        tags = [tags]
    from .model import OPEN_STATUSES  # local: avoid touching module import surface
    blocked_on = str(fm.get("blocked_on") or "").replace(",", " ").split()
    is_operator_ask = (
        (fm.get("status") or DEFAULT_STATUS) in OPEN_STATUSES  # parity w/ asks fold:
        # a terminal task with a stale needs:human tag is not answerable
        and ("needs:human" in tags
             or (fm.get("status") == "blocked"
                 and (fm.get("assignee") == human or human in blocked_on)))
    )
    if not is_operator_ask:
        raise TaskError(f"not a waiting-for-operator ask (for operator {human!r})")
    owner = str(fm.get("owner") or "").strip()
    if not owner:
        raise TaskError("ask has no owner to hand the answer back to")
    status = "active" if fm.get("status") == "blocked" else None
    doc = apply_update(
        existing, now=now, status=status,
        next_action=f"OPERATOR ANSWER: {answer.strip()}",
        assignee=owner, blocked_on="", remove_tags=["needs:human"],
        evidence=(f"operator answer relayed by {relayer}" if relayer else None),
    )
    return doc, owner


def mark_done(existing: Optional[str], *, now: str, evidence: str) -> str:
    """Transition to ``done`` — thin wrapper; ``apply_update`` enforces evidence."""
    return apply_update(existing, now=now, status="done", evidence=evidence)
