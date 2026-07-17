"""Pure policy and diff engine; adapters execute the returned plan."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .ledger import BridgeLedger
from .model import Diagnostic, ManagedRecord, Snapshot, SourceIdentity, WorkRecord
from .policy import Policy


class ChangeKind(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    REOPEN = "reopen"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class Change:
    kind: ChangeKind
    source: SourceIdentity
    provider_id: str | None
    fields: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Plan:
    changes: tuple[Change, ...]
    diagnostics: tuple[Diagnostic, ...]


def _desired(item: WorkRecord, policy: Policy) -> dict[str, Any]:
    labels = tuple(label for label in policy.managed_labels if label in item.tags)
    return {
        "title": item.title,
        "description": item.description,
        "semantic_state": policy.lane_states[item.lane],
        "priority": policy.priority.get(item.priority, policy.priority.get("P2", 3)),
        "labels": labels,
        "project": policy.workstream_projects.get(item.workstream or ""),
        "due_at": item.due_at.isoformat() if item.due_at else None,
        "owner": item.owner,
        "assignee": item.assignee,
        "origin": item.origin,
        "workstream": item.workstream,
        "source_identity": item.source.to_dict(),
        "source_capability": item.capability,
        "policy_version": policy.version,
        "policy_hash": policy.hash,
    }


def _diff(desired: Mapping[str, Any], actual: Mapping[str, Any], policy: Policy) -> dict[str, Any]:
    changed: dict[str, Any] = {}
    for field, wanted in desired.items():
        owner = policy.owns(field)
        if owner == "tracker":
            continue
        current = actual.get(field)
        if owner == "merge" and field == "labels":
            managed = set(policy.managed_labels)
            tracker_owned = tuple(label for label in tuple(current or ()) if label not in managed)
            wanted = tuple(dict.fromkeys((*tracker_owned, *tuple(wanted or ()))))
        if current != wanted:
            changed[field] = wanted
    return changed


def build_plan(
    snapshot: Snapshot,
    managed_records: Iterable[ManagedRecord],
    ledger: BridgeLedger,
    policy: Policy,
) -> Plan:
    """Return a deterministic plan without performing tracker mutations."""

    managed_by_source = {record.source.key: record for record in managed_records}
    items_by_source = {item.source.key: item for item in snapshot.items}
    changes: list[Change] = []
    diagnostics = list(snapshot.diagnostics)

    for item in sorted(snapshot.items, key=lambda value: value.source):
        key = item.source.key
        if item.lane not in policy.included_lanes:
            diagnostics.append(Diagnostic(item.capability, "lane-excluded", item.lane))
            existing = managed_by_source.get(key)
            if existing and not existing.closed:
                changes.append(Change(
                    ChangeKind.CLOSE,
                    item.source,
                    existing.provider_id,
                    MappingProxyType({}),
                ))
            continue
        if policy.included_origins and item.origin not in policy.included_origins:
            diagnostics.append(Diagnostic(item.capability, "origin-excluded", item.source.key))
            continue
        existing = managed_by_source.get(key)
        if item.archived:
            # This is explicit positive evidence from a present source record,
            # not a close inferred from absence. Snapshot completeness only
            # gates the separate absent-ledger pass below.
            if existing and not existing.closed:
                changes.append(Change(ChangeKind.CLOSE, item.source, existing.provider_id, MappingProxyType({})))
            continue
        wanted = _desired(item, policy)
        if existing is None:
            changes.append(Change(ChangeKind.CREATE, item.source, None, MappingProxyType(wanted)))
            continue
        delta = _diff(wanted, existing.fields, policy)
        if existing.closed:
            changes.append(Change(ChangeKind.REOPEN, item.source, existing.provider_id, MappingProxyType(delta)))
        elif delta:
            changes.append(Change(ChangeKind.UPDATE, item.source, existing.provider_id, MappingProxyType(delta)))

    for entry in sorted(ledger, key=lambda value: value.source):
        key = entry.source.key
        if key in items_by_source:
            continue
        existing = managed_by_source.get(key)
        if existing is None or existing.closed or not policy.close_absent:
            continue
        if snapshot.absence_is_authoritative(entry.capability):
            changes.append(Change(ChangeKind.CLOSE, entry.source, existing.provider_id, MappingProxyType({})))
        else:
            diagnostics.append(
                Diagnostic(entry.capability, "close-suppressed", f"absence not authoritative for {key}")
            )

    return Plan(tuple(changes), tuple(diagnostics))
