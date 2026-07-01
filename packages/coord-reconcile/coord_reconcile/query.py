"""Read-side query verbs over the aggregate rows (spec §5).

All pure functions of a row list — the CLI loads ``_coord/summaries.json`` once
and calls these. No network, no per-file reads.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from .model import OPEN_STATUSES, sort_rows


def status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(str(r.get("status")) for r in rows))


def board(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Open work grouped by status (active/waiting/blocked/proposed), sorted."""
    groups: dict[str, list[dict[str, Any]]] = {
        s: [] for s in ("active", "waiting", "blocked", "proposed")
    }
    for r in rows:
        st = r.get("status")
        if st in groups:
            groups[st].append(r)
    return {k: sort_rows(v) for k, v in groups.items()}


def needs_me(
    rows: list[dict[str, Any]], agent: str, *, now: Optional[str] = None
) -> list[dict[str, Any]]:
    """Open rows assigned to ``agent`` or naming it in ``blocked_on``, gated on
    ``not_before`` (an item scheduled for the future is hidden until ``now``).

    ``now`` is an ISO-8601 string; ISO sorts lexically so string compare is a
    valid time compare. If ``now`` is None the gate is skipped.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("status") not in OPEN_STATUSES:
            continue
        assignee = r.get("assignee")
        blocked_on = r.get("blocked_on") or ""
        if assignee != agent and agent not in str(blocked_on):
            continue
        nb = r.get("not_before")
        if nb and now is not None and str(nb) > now:
            continue  # scheduled for the future
        out.append(r)
    return sort_rows(out)


def search(rows: list[dict[str, Any]], q: str) -> list[dict[str, Any]]:
    """Substring match over id/title/description/tags (case-insensitive)."""
    ql = (q or "").lower().strip()
    if not ql:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        hay = " ".join(
            str(x)
            for x in (
                r.get("id"),
                r.get("title"),
                r.get("description"),
                " ".join(str(t) for t in (r.get("tags") or [])),
            )
        ).lower()
        if ql in hay:
            out.append(r)
    return sort_rows(out)
