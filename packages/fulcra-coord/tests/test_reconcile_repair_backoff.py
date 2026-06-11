"""Repair-queue starvation: failing markers must not pin the head of the line.

2026-06-11 live find (post-recovery): cmd_reconcile's task-body-repair loop
iterates op markers in ``cache.list_op_markers()`` order (lexical OP-file
glob). ~12 markers failed deterministically every pass, each burning 30-60s
of remote ops before giving up — and because they sorted at the HEAD of the
iteration order, every reconcile pass (even a 900s one) re-failed the same
head and deferred the ~60 healthy markers behind them at the budget floor.
The queue could never drain past the failing head.

Fix pinned here, three parts:
  (a) a FAILED repair attempt stamps the marker (``repair_attempts`` +
      ``repair_last_attempt_at``) and on later passes previously-failed
      markers are processed AFTER never-attempted ones — fresh markers get
      first claim on the budget;
  (b) a marker inside its per-marker backoff window (min(2**attempts, 32)
      minutes, cap constant patchable) is SKIPPED outright for the pass —
      its marker is KEPT (debt, not success) and it is retried once the
      window expires. Unparseable stamps fail toward retrying
      (parse-don't-compare, never lexical);
  (c) the ``task_body_repair_failed`` ops-log entry carries a per-task
      REASON mapping (and the first few reasons are warned inline) so the
      next 3am diagnosis doesn't have to guess.

Same fixture idiom as test_reconcile_replay: the per-test fake backend
carries real durable state.
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from unittest import mock

from fulcra_coord import cache, remote, schema, timeutil, views


def _seed_repair(op_id: str) -> str:
    """Seed one cached task + a failed-write repair marker; return task id."""
    cache.ensure_dirs()
    t = schema.make_task(title=f"repair {op_id}", workstream="general",
                         agent="hostA:h:r", summary=f"body {op_id}")
    cache.write_cached_task(t)
    cache.write_op_marker(op_id, {
        "op_id": op_id,
        "command": "update",
        "task_id": t["id"],
        "status": "failed",
        "needs_reconcile": True,
        "started_at": "2026-01-01T00:00:00Z",
    })
    return t["id"]


def _marker(op_id: str) -> dict:
    found = [m for m in cache.list_op_markers() if m.get("op_id") == op_id]
    assert found, f"marker {op_id} missing"
    return found[0]


def _fail_upload_for(paths: set[str]):
    """Patch context: remote.upload_json fails fast for the given task paths."""
    real = remote.upload_json

    def failing(data, path, backend=None, timeout=None):
        if path in paths:
            return False
        return real(data, path, backend=backend, timeout=timeout)

    return mock.patch("fulcra_coord.remote.upload_json", side_effect=failing)


# ---------------------------------------------------------------------------
# (a) attempt stamps + fresh-first ordering
# ---------------------------------------------------------------------------

def test_failed_repair_stamps_attempt_bookkeeping(coord_backend):
    from fulcra_coord.cli import cmd_reconcile
    tid = _seed_repair("aaa-fail")
    path = remote.task_remote_path(tid)

    with _fail_upload_for({path}):
        rc = cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)

    assert rc == 1
    m = _marker("aaa-fail")
    assert m.get("repair_attempts") == 1
    # The stamp must be the house ISO shape — parseable, tz-aware.
    stamped = views._parse_dt(m.get("repair_last_attempt_at") or "")
    assert stamped is not None
    assert abs((datetime.now(timezone.utc) - stamped).total_seconds()) < 60


def test_previously_failed_marker_runs_after_fresh_ones(coord_backend,
                                                        monkeypatch):
    # The failing marker's op_id sorts FIRST in list_op_markers() — the bug
    # was exactly that glob order put it at the head every pass. After one
    # failure (stamped) and with its backoff window expired (cap patched to
    # 0), the next pass must attempt BOTH fresh markers before re-attempting
    # the known-bad one.
    from fulcra_coord import cli
    tid_bad = _seed_repair("aaa-fail")
    path_bad = remote.task_remote_path(tid_bad)

    with _fail_upload_for({path_bad}):
        assert cli.cmd_reconcile(types.SimpleNamespace(),
                                 backend=coord_backend) == 1

    tid_f1 = _seed_repair("zzz-fresh1")
    tid_f2 = _seed_repair("zzz-fresh2")
    watched = {path_bad, remote.task_remote_path(tid_f1),
               remote.task_remote_path(tid_f2)}

    # Backoff window -> 0 so the failed marker is retry-ELIGIBLE this pass;
    # what's under test is purely the ordering.
    monkeypatch.setattr(cli, "_REPAIR_BACKOFF_CAP_MINUTES", 0.0)

    order: list[str] = []
    real = remote.upload_json

    def recording(data, path, backend=None, timeout=None):
        if path in watched:
            order.append(path)
            if path == path_bad:
                return False
        return real(data, path, backend=backend, timeout=timeout)

    with mock.patch("fulcra_coord.remote.upload_json", side_effect=recording):
        rc = cli.cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)

    assert rc == 1  # the bad marker still fails — but LAST, not first
    assert order == [remote.task_remote_path(tid_f1),
                     remote.task_remote_path(tid_f2),
                     path_bad], order
    # Both fresh bodies landed despite the failing sibling.
    for tid in (tid_f1, tid_f2):
        assert remote.download_json(remote.task_remote_path(tid),
                                    backend=coord_backend) is not None
    assert _marker("aaa-fail").get("repair_attempts") == 2


# ---------------------------------------------------------------------------
# (b) backoff window: skip while recent, retry after expiry
# ---------------------------------------------------------------------------

def test_recently_failed_marker_skipped_inside_backoff_window(coord_backend):
    from fulcra_coord.cli import cmd_reconcile
    tid = _seed_repair("aaa-fail")
    path = remote.task_remote_path(tid)

    with _fail_upload_for({path}):
        assert cmd_reconcile(types.SimpleNamespace(),
                             backend=coord_backend) == 1
    assert _marker("aaa-fail").get("repair_attempts") == 1

    # Immediately re-reconcile: attempts=1 -> 2-minute window, so the marker
    # is inside backoff. NO repair I/O may run for it (the 30-60s re-fail per
    # pass was the live cost), the tick is NOT a failure (skip is debt, not
    # failure), and the marker is KEPT through the success-path clear loop.
    touched: list[str] = []
    real_dl = remote.download_json

    def recording_dl(p, *args, **kwargs):
        if p == path:
            touched.append(p)
        return real_dl(p, *args, **kwargs)

    with mock.patch("fulcra_coord.remote.download_json",
                    side_effect=recording_dl):
        rc = cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)

    assert rc == 0
    assert touched == [], "in-backoff marker must be skipped, not re-probed"
    m = _marker("aaa-fail")
    assert m.get("repair_attempts") == 1  # no new attempt while skipped
    # Body still not on the bus — the debt is real and still recorded.
    assert remote.download_json(path, backend=coord_backend) is None

    # Expire the window (clock-patch equivalent: move the stamp 10 minutes
    # back, well past the 2-minute window) — the marker must be retried, and
    # with the upload healthy again the repair completes and clears it.
    m["repair_last_attempt_at"] = timeutil.iso_z(
        datetime.now(timezone.utc) - timedelta(minutes=10))
    cache.write_op_marker("aaa-fail", m)

    rc = cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)

    assert rc == 0
    assert remote.download_json(path, backend=coord_backend) is not None
    assert not [x for x in cache.list_op_markers()
                if x.get("op_id") == "aaa-fail"]


def test_unparseable_attempt_stamp_fails_toward_retrying(coord_backend):
    # Parse-don't-compare discipline: a garbage repair_last_attempt_at must
    # be treated as never-attempted (retry now), not lexically compared.
    from fulcra_coord.cli import cmd_reconcile
    tid = _seed_repair("aaa-garbled")
    m = _marker("aaa-garbled")
    m["repair_attempts"] = 3
    m["repair_last_attempt_at"] = "not-a-timestamp"
    cache.write_op_marker("aaa-garbled", m)

    rc = cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)

    assert rc == 0
    assert remote.download_json(remote.task_remote_path(tid),
                                backend=coord_backend) is not None


# ---------------------------------------------------------------------------
# (c) per-task failure reasons in the ops log
# ---------------------------------------------------------------------------

def test_repair_failure_ops_log_carries_per_task_reasons(coord_backend,
                                                         capsys):
    from fulcra_coord.cli import cmd_reconcile
    # Marker 1: the upload itself fails.
    tid_up = _seed_repair("aaa-upfail")
    path_up = remote.task_remote_path(tid_up)
    # Marker 2: tonight's live shape — download says absent but stat says the
    # path EXISTS (soft-delete tombstone / flaky read): absence unconfirmable,
    # so the merge-gated replay must fail rather than blind-clobber.
    tid_stat = _seed_repair("aaa-statfail")
    path_stat = remote.task_remote_path(tid_stat)

    real_stat = remote.stat

    def tombstone_stat(p, *args, **kwargs):
        if p == path_stat:
            return {"path": p, "size": 0, "version": "tombstone"}
        return real_stat(p, *args, **kwargs)

    with _fail_upload_for({path_up}), \
            mock.patch("fulcra_coord.remote.stat", side_effect=tombstone_stat):
        rc = cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)

    assert rc == 1
    entries = [e for e in cache.read_ops_log()
               if e.get("status") == "task_body_repair_failed"]
    assert entries, cache.read_ops_log()
    detail = entries[-1].get("detail") or ""
    # Both ids present, each with a DISTINCT actionable reason.
    assert tid_up in detail and tid_stat in detail
    assert "upload failed" in detail
    assert "stat" in detail  # the absence-unconfirmable reason names the probe
    # ... and the operator tail shows WHY inline (first reasons warned).
    err_text = capsys.readouterr().err
    assert "upload failed" in err_text
