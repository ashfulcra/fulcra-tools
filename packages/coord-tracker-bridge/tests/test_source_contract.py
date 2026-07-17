import json
from dataclasses import dataclass
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
    TeamsSourceAdapter,
    build_plan,
    load_policy,
)


NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)


class TeamsTransport:
    def __init__(self, documents=None, *, list_error=False):
        self.documents = documents or {}
        self.list_error = list_error

    def list_dir(self, _prefix):
        if self.list_error:
            raise RuntimeError("offline")
        return [{"name": name, "size": len(body), "mtime": "now"}
                for name, body in sorted(self.documents.items())]

    def read(self, path):
        return self.documents.get(path.rsplit("/", 1)[-1])


def engine_runner(*, degraded=False):
    payloads = {
        "board": {"active": [{"id": "task-1", "title": "Task"}]},
        "asks": [],
        "threads": [],
        "health": [],
    }

    def run(argv, _timeout):
        if degraded and argv[1] == "board":
            return 1, "", "offline"
        return 0, json.dumps(payloads[argv[1]]), ""

    return run


TASK = """---
type: Task
id: task-1
title: Task
status: active
priority: P2
tags: []
---
body is not parsed
"""


@dataclass(frozen=True)
class SourceCase:
    healthy: object
    degraded: object
    secondary_capability: CapabilityState


@pytest.fixture(params=("engine", "teams"))
def source_case(request):
    if request.param == "engine":
        return SourceCase(
            EngineSourceAdapter("fulcra", runner=engine_runner(), clock=lambda: NOW),
            EngineSourceAdapter("fulcra", runner=engine_runner(degraded=True), clock=lambda: NOW),
            CapabilityState.COMPLETE,
        )
    return SourceCase(
        TeamsSourceAdapter("fulcra", transport=TeamsTransport({"task.md": TASK}), clock=lambda: NOW),
        TeamsSourceAdapter("fulcra", transport=TeamsTransport(list_error=True), clock=lambda: NOW),
        CapabilityState.UNSUPPORTED,
    )


def test_source_contract_returns_normalized_complete_snapshot(source_case):
    snapshot = source_case.healthy.snapshot()

    assert snapshot.complete
    assert snapshot.observed_at == NOW
    assert len(snapshot.items) == 1
    assert snapshot.items[0].source.item_id == "task-1"
    assert snapshot.items[0].capability == "tasks"
    assert snapshot.capabilities["tasks"] is CapabilityState.COMPLETE


def test_source_contract_degrades_failed_enumeration_without_clean_empty(source_case):
    snapshot = source_case.degraded.snapshot()

    assert not snapshot.complete
    assert snapshot.capabilities["tasks"] is CapabilityState.DEGRADED
    assert snapshot.diagnostics


def test_source_contract_advertises_capability_fidelity(source_case):
    snapshot = source_case.healthy.snapshot()

    assert snapshot.capabilities["asks"] is source_case.secondary_capability
    assert snapshot.capabilities["command_intake"] is CapabilityState.UNSUPPORTED
    assert all(isinstance(value, CapabilityState) for value in snapshot.capabilities.values())


def test_source_contract_degradation_suppresses_absence_close(source_case):
    healthy = source_case.healthy.snapshot()
    degraded = source_case.degraded.snapshot()
    prototype = healthy.items[0].source
    missing = SourceIdentity(prototype.provider, prototype.namespace, "missing")
    record = ManagedRecord("LIN-missing", missing, "tasks", {}, False)
    policy = load_policy()
    ledger = BridgeLedger([
        LedgerEntry(missing, "tasks", "linear", "LIN-missing", policy.version, policy.hash)
    ])

    healthy_plan = build_plan(healthy, [record], ledger, policy)
    degraded_plan = build_plan(degraded, [record], ledger, policy)

    assert any(change.kind is ChangeKind.CLOSE for change in healthy_plan.changes)
    assert not any(change.kind is ChangeKind.CLOSE for change in degraded_plan.changes)
    assert any(diagnostic.code == "close-suppressed" for diagnostic in degraded_plan.diagnostics)
