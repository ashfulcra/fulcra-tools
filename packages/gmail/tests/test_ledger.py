"""ledger.py: append-only JSONL, torn-line tolerance, processed-set, outbox key."""
from __future__ import annotations

import json

from fulcra_gmail import ledger as ledger_mod
from fulcra_gmail.ledger import Ledger, LedgerEntry, outbox_key

_ACCT = "acct-synthetic-0000"


def test_append_and_read_roundtrip(tmp_path):
    led = Ledger(_ACCT, root=tmp_path)
    led.append(LedgerEntry.file_done(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        sha256="deadbeef", destination="/collect/gmail/acct/2026-01/m1.json",
    ))
    # A FRESH instance reads the durably-appended entry (fsync per append).
    entries = Ledger(_ACCT, root=tmp_path).entries()
    assert len(entries) == 1
    assert entries[0]["action"] == "file"
    assert entries[0]["status"] == "done"
    assert entries[0]["sha256"] == "deadbeef"
    assert entries[0]["message_id"] == "m1"


def test_ledger_path_is_account_scoped(tmp_path):
    led = Ledger(_ACCT, root=tmp_path)
    assert led.path == tmp_path / "gmail" / _ACCT / "ledger.jsonl"


def test_torn_final_line_is_skipped(tmp_path):
    led = Ledger(_ACCT, root=tmp_path)
    led.append(LedgerEntry.file_done(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        sha256="h1", destination="d1",
    ))
    led.append(LedgerEntry.file_done(
        account_id=_ACCT, message_id="m2", rule_id="r1", rule_version=1,
        sha256="h2", destination="d2",
    ))
    # Simulate a crash mid-append: a partial, non-terminated JSON fragment.
    with led.path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-01-01", "message_id": "m3", "acti')

    entries = led.entries()
    assert [e["message_id"] for e in entries] == ["m1", "m2"]  # torn m3 absent


def test_processed_set_partial_actions_not_fully_done(tmp_path):
    led = Ledger(_ACCT, root=tmp_path)
    led.append(LedgerEntry.file_done(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        sha256="h", destination="d",
    ))
    required = ["file", "relay"]
    assert not led.is_fully_done("m1", "r1", 1, required)
    assert led.remaining_actions("m1", "r1", 1, required) == ["relay"]


def test_processed_set_all_actions_done(tmp_path):
    led = Ledger(_ACCT, root=tmp_path)
    led.append(LedgerEntry.file_done(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        sha256="h", destination="d",
    ))
    led.append(LedgerEntry.relay_done(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        outbox_key=outbox_key(_ACCT, "m1", "r1", 1),
    ))
    required = ["file", "relay"]
    assert led.is_fully_done("m1", "r1", 1, required)
    assert led.remaining_actions("m1", "r1", 1, required) == []


def test_relay_pending_does_not_count_as_done(tmp_path):
    led = Ledger(_ACCT, root=tmp_path)
    led.append(LedgerEntry.relay_pending(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        outbox_key=outbox_key(_ACCT, "m1", "r1", 1),
    ))
    assert not led.is_fully_done("m1", "r1", 1, ["relay"])
    assert led.remaining_actions("m1", "r1", 1, ["relay"]) == ["relay"]


def test_rule_version_bump_starts_fresh_processed_set(tmp_path):
    led = Ledger(_ACCT, root=tmp_path)
    # v1 fully done for file+relay.
    led.append(LedgerEntry.file_done(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        sha256="h", destination="d",
    ))
    led.append(LedgerEntry.relay_done(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        outbox_key=outbox_key(_ACCT, "m1", "r1", 1),
    ))
    assert led.is_fully_done("m1", "r1", 1, ["file", "relay"])
    # SAME message + rule id, bumped version → nothing done yet (fresh set).
    assert not led.is_fully_done("m1", "r1", 2, ["file", "relay"])
    assert led.remaining_actions("m1", "r1", 2, ["file", "relay"]) == \
        ["file", "relay"]


def test_pending_relay_reconciles_to_done(tmp_path):
    led = Ledger(_ACCT, root=tmp_path)
    key = outbox_key(_ACCT, "m1", "r1", 1)
    led.append(LedgerEntry.relay_pending(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        outbox_key=key,
    ))
    led.append(LedgerEntry.relay_done(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        outbox_key=key,
    ))
    assert led.is_fully_done("m1", "r1", 1, ["relay"])


