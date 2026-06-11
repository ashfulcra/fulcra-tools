"""Skip-unchanged view uploads on the write path (2026-06-10/11 incident fix).

THE MEASURED PROBLEM (live bus, 2026-06-10 night): ``_write_task_and_views``
rebuilds ALL views (~55 on the live bus: per-agent views for 33 identities,
inboxes, workstreams, needs-attention, board) and uploads EVERY one through an
8-thread pool on EVERY write. Under 1-16s per-op latency with intermittent
gateway 504s, 50+ uploads per logical write meant every write ended "Task
written, views failed: [~50 names]" -> NeedsReconcile, and reconcile's repair
pass (the same burst shape) could not drain — the repair backlog grew 67->95
across three runs. A tell/update/done actually changes ~5 views; the other ~50
are content-identical rebuilds.

THE FIX under test: each (view_name, view_data) is fingerprinted (sha256 over
the same serialization ``upload_json`` sends, with the per-rebuild top-level
``updated_at``/``generated_at`` stamps excluded — they change on every rebuild
even when the view content does not) and the upload is SKIPPED when the digest
matches the fingerprint recorded at the last CONFIRMED upload.

THE TRAP these tests pin: ``cache.write_cached_view`` is deliberately written
for EVERY view regardless of upload success (failed uploads still cache so
local readers see freshest). "content == cached view" therefore does NOT imply
"remote is current" — the skip decision must key off a SUCCESS-ONLY
fingerprint, written exclusively after a confirmed upload.

Topology used throughout (small + exact, so the pins are exact counts):
  T1: workstream "alpha", agent "agent-a"   (status proposed)
  T2: workstream "beta",  agent "agent-b"   (status proposed)

build_all_views for both tasks yields exactly 11 views:
  index, active, next, recently-done, search-index, needs-attention,
  summaries, workstreams/alpha, workstreams/beta, agents/agent-a,
  agents/agent-b
(no inbox views: no assignees). With both tasks proposed, ``active``,
``recently-done``, ``needs-attention`` AND the workstream views are empty of
summaries (build_workstream_view lists only active/waiting/blocked +
recent-done) and ``index`` carries only counts — so a T1-only field update
changes exactly 4 views: next, search-index, summaries, agents/agent-a
(the agent view does include proposed tasks). The live-incident shape
(~5 changed of ~55) at test scale: 4 changed of 11.
"""

from __future__ import annotations

import hashlib
import json
import os
import types
from pathlib import Path
from unittest import mock

import pytest

from fulcra_coord import cache, cli, remote, schema, views, writepipe
from fulcra_coord.timeutil import now_iso


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _view_path(name: str) -> str:
    return writepipe._view_name_to_remote(name)


def _is_view_path(path: str) -> bool:
    root = remote.remote_root()
    return (
        path == f"{root}/index.json"
        or path.startswith(f"{root}/views/")
        or path.startswith(f"{root}/workstreams/")
        or path.startswith(f"{root}/agents/")
    )


class UploadRecorder:
    """Counting/failing delegate around remote.upload_json.

    ``fail_paths`` force a False return for those exact remote paths (the
    upload is still recorded as attempted). Everything else delegates to the
    real fake-backend upload so the pipeline runs end-to-end."""

    def __init__(self, fail_paths=()):
        self.calls: list[str] = []
        self.fail_paths = set(fail_paths)
        self._real = remote.upload_json

    def __call__(self, data, path, **kw):
        self.calls.append(path)
        if path in self.fail_paths:
            return False
        return self._real(data, path, **kw)

    def view_uploads(self) -> list[str]:
        return [p for p in self.calls if _is_view_path(p)]


def _make_t1() -> dict:
    return schema.make_task(title="alpha task", workstream="alpha",
                            agent="agent-a")


def _make_t2() -> dict:
    return schema.make_task(title="beta task", workstream="beta",
                            agent="agent-b")


