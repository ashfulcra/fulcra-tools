"""The ``_coord/summaries.json`` aggregate + row diffing for the log.

The aggregate is a cache of the concept docs (never authoritative) — deleting it
and re-running reproduces it exactly (spec §4, §6/C4).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA = "coord.teams.summaries.v1"


def build_aggregate(
    team: str,
    rows: list[dict[str, Any]],
    *,
    generated_at: str,
    reconcile_host: str,
    warnings: Optional[list[str]] = None,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "team": team,
        "generated_at": generated_at,
        "reconcile_host": reconcile_host,
        "rows": rows,
        "warnings": warnings or [],
    }


def aggregate_rows(aggregate: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract rows from an aggregate dict, tolerating None/garbage."""
    if not isinstance(aggregate, dict):
        return []
    rows = aggregate.get("rows")
    return rows if isinstance(rows, list) else []


def rows_by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(r.get("id")): r for r in rows if isinstance(r, dict) and r.get("id")}


def rows_by_name(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(r.get("name")): r for r in rows if isinstance(r, dict) and r.get("name")}


def _label(row: dict[str, Any]) -> str:
    title = row.get("title") or row.get("name") or row.get("id") or "untitled"
    link = row.get("name") or row.get("id") or "untitled"
    href = link if str(link).endswith(".md") else f"{link}.md"
    return f"[{title}]({href})"


def diff_rows(
    prior_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]
) -> list[str]:
    """OKF §7 log bullets for changes from ``prior_rows`` to ``new_rows``.

    Creations, status transitions, and removals — keyed by task id. Content-only
    edits (no status change) are intentionally not logged (they're in the file's
    own version history).
    """
    prior = rows_by_id(prior_rows)
    new = rows_by_id(new_rows)
    out: list[str] = []
    for rid, r in new.items():
        if rid not in prior:
            out.append(f"* **Creation**: {_label(r)} created ({r.get('status')}).")
        elif prior[rid].get("status") != r.get("status"):
            out.append(
                f"* **Update**: {_label(r)} "
                f"{prior[rid].get('status')} → {r.get('status')}."
            )
    for rid, r in prior.items():
        if rid not in new:
            out.append(f"* **Deprecation**: {_label(r)} removed.")
    return out


# ---------------------------------------------------------------------------
# Structured transitions — the projection fold's input (Task 2, ADDITIVE)
# ---------------------------------------------------------------------------
#
# ``diff_transitions`` is the STRUCTURED sibling of ``diff_rows``: same three-way
# categorization (creation / status-transition / removal, keyed by task id), but
# it emits fold-ready dicts ``{task_id, kind, ts, title, assignee?, next_action?}``
# instead of markdown bullets. It is a SEPARATE function on purpose:
#
#   * ``diff_rows``' ``list[str]`` return + ``log.md`` output MUST stay
#     byte-identical (existing aggregate/reconcile tests are the guardrail), so
#     its signature is left untouched — a second return value would force every
#     caller (reconcile + the aggregate tests) to change, breaking that guarantee.
#   * The two share no code so ``diff_rows`` cannot be perturbed, but they MUST
#     stay in lockstep on WHICH changes count as a transition (the categorization
#     below mirrors ``diff_rows`` exactly). Kinds: create / update / deprecate.
#
# ``ts`` is the task row's own ``updated_at`` — the frontmatter ``timestamp``
# reconcile stamps on every write (``mtime`` as a defensive fallback) — normalized
# to a UTC-``Z`` zero-padded ISO string per the Task-1 ts contract so the fold's
# watermark ordering and skew-margin arithmetic hold.

def _normalize_ts(raw: Any) -> str:
    """Normalize a row timestamp to a lexicographically-comparable UTC-``Z`` ISO
    string (``2026-07-09T09:00:00Z``). An unparseable value is passed through
    unchanged (the fold tolerates a non-normalized ts — it degrades to emit/keep
    rather than dropping a transition); ``None``/blank -> ``""`` (the fold treats
    a falsy ts as malformed and skips it). Never raises."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    iso = (s[:-1] + "+00:00") if s.endswith(("Z", "z")) else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return s
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(timespec="seconds") + "Z"


def _transition(row: dict[str, Any], kind: str) -> dict[str, Any]:
    """One structured transition dict from a task row (the shape the fold reads)."""
    out: dict[str, Any] = {
        "task_id": str(row.get("id") or row.get("name") or ""),
        "kind": kind,
        "ts": _normalize_ts(row.get("timestamp") or row.get("mtime")),
        "title": str(row.get("title") or row.get("name") or row.get("id") or "untitled"),
    }
    assignee = row.get("assignee")
    if assignee:
        out["assignee"] = str(assignee)
    nxt = row.get("next_action")
    if nxt:
        out["next_action"] = str(nxt)
    return out


def diff_transitions(
    prior_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Structured transitions for the projection fold — the ADDITIVE sibling of
    :func:`diff_rows`, categorized identically (creation / status-transition /
    removal by task id). ``ts`` = each row's own ``updated_at`` (frontmatter
    ``timestamp``), normalized to UTC-``Z``. Content-only edits are not a
    transition (mirrors ``diff_rows``)."""
    prior = rows_by_id(prior_rows)
    new = rows_by_id(new_rows)
    out: list[dict[str, Any]] = []
    for rid, r in new.items():
        if rid not in prior:
            out.append(_transition(r, "create"))
        elif prior[rid].get("status") != r.get("status"):
            out.append(_transition(r, "update"))
    for rid, r in prior.items():
        if rid not in new:
            out.append(_transition(r, "deprecate"))
    return out
