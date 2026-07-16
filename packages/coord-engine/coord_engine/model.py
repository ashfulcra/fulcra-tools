"""Task row model + status/priority constants for L1 coord-reconcile.

A "row" is the structured projection of an OKF ``type: Task`` concept doc, used to
build indexes, the aggregate, and query results. See
``docs/proposals/teams-convergence/02-L1-coord-reconcile.md`` §1–§4.
"""

from __future__ import annotations

from typing import Any, Optional

from . import config

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


#: Default per-field character cap for summaries-row text (`COORD_SUMMARY_TEXT_CAP`).
#: The summaries index is a SUMMARY: rows carry enough of `title`/`description` to
#: triage a fold; the full payload stays in the task doc. Uncapped, a fleet whose
#: directives carry multi-KB payloads inflates `_coord/summaries.json` past what a
#: remote transport can read inside the fold budgets — every remote briefing then
#: degrades to "summaries index unreadable" despite a fresh index.
DEFAULT_SUMMARY_TEXT_CAP = 280
_TRUNCATION_MARK = "…"

#: Version of the summaries-row projection produced by :func:`row_from_frontmatter`.
#: BUMP this whenever the row projection changes in a way that must self-heal an
#: already-serialized index — e.g. the #388 text cap, which older uncapped rows
#: predate. Reconcile stamps every fresh row with the current value ("sv") and
#: force-reparses any prior row whose stamp != this, so a projection change heals
#: the whole index within one full pass instead of only rows that happen to be
#: rebuilt. Kept a tiny key + small int: it is stored per-row in the index.
ROW_SCHEMA_VERSION = 1


def cap_summary_text(text: str, cap: Optional[int] = None) -> str:
    """Bound a summaries-row text field to the configured cap, ellipsis-marked.

    The marker fits INSIDE the cap so a capped field never exceeds it. A cap
    is a positive int (`config` policy: unparseable/non-positive env falls back
    to the default — a bad value must never unbound the index)."""
    if cap is None:
        cap = config.env_int("COORD_SUMMARY_TEXT_CAP", DEFAULT_SUMMARY_TEXT_CAP)
    if len(text) <= cap:
        return text
    return text[: max(cap - len(_TRUNCATION_MARK), 0)] + _TRUNCATION_MARK


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
    first-class (mixed-fleet tolerance, spec §2 step 3). ``title`` and
    ``description`` are capped via :func:`cap_summary_text` — the row is an
    index entry, not the payload's home.
    """
    fm = frontmatter or {}
    tags = fm.get("tags")
    if not isinstance(tags, list):
        tags = [tags] if tags else []
    cap = config.env_int("COORD_SUMMARY_TEXT_CAP", DEFAULT_SUMMARY_TEXT_CAP)
    return {
        "sv": ROW_SCHEMA_VERSION,  # row-projection version; reconcile reparses stale-stamped rows
        "id": fm.get("id") or name,
        "name": name,
        "path": path,
        "title": cap_summary_text(str(fm.get("title") or name), cap),
        "description": cap_summary_text(str(fm.get("description") or ""), cap),
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
        "checkpoint_ref": fm.get("checkpoint_ref"),
        "acked_by": [],  # folded from _coord/acks/ by reconcile
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
