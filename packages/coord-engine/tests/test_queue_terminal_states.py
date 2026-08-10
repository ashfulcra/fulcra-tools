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


def _audit_doc(t):
    audit_prefix = f"team/{TEAM}/{records.CONSUME_AUDIT_PREFIX}/"
    (audit_path,) = [p for p in t.store if p.startswith(audit_prefix)]
    return audit_path, okf.parse_frontmatter(t.store[audit_path])


def test_consume_takeover_writes_audit_before_cursor(monkeypatch, capsys):
    """The audit record: actor (caller), target, reason, timestamp, the
    OBSERVED prior coverage claim, and the authority the takeover INTENDS to
    operate under — observations and intent, never predictions (under
    concurrency this process cannot know what its consuming read will
    actually overtake). Reading the target cursor to capture the observation
    happens BEFORE the audit lands (a read consumes nothing); the write log
    proves no cursor MUTATION precedes the audit."""
    t = _transport(window=[_event_rec("r1", "job")])
    t.put(CURSOR, '{"v":1,"last_read":"2026-07-27T17:30:00Z","seen_ids":[]}')
    assert _takeover(monkeypatch, t, "--consume") == 0
    audit_prefix = f"team/{TEAM}/{records.CONSUME_AUDIT_PREFIX}/"
    # Assert the ORDERING invariant this test is named for, not the write COUNT.
    # It previously pinned the log to exactly [audit, cursor]; that also fixed
    # the number of writes, which is not the property in question — the
    # docstring's claim is that no cursor MUTATION precedes the audit. A third
    # write now follows both: `queue --consume` advances ANOTHER agent's cursor,
    # so it counts as activity and refreshes the actor's presence shard. That is
    # a different path and cannot violate this ordering, so the assertion is
    # stated over the two paths it is actually about.
    audit_at = [i for i, p in enumerate(t.write_log) if p.startswith(audit_prefix)]
    cursor_at = [i for i, p in enumerate(t.write_log) if p == CURSOR]
    assert audit_at, "no audit record was written"
    assert cursor_at, "no cursor write happened"
    assert min(audit_at) < min(cursor_at), "cursor write must FOLLOW the audit"
    audit_path, fm = _audit_doc(t)
    # <UTC-timestamp>-<caller>-takes-<target>.md under the pinned clock
    assert audit_path.startswith(f"{audit_prefix}20260727T180000Z-")
    assert "-takes-" in audit_path and audit_path.endswith(".md")
    assert fm["type"] == "ConsumeAudit"
    assert fm["ts"] == "2026-07-27T18:00:00Z"
    assert fm["caller"] == "operator"
    assert fm["target"] == AGENT
    assert fm["cursor"] == CURSOR
    # what the caller SAW at ts, and the schema it meant to operate under —
    # no predicted timestamp: the actual transition is evidenced by the
    # cursor document afterward.
    assert fm["observed_prior"] == {"schema": "1",
                                    "last_read": "2026-07-27T17:30:00Z"}
    assert fm["intended_authority"] == {"schema": "1"}
    assert "--consume" in fm["reason"]


def test_consume_audit_records_absent_observed_prior(monkeypatch, capsys):
    """A takeover of an agent with no cursor yet must say so: the observation
    is the bare classification ``absent``, not a fabricated empty claim."""
    t = _transport(window=[_event_rec("r1", "job")])
    assert _takeover(monkeypatch, t, "--consume") == 0
    _path, fm = _audit_doc(t)
    assert fm["observed_prior"] == "absent"
    assert fm["intended_authority"] == {"schema": "1"}


def test_consume_audit_records_v2_observation_and_intended_generation(
        monkeypatch, capsys):
    """Under an activated v2 authority the audit names the observed authority
    generation + per-agent revision and the generation the takeover intends
    to operate under — no predicted successor revision: a staged delivery may
    never commit, and a CAS loser adopts the winner's state."""
    t = ClassifiedCas([])
    t.put(records.config_path(TEAM), json.dumps(_config()))
    t.put(V2_CURSOR, _v2_doc())
    assert _takeover(monkeypatch, t, "--consume") == 0
    _path, fm = _audit_doc(t)
    assert fm["cursor"] == V2_CURSOR
    assert fm["observed_prior"] == {"schema": "2", "generation": "3",
                                    "revision": "0"}
    assert fm["intended_authority"] == {"schema": "2", "generation": "3"}
    assert "revision" not in fm["intended_authority"], \
        "a successor revision would be a prediction, not intent"


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
        # 600 r2: the envelope gained an explicit poison channel. `--json` used
        # to skip validation entirely, save the cursor, and then RAISE inside
        # the builder on a malformed event — consumed and invisible. Poison is
        # now delivered here with its reason, so a machine consumer can see what
        # it received and could not format. Empty on a clean window, and this
        # golden says so rather than letting the keys appear only under failure.
        "poison": [],
        "poison_count": 0,
        "cursor": {"path": CURSOR, "advanced": True},
        "engine_version": records.engine_stamp()["engine_version"],
        "protocol": None,                 # legacy authority: no versions to report
        # Round-2 finding 1: DATA skipped the fold too, so it says so. The
        # golden gains one key rather than staying silent about zero fold ops.
        "obligations": {"state": "not-checked"},
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


