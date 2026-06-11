#!/usr/bin/env python3
"""
Multi-agent coordination example using fulcra-coord with a fake backend.

This script simulates two independent agents (Agent A and Agent B) coordinating
through Fulcra Files without any shared memory or direct communication.

Agent A creates a task, does some work, and pauses it.
Agent B picks it up independently, sees the waiting state, claims it, and completes it.

Run:
  python examples/multi_agent_example.py

No live Fulcra account required — uses in-memory fake backend.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import views
from fulcra_coord.schema import (
    make_task, apply_transition, apply_update, validate_task
)

# ---------------------------------------------------------------------------
# Fake backend: in-memory store simulating Fulcra Files
# ---------------------------------------------------------------------------

class FakeStore:
    """In-memory Fulcra Files stub."""

    def __init__(self):
        self._files: dict[str, str] = {}
        self._versions: dict[str, int] = {}

    def upload(self, content: str, path: str) -> bool:
        self._files[path] = content
        self._versions[path] = self._versions.get(path, 0) + 1
        return True

    def download(self, path: str) -> str | None:
        return self._files.get(path)

    def stat(self, path: str) -> dict | None:
        if path not in self._files:
            return None
        return {
            "version_id": f"v{self._versions[path]}",
            "size": len(self._files[path]),
        }

    def list(self, prefix: str) -> list[str]:
        return [p for p in self._files if p.startswith(prefix)]


# Shared store simulating Fulcra cloud
STORE = FakeStore()


# ---------------------------------------------------------------------------
# Minimal agent runtime that routes backend calls to the fake store
# ---------------------------------------------------------------------------

def make_fake_backend_fn(store: FakeStore):
    """Return a function that patches remote I/O to use the fake store."""
    import fulcra_coord.remote as _remote

    original_upload = _remote.upload
    original_download = _remote.download
    original_stat = _remote.stat
    original_list = _remote.list_files

    class patch:
        @staticmethod
        def upload(content: str, path: str, *, backend=None, timeout=None) -> bool:
            return store.upload(content, path)

        @staticmethod
        def download(path: str, *, backend=None, timeout=None) -> str | None:
            return store.download(path)

        @staticmethod
        def stat(path: str, *, backend=None) -> dict | None:
            return store.stat(path)

        @staticmethod
        def list_files(prefix: str, *, backend=None, timeout=None) -> list[str]:
            return store.list(prefix)

    return patch, original_upload, original_download, original_stat, original_list


def apply_patch(store: FakeStore):
    import fulcra_coord.remote as _remote
    _remote.upload = lambda content, path, **kw: store.upload(content, path)
    _remote.download = lambda path, **kw: store.download(path)
    _remote.upload_json = lambda data, path, **kw: store.upload(json.dumps(data, indent=2), path)
    _remote.download_json = lambda path, **kw: (
        json.loads(store.download(path)) if store.download(path) else None
    )
    _remote.stat = lambda path, **kw: store.stat(path)
    _remote.list_files = lambda prefix, **kw: store.list(prefix)


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------

def divider(label: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"{'─'*60}")


def main() -> None:
    tmp = tempfile.mkdtemp()
    os.environ["XDG_CACHE_HOME"] = tmp
    os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/coordination-example"

    apply_patch(STORE)

    from fulcra_coord import remote

    # =========================================================
    # AGENT A: Creates a task and does some work
    # =========================================================
    divider("Agent A: Starting work on 'Deploy search service'")

    task = make_task(
        title="Deploy search service to staging",
        workstream="devops",
        agent="agent-a",
        kind="ops",
        priority="P1",
        summary="Deploy the new search microservice to the staging environment.",
        next_action="Run terraform apply and verify health endpoint.",
    )
    task_id = task["id"]
    print(f"\n  Created task: {task_id}")
    print(f"  Status: {task['status']}")

    # Validate
    errs = validate_task(task)
    assert errs == [], f"Validation errors: {errs}"

    # Upload task
    task_path = remote.task_remote_path(task_id)
    assert remote.upload_json(task, task_path)
    print(f"  Uploaded to: {task_path}")

    # Agent A transitions to active and does some work
    task = apply_transition(task, "active", by="agent-a")
    assert remote.upload_json(task, task_path)
    print(f"  Status -> active (Agent A claimed it)")

    # Agent A updates progress
    task = apply_update(task, by="agent-a",
                        summary="Terraform apply succeeded. Verifying health endpoint...",
                        next_action="Run smoke tests against staging.")
    assert remote.upload_json(task, task_path)
    print(f"  Updated: progress note written")

    # Agent A pauses — session ending
    task = apply_transition(task, "waiting", by="agent-a",
                            next_action="Run smoke tests: GET /search?q=test should return 200.")
    assert remote.upload_json(task, task_path)
    print(f"  Status -> waiting (Agent A pausing — passing to Agent B)")

    # Agent A regenerates views
    all_views = views.build_all_views([task])
    for vname, vdata in all_views.items():
        if vname == "index":
            assert remote.upload_json(vdata, remote.view_remote_path("index"))
    print(f"  Views updated.")

    # =========================================================
    # AGENT B: Independent session, reads coordination state
    # =========================================================
    divider("Agent B: New session — reading coordination state")

    # Agent B reads the index to understand what's waiting
    index = remote.download_json(remote.view_remote_path("index"))
    assert index is not None
    print(f"\n  Index loaded. Active task count: {len(index.get('active', []))}")

    waiting_tasks = [t for t in index.get("active", []) if t["status"] == "waiting"]
    print(f"  Waiting tasks: {len(waiting_tasks)}")

    for summary in waiting_tasks:
        print(f"  - {summary['id'][:28]}  {summary['title']}")
        print(f"    Next: {summary.get('next_action', '')}")

    # Agent B picks up the task
    divider("Agent B: Claiming and completing the waiting task")

    fresh_task = remote.download_json(task_path)
    assert fresh_task is not None
    assert fresh_task["status"] == "waiting"

    # Optimistic concurrency: stat before editing
    pre_stat = remote.stat(task_path)
    print(f"\n  Pre-stat version: {pre_stat.get('version_id', '?')}")

    # Agent B resumes
    fresh_task = apply_transition(fresh_task, "active", by="agent-b",
                                  summary="Agent B picking up smoke tests.")
    assert remote.upload_json(fresh_task, task_path)
    print(f"  Status -> active (Agent B claimed it)")

    # Simulate smoke test work
    fresh_task = apply_update(fresh_task, by="agent-b",
                              summary="Smoke tests passed. /search returns 200 with correct results.",
                              next_action="Mark done and notify.")
    assert remote.upload_json(fresh_task, task_path)

    # Post-stat shows version changed
    post_stat = remote.stat(task_path)
    from fulcra_coord.remote import stat_changed
    changed = stat_changed(pre_stat, post_stat)
    print(f"  Version changed after Agent B write: {changed}")

    # Agent B marks done
    fresh_task = apply_transition(
        fresh_task, "done",
        by="agent-b",
        evidence="Smoke tests passed: GET /search?q=test → 200 OK, 3 results.",
        verification_level="agent-verified",
        confidence="high",
    )
    assert remote.upload_json(fresh_task, task_path)
    print(f"\n>>> Marked {task_id} done: {fresh_task['done']['evidence']}")
    print(f"  Verification: {fresh_task['done']['verification_level']}")
    print(f"  Done by: {fresh_task['done']['done_by']}")

    # Rebuild views
    all_views = views.build_all_views([fresh_task])
    print(f"\n  Views regenerated: {list(all_views.keys())}")

    # =========================================================
    # Verification
    # =========================================================
    divider("Verification: Final state")

    final = remote.download_json(task_path)
    assert final is not None
    assert final["status"] == "done"
    assert final["done"]["done_by"] == "agent-b"

    events = final.get("events", [])
    print(f"\n  Final status: {final['status']}")
    print(f"  Event count: {len(events)}")
    for ev in events:
        print(f"    [{ev['at'][:19]}] {ev['type']} by {ev['by']}: {ev['summary'][:60]}")

    print(f"\n  Tasks in store: {len(STORE.list('/coordination-example/tasks/'))}")
    print(f"  Views in store: {len(STORE.list('/coordination-example/'))}")

    print(f"\nExample complete — two agents coordinated through Fulcra Files without")
    print(f"shared memory, direct calls, or a central broker.")

    # Cleanup
    os.environ.pop("XDG_CACHE_HOME", None)
    os.environ.pop("FULCRA_COORD_REMOTE_ROOT", None)


if __name__ == "__main__":
    main()
