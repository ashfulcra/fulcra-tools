"""Slice 4 exit gates: INVALID as a terminal read state, the audited
``--consume`` takeover, and the single-object ``queue --json`` success envelope.

Three doctrine points, each with the failure it prevents:

- **INVALID is not ABSENT and not ERROR.** Bytes that exist but do not parse
  are human-fixable evidence. Treating them as absent auto-recreates over the
  corrupt document (destroying the only copy of what went wrong); treating
  them as a transport error hides that a retry will never help. The three
  states carry different rc/error_code so automation can tell "fix the file"
  from "check auth/network and retry" from "genuinely not there".
- **A takeover leaves a durable audit record, or it does not happen.** The
  consumption guard exists because a foreign-identity read silently ate an
  agent's pending directives (2026-07-28); ``--consume`` makes the override
  deliberate, and the audit doc written BEFORE the read makes it
  reconstructable. An unauditable takeover is refused — fail closed.
- **``--json`` success is exactly one object.** ``queue-result`` (DATA|CLEAR)
  and ``queue-error`` (INVALID|UNKNOWN) share the ``type`` discriminator, so a
  consumer switches on one field and empty stdout never means anything.
  Text-mode success output stays byte-identical (golden below, captured at
  29bfaa2d before this slice) because shell consumers pipe it.
"""
from __future__ import annotations

import json

import pytest

from coord_engine import cli, okf, records
from test_records_write import (
    QueueTransport, _event_rec, _pin_clock, _versioned_config,
)

TEAM = "r"
AGENT = "amy"
CONFIG = '{"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"}'
CURSOR = records.cursor_path(TEAM, AGENT)


@pytest.fixture(autouse=True)
def _own_identity_unset(monkeypatch):
    """Tests declare identity explicitly; a developer's env must not leak in."""
    monkeypatch.delenv("FULCRA_COORD_AGENT", raising=False)


class LoggedTransport(QueueTransport):
    """QueueTransport with a classified read, an ordered write log, and
    per-prefix write failure — the fake the audit-ordering gates run on."""

    def __init__(self, *a, read_errors=(), fail_writes=(), **kw):
        super().__init__(*a, **kw)
        self.write_log: list[str] = []
        self.read_errors = tuple(read_errors)     # paths that report "error"
        self.fail_writes = tuple(fail_writes)     # path prefixes that lose writes

    def read_classified(self, path):
        if path in self.read_errors:
            return None, "error"
        content = self.read(path)
        return (content, "ok") if content is not None else (None, "absent")

    def write(self, path, content):
        if any(path.startswith(prefix) for prefix in self.fail_writes):
            return False
        self.write_log.append(path)
        return super().write(path, content)


def _transport(window=None, **kw):
    t = LoggedTransport(window=[] if window is None else window, **kw)
    t.put(records.config_path(TEAM), CONFIG)
    return t


def _run(monkeypatch, capsys, t, argv):
    _pin_clock(monkeypatch)
    rc = cli.main(["queue", TEAM, "--agent", AGENT, *argv], transport=t)
    out = capsys.readouterr()
    return rc, out.out, out.err


# === gate 1: invalid-not-absent =============================================

def test_corrupt_config_is_invalid_never_absent(monkeypatch, capsys):
    """rc 3 + INVALID, where a truly absent config is rc 2 — the caller can
    never mistake "fix this file" for "create this file"."""
    t = _transport()
    t.put(records.config_path(TEAM), "{not json at all")
    rc, out, err = _run(monkeypatch, capsys, t, ["--json"])
    assert rc == 3
    assert json.loads(out)["state"] == "INVALID"
    assert "INCOMPATIBLE" in err
    # Fail closed BEFORE any read or write: no window query, no cursor.
    assert t.record_queries == []
    assert CURSOR not in t.store
    # The corrupt bytes are evidence; nothing recreated over them.
    assert t.store[records.config_path(TEAM)] == "{not json at all"


def test_corrupt_cursor_is_invalid_and_never_auto_recreated(monkeypatch, capsys):
    """Pre-slice the engine widened to the 7d lookback and then SAVED a fresh
    cursor over the corrupt bytes at the end of the read. That auto-recreate
    is the exact move INVALID forbids: fail closed, keep the evidence."""
    t = _transport(window=[_event_rec("r1", "job")])
    t.put(CURSOR, "not json at all")
    rc, out, err = _run(monkeypatch, capsys, t, ["--json"])
    assert rc == 3
    row = json.loads(out)
    assert row == {"type": "queue-error", "state": "INVALID",
                   "error_code": "cursor-invalid", "rc": 3}
    assert t.record_queries == []                    # refused before the read
    assert t.store[CURSOR] == "not json at all"      # evidence untouched
    assert CURSOR in err                             # human told what to fix


