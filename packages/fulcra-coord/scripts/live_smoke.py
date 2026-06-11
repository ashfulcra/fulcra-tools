#!/usr/bin/env python3
"""Live smoke test for fulcra-coord.

Requires explicit opt-in:
  FULCRA_COORD_LIVE_SMOKE=1 python scripts/live_smoke.py

Uses a disposable path under the configured remote root to avoid polluting
production coordination data.

Prerequisites:
  - fulcra-api installed and authenticated (run: fulcra-api auth login)
  - FULCRA_COORD_REMOTE_ROOT set to a writable path, e.g. /coordination-smoke
  - Or rely on the default /coordination path if you own it
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

if not os.environ.get("FULCRA_COORD_LIVE_SMOKE"):
    print("Set FULCRA_COORD_LIVE_SMOKE=1 to run live smoke tests.")
    print("WARNING: This will write files to your Fulcra account.")
    sys.exit(0)

# Add package root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import remote, schema, views
from fulcra_coord import remote_root


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        sys.exit(1)


def main() -> None:
    smoke_root = os.environ.get("FULCRA_COORD_REMOTE_ROOT", "/coordination-smoke")
    print(f"\nfulcra-coord live smoke")
    print(f"  Remote root: {remote_root()}")
    print(f"  (using FULCRA_COORD_REMOTE_ROOT={smoke_root})")
    print()

    # 1. Verify CLI is available
    print("[1] CLI availability")
    ok, msg = remote.check_cli_available()
    check("CLI reachable", ok, msg)

    # 2. Upload a test file
    print("\n[2] Upload")
    test_path = f"{remote_root()}/smoke-test.json"
    payload = {"smoke": True, "ts": time.time()}
    upload_ok = remote.upload_json(payload, test_path)
    check("Upload succeeded", upload_ok, test_path)

    # 3. Stat the file
    print("\n[3] Stat")
    s = remote.stat(test_path)
    check("Stat returned data", s is not None)
    check("Stat has version info", bool(s and (s.get("version_id") or s.get("size"))), str(s))

    # 4. Download and verify
    print("\n[4] Download")
    downloaded = remote.download_json(test_path)
    check("Download succeeded", downloaded is not None)
    check("Downloaded content matches", downloaded == payload if downloaded else False)

    # 5. Create a real task and upload it
    print("\n[5] Task round-trip")
    task = schema.make_task(
        title="Live smoke test task",
        workstream="general",
        agent="smoke-test",
        kind="ops",
        priority="P3",
        summary="Created by live_smoke.py",
        next_action="Verify and clean up",
    )
    task_path = remote.task_remote_path(task["id"])
    task_ok = remote.upload_json(task, task_path)
    check("Task upload succeeded", task_ok, task["id"])

    # 6. Stat the task
    task_stat = remote.stat(task_path)
    check("Task stat succeeded", task_stat is not None)

    # 7. Transition and re-upload
    print("\n[6] Status transition")
    active_task = schema.apply_transition(task, "active", by="smoke-test")
    active_ok = remote.upload_json(active_task, task_path)
    check("Active transition upload succeeded", active_ok)

    new_stat = remote.stat(task_path)
    check("Stat changed after upload", remote.stat_changed(task_stat, new_stat))

    # 8. Build and upload views
    print("\n[7] View generation")
    all_tasks = [active_task]
    all_views = views.build_all_views(all_tasks)
    check("Views generated", len(all_views) > 0, f"{len(all_views)} views")

    index_path = remote.view_remote_path("index")
    index_ok = remote.upload_json(all_views["index"], index_path)
    check("Index view uploaded", index_ok)

    # 9. List files under coordination root
    print("\n[8] List files")
    file_list = remote.list_files(remote_root())
    check("List returns entries", len(file_list) > 0, f"{len(file_list)} files")

    print(f"\nAll smoke checks passed.")
    print(f"\nClean up:")
    print(f"  fulcra-api file list {remote_root()}")
    print(f"  (manually delete smoke files from your Fulcra account)")


if __name__ == "__main__":
    main()
