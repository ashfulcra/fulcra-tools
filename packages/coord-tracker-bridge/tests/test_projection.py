from datetime import datetime, timezone

from coord_tracker_bridge import (
    BridgeLedger,
    CapabilityState,
    ChangeKind,
    LedgerEntry,
    ManagedRecord,
    Snapshot,
    SourceIdentity,
    WorkRecord,
    build_plan,
    load_policy,
)


NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
POLICY = load_policy()


def source(item_id: str) -> SourceIdentity:
    return SourceIdentity("coord-engine", "fulcra", item_id)


def item(item_id: str, *, title: str = "Task", capability: str = "tasks", archived: bool = False):
    return WorkRecord(
        source(item_id), capability, title, "active", origin="fleet",
        tags=("lane:active", "kind:task"), archived=archived,
    )


def ledger_entry(item_id: str, capability: str = "tasks") -> LedgerEntry:
    return LedgerEntry(
        source(item_id), capability, "linear", f"LIN-{item_id}", POLICY.version, POLICY.hash
    )


def snapshot(items, capabilities, *, complete=True):
    return Snapshot(tuple(items), complete, (), capabilities, NOW)


def managed(item_id: str, *, title="Task", capability="tasks", closed=False, fields=None):
    default = {
        "title": title,
        "description": "",
        "semantic_state": "started",
        "priority": 3,
        "labels": ("lane:active", "kind:task"),
        "project": None,
        "due_at": None,
        "owner": None,
        "assignee": None,
        "origin": "fleet",
        "workstream": None,
        "source_identity": source(item_id).to_dict(),
        "source_capability": capability,
        "source_lane": "active",
        "policy_version": POLICY.version,
        "policy_hash": POLICY.hash,
    }
    default.update(fields or {})
    return ManagedRecord(f"LIN-{item_id}", source(item_id), capability, default, closed)


def test_rename_updates_by_identity_not_title():
    plan = build_plan(
        snapshot([item("task-1", title="Renamed")], {"tasks": CapabilityState.COMPLETE}),
        [managed("task-1", title="Old")],
        BridgeLedger([ledger_entry("task-1")]),
        POLICY,
    )

    assert [(c.kind, c.provider_id, c.fields) for c in plan.changes] == [
        (ChangeKind.UPDATE, "LIN-task-1", {"title": "Renamed"})
    ]


def test_degradation_suppresses_close_only_for_affected_capability():
    ledger = BridgeLedger([ledger_entry("task-missing"), ledger_entry("ask-missing", "asks")])
    records = [managed("task-missing"), managed("ask-missing", capability="asks")]
    plan = build_plan(
        snapshot([], {"tasks": CapabilityState.COMPLETE, "asks": CapabilityState.DEGRADED}),
        records,
        ledger,
        POLICY,
    )

    assert [(c.kind, c.source.item_id) for c in plan.changes] == [(ChangeKind.CLOSE, "task-missing")]
    assert [(d.scope, d.code) for d in plan.diagnostics] == [("asks", "close-suppressed")]


def test_global_incomplete_snapshot_suppresses_all_absence_closes():
    plan = build_plan(
        snapshot([], {"tasks": CapabilityState.COMPLETE}, complete=False),
        [managed("gone")],
        BridgeLedger([ledger_entry("gone")]),
        POLICY,
    )

    assert not plan.changes
    assert plan.diagnostics[0].code == "close-suppressed"


def test_diff_before_mutate_produces_no_change_when_converged():
    current = managed("task-1")
    plan = build_plan(
        snapshot([item("task-1")], {"tasks": CapabilityState.COMPLETE}),
        [current],
        BridgeLedger([ledger_entry("task-1")]),
        POLICY,
    )

    assert plan.changes == ()


def test_tracker_owned_field_is_preserved(tmp_path):
    import json

    doc = dict(POLICY.document)
    doc["field_ownership"] = dict(doc["field_ownership"])
    doc["field_ownership"]["title"] = "tracker"
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(doc))
    policy = load_policy(path)

    plan = build_plan(
        snapshot([item("task-1", title="Source")], {"tasks": CapabilityState.COMPLETE}),
        [managed("task-1", title="Operator edit")],
        BridgeLedger([ledger_entry("task-1")]),
        policy,
    )

    assert "title" not in (plan.changes[0].fields if plan.changes else {})


def test_managed_label_transition_replaces_stale_label_and_preserves_operator_label():
    blocked = managed(
        "task-1",
        fields={"labels": ("lane:blocked", "kind:task", "operator:keep")},
    )
    plan = build_plan(
        snapshot([item("task-1")], {"tasks": CapabilityState.COMPLETE}),
        [blocked], BridgeLedger([ledger_entry("task-1")]), POLICY,
    )

    assert plan.changes[0].fields["labels"] == (
        "operator:keep", "lane:active", "kind:task"
    )