def test_cursor_with_unparseable_last_read_is_invalid_not_lookback(
        monkeypatch, capsys):
    """A cursor that parses as JSON but carries garbage time is the same
    class of corruption; guessing a lookback would consume under a coverage
    claim nobody can verify."""
    t = _transport()
    t.put(CURSOR, '{"v":1,"last_read":"yesterday-ish","seen_ids":[]}')
    rc, out, _err = _run(monkeypatch, capsys, t, ["--json"])
    assert rc == 3
    assert json.loads(out)["error_code"] == "cursor-invalid"
    assert json.loads(t.store[CURSOR])["last_read"] == "yesterday-ish"


# === gate 2: invalid-not-error ==============================================

def test_transport_failure_is_still_error_distinguishable_from_invalid(
        monkeypatch, capsys):
    """UNKNOWN/-read-failed (retry) versus INVALID/-invalid (fix the file):
    both rc 3, so the envelope carries the discrimination."""
    t = _transport(window=[])
    t.read_errors = (CURSOR,)
    rc, out, _err = _run(monkeypatch, capsys, t, ["--json"])
    assert rc == 3
    row = json.loads(out)
    assert row == {"type": "queue-error", "state": "UNKNOWN",
                   "error_code": "cursor-read-failed", "rc": 3}
    assert t.record_queries == []


def test_load_cursor_classified_separates_all_four_states():
    t = LoggedTransport(window=[])
    assert records.load_cursor_classified(t, TEAM, AGENT) == (None, "absent")
    t.read_errors = (CURSOR,)
    assert records.load_cursor_classified(t, TEAM, AGENT) == (None, "error")
    t.read_errors = ()
    t.put(CURSOR, "corrupt")
    assert records.load_cursor_classified(t, TEAM, AGENT) == (None, "invalid")
    t.put(CURSOR, '{"v":1,"last_read":"2026-07-27T17:30:00Z","seen_ids":[]}')
    cursor, status = records.load_cursor_classified(t, TEAM, AGENT)
    assert status == "ok" and cursor["last_read"] == "2026-07-27T17:30:00Z"


def test_plain_read_transport_still_detects_invalid_bytes():
    """Invalidity is a property of the content: even a transport without a
    classified read must not let corrupt bytes pass as an absent cursor."""
    t = QueueTransport(window=[])
    t.put(CURSOR, "corrupt")
    assert records.load_cursor_classified(t, TEAM, AGENT) == (None, "invalid")
    del t.store[CURSOR]
    assert records.load_cursor_classified(t, TEAM, AGENT) == (None, "absent")


# === gates 3-5: the audited takeover ========================================

def _takeover(monkeypatch, t, *argv):
    monkeypatch.setenv("FULCRA_COORD_AGENT", "operator")
    _pin_clock(monkeypatch)
    return cli.main(["queue", TEAM, "--agent", AGENT, *argv], transport=t)


def test_consume_takeover_writes_audit_before_cursor(monkeypatch, capsys):
    t = _transport(window=[_event_rec("r1", "job")])
    assert _takeover(monkeypatch, t, "--consume") == 0
    audit_prefix = f"team/{TEAM}/{records.CONSUME_AUDIT_PREFIX}/"
    assert [p.startswith(audit_prefix) for p in t.write_log] == [True, False]
    assert t.write_log[1] == CURSOR, "cursor write must FOLLOW the audit"
    (audit_path,) = [p for p in t.write_log if p.startswith(audit_prefix)]
    # <UTC-timestamp>-<caller>-takes-<target>.md under the pinned clock
    assert audit_path.startswith(f"{audit_prefix}20260727T180000Z-")
    assert "-takes-" in audit_path and audit_path.endswith(".md")
    fm = okf.parse_frontmatter(t.store[audit_path])
    assert fm["ts"] == "2026-07-27T18:00:00Z"
    assert fm["caller"] == "operator"
    assert fm["target"] == AGENT
    assert fm["cursor"] == CURSOR
    assert "--consume" in fm["reason"]


