"""Phase 3b Task 2 — directive ack + routing persistence via an append-only SUB-LOG.

THE CONCURRENCY CRUX (read before touching this file): the coordination bus is a
brokerless object store with NO compare-and-swap and many concurrent writers. A
naive read-modify-write of the single ``directives/<id>.json`` record to add an
ack would CLOBBER concurrent acks — two agents acking the same broadcast at once
would each read the old record, add only their own ack, and the slower writer's
upload would overwrite (and lose) the faster one's ack.

The fix (storage model A, sub-log): acks and routing go to an APPEND-ONLY
sub-log where each writer writes a DISTINCT file, so there is no shared mutable
file to clobber:

  * Ack sub-log:    ``directives/<id>/acks/<agent-slug>.json`` — one file PER
    ACKING AGENT. An agent re-acking overwrites only its OWN file (idempotent);
    two different agents NEVER collide; the ack union = list-the-prefix.
  * Routing sub-log: ``directives/<id>/routing/<event_id>.json`` — append-only
    route-event shards keyed by a unique event id (like the event log).

The LOAD-BEARING test here is ``test_concurrent_acks_never_clobber``: two
different agents ack, and BOTH survive. That property is impossible under a
single-record RMW and trivial under the per-agent-file sub-log — this is what
the whole design buys.

Mirrors the coord_backend integration style of test_directive_dualwrite.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import schema, remote, routing
from fulcra_coord.schema import validate_directive


# ---------------------------------------------------------------------------
# remote.py sub-log path helpers
# ---------------------------------------------------------------------------

class TestSubLogPathHelpers:

    def test_acks_prefix_is_under_directive(self):
        p = remote.directive_acks_prefix("DIR-T-TASK-1")
        assert p == f"{remote.directives_prefix()}DIR-T-TASK-1/acks/"

    def test_ack_path_slugifies_agent(self):
        # An agent id with colons/slashes must become a filename-safe basename
        # under the acks prefix — the colons in claude-code:Mac:x can't reach a path.
        p = remote.directive_ack_path("DIR-T-TASK-1", "claude-code:Mac:x")
        assert p.startswith(remote.directive_acks_prefix("DIR-T-TASK-1"))
        assert p.endswith(".json")
        base = p[len(remote.directive_acks_prefix("DIR-T-TASK-1")):]
        assert ":" not in base and "/" not in base

    def test_distinct_agents_get_distinct_ack_paths(self):
        a = remote.directive_ack_path("DIR-T-T", "agent-a")
        b = remote.directive_ack_path("DIR-T-T", "agent-b")
        assert a != b

    def test_routing_prefix_is_under_directive(self):
        p = remote.directive_routing_prefix("DIR-T-TASK-1")
        assert p == f"{remote.directives_prefix()}DIR-T-TASK-1/routing/"

    def test_route_path_keyed_by_event_id(self):
        p = remote.directive_route_path("DIR-T-T", "ev123")
        assert p == f"{remote.directive_routing_prefix('DIR-T-T')}ev123.json"


# ---------------------------------------------------------------------------
# directives.py sub-log API — ack write/read + routing append/read
# ---------------------------------------------------------------------------

def _ack_agents_on_disk(backend, directive_id):
    """The agent slugs that have an ack file under the acks prefix."""
    files = remote.list_files(remote.directive_acks_prefix(directive_id), backend=backend)
    return [Path(f).stem for f in files]


def _route_shards_on_disk(backend, directive_id):
    files = remote.list_files(remote.directive_routing_prefix(directive_id), backend=backend)
    return [Path(f).stem for f in files]


class TestAckSubLog:

    def test_write_then_read_ack(self, coord_backend):
        from fulcra_coord import directives
        assert directives.write_directive_ack("DIR-1", "agent-a", backend=coord_backend) is True
        assert directives.read_directive_acks("DIR-1", backend=coord_backend) == ["agent-a"]

    def test_read_empty_when_no_acks(self, coord_backend):
        from fulcra_coord import directives
        assert directives.read_directive_acks("DIR-NONE", backend=coord_backend) == []

    def test_concurrent_acks_never_clobber(self, coord_backend):
        """THE LOAD-BEARING TEST. Two DIFFERENT agents ack the same directive; the
        union returns BOTH — neither clobbered the other. Under a single-record
        read-modify-write this would lose one ack; per-agent files make it safe
        BY CONSTRUCTION."""
        from fulcra_coord import directives
        directive_id = "DIR-BCAST"
        assert directives.write_directive_ack(directive_id, "agent-a", backend=coord_backend) is True
        assert directives.write_directive_ack(directive_id, "agent-b", backend=coord_backend) is True
        union = directives.read_directive_acks(directive_id, backend=coord_backend)
        assert union == ["agent-a", "agent-b"], (
            f"both concurrent acks must survive (sorted union); got {union}")
        # And they are two DISTINCT files on disk — no shared mutable record.
        assert len(_ack_agents_on_disk(coord_backend, directive_id)) == 2

    def test_idempotent_reack_same_agent(self, coord_backend):
        """An agent acking twice overwrites only its OWN file: still one file,
        union unchanged."""
        from fulcra_coord import directives
        directive_id = "DIR-IDEM"
        directives.write_directive_ack(directive_id, "agent-a", backend=coord_backend)
        directives.write_directive_ack(directive_id, "agent-a", backend=coord_backend)
        assert directives.read_directive_acks(directive_id, backend=coord_backend) == ["agent-a"]
        assert len(_ack_agents_on_disk(coord_backend, directive_id)) == 1

    def test_ack_path_lands_at_expected_location(self, coord_backend):
        from fulcra_coord import directives
        directives.write_directive_ack("DIR-LOC", "claude-code:Mac:x", backend=coord_backend)
        expected = remote.directive_ack_path("DIR-LOC", "claude-code:Mac:x")
        assert remote.download_json(expected, backend=coord_backend) is not None

    def test_ack_write_failure_is_best_effort(self, coord_backend, monkeypatch):
        from fulcra_coord import directives
        monkeypatch.setattr(directives.remote, "upload_json",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        # Must NOT raise; returns False.
        assert directives.write_directive_ack("DIR-FAIL", "agent-a", backend=coord_backend) is False

    def test_read_acks_failure_is_best_effort(self, coord_backend, monkeypatch):
        from fulcra_coord import directives
        monkeypatch.setattr(directives.remote, "list_files",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert directives.read_directive_acks("DIR-X", backend=coord_backend) == []


class TestRoutingSubLog:

    def test_append_then_read_route(self, coord_backend):
        from fulcra_coord import directives
        ev = routing.make_route_event(
            kind="routed", to="agent-b", by="agent-a", attempt=1,
            reason="r", candidate_snapshot=[], observed_updated_at="", at="2026-06-09T00:00:00Z")
        assert directives.append_directive_route("DIR-R", ev, backend=coord_backend) is True
        got = directives.read_directive_routing("DIR-R", backend=coord_backend)
        assert len(got) == 1
        assert got[0]["to"] == "agent-b"

    def test_routing_shards_are_append_only(self, coord_backend):
        """Two route events land as two DISTINCT shards (append-only), neither
        overwriting the other."""
        from fulcra_coord import directives
        e1 = routing.make_route_event(
            kind="routed", to="agent-b", by="agent-a", attempt=1, reason="r",
            candidate_snapshot=[], observed_updated_at="", at="2026-06-09T00:00:00Z")
        e2 = routing.make_route_event(
            kind="rerouted", to="agent-c", by="agent-a", attempt=2, reason="r",
            candidate_snapshot=[], observed_updated_at="", at="2026-06-09T00:01:00Z")
        directives.append_directive_route("DIR-R2", e1, backend=coord_backend)
        directives.append_directive_route("DIR-R2", e2, backend=coord_backend)
        got = directives.read_directive_routing("DIR-R2", backend=coord_backend)
        assert len(got) == 2, f"both route events must persist as shards: {got}"
        assert len(_route_shards_on_disk(coord_backend, "DIR-R2")) == 2

    def test_read_routing_sorted(self, coord_backend):
        from fulcra_coord import directives
        e_late = routing.make_route_event(
            kind="rerouted", to="agent-c", by="agent-a", attempt=2, reason="r",
            candidate_snapshot=[], observed_updated_at="", at="2026-06-09T00:05:00Z")
        e_early = routing.make_route_event(
            kind="routed", to="agent-b", by="agent-a", attempt=1, reason="r",
            candidate_snapshot=[], observed_updated_at="", at="2026-06-09T00:00:00Z")
        directives.append_directive_route("DIR-R3", e_late, backend=coord_backend)
        directives.append_directive_route("DIR-R3", e_early, backend=coord_backend)
        got = directives.read_directive_routing("DIR-R3", backend=coord_backend)
        assert [e["at"] for e in got] == ["2026-06-09T00:00:00Z", "2026-06-09T00:05:00Z"]

    def test_append_route_failure_is_best_effort(self, coord_backend, monkeypatch):
        from fulcra_coord import directives
        ev = {"event_id": "x", "at": "2026-06-09T00:00:00Z"}
        monkeypatch.setattr(directives.remote, "upload_json",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert directives.append_directive_route("DIR-RF", ev, backend=coord_backend) is False

    def test_read_routing_failure_is_best_effort(self, coord_backend, monkeypatch):
        from fulcra_coord import directives
        monkeypatch.setattr(directives.remote, "list_files",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert directives.read_directive_routing("DIR-X", backend=coord_backend) == []


# ---------------------------------------------------------------------------
# Snapshot reflects the durable union — dual_write folds the sub-log in.
# ---------------------------------------------------------------------------

def _task(**overrides) -> dict:
    t = schema.make_task(
        title="Do the thing", workstream="devops", agent="agent-a",
        owner_agent="agent-a", assignee="agent-b", priority="P1",
        summary="please do it", next_action="start now")
    t.update(overrides)
    return t


def _read_directive(backend, directive_id):
    return remote.download_json(remote.directive_remote_path(directive_id), backend=backend)


class TestSnapshotUnion:

    def test_dualwrite_snapshot_includes_sublog_acks(self, coord_backend):
        """After two concurrent acks land in the sub-log, a dual_write re-snapshot
        of the directive has acked_by ⊇ {agent-a, agent-b} — the durable union,
        not just the task body's inline acks."""
        from fulcra_coord import directives
        t = _task()
        directive_id = directives._stable_directive_id(t["id"])
        directives.write_directive_ack(directive_id, "agent-a", backend=coord_backend)
        directives.write_directive_ack(directive_id, "agent-b", backend=coord_backend)
        directives.dual_write(t, command="tell", backend=coord_backend)
        d = _read_directive(coord_backend, directive_id)
        assert d is not None
        assert {"agent-a", "agent-b"}.issubset(set(d["acked_by"])), d["acked_by"]
        assert validate_directive(d) == [], validate_directive(d)

    def test_snapshot_acked_by_never_shrinks_below_sublog(self, coord_backend):
        """The sub-log is the durable truth. A task whose inline events LOST an
        early ack (capped event log) must still re-snapshot with that ack, because
        the sub-log union wins. agentX is in the sub-log but NOT in the task body."""
        from fulcra_coord import directives
        t = _task()
        directive_id = directives._stable_directive_id(t["id"])
        # Sub-log records agentX; the task body has NO inbox_ack for agentX.
        directives.write_directive_ack(directive_id, "agentX", backend=coord_backend)
        assert "agentX" not in directives._acked_by_from_task(t)
        directives.dual_write(t, command="tell", backend=coord_backend)
        d = _read_directive(coord_backend, directive_id)
        assert "agentX" in d["acked_by"], (
            f"sub-log union must never shrink the ack set; got {d['acked_by']}")

    def test_snapshot_includes_routing_sublog(self, coord_backend):
        """dual_write folds the routing sub-log into the snapshot's routing field."""
        from fulcra_coord import directives
        t = _task()
        directive_id = directives._stable_directive_id(t["id"])
        ev = routing.make_route_event(
            kind="routed", to="agent-b", by="agent-a", attempt=1, reason="r",
            candidate_snapshot=[], observed_updated_at="", at="2026-06-09T00:00:00Z")
        directives.append_directive_route(directive_id, ev, backend=coord_backend)
        directives.dual_write(t, command="tell", backend=coord_backend)
        d = _read_directive(coord_backend, directive_id)
        assert len(d.get("routing") or []) == 1
        assert d["routing"][0]["to"] == "agent-b"

    def test_sublog_read_failure_leaves_task_acks_as_is(self, coord_backend, monkeypatch):
        """A sub-log read failure during dual_write must leave the task-derived
        acked_by intact (never worse than today) — best-effort, not fatal."""
        from fulcra_coord import directives
        t = _task()
        t = schema.apply_event(t, "inbox_ack", by="agent-b", summary="acked")
        directive_id = directives._stable_directive_id(t["id"])

        def _boom(*a, **k):
            raise RuntimeError("sublog read boom")
        monkeypatch.setattr(directives, "read_directive_acks", _boom)
        # Must not raise; the directive still lands with the task-derived ack.
        directives.dual_write(t, command="tell", backend=coord_backend)
        d = _read_directive(coord_backend, directive_id)
        assert d is not None
        assert "agent-b" in d["acked_by"]


