import json

import pytest

from coord_tracker_bridge import BridgeLedger, LedgerEntry, SourceIdentity


def entry(item_id: str, tracker_id: str) -> LedgerEntry:
    return LedgerEntry(
        SourceIdentity("coord-engine", "fulcra", item_id),
        "tasks",
        "linear",
        tracker_id,
        "1",
        "a" * 64,
    )


def test_ledger_persists_full_identity_collision_safely(tmp_path):
    path = tmp_path / "bridge-state.json"
    ledger = BridgeLedger([entry("alpha-12345678", "LIN-1"), entry("beta-12345678", "LIN-2")])
    ledger.save(path)

    restored = BridgeLedger.load(path)
    assert len(restored) == 2
    assert restored.get(SourceIdentity("coord-engine", "fulcra", "alpha-12345678")).tracker_record_id == "LIN-1"
    assert restored.get(SourceIdentity("coord-engine", "fulcra", "beta-12345678")).tracker_record_id == "LIN-2"


def test_ledger_save_replaces_complete_document(tmp_path):
    path = tmp_path / "state.json"
    BridgeLedger([entry("one", "LIN-1")]).save(path)
    BridgeLedger([entry("two", "LIN-2")]).save(path)

    assert json.loads(path.read_text())["entries"][0]["source"]["item_id"] == "two"
    assert not list(tmp_path.glob(".state.json.*"))


def test_unknown_ledger_schema_fails_closed(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"schema_version": 99, "entries": []}')

    with pytest.raises(ValueError, match="schema_version"):
        BridgeLedger.load(path)