def _write(task, backend, *, fail_paths=(), monkeypatch=None) -> UploadRecorder:
    """Run one _write_task_and_views with a recording upload, swallowing the
    NeedsReconcile a forced view failure raises (the task body still lands)."""
    rec = UploadRecorder(fail_paths=fail_paths)
    with mock.patch.object(remote, "upload_json", rec):
        try:
            writepipe._write_task_and_views(task, backend=backend,
                                            command="write")
        except schema.NeedsReconcile:
            assert fail_paths, "NeedsReconcile without a forced failure"
    return rec


def _touch(task) -> dict:
    """A field edit that bumps the task's content + updated_at (what a real
    update command does before handing the body to the write pipeline)."""
    task["current_summary"] = f"progress {now_iso()}"
    task["updated_at"] = now_iso()
    return task


# The exact view set a single-task change touches in this topology (summaries
# is also the always-upload freshness beacon, see test below). The workstream
# views do NOT change: proposed tasks never appear in them.
T1_CHANGED_VIEWS = {"next", "search-index", "summaries", "agents/agent-a"}
T2_CHANGED_VIEWS = {"next", "search-index", "summaries", "agents/agent-b"}
ALL_VIEWS_BOTH_TASKS = {
    "index", "active", "next", "recently-done", "search-index",
    "needs-attention", "summaries", "workstreams/alpha", "workstreams/beta",
    "agents/agent-a", "agents/agent-b",
}


# ---------------------------------------------------------------------------
# Write path: first write uploads all, second uploads only what changed
# ---------------------------------------------------------------------------

def test_first_write_uploads_all_views_and_records_fingerprints(coord_backend):
    t1 = _make_t1()
    rec = _write(t1, coord_backend)
    # First write: no fingerprints exist, so every built view uploads.
    # T1-only topology: the 11-view set minus T2's workstream/agent views.
    expected = {_view_path(n) for n in ALL_VIEWS_BOTH_TASKS
                - {"workstreams/beta", "agents/agent-b"}}
    assert set(rec.view_uploads()) == expected
    assert len(rec.view_uploads()) == 9
    # Every successful upload recorded a fingerprint.
    for name in ALL_VIEWS_BOTH_TASKS - {"workstreams/beta", "agents/agent-b"}:
        assert cache.read_view_fingerprint(name), f"no fingerprint for {name}"


def test_second_write_uploads_only_changed_views(coord_backend):
    t1, t2 = _make_t1(), _make_t2()
    _write(t1, coord_backend)
    _write(t2, coord_backend)
    # THE PIN: a T1-only update uploads exactly the 4 views whose content
    # changed — not the full 11-view fan-out (live bus: ~5 instead of ~55).
    rec = _write(_touch(t1), coord_backend)
    assert set(rec.view_uploads()) == {_view_path(n) for n in T1_CHANGED_VIEWS}
    assert len(rec.view_uploads()) == 4


def test_skipped_views_are_still_cached_locally(coord_backend):
    t1, t2 = _make_t1(), _make_t2()
    _write(t1, coord_backend)
    _write(t2, coord_backend)
    before = cache.read_cached_view("workstreams/beta")["updated_at"]
    rec = _write(_touch(t1), coord_backend)
    # workstreams/beta was skipped (content unchanged)…
    assert _view_path("workstreams/beta") not in rec.view_uploads()
    # …but the local cache still got the fresh rebuild (the cache-everything
    # contract is untouched: local readers always see the latest build).
    after = cache.read_cached_view("workstreams/beta")["updated_at"]
    assert after > before


# ---------------------------------------------------------------------------
# The poisoned-cache trap: a FAILED upload must not poison the skip decision
# ---------------------------------------------------------------------------