def test_relay_pending_is_recorded_once_across_retries(tmp_path):
    led = Ledger(_ACCT, root=tmp_path)
    key = outbox_key(_ACCT, "m1", "r1", 1)

    assert led.ensure_relay_pending(LedgerEntry.relay_pending(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        outbox_key=key,
    )) is True
    assert led.ensure_relay_pending(LedgerEntry.relay_pending(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        outbox_key=key,
    )) is False

    assert len(led.entries()) == 1


def test_two_instances_refresh_stale_cache_before_append(tmp_path):
    first = Ledger(_ACCT, root=tmp_path)
    second = Ledger(_ACCT, root=tmp_path)
    assert first.entries() == []
    second.append(LedgerEntry.file_done(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        sha256="h1", destination="d1",
    ))
    first.append(LedgerEntry.file_done(
        account_id=_ACCT, message_id="m2", rule_id="r1", rule_version=1,
        sha256="h2", destination="d2",
    ))
    assert [row["message_id"] for row in first.entries()] == ["m1", "m2"]


def test_two_instances_record_pending_key_once(tmp_path):
    first = Ledger(_ACCT, root=tmp_path)
    second = Ledger(_ACCT, root=tmp_path)
    key = outbox_key(_ACCT, "m1", "r1", 1)
    entry = LedgerEntry.relay_pending(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        outbox_key=key,
    )
    assert first.ensure_relay_pending(entry) is True
    assert second.ensure_relay_pending(entry) is False
    assert len(Ledger(_ACCT, root=tmp_path).entries()) == 1


def test_compact_collapses_duplicate_pending_rows_and_keeps_done(tmp_path):
    led = Ledger(_ACCT, root=tmp_path)
    key = outbox_key(_ACCT, "m1", "r1", 1)
    pending = LedgerEntry.relay_pending(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        outbox_key=key,
    )
    for _ in range(20):
        led.append(pending)
    led.append(LedgerEntry.file_done(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        sha256="h", destination="d",
    ))
    led.append(LedgerEntry.relay_done(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        outbox_key=key,
    ))

    assert led.compact_if_needed(min_entries=1, redundancy_ratio=1.1) is True
    rows = led.entries()
    assert [(row["action"], row["status"]) for row in rows] == [
        ("file", "done"), ("relay", "done"),
    ]
    assert led.remaining_actions("m1", "r1", 1, ["file", "relay"]) == []


def test_done_lookup_reads_large_ledger_only_once_per_instance(tmp_path, monkeypatch):
    led = Ledger(_ACCT, root=tmp_path)
    led.append(LedgerEntry.file_done(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        sha256="h", destination="d",
    ))
    fresh = Ledger(_ACCT, root=tmp_path)
    original = fresh.path.read_text
    reads = 0

    def counted(*args, **kwargs):
        nonlocal reads
        reads += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(type(fresh.path), "read_text", lambda self, *a, **kw: counted(*a, **kw))
    for _ in range(20):
        assert fresh.done_actions("m1", "r1", 1) == {"file"}
    assert reads == 1


# -- outbox key determinism -------------------------------------------------


def test_outbox_key_deterministic():
    k1 = outbox_key(_ACCT, "m1", "r1", 1)
    k2 = outbox_key(_ACCT, "m1", "r1", 1)
    assert k1 == k2
    assert isinstance(k1, str) and k1


def test_outbox_key_differs_on_rule_version():
    assert outbox_key(_ACCT, "m1", "r1", 1) != outbox_key(_ACCT, "m1", "r1", 2)


def test_outbox_key_differs_on_message():
    assert outbox_key(_ACCT, "m1", "r1", 1) != outbox_key(_ACCT, "m2", "r1", 1)


def test_entry_json_shape(tmp_path):
    e = LedgerEntry.relay_pending(
        account_id=_ACCT, message_id="m1", rule_id="r1", rule_version=1,
        outbox_key="ob-1",
    )
    d = e.to_dict()
    assert set(d) >= {"ts", "account_id", "message_id", "rule_id",
                      "rule_version", "action", "status", "outbox_key"}
    assert d["action"] == "relay"
    assert d["status"] == "pending"
    # JSON serializable.
    assert json.loads(json.dumps(d)) == d


def test_default_root_uses_fulcra_collect_config(monkeypatch, tmp_path):
    # Sanity: with no explicit root, the ledger lands under the collect config
    # home (respecting FULCRA_COLLECT_HOME).
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))
    led = Ledger(_ACCT)
    assert str(led.path).startswith(str(tmp_path))
    assert led.path.name == "ledger.jsonl"
    assert ledger_mod  # module import smoke