def test_consume_refused_when_audit_write_fails(monkeypatch, capsys):
    """Fail closed: no audit record, no takeover — the cursor is untouched
    and the caller is told the consume was aborted, not merely degraded."""
    t = _transport(window=[_event_rec("r1", "job")],
                   fail_writes=(f"team/{TEAM}/{records.CONSUME_AUDIT_PREFIX}/",))
    assert _takeover(monkeypatch, t, "--consume", "--json") == 3
    out = capsys.readouterr()
    row = json.loads(out.out)
    assert row == {"type": "queue-error", "state": "UNKNOWN",
                   "error_code": "consume-audit-failed", "rc": 3}
    assert "REFUSED" in out.err
    assert CURSOR not in t.store                     # cursor never mutated
    assert t.record_queries == []                    # takeover read never ran


def test_peek_writes_nothing(monkeypatch, capsys):
    t = _transport(window=[_event_rec("r1", "job")])
    monkeypatch.setenv("FULCRA_COORD_AGENT", AGENT)
    _pin_clock(monkeypatch)
    assert cli.main(["queue", TEAM, "--agent", AGENT, "--peek"],
                    transport=t) == 0
    assert t.write_log == []


def test_foreign_identity_default_peek_writes_nothing(monkeypatch, capsys):
    """The guard's implicit peek is as write-free as an explicit one — no
    audit either, because nothing was taken over."""
    t = _transport(window=[_event_rec("r1", "job")])
    assert _takeover(monkeypatch, t) == 0            # no --consume
    assert t.write_log == []


def test_self_read_and_flag_only_identity_are_not_takeovers(monkeypatch, capsys):
    """Reading as yourself, or with --agent as the sole identity declaration,
    consumes without an audit doc — the audit marks takeovers, not reads."""
    audit_prefix = f"team/{TEAM}/{records.CONSUME_AUDIT_PREFIX}/"
    for env in (AGENT, None):
        t = _transport(window=[_event_rec("r1", "job")])
        if env is None:
            monkeypatch.delenv("FULCRA_COORD_AGENT", raising=False)
        else:
            monkeypatch.setenv("FULCRA_COORD_AGENT", env)
        _pin_clock(monkeypatch)
        assert cli.main(["queue", TEAM, "--agent", AGENT], transport=t) == 0
        assert t.write_log == [CURSOR]
        assert not any(p.startswith(audit_prefix) for p in t.store)
        capsys.readouterr()


# === gate 6: the --json success envelope ====================================

def _single_json_object(out: str) -> dict:
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one stdout object, got {lines!r}"
    return json.loads(lines[0])


def test_json_data_envelope_is_one_object_with_full_event_shape(
        monkeypatch, capsys):
    t = _transport(window=[
        _event_rec("r1", "job-1"),
        _event_rec("r2", "fleet-wide", to="all",
                   at="2026-07-27T17:05:00+00:00"),
    ])
    rc, out, _err = _run(monkeypatch, capsys, t, ["--json"])
    assert rc == 0
    row = _single_json_object(out)
    assert row == {
        "type": "queue-result",
        "state": "DATA",
        "events": [
            {"id": "r1", "ts": "2026-07-27T17:00:00+00:00", "sender": "boss",
             "to": AGENT, "kind": "directive", "pri": "P2", "slug": "job-1",
             "ptr": None},
            {"id": "r2", "ts": "2026-07-27T17:05:00+00:00", "sender": "boss",
             "to": "all", "kind": "directive", "pri": "P2",
             "slug": "fleet-wide", "ptr": None},
        ],
        "count": 2,
        "cursor": {"path": CURSOR, "advanced": True},
        "engine_version": records.engine_stamp()["engine_version"],
        "protocol": None,                 # legacy authority: no versions to report
    }


def test_json_clear_envelope_is_one_object_not_silence(monkeypatch, capsys):
    """An empty window used to emit NOTHING under --json; empty stdout is
    indistinguishable from a crashed pipe. CLEAR is now an affirmative claim."""
    t = _transport(window=[])
    rc, out, _err = _run(monkeypatch, capsys, t, ["--json"])
    assert rc == 0
    row = _single_json_object(out)
    assert (row["type"], row["state"], row["events"], row["count"]) == \
        ("queue-result", "CLEAR", [], 0)
    assert row["cursor"] == {"path": CURSOR, "advanced": True}


