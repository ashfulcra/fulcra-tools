"""W1-s5 Option A: authority/cursor-only migration acceptance gates."""
from __future__ import annotations

import json

from coord_engine import cli, records
from coord_engine_test_helpers import FakeTransport


TEAM = "r"


class ClassifiedTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.writes: list[str] = []
        self.read_errors: set[str] = set()

    def read_classified(self, path):
        if path in self.read_errors:
            return None, "error"
        value = self.read(path)
        return (value, "ok") if value is not None else (None, "absent")

    def write(self, path, content):
        self.writes.append(path)
        return super().write(path, content)


class WriteRefusedTransport(ClassifiedTransport):
    def write(self, path, content):
        self.writes.append(path)
        return False


class VerifyFaultTransport(ClassifiedTransport):
    def __init__(self, verify_fault):
        super().__init__()
        self.verify_fault = verify_fault
        self.authority_written = False

    def read_classified(self, path):
        if path == records.config_path(TEAM) and self.authority_written:
            if self.verify_fault == "unreadable":
                return None, "error"
            if self.verify_fault == "mismatch":
                return _legacy_authority(), "ok"
        return super().read_classified(path)

    def write(self, path, content):
        result = super().write(path, content)
        self.authority_written = True
        return result


def _legacy_authority():
    return json.dumps({
        "data_type": "MomentAnnotation/x",
        "api_version": "v1alpha1",
        "operator_note": "preserve me",
    })


def _legacy_cursor(last="2026-08-01T00:00:00Z"):
    return json.dumps({"v": 1, "last_read": last, "seen_ids": ["old"]})


def _setup(t=None):
    t = t or ClassifiedTransport()
    t.put(records.config_path(TEAM), _legacy_authority())
    # A nested file makes alice discoverable; bob is explicitly supplied and
    # proves the absent branch without inventing another state source.
    t.put(records.cursor_path(TEAM, "alice"), _legacy_cursor())
    return t


def _run(t, *args):
    return cli.main(["bus-v3", "migrate", TEAM, *args, "--json"], transport=t)


def _row(capsys):
    return json.loads(capsys.readouterr().out)


def test_dry_run_proves_readable_and_absent_and_writes_nothing(capsys):
    t = _setup()
    before = dict(t.store)

    assert _run(t, "--dry-run", "--agent", "bob") == 0
    row = _row(capsys)

    assert row["state"] == "READY"
    assert row["error_code"] is None
    assert row["authority"]["classification"] == "readable-legacy"
    assert {item["agent"]: item["classification"] for item in row["cursors"]} == {
        "alice": "readable-legacy", "bob": "absent"}
    assert row["writes"] == {
        "authority": 0, "legacy_cursors": 0, "tasks": 0, "roles": 0}
    assert t.store == before
    assert t.writes == []


def test_malformed_cursor_blocks_apply_and_preserves_every_document(capsys):
    t = _setup()
    t.put(records.cursor_path(TEAM, "alice"), "{broken")
    task_path = f"team/{TEAM}/task/keep.md"
    role_path = f"team/{TEAM}/_coord/roles/keep.md"
    t.put(task_path, "task bytes")
    t.put(role_path, "role bytes")
    before = dict(t.store)

    assert _run(t, "--apply") == 3
    row = _row(capsys)

    assert row["state"] == "BLOCKED"
    assert row["error_code"] == "cursor-malformed"
    assert row["cursors"][0]["classification"] == "malformed-blocks"
    assert t.store == before
    assert t.writes == []


def test_apply_is_idempotent_and_never_mutates_legacy_or_task_role_docs(capsys):
    t = _setup()
    cursor_path = records.cursor_path(TEAM, "alice")
    cursor_raw = t.read(cursor_path)
    task_path = f"team/{TEAM}/task/keep.md"
    role_path = f"team/{TEAM}/_coord/roles/keep.md"
    t.put(task_path, "task bytes")
    t.put(role_path, "role bytes")

    assert _run(t, "--apply") == 0
    first = _row(capsys)
    assert first["state"] == "APPLIED"
    assert first["error_code"] is None
    assert first["writes"]["authority"] == 1
    config = json.loads(t.read(records.config_path(TEAM)))
    assert config["protocol_version"] == 1
    assert config["cursor_schema_version"] == 1
    assert config["minimum_writer_version"] == "1.8.0"
    assert config["operator_note"] == "preserve me"
    assert t.read(cursor_path) == cursor_raw
    assert t.read(task_path) == "task bytes"
    assert t.read(role_path) == "role bytes"

    assert _run(t, "--apply") == 0
    second = _row(capsys)
    assert second["state"] == "CURRENT"
    assert second["error_code"] is None
    assert second["writes"]["authority"] == 0
    assert t.writes == [records.config_path(TEAM)]
    assert t.read(cursor_path) == cursor_raw


def test_unreadable_cursor_blocks_without_guessing_absence(capsys):
    t = _setup()
    cursor_path = records.cursor_path(TEAM, "alice")
    t.read_errors.add(cursor_path)

    assert _run(t, "--dry-run") == 3
    row = _row(capsys)

    assert row["cursors"][0]["classification"] == "unreadable-blocks"
    assert row["error_code"] == "cursor-unreadable"
    assert t.writes == []


def test_malformed_authority_blocks_before_any_write(capsys):
    t = _setup()
    t.put(records.config_path(TEAM), "{broken")

    assert _run(t, "--apply") == 3
    row = _row(capsys)

    assert row["authority"]["classification"] == "malformed-blocks"
    assert row["state"] == "BLOCKED"
    assert row["error_code"] == "authority-malformed"
    assert t.writes == []


def test_refused_write_is_rc2_and_reports_no_mutation(capsys):
    t = _setup(WriteRefusedTransport())
    before = dict(t.store)

    assert _run(t, "--apply") == 2
    row = _row(capsys)

    assert row["state"] == "UNKNOWN"
    assert row["error_code"] == "authority-write-refused"
    assert row["writes"]["authority"] == 0
    assert t.store == before


def test_verify_mismatch_reports_issued_but_unproven(capsys):
    t = _setup(VerifyFaultTransport("mismatch"))

    assert _run(t, "--apply") == 3
    row = _row(capsys)

    assert row["state"] == "UNKNOWN"
    assert row["error_code"] == "authority-verify-mismatch"
    assert row["writes"]["authority"] == "ISSUED-BUT-UNPROVEN"
    assert t.read(records.config_path(TEAM)) != _legacy_authority()


def test_unreadable_verify_reports_issued_but_unproven(capsys):
    t = _setup(VerifyFaultTransport("unreadable"))

    assert _run(t, "--apply") == 3
    row = _row(capsys)

    assert row["state"] == "UNKNOWN"
    assert row["error_code"] == "authority-verify-unreadable"
    assert row["writes"]["authority"] == "ISSUED-BUT-UNPROVEN"
    assert t.read(records.config_path(TEAM)) != _legacy_authority()
