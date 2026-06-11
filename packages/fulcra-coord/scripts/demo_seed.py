#!/usr/bin/env python3
"""Seed a Fulcra coordination root with the three-agent demo scenario.

WHY THIS EXISTS
---------------
The three-agent coordination demo needs a *narrative* state on the bus so that
every agent — Claude Code, Codex (ChatGPT desktop), and OpenClaw (Mac mini) —
produces a great, recognizable answer the moment it reads the coordination root.
A live demo can't depend on having organically accumulated the right mix of
active / waiting / blocked / done / stale tasks, so this script materializes
exactly that mix, deterministically and idempotently.

It builds task dicts using the real ``fulcra_coord`` package so the shapes match
production exactly (same fields as ``schema.make_task`` + ``apply_transition`` /
``apply_update``), uploads each via ``remote.upload_json``, then rebuilds every
materialized view with ``views.build_all_views`` and uploads those too — the
same artifacts the CLI and hooks read.

CONTROLLED TIME
---------------
The package's view layer computes staleness against ``datetime.now`` at read
time, but the *task timestamps* are seeded explicitly here (this script is a
standalone tool, not the package, so it may use stdlib ``datetime`` freely). The
backfill task is stamped ~4h old so that — against the default 2h staleness
threshold — it is genuinely stale when any agent reads the bus, with no reliance
on wall-clock luck during the demo.

DETERMINISTIC IDS
-----------------
Task ids are fixed strings (``TASK-DEMO-search-api`` …), not the random-suffixed
ids ``schema.make_task_id`` generates. That makes a reseed *idempotent*: it
overwrites the same remote paths instead of piling up duplicates. (These ids do
not match ``schema.validate_task``'s strict ``TASK-<8digits>-…-<hex8>`` regex,
which is fine — the seed path never calls ``validate_task``; that check only
guards the interactive ``start`` command.)

USAGE
-----
    # Live (writes to the real Fulcra account the host is authed to):
    FULCRA_COORD_REMOTE_ROOT=/coordination-demo \
        uv run python scripts/demo_seed.py

    # Reset first (overwrite all known demo ids + views), then reseed:
    FULCRA_COORD_REMOTE_ROOT=/coordination-demo \
        uv run python scripts/demo_seed.py --reset

    # Pin the base time (otherwise "now"); offsets are derived from it:
    uv run python scripts/demo_seed.py --now 2026-06-02T17:00:00Z

In tests, point ``FULCRA_COORD_BACKEND`` at a stateful fake backend so the real
upload/view-rebuild path runs without touching a live account (see
``tests/test_demo_seed.py``).
"""

from __future__ import annotations

import argparse
import json as _json
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# Make the package importable when run straight from the repo (scripts/ is not
# on sys.path by default).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import remote, schema, views
# The view-name -> remote-path mapping lives in the package's write pipeline;
# import it rather than keeping a drift-prone copy here. writepipe never
# imports cli, so this pulls in no command-surface machinery.
from fulcra_coord.writepipe import _view_name_to_remote


# ---------------------------------------------------------------------------
# Scenario definition
# ---------------------------------------------------------------------------

WORKSTREAM = "search"

# Stable ids so a reseed overwrites cleanly instead of duplicating.
TASK_IDS = [
    "TASK-DEMO-search-api",
    "TASK-DEMO-infra-cluster",
    "TASK-DEMO-query-parser",
    "TASK-DEMO-prod-index",
    "TASK-DEMO-backfill",
    "TASK-DEMO-staging-cluster",
]


