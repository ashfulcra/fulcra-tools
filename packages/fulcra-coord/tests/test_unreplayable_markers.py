"""No-body op markers must self-clear (and stop being minted in the first place).

THE LIVE BUG (2026-06-11, 15 zombie markers, ops.log reason "no cached body to
replay"): a ``failed``/``unverified`` needs_reconcile op marker whose task has
NO locally cached body could never be repaired — the replay has nothing to
upload — and could never be cleared either: cmd_reconcile's body-repair loop
counted it a failure every pass, which fails the tick, which preserves ALL
markers. Fifteen of these survived every reconcile until an operator deleted
them by hand.

Two fixes, both pinned here:

1. **Self-clear (cmd_reconcile):** a no-cached-body marker is resolved by
   looking at the REMOTE instead of failing blind:

   * remote body READABLE  -> the write evidently landed by another path
     (another host's replay, a later successful write) — marker cleared.
   * remote CONFIRMED ABSENT (genuinely missing or soft-delete tombstoned —
     the ``io._confirmed_absent`` idiom from #170/#177) -> nothing can EVER
     replay this marker: it is pure debt with no asset. Cleared, with the
     ops-log reason "no cached body and remote absent — unreplayable marker
     cleared".
   * remote state UNKNOWN (transport failure) -> marker KEPT — fail toward
     retrying, never toward forgetting a write whose fate is unproven.

2. **Stop minting them (writepipe):** the source of the zombies was
   ``_write_task_and_views`` caching the body only AFTER a successful upload —
   a failed upload returned early with a failed/needs_reconcile marker and NO
   cached body. request-review's escalated-to-human path creates its task
   straight through the write pipeline (no pre-caching caller like cmd_start),
   so every escalation that hit upload weather minted an unreplayable marker.
   The body is now cached BEFORE the upload attempt, making every marker
   replayable by construction.

Same fixture idiom as the other reconcile tests: the per-test fake Fulcra
backend (coord_backend) carries real durable state, so the assertions check
what actually ends up on the bus.
"""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

from fulcra_coord import cache, remote, schema, writepipe

FAKE = Path(__file__).resolve().parent / "fake_fulcra_backend.py"

#: The exact ops-log reason the fix must emit for the pure-debt case.
_UNREPLAYABLE_REASON = ("no cached body and remote absent — "
                        "unreplayable marker cleared")


def _fake_root() -> Path:
    return Path(os.environ["FULCRA_FAKE_ROOT"])


def _tombstone(remote_path: str, prior_content: str = "{}") -> None:
    """Soft-delete ``remote_path`` in the fake store (the test_tombstone_absence
    idiom): no live body, but a ``.tombstone`` sibling that stat keeps
    reporting (version history)."""
    local = _fake_root() / remote_path.lstrip("/")
    local.parent.mkdir(parents=True, exist_ok=True)
    if local.exists():
        local.unlink()
    Path(str(local) + ".tombstone").write_text(prior_content)


def _make_task(summary: str = "body") -> dict:
    return schema.make_task(title="orphaned write", workstream="general",
                            agent="hostA:h:r", summary=summary)


def _seed_marker(task_id: str, op_id: str = "nobody01") -> None:
    """A failed needs_reconcile marker WITHOUT a cached body — the zombie."""
    cache.ensure_dirs()
    cache.write_op_marker(op_id, {
        "op_id": op_id,
        "command": "block",
        "task_id": task_id,
        "status": "failed",
        "needs_reconcile": True,
        "started_at": "2026-01-01T00:00:00Z",
    })


# ---------------------------------------------------------------------------
# 1. self-clear: no cached body + remote confirmed absent / tombstoned
# ---------------------------------------------------------------------------

def test_no_body_marker_with_tombstoned_remote_clears(coord_backend):
    from fulcra_coord.cli import cmd_reconcile
    t = _make_task()
    tid = t["id"]
    _seed_marker(tid)                       # NO cached body, by construction
    task_path = remote.task_remote_path(tid)
    _tombstone(task_path, json.dumps(t))    # deliberately deleted remote

    rc = cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)

    assert rc == 0
    # The zombie is cleared — the debt is resolved, not parked forever.
    assert not any(m.get("op_id") == "nobody01" for m in cache.list_op_markers())
    # Nothing was uploaded to the tombstoned path (no resurrection).
    assert remote.download_json(task_path, backend=coord_backend) is None
    # The exact operator-facing reason is in the ops log.
    entries = cache.read_ops_log()
    assert any(e.get("task_id") == tid
               and e.get("detail") == _UNREPLAYABLE_REASON
               for e in entries), entries


def test_no_body_marker_with_genuinely_absent_remote_clears(coord_backend):
    # Same verdict when the remote path simply never existed (stat misses on a
    # reachable bus — the plain half of the _confirmed_absent idiom).
    from fulcra_coord.cli import cmd_reconcile
    t = _make_task()
    tid = t["id"]
    _seed_marker(tid, op_id="nobody02")
    task_path = remote.task_remote_path(tid)
    assert remote.stat(task_path, backend=coord_backend) is None

    rc = cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)

    assert rc == 0
    assert not any(m.get("op_id") == "nobody02" for m in cache.list_op_markers())
    entries = cache.read_ops_log()
    assert any(e.get("task_id") == tid
               and e.get("detail") == _UNREPLAYABLE_REASON
               for e in entries), entries


# ---------------------------------------------------------------------------
# 2. self-clear: no cached body + remote body READABLE (write landed anyway)
# ---------------------------------------------------------------------------

