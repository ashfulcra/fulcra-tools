"""Task row model + status/priority constants for L1 coord-reconcile.

A "row" is the structured projection of an OKF ``type: Task`` concept doc, used to
build indexes, the aggregate, and query results. See
``docs/proposals/teams-convergence/02-L1-coord-reconcile.md`` §1–§4.
"""

from __future__ import annotations

from typing import Any, Optional

VALID_STATUSES = ("proposed", "active", "waiting", "blocked", "done", "abandoned")
TERMINAL_STATUSES = frozenset({"done", "abandoned"})
OPEN_STATUSES = frozenset(set(VALID_STATUSES) - TERMINAL_STATUSES)
VALID_PRIORITIES = ("P0", "P1", "P2", "P3")

DEFAULT_STATUS = "proposed"
DEFAULT_PRIORITY = "P2"

#: Legal status transitions (mirrors fulcra-coord's machine). Terminal states have
#: no outbound edges. A same-status "transition" is always allowed (idempotent edit).
STATUS_TRANSITIONS = {
    "proposed": {"active", "waiting", "abandoned", "done"},
    "active": {"waiting", "blocked", "done", "abandoned"},
    "waiting": {"active", "blocked", "abandoned"},
    "blocked": {"active", "waiting", "abandoned"},
    "done": set(),
    "abandoned": set(),
}


def is_valid_transition(old: str, new: str) -> bool:
    """True iff ``old -> new`` is a legal status change (or a no-op same-status)."""
    if old == new:
        return True
    return new in STATUS_TRANSITIONS.get(old, set())


#: Priority sort key — P0 is most urgent. Unknown priorities sort last.
_PRIORITY_ORDER = {p: i for i, p in enumerate(VALID_PRIORITIES)}


def is_task(frontmatter: Optional[dict]) -> bool:
    """True iff the parsed frontmatter is an OKF ``type: Task`` concept."""
    return bool(frontmatter) and frontmatter.get("type") == "Task"


def row_from_frontmatter(
    frontmatter: Optional[dict],
    *,
    name: str,
    path: str,
    mtime: Optional[str] = None,
) -> dict[str, Any]:
    """Project parsed frontmatter into a task row, backfilling defaults.

    Bare-``fulcra-agent-teams`` tasks may lack coord's extension keys; missing
    ``status``/``priority``/``id``/``title`` are backfilled so such tasks are
    first-class (mixed-fleet tolerance, spec §2 step 3).
    """
    fm = frontmatter or {}
    tags = fm.get("tags")
    if not isinstance(tags, list):
        tags = [tags] if tags else []
    return {
        "id": fm.get("id") or name,
        "name": name,
        "path": path,
        "title": fm.get("title") or name,
        "description": fm.get("description") or "",
        "status": fm.get("status") or DEFAULT_STATUS,
        "priority": fm.get("priority") or DEFAULT_PRIORITY,
        "owner": fm.get("owner"),
        "assignee": fm.get("assignee"),
        "tags": [str(t) for t in tags],
        "timestamp": fm.get("timestamp"),
        "mtime": mtime,
        "blocked_on": fm.get("blocked_on"),
        "due": fm.get("due"),
        "not_before": fm.get("not_before"),
        "next_action": fm.get("next_action"),
    }


def priority_key(row: dict[str, Any]) -> int:
    """Sort key: P0 first, unknown priorities last."""
    return _PRIORITY_ORDER.get(row.get("priority"), len(VALID_PRIORITIES))


def sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic order within a group: priority ascending (P0 first), then
    newest ``timestamp`` first (missing timestamps last). Stable, so input order
    breaks remaining ties."""
    # ISO-8601 timestamps sort lexically; reverse=True gives newest-first and
    # pushes "" (missing) to the end. A second STABLE sort by priority then keeps
    # that recency order within each priority band.
    by_recency = sorted(rows, key=lambda r: str(r.get("timestamp") or ""), reverse=True)
    return sorted(by_recency, key=priority_key)