def test_explicit_archived_item_closes_even_when_snapshot_is_globally_incomplete():
    closed = build_plan(
        snapshot(
            [item("task-1", archived=True)],
            {"tasks": CapabilityState.DEGRADED},
            complete=False,
        ),
        [managed("task-1")], BridgeLedger([ledger_entry("task-1")]), POLICY,
    )

    assert closed.changes[0].kind is ChangeKind.CLOSE


def test_reappearing_item_reopens():
    reopened = build_plan(
        snapshot([item("task-1")], {"tasks": CapabilityState.COMPLETE}),
        [managed("task-1", closed=True)], BridgeLedger([ledger_entry("task-1")]), POLICY,
    )

    assert reopened.changes[0].kind is ChangeKind.REOPEN


def test_deleted_tracker_record_is_recreated_from_source():
    plan = build_plan(
        snapshot([item("task-1")], {"tasks": CapabilityState.COMPLETE}),
        [], BridgeLedger([ledger_entry("task-1")]), POLICY,
    )

    assert plan.changes[0].kind is ChangeKind.CREATE
    assert plan.changes[0].fields["source_capability"] == "tasks"
    assert plan.changes[0].fields["source_lane"] == "active"


def test_create_is_suppressed_when_same_slug_is_also_closed():
    task_source = SourceIdentity("coord-engine", "fulcra/tasks", "terminal-task")
    thread_source = SourceIdentity("coord-engine", "fulcra/threads", "terminal-task")
    derived_thread = WorkRecord(
        thread_source,
        "threads",
        "Terminal task",
        "threads-missed",
        origin="fleet",
    )
    task_record = ManagedRecord(
        "LIN-terminal-task",
        task_source,
        "tasks",
        {},
        False,
    )
    task_ledger = LedgerEntry(
        task_source,
        "tasks",
        "linear",
        "LIN-terminal-task",
        POLICY.version,
        POLICY.hash,
    )

    plan = build_plan(
        snapshot(
            [derived_thread],
            {"tasks": CapabilityState.COMPLETE, "threads": CapabilityState.COMPLETE},
        ),
        [task_record],
        BridgeLedger([task_ledger]),
        POLICY,
    )

    assert [(change.kind, change.source) for change in plan.changes] == [
        (ChangeKind.CLOSE, task_source)
    ]
    assert [(diagnostic.scope, diagnostic.code, diagnostic.message) for diagnostic in plan.diagnostics] == [
        ("threads", "create-suppressed-conflicting-close", "terminal-task")
    ]


def test_same_short_suffixes_create_distinct_records():
    plan = build_plan(
        snapshot(
            [item("alpha-12345678"), item("beta-12345678")],
            {"tasks": CapabilityState.COMPLETE},
        ),
        [], BridgeLedger(), POLICY,
    )

    assert [change.source.item_id for change in plan.changes] == [
        "alpha-12345678", "beta-12345678"
    ]


def test_lane_allowlist_omission_excludes_instead_of_falling_through():
    proposed = WorkRecord(
        source("proposal-1"), "tasks", "Proposal", "proposed", origin="fleet"
    )

    plan = build_plan(
        snapshot([proposed], {"tasks": CapabilityState.COMPLETE}),
        [], BridgeLedger(), POLICY,
    )

    assert plan.changes == ()
    assert [(d.scope, d.code, d.message) for d in plan.diagnostics] == [
        ("tasks", "lane-excluded", "proposed")
    ]


def test_moving_managed_item_out_of_allowlist_closes_it_from_positive_evidence():
    done = WorkRecord(
        source("task-1"), "tasks", "Done", "done", origin="fleet"
    )

    plan = build_plan(
        snapshot([done], {"tasks": CapabilityState.DEGRADED}, complete=False),
        [managed("task-1")], BridgeLedger([ledger_entry("task-1")]), POLICY,
    )

    assert [(change.kind, change.provider_id) for change in plan.changes] == [
        (ChangeKind.CLOSE, "LIN-task-1")
    ]


# --- waiting on the operator ----------------------------------------------
# The projection computed an assignee for 42 of 63 changes and then buried all
# of them in a description comment, and dropped `blocked_on` entirely, so work
# parked on the operator was invisible in the tracker they actually use.

import pytest

from coord_tracker_bridge.projection import waits_on_human, _desired
from coord_tracker_bridge.policy import load_policy


def _rec(**kw):
    base = dict(
        source=SourceIdentity("coord-engine", "fulcra/tasks", "t1"),
        capability="tasks", title="t", lane="active",
    )
    base.update(kw)
    return WorkRecord(**base)