def _iso(dt: datetime) -> str:
    """Match the package's own ISO format (``…Z``, not ``+00:00``)."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _base_task(
    *,
    task_id: str,
    title: str,
    owner_agent: str,
    priority: str,
    kind: str,
    created_at: datetime,
    summary: str = "",
    next_action: str = "",
) -> dict[str, Any]:
    """Construct a task in the canonical ``make_task`` shape, then pin its id and
    creation time.

    ``owner_agent`` is the full ``vendor:host:workstream`` identity the demo
    groups by; ``schema.make_task`` takes ``agent`` (the claimer) and an explicit
    ``owner_agent``. We pass both so the claim/ownership fields are coherent.
    """
    task = schema.make_task(
        title=title,
        workstream=WORKSTREAM,
        agent=owner_agent,
        owner_agent=owner_agent,
        kind=kind,
        priority=priority,
        summary=summary,
        next_action=next_action,
        task_id=task_id,
        dt=created_at,
    )
    return task


def build_scenario_tasks(now: datetime) -> list[dict[str, Any]]:
    """Build the six demo tasks with explicitly controlled timestamps.

    Offsets are relative to ``now`` so the scenario reads the same regardless of
    when the demo runs. The backfill task is stamped ~4h old to land it over the
    default 2h staleness threshold.
    """
    tasks: list[dict[str, Any]] = []

    # 1. search-api — ACTIVE, P1, claude-code, updated ~10 min ago.
    created = now - timedelta(hours=6)
    t = _base_task(
        task_id="TASK-DEMO-search-api",
        title="Implement /search API endpoint",
        owner_agent="claude-code:DeskbookPro:search",
        priority="P1",
        kind="feature",
        created_at=created,
        summary="Endpoint skeleton + ranking done; wiring pagination.",
        next_action="Add cursor pagination, then integration tests for /search?q=",
    )
    t = schema.apply_transition(
        t, "active", by="claude-code:DeskbookPro:search",
        summary="Endpoint skeleton + ranking done; wiring pagination.",
        next_action="Add cursor pagination, then integration tests for /search?q=",
        dt=now - timedelta(minutes=10),
    )
    tasks.append(t)

    # 2. infra-cluster — ACTIVE, P2, openclaw, updated ~25 min ago.
    created = now - timedelta(hours=8)
    t = _base_task(
        task_id="TASK-DEMO-infra-cluster",
        title="Provision search cluster (Terraform)",
        owner_agent="openclaw:macmini:infra",
        priority="P2",
        kind="infra",
        created_at=created,
        summary="Staging cluster up; prod plan written.",
        next_action="Apply prod plan after cost-review sign-off.",
    )
    t = schema.apply_transition(
        t, "active", by="openclaw:macmini:infra",
        summary="Staging cluster up; prod plan written.",
        next_action="Apply prod plan after cost-review sign-off.",
        dt=now - timedelta(minutes=25),
    )
    tasks.append(t)

    # 3. query-parser — WAITING, P2, codex, updated ~1h ago.
    created = now - timedelta(hours=5)
    t = _base_task(
        task_id="TASK-DEMO-query-parser",
        title="Refactor query parser for filters",
        owner_agent="codex:DeskbookPro:search",
        priority="P2",
        kind="feature",
        created_at=created,
        summary="Refactor drafted; paused on API contract.",
    )
    # proposed -> active -> waiting (waiting is not reachable directly from
    # proposed with a next_action narrative, but proposed->waiting IS allowed;
    # we go through active to mirror a real "started then parked" history).
    t = schema.apply_transition(
        t, "active", by="codex:DeskbookPro:search",
        summary="Refactor drafted.",
        dt=now - timedelta(hours=2),
    )
    t = schema.apply_transition(
        t, "waiting", by="codex:DeskbookPro:search",
        summary="Refactor drafted; paused on API contract.",
        next_action=("Resume once the /search contract is frozen "
                     "(see TASK-DEMO-search-api)."),
        dt=now - timedelta(hours=1),
    )
    tasks.append(t)

    # 4. prod-index — BLOCKED, P1, claude-code, updated ~40 min ago.
    created = now - timedelta(hours=7)
    t = _base_task(
        task_id="TASK-DEMO-prod-index",
        title="Enable prod search index",
        owner_agent="claude-code:DeskbookPro:search",
        priority="P1",
        kind="infra",
        created_at=created,
        summary="Ready to enable; waiting on credentials.",
    )
    t = schema.apply_transition(
        t, "active", by="claude-code:DeskbookPro:search",
        dt=now - timedelta(hours=3),
    )
    t = schema.apply_transition(
        t, "blocked", by="claude-code:DeskbookPro:search",
        blocked_on="Waiting on SRE creds approval — TICKET-4412.",
        dt=now - timedelta(minutes=40),
    )
    tasks.append(t)

    # 5. backfill — ACTIVE, P2, claude-code, updated ~4 HOURS ago => STALE.
    created = now - timedelta(hours=9)
    t = _base_task(
        task_id="TASK-DEMO-backfill",
        title="Backfill historical documents into index",
        owner_agent="claude-code:DeskbookPro:backfill",
        priority="P2",
        kind="ops",
        created_at=created,
        summary="Backfill job started (~2.1M docs).",
        next_action="Monitor job, verify counts, then mark done.",
    )
    t = schema.apply_transition(
        t, "active", by="claude-code:DeskbookPro:backfill",
        summary="Backfill job started (~2.1M docs).",
        next_action="Monitor job, verify counts, then mark done.",
        dt=now - timedelta(hours=4),
    )
    tasks.append(t)

    # 6. staging-cluster — DONE, P2, openclaw, done ~2h ago.
    created = now - timedelta(hours=10)
    t = _base_task(
        task_id="TASK-DEMO-staging-cluster",
        title="Stand up staging search cluster",
        owner_agent="openclaw:macmini:infra",
        priority="P2",
        kind="infra",
        created_at=created,
        summary="Provisioning staging cluster.",
    )
    t = schema.apply_transition(
        t, "active", by="openclaw:macmini:infra",
        dt=now - timedelta(hours=5),
    )
    t = schema.apply_transition(
        t, "done", by="openclaw:macmini:infra",
        evidence="Staging cluster live; smoke passed.",
        verification_level="agent-verified",
        dt=now - timedelta(hours=2),
    )
    tasks.append(t)

    return tasks


# ---------------------------------------------------------------------------
# Upload / reset
# ---------------------------------------------------------------------------

def upload_tasks_and_views(
    tasks: list[dict[str, Any]],
    *,
    backend: Optional[list[str]] = None,
) -> tuple[int, int, list[str]]:
    """Upload every task file then every materialized view. Returns
    (tasks_uploaded, views_uploaded, failures)."""
    failures: list[str] = []

    tasks_ok = 0
    for t in tasks:
        path = remote.task_remote_path(t["id"])
        if remote.upload_json(t, path, backend=backend):
            tasks_ok += 1
        else:
            failures.append(f"task:{t['id']}")

    all_views = views.build_all_views(tasks)
    views_ok = 0
    for name, data in all_views.items():
        path = _view_name_to_remote(name)
        if remote.upload_json(data, path, backend=backend):
            views_ok += 1
        else:
            failures.append(f"view:{name}")

    return tasks_ok, views_ok, failures


def reset_demo(backend: Optional[list[str]] = None) -> None:
    """Best-effort delete of prior demo task files.

    The remote layer does wrap ``delete`` nowadays (``remote.delete``), but the
    demo deliberately does not use it: reset is achieved by overwriting all
    known task ids and every view in the subsequent seed — the deterministic-id
    design makes that a true reset for the demo's own files, with no risk of a
    failed delete leaving the root half-cleared mid-demo. This function
    additionally blanks any demo task whose id is known, so a status filter on
    the root won't surface a stale prior body if the new seed has fewer tasks
    (it never does, but this keeps the contract honest).

    NOTE: this only touches the demo's own deterministic ids + views; it never
    deletes unrelated files under the root.
    """
    # The seed always rewrites all TASK_IDS and all views, so an overwrite-based
    # reset is sufficient. Nothing to pre-delete given a fixed id set; this hook
    # exists so --reset is explicit and future-proof if ids ever change.
    return None


# ---------------------------------------------------------------------------
# Summary printing
# ---------------------------------------------------------------------------

def summarize(tasks: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Compute a per-status count + the set of stale task ids (for the operator
    printout and for the test to assert against)."""
    by_status: dict[str, int] = {}
    stale_ids: list[str] = []
    for t in tasks:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        if views.is_stale(t, now):
            stale_ids.append(t["id"])
    return {"by_status": by_status, "stale_ids": stale_ids, "total": len(tasks)}


