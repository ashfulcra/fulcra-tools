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
        """A sub-log LISTING failure during a FIRST mirror must leave the
        task-derived acked_by intact and still land the snapshot (previous
        snapshot confirmed absent => nothing to regress) — best-effort, never
        fatal. (F9 repointed this test at the real failure surface: dual_write
        no longer rides the swallow-to-[] public readers, so patching
        read_directive_acks would simulate nothing.)"""
        from fulcra_coord import directives
        t = _task()
        t = schema.apply_event(t, "inbox_ack", by="agent-b", summary="acked")
        directive_id = directives._stable_directive_id(t["id"])

        real_list = remote.list_files

        def _flaky(prefix, *a, **k):
            if f"/{directive_id}/" in prefix:
                raise RuntimeError("sublog listing boom")
            return real_list(prefix, *a, **k)
        monkeypatch.setattr(directives.remote, "list_files", _flaky)
        # Must not raise; the directive still lands with the task-derived ack.
        directives.dual_write(t, command="tell", backend=coord_backend)
        d = _read_directive(coord_backend, directive_id)
        assert d is not None
        assert "agent-b" in d["acked_by"]


# ---------------------------------------------------------------------------
# F9 (2026-06-11 wave): the LWW snapshot refresh must never regress below the
# durable sub-log union when the sub-log READS fail. The three best-effort
# readers used to return [] on a listing blip, so a re-mirror during the blip
# uploaded a snapshot whose acked_by SHRANK and whose state re-OPENED a closed
# loop (the board/digest then flapped until the next good fold).
# ---------------------------------------------------------------------------

def _blip_sublogs(monkeypatch, directive_id):
    """Make every sub-log prefix LISTING under directives/<id>/ raise — the
    scripted transient transport blip. Top-level paths stay readable."""
    from fulcra_coord import directives
    real_list = remote.list_files

    def _flaky(prefix, *a, **k):
        if f"/{directive_id}/" in prefix:
            raise RuntimeError("transient listing 504")
        return real_list(prefix, *a, **k)
    monkeypatch.setattr(directives.remote, "list_files", _flaky)


