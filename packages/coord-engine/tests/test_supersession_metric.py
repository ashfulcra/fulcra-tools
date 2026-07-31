"""respec s7 — supersession-adoption metric, deputy-corrected definition.

Exit-gate fixtures per the 2026-07-30 provisional ruling (slug respec-s7):
(1) candidates are directive→directive to the SAME recipient on the SAME slug
    — responses/threads NEVER count (measured live: 11/11 repeated
    sender+slug pairs in 24h were threads, not supersessions);
(2) an earlier directive already terminally classified completed/blocked is
    follow-up work, not a candidate;
(3) the explicit record-level signal (`queue commit --result <id>=superseded`)
    is counted directly — NARROWED in pr-503 round 1 from "the `task
    supersede` verb is counted directly": that verb's evidence lives in task
    documents keyed by task slug with no task→record identity mapping, so
    the fold must not expose a channel no production caller can fill;
(4) unmeasurable is UNKNOWN — never the denominator, never 0%;
plus: empty denominator reads n/a (never 100%), legacy windows read UNKNOWN
(never 0%), the --json outcome_mix key is additive under the cursor block
(legacy envelope byte-identical), and doctor keeps the fold's evidence
UNKNOWN until at least one fleet cursor actually READS ok (empty census,
failed reads, and activation alone are not evidence — pr-503 round 1).
"""

import json
import shutil
from datetime import datetime, timezone

from coord_engine import cli, records
from coord_engine_test_helpers import FakeTransport


def _ev(kind, to, slug, rid, at):
    return {"kind": kind, "to": to, "slug": slug, "record_id": rid,
            "recorded_at": at, "from": "boss", "priority": "P1", "ptr": None}


def test_directive_reissue_with_superseded_classification_counts():
    events = [_ev("directive", "a", "s", "d1", "2026-07-30T10:00:00"),
              _ev("directive", "a", "s", "d2", "2026-07-30T11:00:00")]
    out = records.supersession_adoption(events, {"d1": "superseded"})
    assert (out["counted"], out["superseded"], out["unknown"]) == (1, 1, 0)
    assert out["ratio"] == 1.0


def test_threads_are_not_candidates():
    # Opie's measured case: responses on a reused slug are a THREAD.
    events = [_ev("response", "boss", "respec-s3", f"r{i}",
                  f"2026-07-30T1{i}:00:00") for i in range(4)]
    # and directives on the same slug to DIFFERENT recipients don't pair
    events += [_ev("directive", "a", "x", "da", "2026-07-30T10:00:00"),
               _ev("directive", "b", "x", "db", "2026-07-30T11:00:00")]
    out = records.supersession_adoption(events, {})
    assert (out["counted"], out["unknown"]) == (0, 0)
    assert out["ratio"] is None                      # n/a, never 100%


def test_terminally_classified_predecessor_is_followup_not_candidate():
    events = [_ev("directive", "a", "s", "d1", "2026-07-30T10:00:00"),
              _ev("directive", "a", "s", "d2", "2026-07-30T11:00:00")]
    for terminal in ("completed", "blocked"):
        out = records.supersession_adoption(events, {"d1": terminal})
        assert (out["counted"], out["unknown"]) == (0, 0)


def test_task_verb_channel_is_narrowed_out_of_the_fold():
    """pr-503 round 1 pin: `task supersede` writes `superseded_by` into TASK
    documents keyed by task slug — no identity mapping to event record ids
    exists, so the fold must not accept explicit ids no production caller
    can gather. The explicit record-level signal is the `superseded`
    classification itself (covered by
    test_directive_reissue_with_superseded_classification_counts). Delete
    this pin only when a defined, test-covered task→record mapping wires
    the task-verb channel end to end."""
    import inspect
    params = inspect.signature(records.supersession_adoption).parameters
    assert "explicit_ids" not in params


def test_unclassified_reissue_is_unknown_not_denominator():
    events = [_ev("directive", "a", "s", "d1", "2026-07-30T10:00:00"),
              _ev("directive", "a", "s", "d2", "2026-07-30T11:00:00")]
    out = records.supersession_adoption(events, {})   # no evidence for d1
    assert (out["counted"], out["unknown"]) == (0, 1)
    assert out["ratio"] is None


def test_ignored_reissue_lands_in_denominator_without_adoption():
    events = [_ev("directive", "a", "s", "d1", "2026-07-30T10:00:00"),
              _ev("directive", "a", "s", "d2", "2026-07-30T11:00:00")]
    out = records.supersession_adoption(events, {"d1": "ignored"})
    assert (out["counted"], out["superseded"]) == (1, 0)
    assert out["ratio"] == 0.0


def test_legacy_window_is_unknown_never_zero():
    events = [_ev("directive", "a", "s", "d1", "2026-07-30T10:00:00"),
              _ev("directive", "a", "s", "d2", "2026-07-30T11:00:00")]
    out = records.supersession_adoption(events, None)  # no v2 evidence at all
    assert out["status"] == "unknown"
    assert out["ratio"] is None and out["counted"] == 0


def test_outcome_mix_from_v2_cursor_and_absent_cases():
    cursor = {"committed": {"handled": [
        {"record_id": "a", "outcome": "completed", "token": "t"},
        {"record_id": "b", "outcome": "superseded", "token": "t"},
        {"record_id": "c", "outcome": "superseded", "token": "t2"},
    ]}}
    mix = records.outcome_mix(cursor)
    assert mix == {"completed": 1, "blocked": 0, "superseded": 2, "ignored": 0}
    assert records.outcome_mix(None) is None
    assert records.outcome_mix({"committed": {"handled": []}}) is None


