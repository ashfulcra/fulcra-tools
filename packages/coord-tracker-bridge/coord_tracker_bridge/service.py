"""Explicit plan, apply-resources, and sync orchestration phases."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .lease import FileLease
from .ledger import BridgeLedger, LedgerEntry
from .linear import ResourceMissing, ResourcePlan
from .model import ManagedRecord, Snapshot
from .policy import Policy
from .projection import Plan, build_plan


class SourceAdapter(Protocol):
    provider: str

    def snapshot(self) -> Snapshot: ...


class TrackerAdapter(Protocol):
    provider: str

    def list_managed_records(self, ledger: BridgeLedger) -> list[ManagedRecord]: ...

    def resource_plan(self, labels, projects) -> ResourcePlan: ...

    def apply_resources(self, plan: ResourcePlan) -> None: ...

    def apply_change(self, change) -> str: ...

    def plan_marker_adoptions(self, snapshot, ledger, policy): ...

    def apply_marker_adoption(self, adoption) -> None: ...


@dataclass(frozen=True, slots=True)
class BridgePlan:
    projection: Plan
    resources: ResourcePlan
    snapshot: Snapshot
    managed_records: tuple[ManagedRecord, ...]


@dataclass(frozen=True, slots=True)
class SyncResult:
    applied: int
    plan: BridgePlan


class BridgeService:
    """Orchestrator that keeps resource creation out of ordinary sync."""

    def __init__(
        self,
        source: SourceAdapter,
        tracker: TrackerAdapter,
        policy: Policy,
        ledger_path: str | Path,
        lease_directory: str | Path,
    ) -> None:
        self.source = source
        self.tracker = tracker
        self.policy = policy
        self.ledger_path = Path(ledger_path)
        self.lease_directory = Path(lease_directory)

    def _ledger(self) -> BridgeLedger:
        return BridgeLedger.load(self.ledger_path) if self.ledger_path.exists() else BridgeLedger()

    def plan(self) -> BridgePlan:
        ledger = self._ledger()
        snapshot = self.source.snapshot()
        records = self.tracker.list_managed_records(ledger)
        resources = self.tracker.resource_plan(
            self.policy.managed_labels, self.policy.workstream_projects.values()
        )
        return BridgePlan(
            build_plan(snapshot, records, ledger, self.policy), resources, snapshot, tuple(records)
        )

    def _lease(self) -> FileLease:
        return FileLease(
            self.lease_directory,
            str(getattr(self.source, "source_id", self.source.provider)),
            str(getattr(self.tracker, "tracker_id", self.tracker.provider)),
            self.policy.hash,
        )

    def apply_resources(self) -> ResourcePlan:
        with self._lease():
            plan = self.plan().resources
            self.tracker.apply_resources(plan)
            return plan

    def adopt_markers(self) -> int:
        """One-time, crash-convergent adoption of legacy tracker markers."""

        with self._lease():
            ledger = self._ledger()
            snapshot = self.source.snapshot()

            # Heal any issue already carrying full provider metadata. This is
            # the retry leg when a prior run mutated Linear but crashed before
            # its ledger write.
            healed = False
            for record in self.tracker.list_managed_records(ledger):
                if ledger.get(record.source) is None:
                    ledger.upsert(LedgerEntry(
                        source=record.source,
                        capability=record.capability,
                        tracker_provider=self.tracker.provider,
                        tracker_record_id=record.provider_id,
                        policy_version=self.policy.version,
                        policy_hash=self.policy.hash,
                    ))
                    healed = True
            if healed:
                ledger.save(self.ledger_path)

            adoptions = self.tracker.plan_marker_adoptions(snapshot, ledger, self.policy)
            applied = 0
            for adoption in adoptions:
                self.tracker.apply_marker_adoption(adoption)
                ledger.upsert(LedgerEntry(
                    source=adoption.source,
                    capability=adoption.capability,
                    tracker_provider=self.tracker.provider,
                    tracker_record_id=adoption.provider_id,
                    policy_version=self.policy.version,
                    policy_hash=self.policy.hash,
                ))
                ledger.save(self.ledger_path)
                applied += 1
            return applied

    def sync(self) -> SyncResult:
        lease = self._lease()
        with lease:
            bridge_plan = self.plan()
            if bridge_plan.resources.labels or bridge_plan.resources.projects:
                raise ResourceMissing("resource plan is non-empty; run apply-resources explicitly")
            ledger = self._ledger()
            healed = False
            for record in bridge_plan.managed_records:
                if ledger.get(record.source) is None:
                    ledger.upsert(LedgerEntry(
                        source=record.source,
                        capability=record.capability,
                        tracker_provider=self.tracker.provider,
                        tracker_record_id=record.provider_id,
                        policy_version=self.policy.version,
                        policy_hash=self.policy.hash,
                    ))
                    healed = True
            if healed:
                ledger.save(self.ledger_path)
            capability_by_source = {
                item.source.key: item.capability for item in bridge_plan.snapshot.items
            }
            applied = 0
            for change in bridge_plan.projection.changes:
                lease.refresh()
                provider_id = self.tracker.apply_change(change)
                prior = ledger.get(change.source)
                capability = capability_by_source.get(
                    change.source.key, prior.capability if prior else "tasks"
                )
                ledger.upsert(LedgerEntry(
                    source=change.source,
                    capability=capability,
                    tracker_provider=self.tracker.provider,
                    tracker_record_id=provider_id,
                    policy_version=self.policy.version,
                    policy_hash=self.policy.hash,
                ))
                # Persist after every provider mutation. A crash before this write
                # converges because adapters discover source metadata on retry.
                ledger.save(self.ledger_path)
                applied += 1
            return SyncResult(applied, bridge_plan)