class TestSnapshotRefreshNeverRegressesOnReadFailure:

    def test_remirror_listing_blip_does_not_shrink_acks(self, coord_backend, monkeypatch):
        from fulcra_coord import directives
        t = _task()
        did = directives._stable_directive_id(t["id"])
        directives.write_directive_ack(did, "agent-a", backend=coord_backend)
        directives.write_directive_ack(did, "agent-b", backend=coord_backend)
        # Good mirror: snapshot carries the durable union.
        directives.dual_write(t, command="tell", backend=coord_backend)
        d = _read_directive(coord_backend, did)
        assert {"agent-a", "agent-b"}.issubset(set(d["acked_by"]))
        # Re-mirror DURING a sub-log listing blip: acked_by must not shrink.
        _blip_sublogs(monkeypatch, did)
        directives.dual_write(t, command="tell", backend=coord_backend)
        d2 = _read_directive(coord_backend, did)
        assert d2 is not None
        assert {"agent-a", "agent-b"}.issubset(set(d2["acked_by"])), (
            f"a listing blip shrank the snapshot's acks: {d2['acked_by']}")

    def test_remirror_listing_blip_does_not_reopen_closed_loop(self, coord_backend, monkeypatch):
        from fulcra_coord import directives, loop_ops, loops
        t = _task()
        t["tags"] = sorted(set(t.get("tags", []) + [routing.REVIEW_TAG]))
        did = directives._stable_directive_id(t["id"])
        directives.dual_write(t, command="request-review", backend=coord_backend)
        # A verdict lands on the response sub-log; the next good mirror closes
        # the snapshot.
        loop_ops.append_loop_response(
            did, {"by": "codex:rev:main", "outcome": {"verdict": "approve"}},
            backend=coord_backend)
        directives.dual_write(t, command="request-review", backend=coord_backend)
        d = _read_directive(coord_backend, did)
        assert not loops.is_open_loop(d)
        assert (d.get("outcome") or {}).get("verdict") == "approve"
        # Re-mirror during a responses-listing blip: the closed loop must NOT
        # re-open and the verdict must NOT be nulled.
        _blip_sublogs(monkeypatch, did)
        directives.dual_write(t, command="request-review", backend=coord_backend)
        d2 = _read_directive(coord_backend, did)
        assert d2 is not None
        assert not loops.is_open_loop(d2), (
            f"a responses-listing blip re-opened a closed loop: {d2.get('state')}")
        assert (d2.get("outcome") or {}).get("verdict") == "approve"

    def test_partial_routing_read_does_not_shrink_snapshot(self, coord_backend, monkeypatch):
        from fulcra_coord import directives
        t = _task()
        did = directives._stable_directive_id(t["id"])
        e1 = routing.make_route_event(
            kind="routed", to="agent-b", by="agent-a", attempt=1, reason="r",
            candidate_snapshot=[], observed_updated_at="", at="2026-06-09T00:00:00Z",
            route_id="route-a")
        e2 = routing.make_route_event(
            kind="rerouted", to="agent-c", by="agent-a", attempt=2, reason="r",
            candidate_snapshot=[], observed_updated_at="", at="2026-06-09T00:01:00Z",
            route_id="route-b")
        directives.append_directive_route(did, e1, backend=coord_backend)
        directives.append_directive_route(did, e2, backend=coord_backend)
        directives.dual_write(t, command="tell", backend=coord_backend)
        before = _read_directive(coord_backend, did)
        assert [r["to"] for r in before["routing"]] == ["agent-b", "agent-c"]

        real_dl = remote.download_json
        missing = remote.directive_route_path(did, "route-b")

        def _partial_dl(path, *a, **k):
            if path == missing:
                return None
            return real_dl(path, *a, **k)

        monkeypatch.setattr(directives.remote, "download_json", _partial_dl)
        directives.dual_write(t, command="tell", backend=coord_backend)
        after = real_dl(remote.directive_remote_path(did), backend=coord_backend)
        assert [r["to"] for r in after["routing"]] == ["agent-b", "agent-c"]

    def test_blip_with_unreadable_prev_snapshot_skips_refresh(self, coord_backend, monkeypatch):
        """When the sub-logs are unreadable AND the previous snapshot can't be
        read either (so there is nothing to merge-preserve from and absence is
        NOT confirmable — the file demonstrably exists), the refresh must be
        SKIPPED entirely: shards stay the truth, the old snapshot stays in
        place, and the miss is ops-logged for the parity audit."""
        from fulcra_coord import directives
        t = _task()
        did = directives._stable_directive_id(t["id"])
        directives.write_directive_ack(did, "agent-a", backend=coord_backend)
        directives.dual_write(t, command="tell", backend=coord_backend)
        before = _read_directive(coord_backend, did)
        assert "agent-a" in before["acked_by"]

        _blip_sublogs(monkeypatch, did)
        # The previous snapshot download fails too (same blip)...
        real_dl = remote.download_json
        snap_path = remote.directive_remote_path(did)

        def _flaky_dl(path, *a, **k):
            if path == snap_path:
                return None   # transport failure collapsed to None
            return real_dl(path, *a, **k)
        monkeypatch.setattr(directives.remote, "download_json", _flaky_dl)
        # ...but the file demonstrably EXISTS (stat sees it), so absence is not
        # confirmable and an upload would be a blind regression.
        seen = []
        from fulcra_coord import log as ops_log
        monkeypatch.setattr(ops_log, "log_op", lambda *a, **k: seen.append((a, k)))
        directives.dual_write(t, command="tell", backend=coord_backend)
        # Read back through the REAL transport (the monkeypatched download is
        # the scripted blip; the bus itself is fine).
        after = real_dl(snap_path, backend=coord_backend)
        assert after == before, "the snapshot must be left untouched (skip, not regress)"
        assert any(k.get("status") == "directive_snapshot_skipped"
                   for a, k in seen), seen

    def test_first_mirror_during_blip_still_writes_snapshot(self, coord_backend, monkeypatch):
        """A BRAND-NEW directive whose sub-log listing blips: the previous
        snapshot is CONFIRMED absent (stat misses, bus reachable), so there is
        nothing to regress — the task-derived snapshot must still land (a tell
        must never be invisible to the board because of one flaky listing)."""
        from fulcra_coord import directives
        t = _task()
        did = directives._stable_directive_id(t["id"])
        _blip_sublogs(monkeypatch, did)
        directives.dual_write(t, command="tell", backend=coord_backend)
        d = _read_directive(coord_backend, did)
        assert d is not None
        assert d["audience"] == "agent-b"


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

    def test_request_review_does_not_mirror_route_when_task_upload_fails(
        self, coord_backend, monkeypatch
    ):
        """A failed authoritative task upload must not leave a phantom route shard."""
        from fulcra_coord import routing_ops, directives
        _patch_presence(monkeypatch, routing_ops)
        monkeypatch.setattr(routing_ops.identity, "resolve_agent",
                            lambda *a, **k: "claude-code:author:r")

        real_upload = remote.upload_json
        task_ids = []

        def _task_upload_fails(data, path, *a, **k):
            if path.startswith(f"{remote.remote_root()}/tasks/"):
                task_ids.append(Path(path).stem)
                return False
            return real_upload(data, path, *a, **k)

        monkeypatch.setattr(routing_ops.remote, "upload_json", _task_upload_fails)

        rc = routing_ops.cmd_request_review(_rr_args(), backend=coord_backend)

        assert rc == 1
        assert task_ids, "request-review should have attempted to write a task body"
        directive_id = directives.stable_directive_id(task_ids[0])
        assert remote.list_files(
            remote.directive_routing_prefix(directive_id), backend=coord_backend
        ) == []
