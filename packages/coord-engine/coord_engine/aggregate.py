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


# ---------------------------------------------------------------------------
# Shared categorization — the SINGLE source of truth both diff views consume
# ---------------------------------------------------------------------------
#
# ``diff_rows`` (log bullets) and ``diff_transitions`` (fold-ready dicts) MUST
# agree on WHICH changes count as a transition and in WHAT order. Rather than
# keep two hand-mirrored loops honest with a comment + one test (the byte-identity
# guard only pins ``diff_rows``' formatting, not its categorization, so a change
# to the status-change rule could silently drift the two), both fold over this one
# generator. Drift-proof by construction: edit the rule here and both views move
# together.

def _categorize(
    prior_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]
) -> list[tuple[str, dict[str, Any], Optional[dict[str, Any]]]]:
    """Three-way categorization of the change from ``prior_rows`` to ``new_rows``,
    keyed by task id: ``(kind, row, prior_row)`` tuples where ``kind`` is one of
    ``create`` / ``update`` / ``deprecate``.

    * ``create``   — id present in new only; ``row`` = new row, ``prior_row`` None.
    * ``update``   — id in both with a CHANGED ``status``; ``row`` = new row,
                     ``prior_row`` = the prior row (its old status, for the arrow).
    * ``deprecate``— id present in prior only; ``row`` = the removed prior row,
                     ``prior_row`` None.

    Content-only edits (same status) are intentionally NOT a change (they live in
    the file's own version history). Order is stable: creations + status-updates
    over ``new`` (by id) first, then removals over ``prior`` (by id) — the order
    ``diff_rows`` has always emitted, which the byte-identity guard pins.
    """
    prior = rows_by_id(prior_rows)
    new = rows_by_id(new_rows)
    out: list[tuple[str, dict[str, Any], Optional[dict[str, Any]]]] = []
    for rid, r in new.items():
        if rid not in prior:
            out.append(("create", r, None))
        elif prior[rid].get("status") != r.get("status"):
            out.append(("update", r, prior[rid]))
    for rid, r in prior.items():
        if rid not in new:
            out.append(("deprecate", r, None))
    return out


def diff_rows(
    prior_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]
) -> list[str]:
    """OKF §7 log bullets for changes from ``prior_rows`` to ``new_rows``.

    Creations, status transitions, and removals — keyed by task id. Content-only
    edits (no status change) are intentionally not logged (they're in the file's
    own version history). Categorization comes from :func:`_categorize` (shared
    with :func:`diff_transitions`); this function only renders the bullets.
    """
    out: list[str] = []
    for kind, r, prior_r in _categorize(prior_rows, new_rows):
        if kind == "create":
            out.append(f"* **Creation**: {_label(r)} created ({r.get('status')}).")
        elif kind == "update":
            out.append(
                f"* **Update**: {_label(r)} "
                f"{(prior_r or {}).get('status')} → {r.get('status')}."
            )
        else:  # deprecate
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
#   * The two share their categorization via ``_categorize`` (above) so they can
#     never drift on WHICH changes count as a transition: ``diff_rows`` renders
#     each ``(kind, row, prior_row)`` as a bullet, ``diff_transitions`` as a dict.
#     Kinds: create / update / deprecate.
#
# ``ts`` is the task row's own ``updated_at`` — the frontmatter ``timestamp``
# reconcile stamps on every write (``mtime`` as a defensive fallback) — normalized
# to a UTC-``Z`` zero-padded ISO string per the Task-1 ts contract so the fold's
# watermark ordering and skew-margin arithmetic hold.

#: The store's ``file list`` mtime format(s) — UTC, minute-granular, e.g.
#: ``2026-07-01 04:12PM UTC`` (see ``transport.parse_list_output``). This is the
#: DEFENSIVE ts fallback when a task carries no ``timestamp`` frontmatter. Because
#: it carries a full date (year included), it normalizes cleanly to a UTC-``Z``
#: ISO instant — there is no ls-style yearless ambiguity to resolve, so the ts
#: contract (parseable, lexicographically comparable) holds even for a
#: timestamp-less task, keeping the fold's skew math + seen_ids prune bounded.
_STORE_MTIME_FORMATS = ("%Y-%m-%d %I:%M%p %Z", "%Y-%m-%d %I:%M%p")


def _parse_store_mtime(s: str) -> Optional[datetime]:
    """Parse the transport's list-style mtime string into a datetime, or None.

    The store lists times in UTC; a naive parse (``%Z`` absent) is stamped UTC so
    the normalized result is a real UTC instant, not a floating one. Never raises."""
    for fmt in _STORE_MTIME_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _normalize_ts(raw: Any) -> str:
    """Normalize a row timestamp to a lexicographically-comparable UTC-``Z`` ISO
    string (``2026-07-09T09:00:00Z``). Accepts ISO-8601 (the ``timestamp``
    frontmatter reconcile stamps) AND the store's list-style ``mtime``
    (``2026-07-01 04:12PM UTC``), the defensive fallback — so a task with no
    ``timestamp`` still yields a parseable ISO ts (the fold's ``_parse_ts``
    succeeds, the skew boundary + seen_ids prune stay bounded) rather than a raw,
    unparseable string. A genuinely unparseable value is passed through unchanged
    (the fold tolerates a non-normalized ts — it degrades to emit/keep rather than
    dropping a transition); ``None``/blank -> ``""`` (the fold treats a falsy ts as
    malformed and skips it). Never raises."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    iso = (s[:-1] + "+00:00") if s.endswith(("Z", "z")) else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = _parse_store_mtime(s)
        if dt is None:
            return s  # genuinely unparseable -> pass through (fold tolerates it)
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
