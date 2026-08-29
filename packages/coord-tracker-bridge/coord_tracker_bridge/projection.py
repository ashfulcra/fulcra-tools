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


_USER_PREFIX = "user:"


def waits_on_human(item: WorkRecord, human: str) -> bool:
    """Does this item wait on the OPERATOR rather than on an agent?

    A deliberate re-statement of ``coord_engine.query.blocked_on_human``'s
    rules, and it resolves the same way that one does: ambiguity SURFACES.
    A false positive is a card the operator can wave off; a false negative is
    a decision parked on them that nothing ever shows them, which is the whole
    reason this projection exists.

    Only the typed and named forms count here. A bare ``blocked_on`` naming
    something unrecognised is left to the engine's own classifier, which has
    the roster this module does not.
    """
    if not human:
        return False
    target = human.strip().lower()
    raw = (item.blocked_on or "").strip()
    if raw.lower().startswith(_USER_PREFIX):
        return (raw[len(_USER_PREFIX):].strip().lower() or target) == target
    tokens = {token.strip().lower() for token in raw.replace(",", " ").split() if token.strip()}
    if target in tokens:
        return True
    if "needs:human" in item.tags:
        return True
    return item.lane == "blocked" and (item.assignee or "").strip().lower() == target


def _desired(item: WorkRecord, policy: Policy) -> dict[str, Any]:
    labels = tuple(label for label in policy.managed_labels if label in item.tags)
    # The operator asked for exactly one thing of this projection: that work
    # waiting on THEM be visible as theirs. So a human-blocked item carries the
    # label AND is assigned to them in the tracker; everything else leaves the
    # tracker's assignee alone rather than inventing a Linear identity for an
    # agent that has none.
    human_blocked = waits_on_human(item, policy.human)
    if human_blocked and policy.blocked_on_human_label:
        labels = tuple(dict.fromkeys((*labels, policy.blocked_on_human_label)))
    return {
        "tracker_assignee": policy.human if human_blocked else None,
        "title": item.title,
        "description": item.description,
        "semantic_state": policy.lane_states[item.lane],
        "priority": policy.priority.get(item.priority, policy.priority.get("P2", 3)),
        "labels": labels,
        "project": policy.workstream_projects.get(item.workstream or ""),
        "due_at": item.due_at.isoformat() if item.due_at else None,
        "owner": item.owner,
        "assignee": item.assignee,
        "blocked_on": item.blocked_on,
        "origin": item.origin,
        "workstream": item.workstream,
        "source_identity": item.source.to_dict(),
        "source_capability": item.capability,
        "source_lane": item.lane,
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

    close_item_ids = {
        change.source.item_id for change in changes if change.kind is ChangeKind.CLOSE
    }
    safe_changes: list[Change] = []
    for change in changes:
        if change.kind is ChangeKind.CREATE:
            capability = str(change.fields.get("source_capability") or "").strip()
            lane = str(change.fields.get("source_lane") or "").strip()
            if not capability or not lane:
                diagnostics.append(Diagnostic(
                    capability or "projection",
                    "create-suppressed-unresolved-source",
                    change.source.item_id,
                ))
                continue
            if change.source.item_id in close_item_ids:
                diagnostics.append(Diagnostic(
                    capability,
                    "create-suppressed-conflicting-close",
                    change.source.item_id,
                ))
                continue
        safe_changes.append(change)

    return Plan(tuple(safe_changes), tuple(diagnostics))
