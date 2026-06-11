"""Reconcile body-replay must not clobber newer remote writes.

2026-06-11 bug hunt C2 (P1): when a write left a ``failed``/``unverified``
needs_reconcile op marker, the next ``reconcile`` replayed the locally cached
task body with a BLIND upload — no look at what is on the bus now. Host A
parking an unverified old body therefore reverted host B's newer remote write
(a status transition, a fresh summary) the moment A reconciled. The fix
downloads the current remote body first and routes the replay through the
same ``_try_merge`` the write pipeline uses; the blind upload survives ONLY
for the remote-absent case (the genuine lost-write repair this replay exists
for, pinned below).

Same fixture idiom as the other reconcile tests: the per-test fake Fulcra
backend (coord_backend) carries real durable state, so the assertions check
what actually ends up on the bus.
"""
from __future__ import annotations

import copy
import types

from fulcra_coord import cache, remote, schema


def _seed_marker(task_id: str, status: str = "unverified") -> None:
    cache.ensure_dirs()
    cache.write_op_marker("replay01", {
        "op_id": "replay01",
        "command": "update",
        "task_id": task_id,
        "status": status,
        "needs_reconcile": True,
        "started_at": "2026-01-01T00:00:00Z",
    })


def _old_and_newer_bodies() -> tuple[dict, dict]:
    """One task, two generations: the stale body host A cached, and the newer
    body host B successfully wrote to the bus afterwards."""
    base = schema.make_task(title="shared work item", workstream="general",
                            agent="hostA:h:r", summary="old summary from A")
    base["updated_at"] = "2026-06-10T00:00:00.000000Z"
    newer = copy.deepcopy(base)
    newer["current_summary"] = "newer summary from B"
    newer["last_touched_by"] = "hostB:h:r"
    newer["updated_at"] = "2026-06-11T00:00:00.000000Z"
    return base, newer


def test_replay_does_not_revert_newer_remote_body(coord_backend):
    from fulcra_coord.cli import cmd_reconcile
    old, newer = _old_and_newer_bodies()
    # Host A: stale cached body + an unverified-write debt marker.
    cache.write_cached_task(old)
    _seed_marker(old["id"])
    # Host B: meanwhile wrote a NEWER body to the bus.
    path = remote.task_remote_path(old["id"])
    assert remote.upload_json(newer, path, backend=coord_backend)

    rc = cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)

    assert rc == 0
    on_bus = remote.download_json(path, backend=coord_backend)
    # The newer write SURVIVES the replay (merged, not reverted).
    assert on_bus["current_summary"] == "newer summary from B"
    assert on_bus["updated_at"] >= newer["updated_at"]


def test_replay_skips_unsafe_merge_when_remote_is_newer(coord_backend):
    # Both sides independently transitioned status from the shared base —
    # _try_merge calls that unsafe. With the remote side as-new-or-newer the
    # replay must SKIP (the cached body is the stale one) rather than clobber.
    from fulcra_coord.cli import cmd_reconcile
    old, newer = _old_and_newer_bodies()
    cached = schema.apply_transition(copy.deepcopy(old), "active",
                                     by="hostA:h:r")
    cached["updated_at"] = "2026-06-10T01:00:00.000000Z"
    remote_b = schema.apply_transition(copy.deepcopy(newer), "waiting",
                                       by="hostB:h:r")
    remote_b["updated_at"] = "2026-06-12T00:00:00.000000Z"
    cache.write_cached_task(cached)
    _seed_marker(old["id"])
    path = remote.task_remote_path(old["id"])
    assert remote.upload_json(remote_b, path, backend=coord_backend)

    rc = cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)

    assert rc == 0
    on_bus = remote.download_json(path, backend=coord_backend)
    assert on_bus["status"] == "waiting"            # B's transition survives
    assert on_bus["updated_at"] == remote_b["updated_at"]  # byte-stale check:
    # nothing was uploaded over B's body at all.