def test_no_body_marker_with_readable_remote_clears_without_touching_it(
        coord_backend):
    from fulcra_coord.cli import cmd_reconcile
    t = _make_task(summary="the body that landed by another path")
    tid = t["id"]
    _seed_marker(tid, op_id="nobody03")
    task_path = remote.task_remote_path(tid)
    assert remote.upload_json(t, task_path, backend=coord_backend)

    rc = cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)

    assert rc == 0
    assert not any(m.get("op_id") == "nobody03" for m in cache.list_op_markers())
    # The remote body is untouched — there was nothing of ours to replay.
    on_bus = remote.download_json(task_path, backend=coord_backend)
    assert on_bus is not None
    assert on_bus["current_summary"] == "the body that landed by another path"


# ---------------------------------------------------------------------------
# 3. fail-safe: no cached body + remote state UNKNOWN -> marker KEPT
# ---------------------------------------------------------------------------

def test_no_body_marker_with_unknown_remote_is_kept(coord_backend, tmp_path,
                                                    monkeypatch):
    # Transport failure (504 weather) on the task path: absence is NOT
    # confirmable, the write's fate is unproven — keep the marker and fail the
    # tick, exactly the pre-fix behavior for this case.
    from fulcra_coord.cli import cmd_reconcile
    from fulcra_coord_files import store
    monkeypatch.setattr(store, "_RETRY_BACKOFF_SECONDS", 0.0)
    t = _make_task()
    tid = t["id"]
    _seed_marker(tid, op_id="nobody04")
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
    assert any(m.get("op_id") == "nobody04" for m in cache.list_op_markers())
    # The body is still on the bus, untouched.
    assert json.loads(local.read_text())["id"] == tid


# ---------------------------------------------------------------------------
# 4. the SOURCE: a failed upload must leave a replayable cached body
# ---------------------------------------------------------------------------

def _failing_task_upload(monkeypatch, task_path):
    """Make every upload of ``task_path`` fail; delegate everything else."""
    real_upload = remote.upload_json
    calls = {"n": 0}

    def fake_upload(data, path, **kw):
        if path == task_path:
            calls["n"] += 1
            return False
        return real_upload(data, path, **kw)

    monkeypatch.setattr(remote, "upload_json", fake_upload)
    monkeypatch.setattr(writepipe, "_retry_sleep", lambda seconds: None)
    return calls


def test_failed_upload_leaves_replayable_cached_body(coord_backend, monkeypatch):
    # The minting half of the zombie bug: _write_task_and_views used to cache
    # the body only AFTER a successful upload, so the failed path's marker had
    # no replay asset. The body must now be cached BEFORE the upload attempt.
    t = _make_task(summary="must survive the failed upload")
    task_path = remote.task_remote_path(t["id"])
    _failing_task_upload(monkeypatch, task_path)

    ok = writepipe._write_task_and_views(t, backend=coord_backend,
                                         command="block")

    assert ok is False
    markers = [m for m in cache.list_op_markers() if m.get("task_id") == t["id"]]
    assert markers and markers[0].get("status") == "failed"
    assert markers[0].get("needs_reconcile")
    # THE FIX: the marker is replayable — the body is in the cache.
    cached = cache.read_cached_task(t["id"])
    assert cached is not None
    assert cached["current_summary"] == "must survive the failed upload"


def test_escalation_write_failure_self_heals_via_reconcile(coord_backend,
                                                           monkeypatch):
    # End-to-end on the path that minted the live zombies: request-review's
    # escalated-to-human task is created straight through the write pipeline
    # (no pre-caching caller), so a failed upload used to leave a marker with
    # nothing to replay. Now: failure -> cached body + marker -> the next
    # reconcile (weather cleared) replays it onto the bus and clears the debt.
    from fulcra_coord.cli import cmd_reconcile
    from fulcra_coord.routing_ops import _escalate_review_to_human
    monkeypatch.setattr("fulcra_coord.identity.resolve_human",
                        lambda: "ash@fulcradynamics.com")
    monkeypatch.setattr("fulcra_coord.identity.resolve_agent",
                        lambda *a, **k: "codex:m:main")

    real_upload = remote.upload_json

    def fail_task_uploads(data, path, **kw):
        if path.split("/")[-2:-1] == ["tasks"]:
            return False
        return real_upload(data, path, **kw)

    monkeypatch.setattr(remote, "upload_json", fail_task_uploads)
    monkeypatch.setattr(writepipe, "_retry_sleep", lambda seconds: None)

    _escalate_review_to_human(pr="77", repo="fulcra-tools",
                              tried=["dead:h:r"], backend=coord_backend)

    markers = [m for m in cache.list_op_markers()
               if m.get("status") == "failed" and m.get("needs_reconcile")]
    assert markers, "the failed escalation write must leave a repair marker"
    tid = markers[0]["task_id"]
    cached = cache.read_cached_task(tid)
    assert cached is not None, "the marker must be replayable (body cached)"
    assert "needs:human" in cached.get("tags", [])

    # Weather clears: the standard reconcile replays the cached body.
    monkeypatch.setattr(remote, "upload_json", real_upload)
    rc = cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)
    assert rc == 0
    on_bus = remote.download_json(remote.task_remote_path(tid),
                                  backend=coord_backend)
    assert on_bus is not None and on_bus["id"] == tid
    assert not any(m.get("task_id") == tid and m.get("needs_reconcile")
                   for m in cache.list_op_markers())