def test_failed_view_is_reattempted_even_when_content_unchanged(coord_backend):
    # Write 1: T1's first write, with workstreams/alpha forced to FAIL. The
    # view is still cached locally (deliberate), but its fingerprint must NOT
    # be written — the remote never confirmed it.
    t1 = _make_t1()
    fail = _view_path("workstreams/alpha")
    _write(t1, coord_backend, fail_paths=[fail])
    assert cache.read_view_fingerprint("workstreams/alpha") is None
    # Sanity: the trap's bait is in place — the cache DOES hold the view.
    assert cache.read_cached_view("workstreams/alpha") is not None

    # Write 2 (T2's creation) leaves workstreams/alpha CONTENT unchanged.
    # If skipping inferred "remote current" from the local cache it would skip
    # here and the view would never land. The success-only fingerprint is
    # absent, so the upload must be re-attempted.
    t2 = _make_t2()
    rec = _write(t2, coord_backend)
    assert fail in rec.view_uploads()
    assert cache.read_view_fingerprint("workstreams/alpha")

    # Once confirmed, the next unrelated write skips it again.
    rec2 = _write(_touch(t2), coord_backend)
    assert fail not in rec2.view_uploads()


# ---------------------------------------------------------------------------
# Reconcile repair: writes fingerprints on its successful uploads
# ---------------------------------------------------------------------------

def test_reconcile_repair_updates_fingerprints_then_write_skips(coord_backend):
    t1, t2 = _make_t1(), _make_t2()
    _write(t1, coord_backend)
    # Write 2 changes the index (task counts) but its index upload FAILS ->
    # NeedsReconcile + needs_reconcile op marker.
    index_path = _view_path("index")
    _write(t2, coord_backend, fail_paths=[index_path])
    markers = [m for m in cache.list_op_markers() if m.get("needs_reconcile")]
    assert markers, "partial write must leave a needs_reconcile marker"

    # Reconcile (no failures now) must re-upload the failed view and record
    # its fingerprint; views already confirmed on the remote are skipped.
    rec = UploadRecorder()
    with mock.patch.object(remote, "upload_json", rec):
        rc = cli.cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)
    assert rc == 0
    assert index_path in rec.view_uploads()
    assert cache.read_view_fingerprint("index")
    # Repair only resolves when its views are genuinely on the remote:
    # the marker is cleared on this fully-green tick.
    assert [m for m in cache.list_op_markers() if m.get("needs_reconcile")] == []

    # A subsequent write that does not change the index skips it — the
    # reconcile-written fingerprint counts as a confirmed upload.
    rec2 = _write(_touch(t2), coord_backend)
    assert index_path not in rec2.view_uploads()
    assert set(rec2.view_uploads()) == {_view_path(n) for n in T2_CHANGED_VIEWS}


def test_reconcile_skips_unchanged_views_but_keeps_summaries_beacon(coord_backend):
    """Back-to-back reconciles with nothing changed: the second tick skips the
    content-identical views BUT must still upload the summaries aggregate —
    its ``generated_at`` is the freshness beacon the stale-view read guard
    (FULCRA_COORD_VIEW_STALE_MIN) checks; skipping it would age the stamp and
    push every reader onto the slow direct-listing fallback on a quiet bus."""
    _write(_make_t1(), coord_backend)
    rc = cli.cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)
    assert rc == 0

    rec = UploadRecorder()
    with mock.patch.object(remote, "upload_json", rec):
        rc = cli.cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)
    assert rc == 0
    task_views = [p for p in rec.view_uploads()
                  if p != remote.presence_view_path()]
    assert task_views == [_view_path("summaries")]


# ---------------------------------------------------------------------------
# Escape hatch: FULCRA_COORD_VIEW_SKIP_UNCHANGED=0 restores upload-everything
# ---------------------------------------------------------------------------

def test_skip_disabled_env_restores_upload_everything(coord_backend):
    t1, t2 = _make_t1(), _make_t2()
    _write(t1, coord_backend)
    _write(t2, coord_backend)
    with mock.patch.dict(os.environ, {"FULCRA_COORD_VIEW_SKIP_UNCHANGED": "0"}):
        rec = _write(_touch(t1), coord_backend)
    # Today's behavior: every one of the 11 views uploads, changed or not.
    assert set(rec.view_uploads()) == {_view_path(n) for n in ALL_VIEWS_BOTH_TASKS}
    assert len(rec.view_uploads()) == 11