def test_json_envelope_reports_versioned_authority_protocol(monkeypatch, capsys):
    t = _transport(window=[])
    t.put(records.config_path(TEAM), json.dumps(_versioned_config()))
    rc, out, _err = _run(monkeypatch, capsys, t, ["--json"])
    assert rc == 0
    row = _single_json_object(out)
    assert row["protocol"] == {
        "protocol_version": 1, "cursor_schema_version": 1,
        "cursor_generation": 0,
    }


def test_json_envelope_advanced_false_when_cursor_save_fails(
        monkeypatch, capsys):
    """A failed save is latency, not loss (rc stays 0) — but the envelope
    must not claim coverage advanced when it did not."""
    t = _transport(window=[_event_rec("r1", "job")], write_ok=False)
    rc, out, err = _run(monkeypatch, capsys, t, ["--json"])
    assert rc == 0
    row = _single_json_object(out)
    assert row["state"] == "DATA"
    assert row["cursor"]["advanced"] is False
    assert "cursor save failed" in err


def test_json_peek_emits_one_unadvanced_envelope(monkeypatch, capsys):
    t = _transport(window=[_event_rec("r1", "job")])
    rc, out, _err = _run(monkeypatch, capsys, t, ["--peek", "--json"])
    assert rc == 0
    row = _single_json_object(out)
    assert row["state"] == "DATA" and row["count"] == 1
    assert row["cursor"] == {"path": CURSOR, "advanced": False}
    assert t.write_log == []


def test_json_error_envelope_is_unchanged_by_the_success_envelope(
        monkeypatch, capsys):
    """The slice-2 failure contract survives verbatim; success and failure
    share only the ``type`` discriminator convention."""
    t = _transport()
    t.put(records.config_path(TEAM), "{not json at all")
    rc, out, _err = _run(monkeypatch, capsys, t, ["--json"])
    assert rc == 3
    assert _single_json_object(out) == {
        "type": "queue-error", "state": "INVALID",
        "error_code": "config-invalid", "rc": 3,
    }


def test_json_unknown_window_now_carries_the_error_envelope(
        monkeypatch, capsys):
    """With success guaranteed to print one object, a silent rc-3 stdout
    would be the one remaining hole; the UNKNOWN window joins the envelope."""
    t = LoggedTransport(window=None)                 # transport.records → UNKNOWN
    t.put(records.config_path(TEAM), CONFIG)
    rc, out, err = _run(monkeypatch, capsys, t, ["--json"])
    assert rc == 3
    assert _single_json_object(out) == {
        "type": "queue-error", "state": "UNKNOWN",
        "error_code": "window-unknown", "rc": 3,
    }
    assert "DEGRADED" in err


# === gate 7: text-mode success output is byte-identical =====================
#
# Golden strings captured at 29bfaa2d (pre-slice head) with the same pinned
# clock and fixtures. The text surface is piped by shell consumers
# (queue-sweep.sh and downstream greps); this slice must not move a byte of it.

GOLDEN_DATA_STDOUT = ("2026-07-27T17:00:00 boss directive P2 hello -\n"
                      "2026-07-27T17:05:00 boss directive P2 world -\n")
GOLDEN_WARNING = ("queue: VERSION WARNING — legacy bus-v3 authority has no "
                  "fleet version fence; cursor v2 activation is forbidden\n")
GOLDEN_PEEK_NOTICE = ("queue: peek — 1 event(s) shown, cursor NOT advanced "
                      "(the owning agent still receives them)\n")


def test_plain_data_output_byte_identical_to_pre_slice(monkeypatch, capsys):
    t = _transport(window=[
        _event_rec("r1", "hello"),
        _event_rec("r2", "world", at="2026-07-27T17:05:00+00:00"),
    ])
    rc, out, err = _run(monkeypatch, capsys, t, [])
    assert rc == 0
    assert out == GOLDEN_DATA_STDOUT
    assert err == GOLDEN_WARNING


def test_plain_clear_output_byte_identical_to_pre_slice(monkeypatch, capsys):
    t = _transport(window=[])
    rc, out, err = _run(monkeypatch, capsys, t, [])
    assert rc == 0
    assert out == ""
    assert err == GOLDEN_WARNING


def test_plain_peek_output_byte_identical_to_pre_slice(monkeypatch, capsys):
    t = _transport(window=[_event_rec("r1", "hello")])
    rc, out, err = _run(monkeypatch, capsys, t, ["--peek"])
    assert rc == 0
    assert out == "2026-07-27T17:00:00 boss directive P2 hello -\n"
    assert err == GOLDEN_WARNING + GOLDEN_PEEK_NOTICE
