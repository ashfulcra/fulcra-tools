"""Phase 3b Task 1 — directive dual-write tests.

Two halves, mirroring the event dual-write (Phase 1/2a) test layout:

1. PURE mapping tests for ``directives.directive_from_task`` /
   ``directives._directive_status_for`` — no backend, no I/O. These are the
   testable core: a legacy "task with assignee" maps deterministically onto a
   first-class Directive record.

2. INTEGRATION tests for the best-effort dual-write hook bolted onto
   ``cmd_tell`` / ``cmd_broadcast``: a successful directive command ALSO writes
   a ``directives/<id>.json`` mirror, and a directive-write FAILURE never fails
   or alters the authoritative legacy task write.

Written test-first: the pure mapping tests and the dual-write tests FAIL before
``directives.py`` and the hook exist; all PASS once they land.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import schema, remote, routing, views
from fulcra_coord.schema import validate_directive


# ---------------------------------------------------------------------------
# Task-dict helpers
# ---------------------------------------------------------------------------

def _task(**overrides) -> dict:
    """A minimal realistic task (the legacy directive-as-task), overridable."""
    t = schema.make_task(
        title="Do the thing",
        workstream="devops",
        agent="agent-a",
        owner_agent="agent-a",
        assignee="agent-b",
        priority="P1",
        summary="please do it",
        next_action="start now",
    )
    t.update(overrides)
    return t


def _ack(task: dict, by: str) -> dict:
    """Append an inbox_ack event from ``by`` (the 'some agent acked' signal)."""
    return schema.apply_event(task, "inbox_ack", by=by, summary=f"acked by {by}")


# ---------------------------------------------------------------------------
# _directive_status_for — the pure status map
# ---------------------------------------------------------------------------

class TestDirectiveStatusFor:

    def test_proposed_maps_to_proposed(self):
        from fulcra_coord import directives
        assert directives._directive_status_for(_task(status="proposed")) == "proposed"

    def test_acked_when_inbox_ack_present(self):
        from fulcra_coord import directives
        t = _ack(_task(status="proposed"), "agent-b")
        # An ack outranks the bare proposed status -> acked.
        assert directives._directive_status_for(t) == "acked"

    def test_active_maps_to_acted(self):
        from fulcra_coord import directives
        assert directives._directive_status_for(_task(status="active")) == "acted"

    def test_waiting_maps_to_acted(self):
        from fulcra_coord import directives
        assert directives._directive_status_for(_task(status="waiting")) == "acted"

    def test_blocked_maps_to_acted(self):
        from fulcra_coord import directives
        assert directives._directive_status_for(_task(status="blocked")) == "acted"

    def test_done_claimed_maps_to_acted(self):
        from fulcra_coord import directives
        # A done task that WAS claimed (active event present) -> acted (terminal-ack).
        t = _task(status="active")
        t = schema.apply_transition(t, "done", by="agent-b",
                                    evidence="shipped", verification_level="agent-verified")
        assert directives._directive_status_for(t) == "acted"

    def test_done_never_claimed_maps_to_expired(self):
        from fulcra_coord import directives
        # A directive marked done that was NEVER claimed/acked -> expired.
        t = _task(status="done")
        # Strip any claim/pickup signal so it reads as never-claimed.
        t["events"] = [e for e in t.get("events", []) if e.get("type") == "created"]
        assert directives._directive_status_for(t) == "expired"

    def test_abandoned_maps_to_expired(self):
        from fulcra_coord import directives
        assert directives._directive_status_for(_task(status="abandoned")) == "expired"


# ---------------------------------------------------------------------------
# directive_from_task — the mapping into a valid Directive record
# ---------------------------------------------------------------------------

class TestDirectiveFromTask:

    def test_builds_valid_directive(self):
        from fulcra_coord import directives
        d = directives.directive_from_task(_task())
        assert validate_directive(d) == [], validate_directive(d)

    def test_type_tell_for_concrete_assignee(self):
        from fulcra_coord import directives
        d = directives.directive_from_task(_task(assignee="agent-b"))
        assert d["directive_type"] == "tell"

    def test_type_broadcast_for_wildcard_assignee(self):
        from fulcra_coord import directives
        d = directives.directive_from_task(_task(assignee=views.BROADCAST))
        assert d["directive_type"] == "broadcast"

    def test_type_review_for_review_tagged_task(self):
        from fulcra_coord import directives
        t = _task()
        t["tags"] = sorted(set(t.get("tags", []) + [routing.REVIEW_TAG]))
        d = directives.directive_from_task(t)
        assert d["directive_type"] == "review"

    def test_from_audience_and_backref(self):
        from fulcra_coord import directives
        t = _task(owner_agent="agent-a", assignee="agent-b")
        d = directives.directive_from_task(t)
        assert d["from"] == "agent-a"
        assert d["audience"] == "agent-b"
        assert d["task_id"] == t["id"]

    def test_carries_title_summary_next_priority_workstream(self):
        from fulcra_coord import directives
        d = directives.directive_from_task(_task())
        assert d["title"] == "Do the thing"
        assert d["summary"] == "please do it"
        assert d["next_action"] == "start now"
        assert d["priority"] == "P1"
        assert d["workstream"] == "devops"

    def test_acked_by_from_inbox_ack(self):
        from fulcra_coord import directives
        t = _ack(_ack(_task(), "agent-b"), "agent-c")
        d = directives.directive_from_task(t)
        assert set(d["acked_by"]) == {"agent-b", "agent-c"}
        assert validate_directive(d) == []

    def test_status_reflects_task_state(self):
        from fulcra_coord import directives
        assert directives.directive_from_task(_task(status="active"))["status"] == "acted"
        assert directives.directive_from_task(_task(status="proposed"))["status"] == "proposed"

    def test_workstream_fallback_never_raises(self):
        from fulcra_coord import directives
        # A task with an empty/missing workstream must still produce a directive:
        # make_directive requires non-empty workstream, so the mapper falls back.
        t = _task()
        t["workstream"] = ""
        d = directives.directive_from_task(t)
        assert d["workstream"] == "general"
        assert validate_directive(d) == []

    def test_workstream_fully_absent_falls_back(self):
        from fulcra_coord import directives
        t = _task()
        del t["workstream"]
        d = directives.directive_from_task(t)
        assert d["workstream"] == "general"
        assert validate_directive(d) == []


# ---------------------------------------------------------------------------
# Layering: directives.py must not import any up-layer module.
# ---------------------------------------------------------------------------

class TestDirectivesLayering:

    def test_directives_imports_no_up_layer_module(self):
        import ast
        pkg = Path(__file__).resolve().parents[1] / "fulcra_coord"
        src = (pkg / "directives.py").read_text(encoding="utf-8")
        forbidden = {"lifecycle", "cli", "views", "writepipe", "inbox"}
        imported: set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom):
                if (node.level or 0) >= 1:
                    if node.module:
                        imported.add(node.module.split(".")[0])
                    else:
                        for a in node.names:
                            imported.add(a.name.split(".")[0])
                elif (node.module or "").split(".")[0] == "fulcra_coord":
                    parts = node.module.split(".")
                    if len(parts) >= 2:
                        imported.add(parts[1])
                    else:
                        for a in node.names:
                            imported.add(a.name.split(".")[0])
            elif isinstance(node, ast.Import):
                for a in node.names:
                    parts = a.name.split(".")
                    if parts[0] == "fulcra_coord" and len(parts) >= 2:
                        imported.add(parts[1])
        offenders = imported & forbidden
        assert offenders == set(), f"directives.py imports up-layer modules: {offenders}"


# ---------------------------------------------------------------------------
# Integration: best-effort dual-write hook on cmd_tell / cmd_broadcast
# ---------------------------------------------------------------------------

def _tell_args(**overrides) -> SimpleNamespace:
    base = dict(
        title="Do the thing",
        assignee="agent-b",
        workstream="devops",
        priority="P1",
        summary="please do it",
        next="start now",
    )
    base.update(overrides)
    ns = SimpleNamespace(**base)
    setattr(ns, "from", overrides.get("from_agent", "agent-a"))
    return ns


def _read_directive(backend, directive_id):
    return remote.download_json(remote.directive_remote_path(directive_id), backend=backend)


def _list_directive_ids(backend):
    files = remote.list_files(remote.directives_prefix(), backend=backend)
    return [Path(f).stem for f in files]


def test_tell_dual_writes_a_directive(coord_backend, monkeypatch):
    from fulcra_coord import lifecycle
    rc = lifecycle.cmd_tell(_tell_args(), backend=coord_backend)
    assert rc == 0
    ids = _list_directive_ids(coord_backend)
    assert len(ids) == 1, f"expected exactly one directive, got {ids}"
    d = _read_directive(coord_backend, ids[0])
    assert d is not None
    assert d["directive_type"] == "tell"
    assert d["audience"] == "agent-b"
    assert d["from"] == "agent-a"
    assert d["task_id"].startswith("TASK-")
    assert validate_directive(d) == [], validate_directive(d)


def test_broadcast_dual_writes_a_broadcast_directive(coord_backend, monkeypatch):
    from fulcra_coord import lifecycle
    args = _tell_args()
    # cmd_broadcast sets assignee=* itself; don't pre-set it.
    rc = lifecycle.cmd_broadcast(args, backend=coord_backend)
    assert rc == 0
    ids = _list_directive_ids(coord_backend)
    assert len(ids) == 1, f"expected exactly one directive, got {ids}"
    d = _read_directive(coord_backend, ids[0])
    assert d is not None
    assert d["directive_type"] == "broadcast"
    assert d["audience"] == views.BROADCAST
    assert validate_directive(d) == []


def test_directive_write_failure_does_not_fail_tell(coord_backend, monkeypatch):
    """A raising directive upload must NOT fail the tell; the task still lands."""
    import fulcra_coord.lifecycle as lc

    real_upload = remote.upload_json

    def _selective(data, path, *a, **k):
        # Only the directives path raises; the task + views write normally.
        if "/directives/" in path:
            raise RuntimeError("boom")
        return real_upload(data, path, *a, **k)

    monkeypatch.setattr(lc.remote, "upload_json", _selective)

    seen = []
    monkeypatch.setattr(lc.ops_log, "log_op",
                        lambda *a, **k: seen.append((a, k)))

    rc = lc.cmd_tell(_tell_args(), backend=coord_backend)
    assert rc == 0, "tell must succeed even when the directive write raises"
    # No directive landed (it raised), but the legacy task DID.
    assert _list_directive_ids(coord_backend) == []
    # And the failure was logged.
    assert any(k.get("status") == "directive_write_failed" for a, k in seen), seen


def test_directive_write_false_return_logs_failure_without_failing_tell(coord_backend, monkeypatch):
    import fulcra_coord.lifecycle as lc

    real_upload = remote.upload_json

    def _selective(data, path, *a, **k):
        if "/directives/" in path:
            return False
        return real_upload(data, path, *a, **k)

    monkeypatch.setattr(lc.remote, "upload_json", _selective)
    seen = []
    monkeypatch.setattr(lc.ops_log, "log_op", lambda *a, **k: seen.append((a, k)))

    rc = lc.cmd_tell(_tell_args(), backend=coord_backend)
    assert rc == 0
    assert any(k.get("status") == "directive_write_failed" for a, k in seen), seen
