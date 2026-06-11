"""Tombstone-aware absence: soft-deleted remote files must be confirmable.

THE LIVE BUG (2026-06-11, ~12 repair markers blocked forever): the Fulcra
Files platform DELETE is a SOFT delete. A deleted file keeps version history
that ``fulcra file stat`` still reports (the live stat text shape carries
``Version:`` / ``Previous Versions:`` lines — pinned in test_fulcra_coord's
``test_parse_live_text_stat_shape``), while ``download`` fails
deterministically with a not-found-class error (#167's transient classifier
deliberately treats 404/Not Found as NOT transient). Every absence check
built on "stat is None => maybe absent" — ``io._confirmed_absent`` and the
repair loop's C2 guard — therefore concluded "exists but unreadable" for a
tombstone: absence was never confirmable, and repairs against tombstoned
paths re-failed every tick, forever.

THE TOMBSTONE SIGNATURE (established from the codebase, see store.delete /
retention's soft-delete commentary / cmd_restore's ``fulcra file restore
<VERSION_ID>`` notes): stat succeeds (version history is still visible) AND a
fresh download fails with a POSITIVE not-found-class error (never a 5xx /
timeout / unknown failure) AND the bus probes reachable. Only all three
together confirm absence; a transient or unknown download failure keeps the
fail-safe "unconfirmable" verdict.

RESURRECTION HAZARD (the F7-adjacent class): a tombstoned task path means the
task was DELIBERATELY deleted (archived/pruned/moved). The repair loop must
NEVER respond by re-uploading its cached body — that would resurrect a dead
task. Archived => the marker is obsolete (the truth lives in the archive);
not archived => clear with a distinct ops-log reason pointing the operator at
the platform's version history, still without re-uploading.

The fake backend models the soft delete via a ``<path>.tombstone`` sibling
file: stat answers with the prior version's metadata, download exits 1 with
the not-found-class stderr the real CLI emits.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

from fulcra_coord import cache, remote, schema
from fulcra_coord.io import _confirmed_absent

FAKE = Path(__file__).resolve().parent / "fake_fulcra_backend.py"


def _fake_root() -> Path:
    return Path(os.environ["FULCRA_FAKE_ROOT"])


def _tombstone(remote_path: str, prior_content: str = "{}") -> None:
    """Soft-delete ``remote_path`` in the fake store: no live body, but a
    ``.tombstone`` sibling that stat keeps reporting (version history)."""
    local = _fake_root() / remote_path.lstrip("/")
    local.parent.mkdir(parents=True, exist_ok=True)
    if local.exists():
        local.unlink()
    Path(str(local) + ".tombstone").write_text(prior_content)


def _make_task(summary: str = "cached body") -> dict:
    return schema.make_task(title="tombstoned work item", workstream="general",
                            agent="hostA:h:r", summary=summary)


# ---------------------------------------------------------------------------
# (a) io._confirmed_absent
# ---------------------------------------------------------------------------

def test_confirmed_absent_true_for_tombstone(coord_backend):
    # stat sees version history, download deterministically 404s, the bus is
    # reachable: that IS confirmed absence (the soft-delete tombstone).
    t = _make_task()
    path = remote.task_remote_path(t["id"])
    _tombstone(path, json.dumps(t))

    assert remote.stat(path, backend=coord_backend) is not None  # the trap
    assert remote.download(path, backend=coord_backend) is None

    assert _confirmed_absent(path, backend=coord_backend) is True


def test_confirmed_absent_false_when_download_failure_is_transient(
        coord_backend, tmp_path, monkeypatch):
    # Same stat-visible path, but the download fails with TRANSIENT weather
    # (504): absence stays UNCONFIRMABLE — fail-safe, exactly as before.
    from fulcra_coord_files import store
    monkeypatch.setattr(store, "_RETRY_BACKOFF_SECONDS", 0.0)
    t = _make_task()
    path = remote.task_remote_path(t["id"])
    local = _fake_root() / path.lstrip("/")
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(json.dumps(t))

    wrapper = tmp_path / "transient_download_backend.py"
    wrapper.write_text(
        f"""
import os, sys
if sys.argv[1:2] == ["download"]:
    sys.stderr.write("Error: HTTP Error 504: Gateway Timeout\\n")
    sys.exit(1)
os.execv({sys.executable!r}, [{sys.executable!r}, {str(FAKE)!r}] + sys.argv[1:])
"""
    )
    backend = [sys.executable, str(wrapper)]
    assert _confirmed_absent(path, backend=backend) is False


def test_confirmed_absent_false_when_bus_unreachable(coord_backend, tmp_path):
    # Tombstone-shaped failure but the reachability probe fails: nothing is
    # confirmable on an unreachable bus.
    t = _make_task()
    path = remote.task_remote_path(t["id"])
    _tombstone(path, json.dumps(t))

    wrapper = tmp_path / "unreachable_backend.py"
    wrapper.write_text(
        f"""
