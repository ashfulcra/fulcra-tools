"""respec s7 — supersession-adoption metric, deputy-corrected definition.

Exit-gate fixtures per the 2026-07-30 provisional ruling (slug respec-s7):
(1) candidates are directive→directive to the SAME recipient on the SAME slug
    — responses/threads NEVER count (measured live: 11/11 repeated
    sender+slug pairs in 24h were threads, not supersessions);
(2) an earlier directive already terminally classified completed/blocked is
    follow-up work, not a candidate;
(3) the explicit `task supersede` signal is counted directly;
(4) unmeasurable is UNKNOWN — never the denominator, never 0%;
plus: empty denominator reads n/a (never 100%), legacy windows read UNKNOWN
(never 0%), and the --json outcome_mix key is additive under the cursor block
(legacy envelope byte-identical).
"""

import json

from coord_engine import cli, records


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


def test_explicit_supersede_verb_counts_directly():
    events = [_ev("directive", "a", "s", "d1", "2026-07-30T10:00:00"),
              _ev("directive", "a", "s", "d2", "2026-07-30T11:00:00")]
    out = records.supersession_adoption(events, {}, explicit_ids={"d1"})
    assert (out["counted"], out["superseded"]) == (1, 1)


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