def test_replay_unsafe_merge_with_newer_cache_keeps_the_debt(coord_backend):
    # Unsafe merge AND the cached side is newer: neither blind replay (would
    # clobber B's transition) nor skip (would silently drop A's newer work)
    # is safe — the repair must FAIL VISIBLY (marker preserved, exit 1) so a
    # human/maintainer resolves it, exactly like an un-uploadable body.
    from fulcra_coord.cli import cmd_reconcile
    old, newer = _old_and_newer_bodies()
    remote_b = schema.apply_transition(copy.deepcopy(old), "waiting",
                                       by="hostB:h:r")
    remote_b["updated_at"] = "2026-06-10T02:00:00.000000Z"
    cached = schema.apply_transition(copy.deepcopy(newer), "active",
                                     by="hostA:h:r")
    cached["updated_at"] = "2026-06-12T00:00:00.000000Z"
    cache.write_cached_task(cached)
    _seed_marker(old["id"])
    path = remote.task_remote_path(old["id"])
    assert remote.upload_json(remote_b, path, backend=coord_backend)

    rc = cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)

    assert rc == 1
    on_bus = remote.download_json(path, backend=coord_backend)
    assert on_bus["status"] == "waiting"   # B's body untouched either way
    remaining = cache.list_op_markers()
    assert any(m["op_id"] == "replay01" for m in remaining)


def test_replay_still_uploads_when_remote_is_absent(coord_backend):
    # PIN of the existing repair behavior: the genuine lost-write case (the
    # body never landed on the bus at all) must still blind-replay the cache.
    from fulcra_coord.cli import cmd_reconcile
    old, _ = _old_and_newer_bodies()
    cache.write_cached_task(old)
    _seed_marker(old["id"], status="failed")

    rc = cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)

    assert rc == 0
    on_bus = remote.download_json(remote.task_remote_path(old["id"]),
                                  backend=coord_backend)
    assert on_bus is not None
    assert on_bus["current_summary"] == "old summary from A"


# ---------------------------------------------------------------------------
# 2026-06-11 bug hunt S7: per-view upload budget. Each view upload in the
# reconcile pool used to receive the WHOLE remaining reconcile deadline as
# its subprocess timeout — one wedged backend call could then consume the
# entire tick's budget (and the retry could do it again), starving every
# other view. The per-view budget must be min(remaining, _write_timeout()),
# resolved through the function (env-tunable, post-#157), never a constant.
# ---------------------------------------------------------------------------

def _capture_view_upload_timeouts(coord_backend, monkeypatch):
    """Run cmd_reconcile with one seeded task; return the explicit ``timeout``
    values passed to remote.upload_json. Only the view-upload pool passes an
    explicit timeout in cmd_reconcile, so the capture isolates exactly it."""
    from fulcra_coord.cli import cmd_reconcile
    t = schema.make_task(title="budget probe", workstream="general",
                         agent="a:h:r", summary="probe")
    assert remote.upload_json(t, remote.task_remote_path(t["id"]),
                              backend=coord_backend)
    captured: list[int] = []
    real_upload = remote.upload_json

    def capturing(data, path, backend=None, timeout=None):
        if timeout is not None:
            captured.append(timeout)
        return real_upload(data, path, backend=backend, timeout=timeout)

    monkeypatch.setattr(remote, "upload_json", capturing)
    assert cmd_reconcile(types.SimpleNamespace(), backend=coord_backend) == 0
    assert captured, "the view-upload pool never ran"
    return captured


def test_view_upload_budget_capped_at_write_timeout(coord_backend, monkeypatch):
    # Plenty of deadline headroom (~80s remaining) -> every per-view budget
    # is exactly the transport write timeout, not the whole deadline.
    monkeypatch.setenv("FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS", "80")
    monkeypatch.setattr(remote, "_write_timeout", lambda: 60)
    captured = _capture_view_upload_timeouts(coord_backend, monkeypatch)
    assert all(t == 60 for t in captured), captured


def test_view_upload_budget_shrinks_to_remaining_deadline(coord_backend,
                                                          monkeypatch):
    # Tight deadline (10s) below the write timeout -> the remaining deadline
    # is the binding constraint (hard ceiling preserved).
    monkeypatch.setenv("FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS", "10")
    monkeypatch.setattr(remote, "_write_timeout", lambda: 60)
    captured = _capture_view_upload_timeouts(coord_backend, monkeypatch)
    assert all(1 <= t <= 10 for t in captured), captured