import os, sys
if sys.argv[1:2] == ["list"]:
    sys.stderr.write("Connection refused\\n")
    sys.exit(1)
os.execv({sys.executable!r}, [{sys.executable!r}, {str(FAKE)!r}] + sys.argv[1:])
"""
    )
    backend = [sys.executable, str(wrapper)]
    assert _confirmed_absent(path, backend=backend) is False


# ---------------------------------------------------------------------------
# (b)/(c) the repair loop's C2 guard
# ---------------------------------------------------------------------------

def _seed_marker(task_id: str, op_id: str = "tomb01") -> None:
    cache.ensure_dirs()
    cache.write_op_marker(op_id, {
        "op_id": op_id,
        "command": "update",
        "task_id": task_id,
        "status": "failed",
        "needs_reconcile": True,
        "started_at": "2026-01-01T00:00:00Z",
    })


def test_repair_marker_for_tombstoned_archived_task_clears_without_upload(
        coord_backend):
    from fulcra_coord.cli import cmd_reconcile
    t = _make_task()
    tid = t["id"]
    cache.write_cached_task(t)
    _seed_marker(tid)
    task_path = remote.task_remote_path(tid)
    _tombstone(task_path, json.dumps(t))
    # The task lives in the cold archive: body + index shard.
    archive_path = remote.archive_task_path(tid, "2026-05")
    assert remote.upload_json(t, archive_path, backend=coord_backend)
    assert remote.upload_json(
        {"id": tid, "archive_path": archive_path},
        remote.archive_index_path(tid), backend=coord_backend)

    rc = cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)

    assert rc == 0
    # The cached body was NOT re-uploaded (no resurrection of an archived task).
    assert remote.download_json(task_path, backend=coord_backend) is None
    assert not (_fake_root() / task_path.lstrip("/")).exists()
    # The obsolete marker is cleared — the debt is resolved, not parked.
    assert not any(m.get("op_id") == "tomb01" for m in cache.list_op_markers())
    # Distinct per-task reason in the ops log.
    entries = cache.read_ops_log()
    assert any(e.get("status") == "task_body_repair_tombstone"
               and e.get("task_id") == tid
               and e.get("detail") == "tombstone: archived, marker cleared"
               for e in entries), entries
    # The local cached copy is evicted (same rationale as _archive_task: the
    # cache-seeded loader would resurrect the id into the views).
    assert cache.read_cached_task(tid) is None


def test_repair_marker_for_tombstoned_unarchived_task_clears_without_reupload(
        coord_backend):
    from fulcra_coord.cli import cmd_reconcile
    t = _make_task()
    tid = t["id"]
    cache.write_cached_task(t)
    _seed_marker(tid, op_id="tomb02")
    task_path = remote.task_remote_path(tid)
    _tombstone(task_path, json.dumps(t))
    # NO archive shard: operator intent is ambiguous — still never re-upload.

    rc = cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)

    assert rc == 0
    assert remote.download_json(task_path, backend=coord_backend) is None
    assert not (_fake_root() / task_path.lstrip("/")).exists()
    assert not any(m.get("op_id") == "tomb02" for m in cache.list_op_markers())
    entries = cache.read_ops_log()
    assert any(e.get("status") == "task_body_repair_tombstone"
               and e.get("task_id") == tid
               and e.get("detail") == ("tombstone: not in archive, "
                                       "marker cleared without re-upload")
               for e in entries), entries


def test_repair_marker_kept_when_download_failure_is_transient(
        coord_backend, tmp_path, monkeypatch):
    # Counter-pin: a stat-visible body whose download fails TRANSIENTLY is the
    # existing "exists but unreadable" case — marker kept, tick fails, exactly
    # the pre-fix behavior (#176's backoff then rotates it out of the head).
    from fulcra_coord.cli import cmd_reconcile
    from fulcra_coord_files import store
    monkeypatch.setattr(store, "_RETRY_BACKOFF_SECONDS", 0.0)
    t = _make_task()
    tid = t["id"]
    cache.write_cached_task(t)
    _seed_marker(tid, op_id="tomb03")
    task_path = remote.task_remote_path(tid)
    local = _fake_root() / task_path.lstrip("/")
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(json.dumps(t))

    wrapper = tmp_path / "transient_task_download_backend.py"
    wrapper.write_text(
        f"""
import os, sys
if sys.argv[1:2] == ["download"] and sys.argv[2:3] == [{task_path!r}]:
    sys.stderr.write("Error: HTTP Error 504: Gateway Timeout\\n")
    sys.exit(1)
os.execv({sys.executable!r}, [{sys.executable!r}, {str(FAKE)!r}] + sys.argv[1:])
"""
    )
    backend = [sys.executable, str(wrapper)]
    rc = cmd_reconcile(types.SimpleNamespace(), backend=backend)

    assert rc == 1
    assert any(m.get("op_id") == "tomb03" for m in cache.list_op_markers())
    # The body is still on the bus, untouched.
    assert json.loads(local.read_text())["id"] == tid