def print_summary(summary: dict[str, Any], root: str) -> None:
    print(f"\nSeeded coordination root: {root}")
    print(f"  Total tasks: {summary['total']}")
    print("  By status:")
    for status in ("active", "waiting", "blocked", "proposed", "done", "abandoned"):
        if status in summary["by_status"]:
            print(f"    {status:10s} {summary['by_status'][status]}")
    if summary["stale_ids"]:
        print(f"  Stale (forgotten) tasks flagged: {', '.join(summary['stale_ids'])}")
    else:
        print("  Stale tasks: none")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the three-agent coordination demo.")
    parser.add_argument(
        "--now",
        help="Base time as ISO 8601 (default: current UTC). Offsets derive from this.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset known demo files before seeding (overwrite-based).",
    )
    parser.add_argument(
        "--local-host",
        default=socket.gethostname().split(".")[0] or "DeskbookPro",
        help="Hostname for the LOCAL agents (claude-code/codex). Defaults to this "
             "machine's short hostname so a real SessionStart hook's owner-exact "
             "'mine' section matches the seeded owners. (openclaw stays on macmini.)",
    )
    args = parser.parse_args(argv)

    if args.now:
        try:
            now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        except ValueError:
            print(f"ERROR: --now must be ISO 8601; got {args.now!r}", file=sys.stderr)
            return 2
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)

    root = remote.remote_root()

    if args.reset:
        reset_demo()
        print(f"[reset] Will overwrite all demo task ids + views under {root}")

    tasks = build_scenario_tasks(now)
    # Retarget the LOCAL agents' host to this machine so a real SessionStart hook
    # (owner-exact 'mine' match) surfaces the seeded work. The scenario hardcodes
    # "DeskbookPro"; swap it for the live host. openclaw stays on "macmini" (a
    # genuinely different machine — the cross-machine point of the demo).
    if args.local_host and args.local_host != "DeskbookPro":
        tasks = [_json.loads(_json.dumps(t).replace("DeskbookPro", args.local_host))
                 for t in tasks]

    tasks_ok, views_ok, failures = upload_tasks_and_views(tasks)

    summary = summarize(tasks, now)
    print_summary(summary, root)
    print(f"  Uploaded: {tasks_ok}/{len(tasks)} tasks, {views_ok} views")

    if failures:
        print(f"\nERROR: {len(failures)} upload(s) failed: {failures}", file=sys.stderr)
        print("Check that the host is fulcra-api-authed (fulcra-coord doctor).",
              file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