@pytest.mark.parametrize("record", [
    _rec(blocked_on="user:ash"),
    _rec(blocked_on="USER:Ash"),
    _rec(blocked_on="user:"),
    _rec(blocked_on="waiting on ash for the 409A"),
    _rec(tags=("needs:human",)),
    _rec(lane="blocked", assignee="ash"),
])
def test_work_waiting_on_the_operator_is_recognised(record):
    assert waits_on_human(record, "ash")


@pytest.mark.parametrize("record", [
    _rec(blocked_on="codex-coder"),
    _rec(blocked_on="user:someone-else"),
    _rec(),
    _rec(lane="blocked", assignee="codex-coder"),
    _rec(assignee="ash"),
])
def test_work_not_waiting_on_the_operator_is_left_alone(record):
    """NEGATIVE CONTROL. Over-assigning to the operator makes the queue useless
    in the other direction — a list of everything is a list of nothing."""
    assert not waits_on_human(record, "ash")


def test_no_configured_human_means_nothing_is_ever_assigned():
    assert not waits_on_human(_rec(blocked_on="user:ash"), "")


def test_the_desired_state_labels_and_assigns_operator_blocked_work():
    policy = load_policy()
    fields = _desired(_rec(blocked_on="user:ash"), policy)
    assert fields["tracker_assignee"] == "ash"
    assert policy.blocked_on_human_label in fields["labels"]
    assert fields["blocked_on"] == "user:ash"


def test_agent_blocked_work_clears_rather_than_keeps_an_operator_assignment():
    """An item that STOPS waiting on the operator has to clear, or their queue
    only ever grows and stops meaning anything."""
    fields = _desired(_rec(blocked_on="codex-coder"), load_policy())
    assert fields["tracker_assignee"] is None
    assert load_policy().blocked_on_human_label not in fields["labels"]


def test_an_excluded_lane_cannot_hide_work_waiting_on_the_operator():
    """THE REGRESSION. 577 of ~700 fleet items sat in the excluded `proposed`
    lane, and every newly filed P1 is born there, so an item parked on the
    operator was dropped before anything could see it was parked on them."""
    policy = load_policy()
    assert "proposed" not in policy.included_lanes
    item = _rec(lane="proposed", blocked_on="user:ash")
    snapshot = Snapshot(items=(item,), complete=True, diagnostics=(),
                        capabilities={"tasks": CapabilityState.COMPLETE},
                        observed_at=datetime(2026, 8, 29, tzinfo=timezone.utc))
    plan = build_plan(snapshot, (), BridgeLedger(), policy)
    assert [c.kind for c in plan.changes] == [ChangeKind.CREATE]
    assert plan.changes[0].fields["tracker_assignee"] == "ash"


def test_an_excluded_lane_still_excludes_everything_else():
    """NEGATIVE CONTROL: the override is for the operator's queue only. If it
    leaked, one sync would push 577 cards into a curated board."""
    policy = load_policy()
    snapshot = Snapshot(items=(_rec(lane="proposed"),), complete=True,
                        diagnostics=(), capabilities={"tasks": CapabilityState.COMPLETE},
                        observed_at=datetime(2026, 8, 29, tzinfo=timezone.utc))
    plan = build_plan(snapshot, (), BridgeLedger(), policy)
    assert plan.changes == ()
    assert [d.code for d in plan.diagnostics] == ["lane-excluded"]


def test_an_excluded_origin_cannot_hide_work_waiting_on_the_operator_either():
    policy = load_policy()
    item = _rec(lane="active", origin="somewhere-else", blocked_on="user:ash")
    snapshot = Snapshot(items=(item,), complete=True, diagnostics=(),
                        capabilities={"tasks": CapabilityState.COMPLETE},
                        observed_at=datetime(2026, 8, 29, tzinfo=timezone.utc))
    plan = build_plan(snapshot, (), BridgeLedger(), policy)
    assert [c.kind for c in plan.changes] == [ChangeKind.CREATE]


def test_an_excluded_origin_still_excludes_everything_else():
    """NEGATIVE CONTROL, the origin twin."""
    policy = load_policy()
    snapshot = Snapshot(items=(_rec(lane="active", origin="somewhere-else"),),
                        complete=True, diagnostics=(),
                        capabilities={"tasks": CapabilityState.COMPLETE},
                        observed_at=datetime(2026, 8, 29, tzinfo=timezone.utc))
    plan = build_plan(snapshot, (), BridgeLedger(), policy)
    assert plan.changes == ()
    assert [d.code for d in plan.diagnostics] == ["origin-excluded"]


def test_an_unmapped_lane_projects_as_unstarted_rather_than_crashing():
    """The override can pull an item out of a lane the policy never mapped, so
    the state lookup cannot be a bare subscript."""
    policy = load_policy()
    assert "proposed" not in policy.lane_states
    fields = _desired(_rec(lane="proposed", blocked_on="user:ash"), policy)
    assert fields["semantic_state"] == "unstarted"