@pytest.mark.parametrize("argv", [[], ["--no-obligations"]])
@pytest.mark.parametrize("window,state", [
    ([], "CLEAR"),
    ([_event_rec("r1", "job")], "DATA"),
])
def test_skipped_fold_is_declared_never_silent(
        monkeypatch, capsys, argv, window, state):
    """A skipped fold is stated, not omitted — on BOTH terminal states.

    REVERSED 2026-08-03 (promise plan T3(b), reviewer round-2). This test used
    to assert ``"obligations" not in row`` — it codified SILENT skipping. With
    the fold now opt-in, silence would let automation read CLEAR as "nothing
    owed", which is the exact false inference the fold exists to prevent; the
    round-2 requirement is that the marker be universal on every
    machine-readable skipped path. ``--no-obligations`` remains an accepted
    no-op alias, so both spellings of "do not fold" carry the marker.

    EXTENDED for round-2 finding 1: DATA carries it too. A DATA envelope from
    a default read performed exactly zero fold ops, so omitting the key there
    left the one state where the marker is cheapest to add reporting nothing
    about coverage it never checked — and made the key's presence a proxy for
    "the window was empty" rather than for "the fold did not run".
    """
    t = _transport(window=window)
    rc, out, _err = _run(monkeypatch, capsys, t, ["--json", *argv])
    row = _single_json_object(out)
    assert rc == 0
    assert row["state"] == state
    assert row["obligations"] == {"state": "not-checked"}


@pytest.mark.parametrize("argv", [["--json"], ["--json", "--peek"]])
def test_default_json_read_declares_not_checked_and_folds_nothing(
        monkeypatch, capsys, argv):
    """The marker is universal across default machine-readable success paths.

    Non-vacuity for finding 1: the fold is booby-trapped, so a marker that
    appeared because the fold ran (and happened to answer) would fail here.
    """
    from coord_engine import obligations as obligations_mod

    monkeypatch.setattr(
        obligations_mod, "fold",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("a default read must perform zero fold ops")))
    t = _transport(window=[_event_rec("r1", "job")])
    rc, out, _err = _run(monkeypatch, capsys, t, argv)
    row = _single_json_object(out)
    assert rc == 0
    assert row["state"] == "DATA"
    assert row["obligations"] == {"state": "not-checked"}


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
    """CLEAR stays byte-identical on BOTH streams again.

    Flipped 2026-08-03 (promise plan T3(b)): fold-on-empty is opt-in, which
    reverses the 2026-07-30 slice-3 default and restores the pre-ruling bytes
    on the default read — stdout "" and stderr exactly the version warning.
    The measured grounds are in the plan: at the default budget the fold could
    only ever answer UNKNOWN in production, so every default wake paid for a
    signal with no information. The verdict is still available on demand
    (pinned below), and the machine-readable envelope always states that the
    fold was not checked.
    """
    t = _transport(window=[])
    rc, out, err = _run(monkeypatch, capsys, t, [])
    assert rc == 0
    assert out == "", "the stdout half of the golden contract is unchanged"
    assert err == GOLDEN_WARNING
    assert "obligations" not in err


@pytest.mark.parametrize("argv", [[], ["--no-obligations"]])
def test_plain_clear_with_no_obligations_is_byte_identical_to_pre_slice(
        monkeypatch, capsys, argv):
    """Opting out explicitly is now identical to the default — a no-op alias.

    ``--no-obligations`` is retained for compatibility with every caller
    already passing it (promise plan T3(b)); it must keep parsing and keep
    producing the same bytes as the plain read.
    """
    t = _transport(window=[])
    rc, out, err = _run(monkeypatch, capsys, t, argv)
    assert rc == 0
    assert out == ""
    assert err == GOLDEN_WARNING


