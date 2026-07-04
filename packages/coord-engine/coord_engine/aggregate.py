"""The ``_coord/summaries.json`` aggregate + row diffing for the log.

The aggregate is a cache of the concept docs (never authoritative) — deleting it
and re-running reproduces it exactly (spec §4, §6/C4).
"""

from __future__ import annotations

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
