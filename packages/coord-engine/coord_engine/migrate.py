"""One-shot exporter: incumbent fulcra-coord JSON tasks -> coord2 task docs.

The migration plan's approach C (docs 06): deterministic field mapping, idempotent
(re-runs skip already-migrated work), one-way, and **marked** — after a verified
coord2 write the incumbent task gains a ``migrated:coord2`` tag so a task lives in
exactly one active system. Never deletes anything on the incumbent.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from . import okf
from .model import VALID_PRIORITIES, VALID_STATUSES
from .tasks import agent_key, slugify
from .transport import TransportError

MIGRATED_TAG = "migrated:coord2"

#: Tasks in an open review loop are NOT migration-eligible (the verdict path
#: lives on the incumbent until the loop closes — plan review, Resolution §3).
_REVIEW_KINDS = ("kind:review", "kind:review-verdict")


def _in_open_review(t: dict[str, Any]) -> bool:
    if t.get("pr"):
        return True
    if t.get("workstream") == "review":
        return True
    tags = t.get("tags") or []
    return "workstream:review" in tags or any(k in tags for k in _REVIEW_KINDS)


def _terminalize(t: dict[str, Any], *, now: str, team: str, slug: str) -> dict[str, Any]:
    """Terminal transition on the incumbent (the ONE-ACTIVE-SYSTEM mechanism —
    the tag alone does not hide a task from incumbent boards)."""
    t["status"] = "abandoned"
    t["updated_at"] = now
    tags = t.setdefault("tags", [])
    if MIGRATED_TAG not in tags:
        tags.append(MIGRATED_TAG)
    t.setdefault("events", []).append({
        "at": now, "type": "abandoned", "by": "coord2-migrate",
        "summary": f"migrated to coord2 team/{team}/task/{slug}.md",
    })
    return t


def map_task(t: dict[str, Any], *, now: str) -> tuple[str, dict[str, Any], str]:
    """Deterministic incumbent-task -> (slug, frontmatter, body) mapping."""
    title = str(t.get("title") or t.get("id") or "untitled")
    slug = slugify(title)
    # a capped slug from a long title needs a stable disambiguator (two long
    # titles sharing an 80-char prefix must not collide)
    from .tasks import MAX_SLUG_LEN
    raw = title.lower()
    if len(raw) > MAX_SLUG_LEN:
        slug = f"{slug[:MAX_SLUG_LEN - 7]}-{agent_key(str(t.get('id') or title))[-6:]}"
    status = t.get("status") if t.get("status") in VALID_STATUSES else "proposed"
    priority = t.get("priority") if t.get("priority") in VALID_PRIORITIES else "P2"
    tags: list[str] = []
    if t.get("workstream"):
        tags.append(f"workstream:{t['workstream']}")
    if t.get("kind"):
        tags.append(f"kind:{t['kind']}")
    for tag in t.get("tags") or []:
        s = str(tag)
        # drop the incumbent's denormalized dupes; keep real labels
        if not s.startswith(("agent:", "status:", "priority:", "workstream:", "kind:")):
            tags.append(s)
    fm = {
        "type": "Task", "title": title,
        "description": t.get("current_summary") or "",
        "timestamp": t.get("updated_at") or t.get("created_at") or now,
        "tags": tags, "id": slug,
        "status": status, "priority": priority,
        "owner": t.get("owner_agent"), "assignee": t.get("assignee"),
        "next_action": t.get("next_action"), "blocked_on": t.get("blocked_on"),
        "not_before": t.get("not_before"), "due": t.get("due"),
        "checkpoint_ref": t.get("checkpoint_ref"),
        "migrated_from": t.get("id"),
    }
    body_lines = [f"\n# {title}", "",
                  f"- Migrated from fulcra-coord task `{t.get('id')}` on {now}."]
    links = t.get("links") or {}
    for pr in (links.get("prs") or []):
        body_lines.append(f"- PR: {pr}")
    if links.get("local_ticket"):
        body_lines.append(f"- Ticket: {links['local_ticket']}")
    for item in (t.get("checklist") or []):
        body_lines.append(f"- [ ] {item}")
    return slug, fm, "\n".join(body_lines) + "\n"


def migrate(
    transport: Any,
    team: str,
    *,
    now: str,
    source: str = "/coordination",
    dry_run: bool = False,
    mark: bool = True,
    include_terminal: bool = False,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """One pass. Returns {planned, migrated, skipped, marked, repaired,
    skipped_review, errors}.

    Note: ``--limit`` counts PLANNED items in dry-run but SUCCESSFUL writes live,
    so a limited live run may scan further than its dry-run preview.
    PREFLIGHT (runbook): confirm no fleet host runs FULCRA_COORD_READ_SOURCE=events
    — such a host folds status from the event store this exporter never writes,
    so migrated tasks would stay live there."""
    planned: list[str] = []
    errors: list[str] = []
    migrated = skipped = marked = repaired = skipped_review = 0
    try:
        entries = transport.list_dir(f"{source}/tasks/")
    except TransportError as e:
        return {"planned": [], "migrated": 0, "skipped": 0, "marked": 0,
                "errors": [f"source unreadable: {e}"]}
    # existing migrated_from ids in the team (idempotence)
    existing_from: dict[str, str] = {}
    existing_slugs: set = set()
    try:
        for e in transport.list_dir(f"team/{team}/task/"):
            n = e.get("name") or ""
            if e.get("is_dir") or not n.endswith(".md") or n in ("index.md", "log.md"):
                continue
            fm = okf.parse_frontmatter(transport.read(f"team/{team}/task/{n}")) or {}
            existing_slugs.add(n[:-3])
            if fm.get("migrated_from"):
                existing_from[str(fm["migrated_from"])] = n[:-3]
    except TransportError:
        pass
    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".json"):
            continue
        raw = transport.read(f"{source}/tasks/{n}")
        try:
            t = json.loads(raw) if raw else None
        except Exception:
            t = None
        if not isinstance(t, dict) or not t.get("id"):
            continue
        already_terminal = t.get("status") in ("done", "abandoned")
        twin = existing_from.get(str(t.get("id")))
        if twin is not None:
            # REPAIR PASS: twin exists but the incumbent transition never landed.
            # If OUR abandoned event is already there and the task is open again,
            # a human deliberately REOPENED it — never re-terminalize (review finding).
            reopened = any(e.get("by") == "coord2-migrate" and e.get("type") == "abandoned"
                           for e in (t.get("events") or []))
            if reopened and not already_terminal:
                errors.append(f"{t['id']}: reopened by operator after migration — left open "
                              f"(coord2 twin task/{twin}.md also exists; resolve manually)")
                continue
            if not already_terminal and mark and not dry_run:
                if transport.write(f"{source}/tasks/{n}",
                                   json.dumps(_terminalize(t, now=now, team=team, slug=twin), indent=2)):
                    repaired += 1
                else:
                    errors.append(f"{t['id']}: repair transition failed (still open on incumbent)")
            else:
                skipped += 1
            continue
        if MIGRATED_TAG in (t.get("tags") or []):
            skipped += 1
            continue
        if not include_terminal and already_terminal:
            continue
        if _in_open_review(t):
            skipped_review += 1
            continue
        if limit is not None and migrated + len(planned) >= limit and dry_run:
            break
        if limit is not None and migrated >= limit and not dry_run:
            break
        slug, fm, body = map_task(t, now=now)
        if slug in existing_slugs:  # collision with a non-migrated doc: disambiguate
            slug = f"{slug}-{agent_key(str(t['id']))[-6:]}"
            fm["id"] = slug
        if dry_run:
            planned.append(f"{t['id']} -> task/{slug}.md [{fm['status']}/{fm['priority']}]")
            continue
        dst = f"team/{team}/task/{slug}.md"
        content = okf.render_frontmatter(fm) + body
        if not transport.write(dst, content):
            errors.append(f"{t['id']}: coord2 write failed; incumbent untouched")
            continue
        if transport.read(dst) != content:  # verify before marking (one-active-system)
            errors.append(f"{t['id']}: coord2 write not readable back; incumbent untouched")
            continue
        migrated += 1
        existing_slugs.add(slug)
        if mark:
            if transport.write(f"{source}/tasks/{n}",
                               json.dumps(_terminalize(t, now=now, team=team, slug=slug), indent=2)):
                marked += 1
            else:
                errors.append(f"{t['id']}: migrated but incumbent transition FAILED — "
                              f"still open there; next run's repair pass will finish it")
    return {"planned": planned, "migrated": migrated, "skipped": skipped,
            "marked": marked, "repaired": repaired, "skipped_review": skipped_review,
            "errors": errors}
