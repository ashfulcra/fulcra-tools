"""An absence-close needs evidence the row was ever THERE.

The defect these cover, found live on 2026-09-03: the first real sync healed 90
pre-existing Linear cards into an empty ledger from their description footers,
and the very next plan proposed closing 52 of them — 41 Backlog and 11 In
Progress — because their source rows were absent from the fold. The gate that
was supposed to prevent that, `absence_is_authoritative`, measures whether the
ENUMERATION was complete. It cannot see whether a particular entry was ever in
one, so adoption read as deletion.
"""

from __future__ import annotations

from datetime import UTC, datetime

from coord_tracker_bridge.ledger import BridgeLedger, LedgerEntry
from coord_tracker_bridge.model import (
    CapabilityState,
    ManagedRecord,
    Snapshot,
    SourceIdentity,
)
from coord_tracker_bridge.policy import load_policy
from coord_tracker_bridge.projection import ChangeKind, build_plan

OBSERVED_AT = datetime(2026, 9, 3, 10, 16, tzinfo=UTC)


def _source(item_id: str = "a-real-task-0000dead") -> SourceIdentity:
    return SourceIdentity(provider="coord-engine", namespace="fulcra/tasks", item_id=item_id)


def _entry(source: SourceIdentity, *, observed: str | None) -> LedgerEntry:
    return LedgerEntry(
        source=source,
        capability="tasks",
        tracker_provider="linear",
        tracker_record_id="prov-1",
        policy_version="2",
        policy_hash="h" * 64,
        last_observed_at=observed,
    )


def _empty_but_complete_snapshot() -> Snapshot:
    """A fold that enumerated everything and legitimately found no rows."""

    return Snapshot(
        items=(),
        complete=True,
        diagnostics=(),
        capabilities={"tasks": CapabilityState.COMPLETE},
        observed_at=OBSERVED_AT,
    )


def _managed(source: SourceIdentity) -> ManagedRecord:
    return ManagedRecord(
        provider_id="prov-1", source=source, capability="tasks", fields={}, closed=False
    )


def test_never_observed_entry_is_not_closed_when_absent() -> None:
    """Identity healed from provider metadata must not read as a deletion."""

    source = _source()
    ledger = BridgeLedger([_entry(source, observed=None)])

    plan = build_plan(
        _empty_but_complete_snapshot(), [_managed(source)], ledger, load_policy()
    )

    assert [c for c in plan.changes if c.kind is ChangeKind.CLOSE] == []
    codes = {d.code for d in plan.diagnostics}
    assert "close-suppressed-never-observed" in codes


def test_observed_entry_is_still_closed_when_absent() -> None:
    """The real deletion path must survive the fix, or absent rows leak forever."""

    source = _source()
    ledger = BridgeLedger([_entry(source, observed="2026-09-02T00:00:00+00:00")])

    plan = build_plan(
        _empty_but_complete_snapshot(), [_managed(source)], ledger, load_policy()
    )

    closes = [c for c in plan.changes if c.kind is ChangeKind.CLOSE]
    assert [c.source.item_id for c in closes] == [source.item_id]


def test_mark_observed_stamps_only_known_sources() -> None:
    source = _source()
    ledger = BridgeLedger([_entry(source, observed=None)])

    assert ledger.mark_observed(source, "2026-09-03T10:16:00+00:00") is True
    assert ledger.get(source).observed is True
    # Idempotent: the same stamp is not a change, so it triggers no write.
    assert ledger.mark_observed(source, "2026-09-03T10:16:00+00:00") is False
    # Observation is provenance on an existing entry, never a way to mint one.
    assert ledger.mark_observed(_source("never-heard-of-it-0000beef"), "x") is False
    assert len(ledger) == 1


def test_v1_ledger_loads_as_never_observed() -> None:
    """Migration must not invent evidence the old file never recorded."""

    v1 = {
        "schema_version": 1,
        "entries": [_entry(_source(), observed=None).to_dict()],
    }
    ledger = BridgeLedger.from_dict(v1)

    assert len(ledger) == 1
    assert ledger.get(_source()).observed is False


def test_observation_survives_a_save_load_round_trip(tmp_path) -> None:
    source = _source()
    ledger = BridgeLedger([_entry(source, observed=None)])
    ledger.mark_observed(source, "2026-09-03T10:16:00+00:00")
    path = tmp_path / "ledger.json"
    ledger.save(path)

    reloaded = BridgeLedger.load(path)

    assert reloaded.get(source).last_observed_at == "2026-09-03T10:16:00+00:00"