# ---------------------------------------------------------------------------
# Hook: inbox --ack writes a durable per-agent directive ack file.
# ---------------------------------------------------------------------------

def _start_args(**overrides) -> SimpleNamespace:
    base = dict(title="A task to ack", workstream="devops", agent="agent-a",
                kind="ops", priority="P2", summary="s", next="n", surface=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def _started_task_id(backend):
    files = remote.list_files(f"{remote.remote_root()}/tasks/", backend=backend)
    ids = [Path(f).stem for f in files]
    assert len(ids) == 1, f"expected exactly one started task, got {ids}"
    return ids[0]


def _inbox_ack_args(ack_id, agent) -> SimpleNamespace:
    return SimpleNamespace(agent=agent, ack=ack_id, format="table", all=False)


class TestInboxAckHook:

    def test_inbox_ack_writes_durable_directive_ack(self, coord_backend):
        from fulcra_coord import lifecycle, inbox, directives
        # Land a task addressed to agent-b.
        assert lifecycle.cmd_start(_start_args(), backend=coord_backend) == 0
        task_id = _started_task_id(coord_backend)
        # agent-b acks it via the inbox command.
        rc = inbox.cmd_inbox(_inbox_ack_args(task_id, "agent-b"), backend=coord_backend)
        assert rc == 0
        # The durable per-agent ack file must exist at the deterministic path.
        directive_id = directives._stable_directive_id(task_id)
        ack_path = remote.directive_ack_path(directive_id, "agent-b")
        assert remote.download_json(ack_path, backend=coord_backend) is not None, (
            "inbox --ack must write the durable per-agent directive ack file")
        assert "agent-b" in directives.read_directive_acks(directive_id, backend=coord_backend)

    def test_inbox_ack_survives_sublog_write_failure(self, coord_backend, monkeypatch):
        """A directive-ack sub-log write failure must NOT fail the inbox-ack."""
        from fulcra_coord import lifecycle, inbox, directives
        assert lifecycle.cmd_start(_start_args(), backend=coord_backend) == 0
        task_id = _started_task_id(coord_backend)

        def _boom(*a, **k):
            raise RuntimeError("ack sublog boom")
        monkeypatch.setattr(directives, "write_directive_ack", _boom)
        rc = inbox.cmd_inbox(_inbox_ack_args(task_id, "agent-b"), backend=coord_backend)
        assert rc == 0, "inbox --ack must succeed even when the directive ack sub-log write raises"


# ---------------------------------------------------------------------------
# Hook: a route event mirrors to the routing sub-log.
# ---------------------------------------------------------------------------

def _rr_args(**overrides) -> SimpleNamespace:
    base = dict(pr="42", repo="fulcra-tools", dry_run=False,
                candidate_list=None, format="json", agent=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def _now_ls() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")


def _patch_presence(monkeypatch, routing_ops):
    real_dl = remote.download_json
    agg = {"agents": [{"agent": "codex:m:main", "last_seen": _now_ls(),
                       "capabilities": ["review"]}]}
    presence_path = remote.presence_view_path()

    def _dl(path, *a, **k):
        if path == presence_path:
            return agg
        return real_dl(path, *a, **k)
    monkeypatch.setattr(routing_ops.remote, "download_json", _dl)


class TestRouteEventHook:

    def test_request_review_mirrors_route_to_routing_sublog(self, coord_backend, monkeypatch):
        from fulcra_coord import routing_ops, directives
        _patch_presence(monkeypatch, routing_ops)
        monkeypatch.setattr(routing_ops.identity, "resolve_agent",
                            lambda *a, **k: "claude-code:author:r")
        rc = routing_ops.cmd_request_review(_rr_args(), backend=coord_backend)
        assert rc == 0
        # The routed review task's directive id is the deterministic mirror of the
        # one started task. (We resolve it from the task id rather than listing the
        # directives prefix, which now also surfaces the routing/ack sub-log shards.)
        task_id = _started_task_id(coord_backend)
        directive_id = directives.stable_directive_id(task_id)
        shards = remote.list_files(remote.directive_routing_prefix(directive_id),
                                   backend=coord_backend)
        assert len(shards) >= 1, "request-review must mirror its route event to the routing sub-log"
        routes = directives.read_directive_routing(directive_id, backend=coord_backend)
        assert any(r.get("type") in ("routed", "rerouted") for r in routes), routes

    def test_request_review_survives_routing_sublog_failure(self, coord_backend, monkeypatch):
        """A routing sub-log mirror failure must NOT fail request-review."""
        from fulcra_coord import routing_ops, directives
        _patch_presence(monkeypatch, routing_ops)
        monkeypatch.setattr(routing_ops.identity, "resolve_agent",
                            lambda *a, **k: "claude-code:author:r")

        def _boom(*a, **k):
            raise RuntimeError("route sublog boom")
        monkeypatch.setattr(directives, "append_directive_route", _boom)
        rc = routing_ops.cmd_request_review(_rr_args(), backend=coord_backend)
        assert rc == 0, "request-review must survive a routing sub-log mirror failure"
