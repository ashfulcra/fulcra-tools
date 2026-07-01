"""L1 reconcile orchestration (spec §3, §8).

Scan a team's ``task/`` namespace, parse changed OKF Task docs, and heal the
engine-owned derived artifacts (``index.md``, ``log.md``, ``_coord/summaries.json``).
Transport is injected (duck-typed: ``list_dir``/``read``/``write``), so this is
fully testable without the network.

Orphan-proof by construction: rows are rebuilt from the live listing each pass,
never unioned with stale state.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from . import aggregate, model, okf
from .log import get_logger
from .transport import TransportError


def task_prefix(team: str) -> str:
    return f"team/{team}/task/"


def index_path(team: str) -> str:
    return f"team/{team}/task/index.md"


def log_path(team: str) -> str:
    return f"team/{team}/task/log.md"


def summaries_path(team: str) -> str:
    return f"team/{team}/_coord/summaries.json"


def _load_prior_aggregate(transport: Any, team: str) -> Optional[dict[str, Any]]:
    raw = transport.read(summaries_path(team))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def reconcile(
    transport: Any,
    team: str,
    *,
    now: str,
    today: str,
    host: str,
    logger: Any = None,
) -> dict[str, Any]:
    """Run one reconcile pass. Returns a summary dict.

    On a listing failure the pass aborts and writes nothing (leaves prior derived
    artifacts intact) — never publish a truncated index (§8).
    """
    log = logger or get_logger("reconcile")

    prior_agg = _load_prior_aggregate(transport, team)
    prior_rows = aggregate.aggregate_rows(prior_agg)
    prior_by_name = aggregate.rows_by_name(prior_rows)

    prefix = task_prefix(team)
    try:
        listing = transport.list_dir(prefix)
    except TransportError as e:
        log.error("list failed, pass aborted (prior artifacts intact)", team=team, error=str(e))
        return {"degraded": True, "reason": str(e), "tasks": 0}

    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    reused = parsed = 0

    for entry in listing:
        name = entry.get("name") or ""
        if entry.get("is_dir") or not name.endswith(".md") or name in ("index.md", "log.md"):
            continue
        slug = name[:-3]
        prior = prior_by_name.get(slug)
        # incremental: reuse the prior row iff the list minute-timestamp is
        # unchanged (equality — the conservative reading of minute resolution).
        if prior and entry.get("mtime") and prior.get("mtime") == entry.get("mtime"):
            rows.append(prior)
            reused += 1
            continue
        content = transport.read(f"{prefix}{name}")
        fm = okf.parse_frontmatter(content)
        if fm is None:
            # unparseable / unreadable: never drop a task — keep the prior row.
            if prior:
                warnings.append(f"{name}: unparseable frontmatter, kept prior row")
                rows.append(prior)
            else:
                warnings.append(f"{name}: unparseable frontmatter, no prior row, skipped")
            continue
        if not model.is_task(fm):
            continue  # not a Task concept doc — silently ignore
        rows.append(
            model.row_from_frontmatter(
                fm, name=slug, path=f"task/{name}", mtime=entry.get("mtime")
            )
        )
        parsed += 1

    # --- heal engine-owned derived artifacts ---
    if not transport.write(index_path(team), okf.render_index(rows)):
        warnings.append("index.md write failed")

    transitions = aggregate.diff_rows(prior_rows, rows)
    if transitions:
        existing_log = transport.read(log_path(team))
        if not transport.write(
            log_path(team), okf.merge_log(existing_log, transitions, date=today)
        ):
            warnings.append("log.md write failed")

    agg = aggregate.build_aggregate(
        team, rows, generated_at=now, reconcile_host=host, warnings=warnings
    )
    if not transport.write(summaries_path(team), json.dumps(agg, indent=2)):
        warnings.append("summaries.json write failed")

    log.info(
        "reconciled", team=team, tasks=len(rows), reused=reused, parsed=parsed,
        transitions=len(transitions), warnings=len(warnings),
    )
    return {
        "degraded": False,
        "tasks": len(rows),
        "reused": reused,
        "parsed": parsed,
        "transitions": len(transitions),
        "warnings": warnings,
        "rows": rows,
    }