def test_envelope_outcome_mix_is_additive_only():
    """Legacy envelope (no mix) must be byte-identical to the pre-s7 shape;
    the v2 envelope gains exactly one key under cursor."""
    base = cli._queue_result_envelope(
        [], cfg={}, cursor_path="p", advanced=True)
    assert set(base["cursor"]) == {"path", "advanced"}
    withmix = cli._queue_result_envelope(
        [], cfg={}, cursor_path="p", advanced=True,
        outcome_mix={"completed": 1, "blocked": 0, "superseded": 0,
                     "ignored": 0})
    assert set(withmix["cursor"]) == {"path", "advanced", "outcome_mix"}
    # everything else identical
    for k in base:
        if k != "cursor":
            assert base[k] == withmix[k]
    json.dumps(withmix)  # envelope stays JSON-serializable


# --- doctor-level trusted-empty gates (pr-503 round 1) -----------------------
# Activation alone is not evidence: doctor must keep the fold UNKNOWN until at
# least one fleet cursor actually READS ok. Empty census = no sources; failed
# reads = unreadable evidence; a readable empty cursor IS evidence (n/a).

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def _v2_config():
    return {
        "data_type": "MomentAnnotation/x",
        "api_version": "v1alpha1",
        "protocol_version": 1,
        "cursor_schema_version": 2,
        "minimum_reader_version": "1.9.0",
        "minimum_writer_version": "1.9.0",
        "cursor_generation": 3,
        "cursor_activated_at": "2026-07-30T09:00:00Z",
    }


def _record(rid, slug, at):
    return {"id": rid, "recorded_at": at, "sources": ["boss"],
            "note": records.build_payload(
                to="amy", kind="directive", priority="P1", slug=slug)}


class _DoctorTransport(FakeTransport):
    """FakeTransport + the classified-read/records/CAS surface doctor needs."""

    def __init__(self, window=None):
        super().__init__()
        self.window = [] if window is None else window

    def read_classified(self, path):
        value = self.read(path)
        return (value, "ok") if value is not None else (None, "absent")

    def records(self, data_type, since, until):
        return self.window

    def compare_and_swap(self, path, expected_raw, new_raw):
        if self.read(path) != expected_raw:
            return False
        self.put(path, new_raw)
        return True


def _doctor_setup(monkeypatch, t, *, presence_for=()):
    t.put(records.config_path("r"), json.dumps(_v2_config()))
    for agent in presence_for:
        t.put(f"team/r/presence/{agent}.md",
              f"---\nagent: {agent}\ntimestamp: 2026-07-30T11:55:00Z\n---\n")
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    monkeypatch.setattr(shutil, "which", lambda _name: "/bin/fulcra-api")
    return t


def _doctor_out(capsys, t):
    rc = cli.main(["doctor", "r"], transport=t)
    return rc, capsys.readouterr().out


def test_doctor_empty_census_reads_unknown_not_na(monkeypatch, capsys):
    """v2 active but ZERO census agents: no cursor was (or could be) read, so
    the fold has no evidence source at all — UNKNOWN, never n/a."""
    t = _doctor_setup(monkeypatch, _DoctorTransport([]))
    rc, out = _doctor_out(capsys, t)
    assert rc == 0
    assert "Supersession adoption: UNKNOWN" in out
    assert "n/a" not in out


def test_doctor_failed_cursor_reads_stay_unknown(monkeypatch, capsys):
    """v2 active, census populated, but every cursor read fails: unreadable
    evidence is NOT an empty classification set — UNKNOWN, never n/a."""
    class CursorReadFails(_DoctorTransport):
        def read_classified(self, path):
            if path == records.v2_cursor_path("r", "amy", 3):
                return None, "error"
            return super().read_classified(path)

    t = _doctor_setup(
        monkeypatch,
        CursorReadFails([_record("d1", "s", "2026-07-30T10:00:00Z"),
                         _record("d2", "s", "2026-07-30T11:00:00Z")]),
        presence_for=("amy",))
    rc, out = _doctor_out(capsys, t)
    assert rc == 0
    assert "Supersession adoption: UNKNOWN" in out
    assert "n/a" not in out


def test_doctor_readable_empty_cursor_is_evidence(monkeypatch, capsys):
    """A cursor that READS ok with zero handled rows IS successfully read
    evidence: the fold runs, the unclassified reissue lands in the unknown
    bucket, and the empty denominator reads n/a — not UNKNOWN, not 100%."""
    t = _doctor_setup(
        monkeypatch,
        _DoctorTransport([_record("d1", "s", "2026-07-30T10:00:00Z"),
                          _record("d2", "s", "2026-07-30T11:00:00Z")]),
        presence_for=("amy",))
    t.put(records.v2_cursor_path("r", "amy", 3), json.dumps({
        "v": 2, "authority_generation": 3, "revision": 0,
        "committed": {"last_read": "2026-07-30T09:00:00Z", "seen_ids": [],
                      "committed_tokens": [], "handled": []}}))
    rc, out = _doctor_out(capsys, t)
    assert rc == 0
    assert "Supersession adoption: n/a (0 candidates; 1 unmeasurable)" in out
