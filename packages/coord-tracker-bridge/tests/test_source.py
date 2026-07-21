import json
from datetime import datetime, timezone

import pytest

from coord_tracker_bridge import (
    BridgeLedger,
    CapabilityState,
    ChangeKind,
    EngineSourceAdapter,
    LedgerEntry,
    ManagedRecord,
    SourceIdentity,
    build_plan,
    load_policy,
)


NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)


def runner_for(payloads):
    def run(argv, _timeout):
        capability = argv[1]
        value = payloads[capability]
        if isinstance(value, Exception):
            return 1, "", str(value)
        return 0, json.dumps(value), ""
    return run


def test_engine_source_resolves_terminal_legacy_slug_from_archived_search():
    def run(argv, _timeout):
        assert argv == (
            "coord-engine", "search", "fulcra", "task-done", "--archived", "--json"
        )
        return 0, json.dumps([{
            "id": "task-done",
            "title": "Done",
            "status": "done",
            "priority": "P2",
        }]), ""

    record = EngineSourceAdapter("fulcra", runner=run).resolve_legacy_slug("task-done")

    assert record is not None
    assert record.source == SourceIdentity("coord-engine", "fulcra/tasks", "task-done")
    assert record.capability == "tasks"
    assert record.lane == "done"


def test_engine_source_rejects_degraded_archived_search():
    def run(_argv, _timeout):
        return 0, json.dumps([{
            "type": "read-degraded",
            "reason": "summaries index unreadable",
        }]), ""

    with pytest.raises(ValueError, match="legacy slug lookup failed"):
        EngineSourceAdapter("fulcra", runner=run).resolve_legacy_slug("task-done")


def test_engine_source_rejects_duplicate_exact_archived_search_matches():
    def run(_argv, _timeout):
        row = {"id": "task-done", "title": "Done", "status": "done"}
        return 0, json.dumps([row, {**row, "archived": "2026-06"}]), ""

    with pytest.raises(ValueError, match="legacy slug lookup is ambiguous"):
        EngineSourceAdapter("fulcra", runner=run).resolve_legacy_slug("task-done")


def test_engine_source_batches_legacy_slug_resolution():
    def run(argv, _timeout):
        slug = argv[3]
        return 0, json.dumps([{
            "id": slug,
            "title": slug,
            "status": "done",
        }]), ""

    records = EngineSourceAdapter("fulcra", runner=run).resolve_legacy_slugs(
        ("task-a", "task-b", "task-a")
    )

    assert tuple(records) == ("task-a", "task-b")
    assert records["task-a"].source.item_id == "task-a"
    assert records["task-b"].source.item_id == "task-b"


def test_engine_source_normalizes_each_capability_and_sanitizes_text():
    adapter = EngineSourceAdapter(
        "fulcra",
        runner=runner_for({
            "board": {"active": [{"id": "task-1", "title": "Task\u0000 title", "tags": ["kind:task"]}]},
            "asks": [{"id": "ask-1", "title": "Question"}],
            "threads": [],
            "health": {"healthy": True, "fresh": 0, "total": 0, "hosts": [], "continuity_stale": []},
        }),
        clock=lambda: NOW,
    )

    snapshot = adapter.snapshot()

    assert snapshot.complete
    assert [item.source.item_id for item in snapshot.items] == ["task-1", "ask-1"]
    assert snapshot.items[0].title == "Task  title"
    assert snapshot.capabilities["expectations"] is CapabilityState.UNSUPPORTED


def test_engine_source_degrades_only_failed_capability_and_never_returns_clean_complete():
    adapter = EngineSourceAdapter(
        "fulcra",
        runner=runner_for({"board": RuntimeError("secret source failure"), "asks": [], "threads": [], "health": {"hosts": []}}),
        clock=lambda: NOW,
    )

    snapshot = adapter.snapshot()

    assert not snapshot.complete
    assert snapshot.capabilities["tasks"] is CapabilityState.DEGRADED
    assert snapshot.capabilities["asks"] is CapabilityState.COMPLETE
    assert snapshot.diagnostics[0].scope == "tasks"


def test_engine_source_honors_embedded_degraded_rows():
    adapter = EngineSourceAdapter(
        "fulcra",
        runner=runner_for({
            "board": {"active": [], "read-degraded": {"reason": "unknown"}},
            "asks": [], "threads": [], "health": {"hosts": []},
        }),
        clock=lambda: NOW,
    )

    snapshot = adapter.snapshot()
    assert snapshot.capabilities["tasks"] is CapabilityState.DEGRADED
    assert snapshot.diagnostics[0].message == "$.read-degraded: unknown"


