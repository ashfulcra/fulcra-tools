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
        "health": {"hosts": []},
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
        LedgerEntry(
            missing, "tasks", "linear", "LIN-missing", policy.version, policy.hash,
            # Seen in an earlier fold, so its absence now is a deletion the
            # healthy plan must act on — which is what this test asserts.
            last_observed_at="2026-09-02T00:00:00+00:00",
        )
    ])

    healthy_plan = build_plan(healthy, [record], ledger, policy)
    degraded_plan = build_plan(degraded, [record], ledger, policy)

    assert any(change.kind is ChangeKind.CLOSE for change in healthy_plan.changes)
    assert not any(change.kind is ChangeKind.CLOSE for change in degraded_plan.changes)
    assert any(diagnostic.code == "close-suppressed" for diagnostic in degraded_plan.diagnostics)


# --- engine envelope contract v2 (coord-engine 2.0.x), 2026-08-22 ------------
#
# The engine adopted a standard envelope during the truthfulness work:
# scalar `contract`, plus `health`/`source`/`degraded`/`basis` alongside the
# payload. The adapter predates it and read live 2.0.2 as schema-degraded:
#   tasks -> "$.contract: expected list, got int"   (board gained contract: 2)
#   asks  -> "$: expected list, got dict"           (asks became an envelope)
# Measured on team fulcra 2026-08-22: board keys were
# active/waiting/blocked/proposed (lists) + contract=2; asks was
# {contract, health, source, degraded, basis, rows}. A degraded capability
# cannot authorize absence-based closes, so this silently narrowed coverage —
# the asks lane was invisible entirely.

def _engine_source(payloads):
    """EngineSourceAdapter wired to canned per-capability payloads."""
    from coord_tracker_bridge import source as source_mod

    class _Canned(source_mod.EngineSourceAdapter):
        def _read(self, capability):  # type: ignore[override]
            return payloads.get(capability.name), None

    return _Canned("fulcra")


def _task_row(name="t-1", status="active"):
    return {"id": name, "name": name, "title": "x", "status": status,
            "assignee": "alice", "owner": "coord-boss", "priority": "P2"}


def test_board_envelope_scalar_is_not_a_lane():
    """`contract: 2` sits beside the lanes; it is metadata, not a lane of rows."""
    snap = _engine_source({
        "tasks": {"active": [_task_row()], "waiting": [], "blocked": [],
                  "proposed": [], "contract": 2},
    }).snapshot()
    schema = [d for d in snap.diagnostics if d.code == "source-schema-degraded"]
    assert not schema, f"envelope scalar must not degrade the lane read: {schema}"
    assert any(r.source.item_id == "t-1" for r in snap.items), "the lane row must survive"


def test_asks_envelope_rows_are_read():
    """`asks --json` returns an envelope; the rows live under `rows`."""
    snap = _engine_source({
        "asks": {"contract": 2, "health": "CLEAR", "source": "raw-scan",
                 "degraded": [], "basis": [],
                 "rows": [_task_row(name="ask-1", status="open")]},
    }).snapshot()
    schema = [d for d in snap.diagnostics if d.code == "source-schema-degraded"]
    assert not schema, f"the asks envelope must be read, not degraded: {schema}"
    assert any(r.source.item_id == "ask-1" for r in snap.items)


def test_a_genuinely_wrong_shape_still_degrades():
    """The fix must not turn into blanket tolerance — unknown shapes fail closed."""
    snap = _engine_source({"asks": "not-a-payload"}).snapshot()
    assert [d for d in snap.diagnostics if d.code == "source-schema-degraded"], (
        "a scalar payload is not an envelope and must still degrade")


def test_one_row_in_two_folds_projects_ONE_card():
    """The engine shows a row blocked on a human in two folds, and should.

    That is right for the engine and wrong for a tracker: the source namespace
    carries the fold, so two folds meant two identities and TWO CARDS FOR ONE
    THING. Measured live 2026-09-04 -- the operator's "blocked on me" view held
    30 cards for 15 real rows, 12 of them doubled. He cannot tell which copy to
    answer, and answering one leaves its twin looking unanswered.
    """

    row = _task_row(name="needs-a-decision", status="blocked")
    snap = _engine_source({
        "tasks": {"active": [], "waiting": [], "blocked": [row], "proposed": []},
        "asks": [row],
    }).snapshot()

    assert [item.source.item_id for item in snap.items] == ["needs-a-decision"]
    # asks wins: it is the fold `coord-engine answer` is defined over, so the
    # card left in the view is the one that can actually be settled.
    assert [item.capability for item in snap.items] == ["asks"]


def test_the_collapsed_fold_is_reported_never_silent():
    """WHICH identity won decides which card the operator sees, so a collapse is
    a fact about his board -- never an invisible one."""

    row = _task_row(name="needs-a-decision", status="blocked")
    snap = _engine_source({
        "tasks": {"active": [], "waiting": [], "blocked": [row], "proposed": []},
        "asks": [row],
    }).snapshot()

    collapsed = [d for d in snap.diagnostics if d.code == "fold-duplicate-collapsed"]
    assert len(collapsed) == 1
    assert collapsed[0].scope == "tasks"
    assert "asks" in collapsed[0].message


def test_distinct_rows_are_never_collapsed():
    snap = _engine_source({
        "tasks": {"active": [_task_row(name="a")], "waiting": [],
                  "blocked": [_task_row(name="b", status="blocked")], "proposed": []},
        "asks": [_task_row(name="c", status="blocked")],
    }).snapshot()

    assert sorted(item.source.item_id for item in snap.items) == ["a", "b", "c"]
    assert not [d for d in snap.diagnostics if d.code == "fold-duplicate-collapsed"]


def test_a_row_only_the_tasks_fold_holds_keeps_its_card():
    """The three rows blocked on the operator but assigned to an AGENT have no
    ask twin. Collapsing must not cost them their card -- they are exactly the
    rows the engine will not answer, so the card is all he has."""

    snap = _engine_source({
        "tasks": {"active": [], "waiting": [],
                  "blocked": [_task_row(name="409a", status="blocked")], "proposed": []},
        "asks": [],
    }).snapshot()

    assert [(i.source.item_id, i.capability) for i in snap.items] == [("409a", "tasks")]