def test_obligations_opt_in_still_reconciles_the_empty_read(
        monkeypatch, capsys):
    """The fold is not deleted, only unsubscribed from the default wake."""
    t = _transport(window=[])
    rc, out, err = _run(monkeypatch, capsys, t, ["--obligations"])
    assert rc == 0
    assert out == ""
    assert "obligations" in err


def test_plain_peek_output_byte_identical_to_pre_slice(monkeypatch, capsys):
    t = _transport(window=[_event_rec("r1", "hello")])
    rc, out, err = _run(monkeypatch, capsys, t, ["--peek"])
    assert rc == 0
    assert out == "2026-07-27T17:00:00 boss directive P2 hello -\n"
    assert err == GOLDEN_WARNING + GOLDEN_PEEK_NOTICE


# === every nonzero exit carries the envelope (review round 1, P1) ===========
#
# The table below enumerates EVERY nonzero-exit branch of the queue family —
# `queue` (legacy and v2-active) and `queue commit` — and asserts each one
# emits exactly one parseable queue-error object under --json. The AST gate
# at the end is the completeness check: a future branch that returns a bare
# nonzero constant instead of routing through _queue_failure fails it even
# before anyone writes its table row.

from test_records_transactional import CasTransport, _config  # noqa: E402

V2_CURSOR = records.v2_cursor_path(TEAM, AGENT, 3)
AUDIT_PREFIX = f"team/{TEAM}/{records.CONSUME_AUDIT_PREFIX}/"


class ClassifiedCas(CasTransport):
    """CasTransport that can simulate per-path transport read failures."""

    def __init__(self, *a, read_errors=(), **kw):
        super().__init__(*a, **kw)
        self.read_errors = tuple(read_errors)

    def read_classified(self, path):
        if path in self.read_errors:
            return None, "error"
        return super().read_classified(path)


class NoCas(ClassifiedCas):
    compare_and_swap = None


class UnknownWindowCas(ClassifiedCas):
    def records(self, data_type, since, until):
        return None


