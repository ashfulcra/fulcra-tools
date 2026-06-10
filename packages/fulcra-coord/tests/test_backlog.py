"""The two remaining loop-kind wirings: backlog (`later` → kind=idea) and
dispatch asks (`tell --expects-response` → kind=dispatch).

Operator requirement (2026-06-10): "do later" tasks must live ON THE BUS so the
backlog is portable across sessions/agents — never only in one agent's session
memory. `later` creates a task addressed to the ``@backlog`` ROLE audience
(nobody holds it: zero inbox spam, durable, board-visible, claimable by a
future backlog-groomer role) and dual-writes a ``kind=idea`` captured loop.

Two tiers, mirroring test_directive_dualwrite.py:
  * INTEGRATION (coord_backend): cmd_later end-to-end — the task lands with the
    @backlog audience + kind:idea tag; the dual-written directive is an idea
    loop (captured, expects_response=False); detection folds / board / inbox
    behave per the contract; `assign` to a concrete agent folds state→routed.
  * WIRING: the entry.py parser + dispatch for `later`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import lifecycle, loops, loop_ops, remote, routing, views

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def _later_args(**overrides) -> SimpleNamespace:
    base = dict(
        title="Try the new retention sweep idea",
        summary="captured during the loops session",
        workstream=None,    # cmd_later defaults this to "general"
        priority=None,      # cmd_later defaults this to "P3"
    )
    base.update(overrides)
    ns = SimpleNamespace(**base)
    setattr(ns, "from", overrides.get("from_agent", "agent-a"))
    return ns


def _load_tasks(backend):
    prefix = f"{remote.remote_root()}/tasks/"
    files = remote.list_files(prefix, backend=backend)
    tasks = []
    for f in files:
        t = remote.download_json(f, backend=backend)
        if isinstance(t, dict):
            tasks.append(t)
    return tasks


def _the_task(backend):
    tasks = _load_tasks(backend)
    assert len(tasks) == 1, f"expected exactly one task, got {[t.get('id') for t in tasks]}"
    return tasks[0]


def _read_directive_for(backend, task_id):
    from fulcra_coord import directives
    return remote.download_json(
        remote.directive_remote_path(directives.stable_directive_id(task_id)),
        backend=backend)


# ---------------------------------------------------------------------------
# The captured task
# ---------------------------------------------------------------------------

def test_later_creates_backlog_task_with_idea_tag(coord_backend):
    rc = lifecycle.cmd_later(_later_args(), backend=coord_backend)
    assert rc == 0
    task = _the_task(coord_backend)
    assert task["assignee"] == "@backlog"
    assert routing.IDEA_TAG in task["tags"]
    assert task["status"] == "proposed"
    assert task["owner_agent"] == "agent-a"


def test_later_defaults_workstream_general_and_priority_p3(coord_backend):
    rc = lifecycle.cmd_later(_later_args(), backend=coord_backend)
    assert rc == 0
    task = _the_task(coord_backend)
    assert task["workstream"] == "general"
    assert task["priority"] == "P3"


def test_later_honors_explicit_workstream_and_priority(coord_backend):
    rc = lifecycle.cmd_later(
        _later_args(workstream="devops", priority="P1"), backend=coord_backend)
    assert rc == 0
    task = _the_task(coord_backend)
    assert task["workstream"] == "devops"
    assert task["priority"] == "P1"


# ---------------------------------------------------------------------------
# The dual-written idea loop
# ---------------------------------------------------------------------------

def test_later_dual_writes_a_captured_idea_loop(coord_backend):
    rc = lifecycle.cmd_later(_later_args(), backend=coord_backend)
    assert rc == 0
    task = _the_task(coord_backend)
    d = _read_directive_for(coord_backend, task["id"])
    assert d is not None
    # The wire enum is closed: kind carries the semantics, type stays "tell".
    assert d["directive_type"] == "tell"
    assert d["kind"] == "idea"
    assert d["state"] == "captured"
    assert d["expects_response"] is False
    assert d["audience"] == "@backlog"


def test_idea_loop_is_not_awaiting_anyone(coord_backend):
    # Ideas are a pipeline, not an ask: expects_response=False keeps them out
    # of both sides of the open-loop ledger for every party.
    rc = lifecycle.cmd_later(_later_args(), backend=coord_backend)
    assert rc == 0
    records = loop_ops.load_loop_records(backend=coord_backend)
    assert len(records) == 1
    assert loops.awaiting_others("agent-a", records, now=NOW) == []
    assert loops.awaiting_me("@backlog", records, now=NOW) == []
    assert loops.awaiting_me("agent-a", records, now=NOW) == []


def test_idea_loop_appears_in_board_ideas_pipeline(coord_backend):
    rc = lifecycle.cmd_later(_later_args(), backend=coord_backend)
    assert rc == 0
    records = loop_ops.load_loop_records(backend=coord_backend)
    board = loops.loop_board("agent-a", records, now=NOW)
    assert board["ideas_pipeline"].get("captured") == 1


# ---------------------------------------------------------------------------
# Delivery: @backlog spams nobody; a declared backlog role holder sees it
# ---------------------------------------------------------------------------

def test_backlog_item_surfaces_in_no_roleless_inbox(coord_backend):
    rc = lifecycle.cmd_later(_later_args(), backend=coord_backend)
    assert rc == 0
    tasks = _load_tasks(coord_backend)
    # No agent (not even the sender) sees it without holding the backlog role.
    for me in ("agent-a", "agent-b", "claude-code:host:repo"):
        assert views.inbox_for(me, tasks, now=NOW) == []
        assert views.inbox_for(me, tasks, now=NOW, roles={"review"}) == []


def test_backlog_item_surfaces_for_backlog_role_holder(coord_backend):
    rc = lifecycle.cmd_later(_later_args(), backend=coord_backend)
    assert rc == 0
    tasks = _load_tasks(coord_backend)
    got = views.inbox_for("groomer:h:r", tasks, now=NOW, roles={"backlog"})
    assert [s["id"] for s in got] == [tasks[0]["id"]]


# ---------------------------------------------------------------------------
# Routing a backlog item later = the existing `assign`
# ---------------------------------------------------------------------------

def test_assign_idea_to_concrete_agent_maps_state_routed(coord_backend):
    rc = lifecycle.cmd_later(_later_args(), backend=coord_backend)
    assert rc == 0
    task = _the_task(coord_backend)
    rc = lifecycle.cmd_assign(
        SimpleNamespace(task_id=task["id"], assignee="agent-b", agent="agent-a"),
        backend=coord_backend)
    assert rc == 0
    d = _read_directive_for(coord_backend, task["id"])
    assert d["kind"] == "idea"
    assert d["audience"] == "agent-b"
    assert d["state"] == "routed"
    assert d["expects_response"] is False


# ---------------------------------------------------------------------------
# Wiring: entry.py parser + dispatch
# ---------------------------------------------------------------------------

def test_later_parser_and_dispatch():
    from fulcra_coord import cli, entry
    assert entry.COMMAND_MAP["later"] is cli.cmd_later
    parser = entry.build_parser()
    args = parser.parse_args(["later", "ship the groomer", "-s", "ctx"])
    assert args.command == "later"
    assert args.title == "ship the groomer"
    assert args.summary == "ctx"
    assert args.workstream == "general"
    assert args.priority == "P3"
    assert getattr(args, "from") is None


# ---------------------------------------------------------------------------
# Dispatch asks: tell --expects-response → an OPEN kind=dispatch loop
# ---------------------------------------------------------------------------

def _tell_args(**overrides) -> SimpleNamespace:
    base = dict(
        title="Port the digest emitter",
        assignee="agent-b",
        workstream="devops",
        priority="P2",
        summary="please port it",
        expects_response=False,
    )
    base.update(overrides)
    ns = SimpleNamespace(**base)
    setattr(ns, "from", overrides.get("from_agent", "agent-a"))
    return ns


def test_tell_expects_response_dual_writes_open_dispatch_loop(coord_backend):
    rc = lifecycle.cmd_tell(_tell_args(expects_response=True),
                            backend=coord_backend)
    assert rc == 0
    task = _the_task(coord_backend)
    assert routing.DISPATCH_TAG in task["tags"]
    d = _read_directive_for(coord_backend, task["id"])
    assert d["directive_type"] == "tell"   # wire enum closed; kind carries it
    assert d["kind"] == "dispatch"
    assert d["state"] == "assigned"
    assert d["expects_response"] is True
    # SLA comes from the registry default, not a local hardcode.
    assert d["sla_hours"] == loops.KINDS["dispatch"]["sla_hours"]


def test_plain_tell_stays_a_legacy_tell(coord_backend):
    rc = lifecycle.cmd_tell(_tell_args(), backend=coord_backend)
    assert rc == 0
    task = _the_task(coord_backend)
    assert routing.DISPATCH_TAG not in task["tags"]
    d = _read_directive_for(coord_backend, task["id"])
    assert d["kind"] is None
    assert d["expects_response"] is False


def test_tell_parser_gains_expects_response_but_broadcast_does_not():
    from fulcra_coord import entry
    parser = entry.build_parser()
    args = parser.parse_args(["tell", "agent-b", "do x", "--expects-response"])
    assert args.expects_response is True
    args = parser.parse_args(["tell", "agent-b", "do x"])
    assert args.expects_response is False
    # broadcast is fan-out FYI — it must NOT grow an open-loop flag.
    args = parser.parse_args(["broadcast", "heads up"])
    assert not getattr(args, "expects_response", False)


def test_dispatch_loop_e2e_open_then_respond_closes(coord_backend):
    # The full dispatch round trip: tell --expects-response opens the loop in
    # the sender's awaiting_others; the recipient's `respond` (the existing
    # generic return leg) folds it closed and clears the ledger.
    rc = lifecycle.cmd_tell(_tell_args(expects_response=True),
                            backend=coord_backend)
    assert rc == 0
    records = loop_ops.load_loop_records(backend=coord_backend)
    open_asks = loops.awaiting_others("agent-a", records, now=NOW)
    assert len(open_asks) == 1
    assert open_asks[0]["kind"] == "dispatch"
    loop_id = open_asks[0]["id"]

    rc = loop_ops.cmd_respond(
        SimpleNamespace(loop_id=loop_id, outcome="delivered",
                        evidence="branch pushed + tests green",
                        agent="agent-b", format="table"),
        backend=coord_backend)
    assert rc == 0

    records = loop_ops.load_loop_records(backend=coord_backend)
    assert loops.awaiting_others("agent-a", records, now=NOW) == []
    d = _read_directive_for(coord_backend, _the_task(coord_backend)["id"])
    assert d["state"] == "closed"
    assert d["outcome"]["verdict"] == "delivered"