# ---------------------------------------------------------------------------
# Serialization-drift guard: fingerprint bytes == upload bytes
# ---------------------------------------------------------------------------

def test_fingerprint_serialization_matches_upload_bytes(coord_backend, tmp_path):
    """The fingerprint must be computed with the EXACT serialization
    upload_json sends — if the two ever drift (e.g. upload grows sort_keys or
    changes indent without the fingerprint following), skipping silently
    breaks: uploads-forever at best, skipping a real change at worst. The fake
    backend copies the uploaded bytes verbatim, so the file on disk IS what
    the wire saw."""
    view = views.build_index([_make_t1()])
    vpath = _view_path("index")
    assert remote.upload_json(view, vpath, backend=coord_backend)
    received = (Path(os.environ["FULCRA_FAKE_ROOT"]) /
                vpath.lstrip("/")).read_text()
    # 1) The shared serializer produces byte-for-byte what the upload sent.
    assert received == remote.serialize_json(view)
    # 2) The fingerprint is that same serialization over the view with ONLY
    #    the top-level per-rebuild stamps excluded (they differ on every
    #    rebuild even when content is identical — hashing them would make
    #    skip a permanent no-op).
    parsed = json.loads(received)
    stripped = {k: v for k, v in parsed.items()
                if k not in writepipe._VIEW_VOLATILE_STAMP_KEYS}
    expected = hashlib.sha256(
        remote.serialize_json(stripped).encode("utf-8")).hexdigest()
    assert writepipe._view_fingerprint(view) == expected


def test_fingerprint_stable_across_rebuilds_and_sensitive_to_content():
    t1 = _make_t1()
    a = views.build_index([t1], updated_at="2026-06-10T00:00:00.000000Z")
    b = views.build_index([t1], updated_at="2026-06-11T11:11:11.111111Z")
    # Different rebuild stamps, identical content -> identical fingerprints.
    assert writepipe._view_fingerprint(a) == writepipe._view_fingerprint(b)
    # Real content change -> different fingerprint.
    t1b = dict(t1, status="active",
               updated_at=now_iso())
    c = views.build_index([t1b], updated_at="2026-06-11T11:11:11.111111Z")
    assert writepipe._view_fingerprint(c) != writepipe._view_fingerprint(a)


# ---------------------------------------------------------------------------
# cache fingerprint helpers
# ---------------------------------------------------------------------------

def test_view_fingerprint_roundtrip_and_missing():
    assert cache.read_view_fingerprint("index") is None
    cache.write_view_fingerprint("index", "ab" * 32)
    assert cache.read_view_fingerprint("index") == "ab" * 32
    cache.write_view_fingerprint("index", "cd" * 32)
    assert cache.read_view_fingerprint("index") == "cd" * 32


def test_view_fingerprint_names_are_path_safe():
    """View names carry slashes and agent ids carry colons — none of that may
    escape the fingerprint dir or collide lossily."""
    n1 = "agents/claude-code:Ashs-MBP:repo"
    n2 = "agents/claude-code-Ashs-MBP-repo"  # sanitized twin must NOT collide
    cache.write_view_fingerprint(n1, "1" * 64)
    cache.write_view_fingerprint(n2, "2" * 64)
    assert cache.read_view_fingerprint(n1) == "1" * 64
    assert cache.read_view_fingerprint(n2) == "2" * 64
    # Everything stays inside the dedicated per-root dir.
    fdir = cache._root_cache() / "view-fingerprints"
    assert fdir.is_dir()
    for p in fdir.rglob("*"):
        assert p.parent == fdir, f"fingerprint escaped its dir: {p}"
