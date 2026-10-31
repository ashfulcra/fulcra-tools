"""Interactive intents survive a lost reply without moving accounts."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import sqlite3

import pytest

from fulcra_common.v1 import build_event_record

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"
ACTION = "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"
DESTINATION = "Event/11111111-2222-4333-8444-555555555555"


def payload(_request=None):
    return {"data_type": DESTINATION, "record": build_event_record(
        data_type=DESTINATION, source_id="synthetic.quick-record." + ACTION,
        start_time=datetime(2026, 1, 2, tzinfo=timezone.utc), fields={"note": "Synthetic note"})}


def capture(journal, **changes):
    args = dict(action_id=ACTION, account_id=ACCOUNT, epoch="synthetic-epoch",
        operation="record", request={"data_type": DESTINATION, "note": "Synthetic note"},
        build_payload=payload)
    args.update(changes)
    return journal.capture(**args)


@pytest.fixture
def journal(tmp_path):
    from fulcra_collect.record_actions import ActionJournal
    return ActionJournal(tmp_path / "private" / "actions.sqlite")


def test_capture_is_durable_and_retries_return_the_original_frozen_payload(journal):
    from fulcra_collect.record_actions import ActionJournal
    original = payload()
    first = capture(journal, build_payload=lambda _request: original)
    original["record"]["note"] = "caller changed"
    first.payload["record"]["note"] = "caller changed returned snapshot"
    def must_not_build_again(_request):
        pytest.fail("an existing action cannot acquire a new timestamp or payload")
    resumed = capture(ActionJournal(journal.path), build_payload=must_not_build_again)
    assert resumed.payload == payload()
    assert resumed.status == "pending"
    assert resumed.action_id == ACTION and resumed.account_id == ACCOUNT
    assert resumed.epoch == "synthetic-epoch"
    assert resumed.created_at == first.created_at
    assert journal.get(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch").payload == payload()
    assert os.stat(journal.path).st_mode & 0o777 == 0o600


@pytest.mark.parametrize("change", [
    {"account_id": OTHER}, {"epoch": "changed"}, {"operation": "delete"},
    {"request": {"data_type": DESTINATION, "note": "changed request"}},
])
def test_action_identity_is_global_and_never_reassigned_by_a_retry(journal, change):
    first = capture(journal)
    with pytest.raises(ValueError, match="action.*changed|action.*belongs"):
        capture(journal, **change)
    assert journal.get(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch").payload == first.payload


def test_typed_request_identity_does_not_confuse_boolean_and_number(journal):
    capture(journal, request={"value": 0})
    with pytest.raises(ValueError, match="action.*changed"):
        capture(journal, request={"value": False})


@pytest.mark.parametrize("bad", [{1: "numeric"}, "\ud800"])
@pytest.mark.parametrize("boundary", ["request", "payload", "evidence"])
def test_durable_actions_reject_lossy_nested_json_at_every_boundary(journal, bad, boundary):
    if boundary == "request":
        with pytest.raises(ValueError, match="JSON"):
            capture(journal, request={"custom": bad})
        assert journal.get(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch") is None
    elif boundary == "payload":
        bad_payload = payload()
        bad_payload["record"]["custom"] = bad
        with pytest.raises(ValueError, match="JSON"):
            capture(journal, build_payload=lambda _: bad_payload)
        assert journal.get(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch") is None
    else:
        capture(journal)
        with pytest.raises(ValueError, match="JSON"):
            journal.advance(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch", expected_status="pending",
                status="submitted", evidence={"custom": bad})
        assert journal.get(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch").status == "pending"
        assert len(journal.history(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch")) == 1


def test_concurrent_capture_executes_one_pure_payload_builder(journal):
    calls = []
    def build(_request):
        calls.append(True)
        return payload()
    with ThreadPoolExecutor(max_workers=6) as pool:
        actions = list(pool.map(lambda _: capture(journal, build_payload=build), range(12)))
    assert len(calls) == 1
    assert all(action.payload == actions[0].payload for action in actions)


def test_submitted_is_not_verified_and_audited_transitions_survive_restart(journal):
    from fulcra_collect.record_actions import ActionJournal
    capture(journal)
    submitted = journal.advance(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch",
        expected_status="pending", status="submitted", evidence={"upload_id": "synthetic-receipt"})
    assert submitted.status == "submitted"
    fresh = ActionJournal(journal.path)
    verified = fresh.advance(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch",
        expected_status="submitted", status="verified", evidence={"readback": "exact_fields"})
    assert verified.status == "verified"
    events = fresh.history(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch")
    assert [event["status"] for event in events] == ["pending", "submitted", "verified"]
    assert events[1]["evidence"] == {"upload_id": "synthetic-receipt"}
    assert verified.payload == payload()
    assert fresh.advance(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch",
        expected_status="submitted", status="verified", evidence={"readback": "exact_fields"}).status == "verified"
    assert len(fresh.history(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch")) == 3


def test_stale_or_cross_account_transition_cannot_change_an_action(journal):
    capture(journal)
    journal.advance(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch",
        expected_status="pending", status="submitted", evidence={})
    for changes in [{"account_id": OTHER}, {"epoch": "changed"}, {"expected_status": "pending"}]:
        args = dict(account_id=ACCOUNT, epoch="synthetic-epoch", expected_status="submitted",
                    status="verified", evidence={})
        args.update(changes)
        with pytest.raises(ValueError):
            journal.advance(ACTION, **args)
    assert journal.get(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch").status == "submitted"


def test_terminal_action_cannot_be_reopened_or_have_its_proof_replaced(journal):
    capture(journal)
    journal.advance(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch",
        expected_status="pending", status="verified", evidence={"readback": "exact_fields"})
    for status in ["pending", "submitted", "cancelled", "verified"]:
        with pytest.raises(ValueError):
            journal.advance(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch",
                expected_status="verified", status=status, evidence={"changed": True})


def test_deletion_intent_keeps_its_exact_target_and_policy_observation(journal):
    target = payload()["record"]["id"]
    action = capture(journal, operation="delete", request={"source_action": ACTION},
        build_payload=lambda _request: {"data_type": DESTINATION, "record_id": target})
    assert action.payload == {"data_type": DESTINATION, "record_id": target}
    journal.advance(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch",
        expected_status="pending", status="pending", evidence={"exclusion_visible": True})
    result = journal.advance(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch",
        expected_status="pending", status="verified", evidence={"target_absent": True})
    assert result.operation == "delete" and result.payload["record_id"] == target
    assert journal.history(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch")[1]["evidence"]["exclusion_visible"]


@pytest.mark.parametrize("change", [
    {"action_id": "bad"}, {"account_id": "bad"}, {"epoch": ""},
    {"operation": "unknown"}, {"request": {"bad": float("nan")}},
    {"build_payload": lambda _request: {"data_type": "MomentAnnotation", "record": {"id": ACTION}}},
    {"build_payload": lambda _request: {"data_type": DESTINATION, "record": {"id": "bad"}}},
])
def test_invalid_intent_never_leaves_a_partial_action(journal, change):
    with pytest.raises(ValueError):
        capture(journal, **change)
    assert journal.get(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch") is None


def test_builder_failure_rolls_back_without_reserving_a_broken_action(journal):
    def fail(_request):
        raise RuntimeError("synthetic builder failure")
    with pytest.raises(RuntimeError, match="builder"):
        capture(journal, build_payload=fail)
    assert journal.get(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch") is None
    assert capture(journal).status == "pending"


def test_corrupt_persisted_payload_is_unknown_and_cannot_be_overwritten(journal):
    capture(journal)
    with sqlite3.connect(journal.path) as db:
        db.execute("UPDATE actions SET payload_json=? WHERE action_id=?", ('{}', ACTION))
    with pytest.raises(ValueError, match="action.*corrupt|action.*unreadable"):
        journal.get(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch")
    with pytest.raises(ValueError):
        capture(journal)


def test_symlink_or_nonprivate_journal_is_refused(tmp_path):
    from fulcra_collect.record_actions import ActionJournal
    destination = tmp_path / "target"
    destination.write_text("synthetic original")
    link = tmp_path / "link"
    link.symlink_to(destination)
    with pytest.raises((ValueError, OSError)):
        ActionJournal(link)
    assert destination.read_text() == "synthetic original"
    destination.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        ActionJournal(destination)


@pytest.mark.parametrize("column,value", [
    ("status", "verified"), ("evidence_json", '{"changed": true}'),
])
def test_current_action_must_agree_with_its_latest_audit_event(journal, column, value):
    capture(journal)
    with sqlite3.connect(journal.path) as db:
        # Fixed column names above; never interpolate an external identifier.
        db.execute(f"UPDATE actions SET {column}=? WHERE action_id=?", (value, ACTION))
    with pytest.raises(ValueError, match="corrupt|unreadable"):
        journal.get(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch")


def test_journal_refuses_new_writes_after_file_permissions_become_public(journal):
    journal.path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        capture(journal)


@pytest.mark.parametrize("large_part", ["request", "payload"])
def test_oversized_action_is_rejected_without_reserving_identity(journal, large_part):
    changes = {"request": {"note": "x" * (1024 * 1024 + 1)}} if large_part == "request" else {
        "build_payload": lambda _request: {**payload(), "record": {**payload()["record"], "note": "x" * (1024 * 1024 + 1)}}}
    with pytest.raises(ValueError, match="size limit"):
        capture(journal, **changes)
    assert journal.get(ACTION, account_id=ACCOUNT, epoch="synthetic-epoch") is None


def test_payload_builder_receives_the_frozen_request_not_the_callers_mutable_input(journal):
    request = {"note": "first input"}
    def build(snapshot=None):
        request["note"] = "changed while building"
        frozen = request if snapshot is None else snapshot
        return {**payload(), "record": {**payload()["record"], "note": frozen["note"]}}
    action = capture(journal, request=request, build_payload=build)
    assert action.request == {"note": "first input"}
    assert action.payload["record"]["note"] == "first input"
