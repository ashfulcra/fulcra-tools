from datetime import datetime, timezone

import pytest

from coord_tracker_bridge import (
    BridgeLedger,
    BridgeService,
    CapabilityState,
    ManagedRecord,
    ResourceMissing,
    ResourcePlan,
    Snapshot,
    SourceIdentity,
    WorkRecord,
    load_policy,
)


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class Source:
    provider = "coord-engine:fulcra"

    def __init__(self, snapshot):
        self.value = snapshot

    def snapshot(self):
        return self.value


class Tracker:
    provider = "linear:team"

    def __init__(self, *, resources=ResourcePlan((), ()), records=()):
        self.resources = resources
        self.records = list(records)
        self.applied_resources = []
        self.changes = []

    def list_managed_records(self, _ledger):
        return list(self.records)

    def resource_plan(self, _labels, _projects):
        return self.resources

    def apply_resources(self, plan):
        self.applied_resources.append(plan)

    def apply_change(self, change):
        self.changes.append(change)
        provider_id = change.provider_id or "LIN-created"
        if change.kind == "create":
            self.records.append(ManagedRecord(
                provider_id, change.source, "tasks", dict(change.fields), False
            ))
        return provider_id


def snapshot():
    identity = SourceIdentity("coord-engine", "fulcra", "task-1")
    record = WorkRecord(identity, "tasks", "Task", "active", origin="fleet")
    return Snapshot((record,), True, (), {"tasks": CapabilityState.COMPLETE}, NOW)


def service(tmp_path, tracker):
    return BridgeService(
        Source(snapshot()), tracker, load_policy(), tmp_path / "ledger.json", tmp_path / "leases"
    )


def test_plan_is_read_only(tmp_path):
    tracker = Tracker()
    bridge = service(tmp_path, tracker)

    plan = bridge.plan()

    assert plan.projection.changes[0].kind == "create"
    assert not tracker.changes
    assert not (tmp_path / "ledger.json").exists()


def test_sync_never_auto_creates_resources(tmp_path):
    tracker = Tracker(resources=ResourcePlan(("lane:active",), ()))
    bridge = service(tmp_path, tracker)

    with pytest.raises(ResourceMissing, match="apply-resources"):
        bridge.sync()

    assert not tracker.applied_resources
    assert not tracker.changes


def test_apply_resources_is_explicit_phase(tmp_path):
    plan = ResourcePlan(("lane:active",), ("Engine",))
    tracker = Tracker(resources=plan)

    assert service(tmp_path, tracker).apply_resources() == plan
    assert tracker.applied_resources == [plan]


def test_sync_persists_ledger_after_provider_mutation(tmp_path):
    tracker = Tracker()
    result = service(tmp_path, tracker).sync()
    ledger = BridgeLedger.load(tmp_path / "ledger.json")

    assert result.applied == 1
    assert ledger.get(SourceIdentity("coord-engine", "fulcra", "task-1")).tracker_record_id == "LIN-created"


def test_retry_converges_after_create_succeeds_before_ledger_write(tmp_path, monkeypatch):
    tracker = Tracker()
    bridge = service(tmp_path, tracker)
    real_save = BridgeLedger.save
    calls = [0]

    def fail_first_save(self, path):
        calls[0] += 1
        if calls[0] == 1:
            raise OSError("simulated crash")
        return real_save(self, path)

    monkeypatch.setattr(BridgeLedger, "save", fail_first_save)
    with pytest.raises(OSError, match="simulated crash"):
        bridge.sync()

    assert len(tracker.records) == 1
    assert not (tmp_path / "ledger.json").exists()
    assert bridge.sync().applied == 0
    assert len(tracker.records) == 1
    healed = BridgeLedger.load(tmp_path / "ledger.json")
    assert healed.get(SourceIdentity("coord-engine", "fulcra", "task-1")).tracker_record_id == "LIN-created"
