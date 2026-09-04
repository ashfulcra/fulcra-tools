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
    labels = [label for label in policy.managed_labels if label in item.tags]
    # The per-consumer label is DERIVED, not copied from a source tag: it is
    # what a saved Linear view filters on, and no source row carries it.
    if item.blocked_on_user or item.lane in policy.consumer_lanes:
        consumer_label = policy.label_for_consumer(item.blocked_on_user)
        if consumer_label and consumer_label not in labels:
            labels.append(consumer_label)
    labels = tuple(labels)
    return {
        "title": item.title,
        "description": item.description,
        "semantic_state": policy.lane_states[item.lane],
        "priority": policy.lane_priority.get(
            item.lane, policy.priority.get(item.priority, policy.priority.get("P2", 3))
        ),
        "labels": labels,
        "project": policy.project_for(item.lane, item.workstream, item.blocked_on_user),
        "due_at": item.due_at.isoformat() if item.due_at else None,
        "owner": item.owner,
        "assignee": item.assignee,
        # Rides in the card's own metadata so the return leg can attribute an
        # answer to the person it was actually blocked on, rather than to one
        # global handle. With more than one consumer, a global handle records
        # somebody else's decision under the wrong name.
        "blocked_on_user": item.blocked_on_user,
        # The real Linear assignee. Without it the card lands in the right
        # project and still never reaches the person it is blocking.
        "linear_assignee": policy.linear_user_for(item.blocked_on_user),
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
            managed = set(policy.all_managed_labels)
            tracker_owned = tuple(label for label in tuple(current or ()) if label not in managed)
            wanted = tuple(dict.fromkeys((*tracker_owned, *tuple(wanted or ()))))
        if current != wanted:
            changed[field] = wanted
    return changed


def _close_fields(existing: ManagedRecord, policy: Policy) -> dict[str, Any]:
    """What a CLOSE must still write: the consumer label has to come off.

    A LABEL IS A CLAIM ABOUT THE PRESENT. "blocked-on-ash" says this is blocked
    on Ash right now; a closed card is not. It matters because a saved Linear
    view filters on the LABEL and not on the card's state, and the operator's own
    view is set to show completed issues. Measured live 2026-09-04: 12 cards
    were correctly closed and kept the label, so his "blocked on me" view still
    showed 29 things when 17 were real -- the closes were invisible to him.

    Only the consumer labels come off. `lane:blocked` and the rest describe what
    the row was and stay as history.
    """

    current = tuple(existing.fields.get("labels") or ())
    kept = tuple(label for label in current if label not in policy.consumer_labels)
    return {"labels": kept} if kept != current else {}


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
                    MappingProxyType(_close_fields(existing, policy)),
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
                changes.append(Change(ChangeKind.CLOSE, item.source, existing.provider_id,
                                      MappingProxyType(_close_fields(existing, policy))))
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
        if existing is None or not policy.close_absent:
            continue
        if existing.closed:
            # ALREADY closed, and the rule above only fires on the transition.
            # A card closed BEFORE that rule existed still carries the consumer
            # label, and a saved view filtering on the label still shows it --
            # so a fix that only applies going forward leaves the operator's
            # view wrong about everything already done. Converge instead:
            # anything still claiming to block a person, that no longer does,
            # gets the claim removed whatever state it is in.
            stale = _close_fields(existing, policy)
            if stale:
                changes.append(Change(
                    ChangeKind.UPDATE, entry.source, existing.provider_id,
                    MappingProxyType(stale),
                ))
            continue
        if not entry.observed:
            # The enumeration being complete says the FOLD saw everything; it
            # says nothing about an entry whose row this bridge has never seen
            # present. Identity healed from provider metadata lands here: it
            # proves the Linear card exists, not that a source row ever did.
            # Closing on that reads adoption as deletion, which is how a first
            # sync arms a mass close of every card it just adopted.
            diagnostics.append(
                Diagnostic(entry.capability, "close-suppressed-never-observed", key)
            )
        elif snapshot.absence_is_authoritative(entry.capability):
            changes.append(Change(ChangeKind.CLOSE, entry.source, existing.provider_id,
                                  MappingProxyType(_close_fields(existing, policy))))
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