def test_engine_source_parses_jsonl_folds_and_uses_slow_health_bound():
    seen = {}

    def run(argv, timeout):
        seen[argv[1]] = timeout
        if argv[1] == "threads":
            return 0, '{"id":"thread-1","title":"One"}\n{"id":"thread-2","title":"Two"}\n', ""
        if argv[1] == "health":
            return 0, json.dumps({"healthy": True, "fresh": 1, "total": 1,
                                  "hosts": [{"host": "builder-1", "stale": False, "tasks": 10}],
                                  "continuity_stale": []}), ""
        return 0, json.dumps({"active": []} if argv[1] == "board" else []), ""

    snapshot = EngineSourceAdapter(
        "fulcra", runner=run, timeout=12.0, health_timeout=345.0, clock=lambda: NOW
    ).snapshot()

    assert [item.source.item_id for item in snapshot.items] == ["thread-1", "thread-2", "builder-1"]
    assert snapshot.capabilities["health"] is CapabilityState.COMPLETE
    assert seen == {"board": 12.0, "asks": 12.0, "threads": 12.0, "health": 345.0}


def test_schema_invalid_jsonl_row_degrades_scope_and_suppresses_close():
    def run(argv, _timeout):
        if argv[1] == "threads":
            return 0, '{"id":"present","title":"Present"}\n{"title":"missing id"}\n', ""
        if argv[1] == "health":
            return 0, json.dumps({"hosts": []}), ""
        return 0, json.dumps({"active": []} if argv[1] == "board" else []), ""

    snapshot = EngineSourceAdapter("fulcra", runner=run, clock=lambda: NOW).snapshot()
    missing = SourceIdentity("coord-engine", "fulcra/threads", "gone")
    policy = load_policy()
    ledger = BridgeLedger([
        LedgerEntry(missing, "threads", "linear", "LIN-gone", policy.version, policy.hash)
    ])
    managed = [ManagedRecord("LIN-gone", missing, "threads", {}, False)]

    plan = build_plan(snapshot, managed, ledger, policy)

    assert snapshot.capabilities["threads"] is CapabilityState.DEGRADED
    assert snapshot.diagnostics[0].message == "$[1]: missing stable id/name"
    assert all(change.kind is not ChangeKind.CLOSE for change in plan.changes)


def test_engine_source_derives_curated_backlog_and_named_auxiliary_lanes():
    adapter = EngineSourceAdapter(
        "fulcra",
        runner=runner_for({
            "board": {
                "proposed": [{"id": "later-1", "title": "Later", "assignee": "@backlog"}],
                "waiting": [{"id": "wait-1", "title": "Wait", "assignee": "agent"}],
            },
            "asks": [{"id": "ask-1", "title": "Ask"}],
            "threads": [{"id": "thread-1", "title": "Missed"}],
            "health": {"hosts": []},
        }),
        clock=lambda: NOW,
    )

    snapshot = adapter.snapshot()

    assert [(item.source.item_id, item.lane) for item in snapshot.items] == [
        ("later-1", "backlog"),
        ("wait-1", "waiting"),
        ("ask-1", "asks"),
        ("thread-1", "threads-missed"),
    ]


def test_prose_degraded_line_keeps_valid_jsonl_rows_but_degrades_capability():
    def run(argv, _timeout):
        if argv[1] == "threads":
            return 0, (
                '{"id":"thread-1","title":"One"}\n'
                'THREADS DEGRADED: fold budget exhausted\n'
                '{"id":"thread-2","title":"Two"}\n'
            ), ""
        if argv[1] == "health":
            return 0, json.dumps({"hosts": []}), ""
        return 0, json.dumps({"active": []} if argv[1] == "board" else []), ""

    snapshot = EngineSourceAdapter("fulcra", runner=run, clock=lambda: NOW).snapshot()

    assert [item.source.item_id for item in snapshot.items] == ["thread-1", "thread-2"]
    assert snapshot.capabilities["threads"] is CapabilityState.DEGRADED
    assert snapshot.diagnostics[0].code == "source-line-degraded"
    assert snapshot.diagnostics[0].message == "line 2: THREADS DEGRADED: fold budget exhausted"