class VanishingCas(ClassifiedCas):
    """CAS present at the readiness probe, gone at stage time — the only way
    to reach the defensive stage-unsupported branch through the CLI."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._cas_probes = 0

    @property
    def compare_and_swap(self):
        self._cas_probes += 1
        if self._cas_probes == 1:
            return lambda path, expected_raw, new_raw: True
        return None


def _v2_doc(*, pending=None, last_read=None):
    return json.dumps({
        "v": 2, "authority_generation": 3, "revision": 0,
        "committed": {"last_read": last_read, "seen_ids": [],
                      "last_token": None, "committed_tokens": [],
                      "handled": []},
        "pending": pending,
    })


def _pending(token="tok", events=None):
    return {"token": token, "base_revision": 0,
            "staged_at": "2026-07-29T12:00:00Z",
            "window_start": "2026-07-29T11:00:00Z",
            "window_end": "2026-07-29T12:00:00Z",
            "events": [{"record_id": "r1"}] if events is None else events}


def _legacy_t(*, config=CONFIG, window=(), cursor=None, read_errors=(),
              fail_writes=()):
    t = LoggedTransport(window=None if window is None else list(window),
                        read_errors=read_errors, fail_writes=fail_writes)
    if config is not None:
        t.put(records.config_path(TEAM), config)
    if cursor is not None:
        t.put(records.cursor_path(TEAM, AGENT), cursor)
    return t


def _v2_t(cls=ClassifiedCas, *, config=None, window=(), v2_cursor=None,
          legacy_cursor=None, read_errors=(), reject_cas=False):
    t = cls(list(window), read_errors=read_errors)
    t.put(records.config_path(TEAM), json.dumps(config or _config()))
    if v2_cursor is not None:
        t.put(V2_CURSOR, v2_cursor)
    if legacy_cursor is not None:
        t.put(records.cursor_path(TEAM, AGENT), legacy_cursor)
    if reject_cas:
        t.reject_next_cas = True
    return t


READ = ["queue", TEAM, "--agent", AGENT, "--json"]
COMMIT = ["queue", "commit", TEAM, "--agent", AGENT, "--token", "tok",
          "--json"]

#: (branch, argv, transport factory, env identity, rc, state, error_code) —
#: one row per nonzero-exit branch of cmd_queue/_cmd_queue_v2/cmd_queue_commit.
ENVELOPE_BRANCHES = [
    # -- queue: top-level and legacy (schema v1) path
    ("usage-second-team", ["queue", TEAM, "extra", "--json"],
     lambda: _legacy_t(), None, 2, "REFUSED", "usage"),
    ("usage-no-agent", ["queue", TEAM, "--json"],
     lambda: _legacy_t(), None, 2, "REFUSED", "usage"),
    ("config-read-failed", READ,
     lambda: _legacy_t(read_errors=(records.config_path(TEAM),)),
     None, 3, "UNKNOWN", "config-read-failed"),
    ("config-invalid", READ,
     lambda: _legacy_t(config="{not json at all"),
     None, 3, "INVALID", "config-invalid"),
    ("config-absent", READ,
     lambda: _legacy_t(config=None), None, 2, "ABSENT", "config-absent"),
    ("reader-below-floor", READ,
     lambda: _legacy_t(config=json.dumps(_versioned_config(
         minimum_reader_version="9.0.0", minimum_writer_version="9.0.0"))),
     None, 3, "INCOMPATIBLE", "engine-incompatible"),
    ("consume-audit-failed", READ + ["--consume"],
     lambda: _legacy_t(window=[_event_rec("r1", "job")],
                       fail_writes=(AUDIT_PREFIX,)),
     "operator", 3, "UNKNOWN", "consume-audit-failed"),
    ("writer-below-floor-legacy", READ,
     lambda: _legacy_t(config=json.dumps(_versioned_config(
         minimum_writer_version="9.0.0"))),
     None, 3, "INCOMPATIBLE", "engine-incompatible"),
    ("cursor-read-failed", READ,
     lambda: _legacy_t(read_errors=(records.cursor_path(TEAM, AGENT),)),
     None, 3, "UNKNOWN", "cursor-read-failed"),
    ("cursor-invalid", READ,
     lambda: _legacy_t(cursor="not json at all"),
     None, 3, "INVALID", "cursor-invalid"),
    ("cursor-last-read-invalid", READ,
     lambda: _legacy_t(cursor='{"v":1,"last_read":"garbage","seen_ids":[]}'),
     None, 3, "INVALID", "cursor-invalid"),
    ("window-unknown", READ,
     lambda: _legacy_t(window=None), None, 3, "UNKNOWN", "window-unknown"),
    # -- queue: v2-active path
    ("v2-cursor-read-failed", READ,
     lambda: _v2_t(read_errors=(V2_CURSOR,)),
     None, 3, "UNKNOWN", "cursor-read-failed"),
    ("v2-cursor-invalid", READ,
     lambda: _v2_t(v2_cursor="not json at all"),
     None, 3, "INVALID", "cursor-invalid"),
    ("v2-legacy-seed-read-failed", READ,
     lambda: _v2_t(read_errors=(records.cursor_path(TEAM, AGENT),)),
     None, 3, "UNKNOWN", "legacy-cursor-read-failed"),
    ("v2-legacy-seed-invalid", READ,
     lambda: _v2_t(legacy_cursor="not json at all"),
     None, 3, "INVALID", "legacy-cursor-invalid"),
    ("v2-writer-below-floor", READ,
     lambda: _v2_t(config=_config(minimum_writer_version="9.0.0")),
     None, 3, "INCOMPATIBLE", "engine-incompatible"),
    ("v2-cas-unsupported", READ,
     lambda: _v2_t(NoCas), None, 3, "INCOMPATIBLE", "cas-unsupported"),
    ("v2-committed-time-invalid", READ,
     lambda: _v2_t(v2_cursor=_v2_doc(last_read="garbage")),
     None, 3, "INVALID", "cursor-invalid"),
    ("v2-event-id-missing", READ,
     lambda: _v2_t(window=[dict(_event_rec(None, "no-id"),
                                recorded_at="2026-07-29T11:30:00Z")]),
     None, 3, "INVALID", "event-id-missing"),
    ("v2-window-unknown", READ,
     lambda: _v2_t(UnknownWindowCas),
     None, 3, "UNKNOWN", "window-unknown"),
    ("v2-stage-cas-vanishes", READ,
     lambda: _v2_t(VanishingCas,
                   window=[dict(_event_rec("r1", "job"),
                                recorded_at="2026-07-29T11:30:00Z")]),
     None, 3, "INCOMPATIBLE", "cas-unsupported"),
    ("v2-stage-lost-unverified", READ,
     lambda: _v2_t(reject_cas=True,
                   window=[dict(_event_rec("r1", "job"),
                                recorded_at="2026-07-29T11:30:00Z")]),
     None, 3, "UNKNOWN", "stage-race-unverified"),
    # -- queue commit
    ("commit-usage-missing-token",
     ["queue", "commit", TEAM, "--agent", AGENT, "--json"],
     lambda: _v2_t(), None, 2, "REFUSED", "usage"),
    ("commit-usage-bad-result", COMMIT + ["--result", "r1=bogus"],
     lambda: _v2_t(), None, 2, "REFUSED", "usage"),
    ("commit-config-read-failed", COMMIT,
     lambda: _v2_t(read_errors=(records.config_path(TEAM),)),
     None, 3, "UNKNOWN", "config-read-failed"),
    ("commit-config-invalid", COMMIT,
     lambda: _legacy_t(config="{not json at all"),
     None, 3, "INVALID", "config-invalid"),
    ("commit-authority-not-v2", COMMIT,
     lambda: _legacy_t(), None, 3, "INCOMPATIBLE", "authority-not-v2"),
    ("commit-authority-absent", COMMIT,
     lambda: _legacy_t(config=None),
     None, 3, "INCOMPATIBLE", "authority-not-v2"),
    ("commit-writer-below-floor", COMMIT,
     lambda: _v2_t(config=_config(minimum_writer_version="9.0.0")),
     None, 3, "INCOMPATIBLE", "engine-incompatible"),
    ("commit-cursor-absent", COMMIT,
     lambda: _v2_t(), None, 3, "UNKNOWN", "cursor-absent"),
    ("commit-cursor-invalid", COMMIT,
     lambda: _v2_t(v2_cursor="not json at all"),
     None, 3, "INVALID", "cursor-invalid"),
    ("commit-cursor-read-failed", COMMIT,
     lambda: _v2_t(read_errors=(V2_CURSOR,)),
     None, 3, "UNKNOWN", "cursor-read-failed"),
    ("commit-stale-token", COMMIT + ["--result", "r1=completed"],
     lambda: _v2_t(v2_cursor=_v2_doc(pending=_pending(token="other"))),
     None, 3, "REFUSED", "stale-token"),
    ("commit-results-incomplete", COMMIT,
     lambda: _v2_t(v2_cursor=_v2_doc(pending=_pending())),
     None, 2, "REFUSED", "results-incomplete"),
    ("commit-event-id-missing", COMMIT,
     lambda: _v2_t(v2_cursor=_v2_doc(pending=_pending(events=[{}]))),
     None, 3, "INVALID", "event-id-missing"),
    ("commit-cas-unsupported", COMMIT + ["--result", "r1=completed"],
     lambda: _v2_t(NoCas, v2_cursor=_v2_doc(pending=_pending())),
     None, 3, "INCOMPATIBLE", "cas-unsupported"),
]


@pytest.mark.parametrize(
    "branch,argv,make_transport,identity,rc,state,error_code",
    ENVELOPE_BRANCHES, ids=[row[0] for row in ENVELOPE_BRANCHES])
def test_every_nonzero_json_exit_emits_one_error_envelope(
        monkeypatch, capsys, branch, argv, make_transport, identity, rc,
        state, error_code):
    if identity is None:
        monkeypatch.delenv("FULCRA_COORD_AGENT", raising=False)
    else:
        monkeypatch.setenv("FULCRA_COORD_AGENT", identity)
    _pin_clock(monkeypatch)
    got_rc = cli.main(argv, transport=make_transport())
    out = capsys.readouterr()
    assert got_rc == rc, f"{branch}: rc {got_rc} != {rc}"
    assert _single_json_object(out.out) == {
        "type": "queue-error", "state": state,
        "error_code": error_code, "rc": rc,
    }, f"{branch}: envelope mismatch"
    assert out.err.strip(), f"{branch}: stderr diagnostic missing"


def test_no_queue_branch_can_exit_nonzero_without_the_envelope():
    """Completeness gate for the table above: every nonzero exit of the queue
    family must be a ``return _queue_failure(...)``. A bare ``return 2`` or
    ``return 3`` added later fails here even before its table row exists.
    (argparse's own usage exits happen before any queue code runs and are
    outside this contract — see BUS-V3.md.)"""
    import ast
    import inspect
    import textwrap

    for fn in (cli.cmd_queue, cli._cmd_queue_v2, cli.cmd_queue_commit):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Return)
                    and isinstance(node.value, ast.Constant)
                    and node.value.value != 0):
                raise AssertionError(
                    f"{fn.__name__} line {node.lineno}: bare nonzero exit "
                    "bypasses the queue-error envelope; route it through "
                    "_queue_failure")
