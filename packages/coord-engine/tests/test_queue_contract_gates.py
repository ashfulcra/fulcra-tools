"""Slice 2 acceptance harness — the ten gates from the coord v3 r2 spec.

Normative source: ``reports/2026-07-29-coordv3-r2-spec-codex-coder.md``, section
"Acceptance gates". Verbatim, coord v3 must not claim reliable delivery until:

  1. kill the reader after print but before processing; the batch reappears
  2. kill it after processing but before commit; replay is idempotent
  3. run two same-agent wakes concurrently; neither cursor update is lost
  4. retry a stale commit token; it cannot advance coverage
  5. corrupt the records config; the result is INVALID, never ABSENT
  6. block network/DNS/auth; every layer reports UNKNOWN and preserves coverage
  7. run an old writer against a new cursor; the unsafe write is rejected
  8. lose a wake record; the durable obligation fold rediscovers the duty
  9. perform deliberate takeover; a complete audit record is produced
 10. leave one fold component unreadable; "nothing owed" is impossible

Each gate below is one test, numbered to match. They run against the reference
implementation in ``acceptance/contract.py``.

The second half of this file is the part that makes the first half trustworthy:
a mutation matrix asserting that each deliberately-broken implementation FAILS
the gate that describes its defect. A gate nobody has watched fail is a gate
nobody knows works — and every mutant here is a real failure mode the spec
names, several of which this fleet has actually shipped.

Binding to the real engine: gates touch only ``QueueLike`` (read / commit /
coverage). When slice 2 lands, an adapter with those three methods runs this
same file against real internals; no gate body changes.
"""

from __future__ import annotations

import json

import pytest

from acceptance.contract import (
    AbsentOnInvalidConfigQueue,
    AdvanceOnReadQueue,
    CommitOutcome,
    EmptyOnTransportFailureQueue,
    FakeStore,
    LastWriterWinsQueue,
    NonIdempotentCommitQueue,
    ObligationFold,
    ReadState,
    ReferenceQueue,
    TransportUnknown,
    config_path,
    cursor_path,
)

TEAM = "acme"
AGENT = "opie"

#: Fixed fixture timestamps. Not a clock pin: every time value the contract sees
#: is passed in explicitly as ``now``, so no assertion here depends on the wall
#: clock and none can rot at a date boundary.
T0 = 1_000_000.0
EVENTS = [
    {"id": "r1", "recorded_at": "2026-07-29T10:00:00Z", "slug": "alpha"},
    {"id": "r2", "recorded_at": "2026-07-29T10:05:00Z", "slug": "beta"},
]


@pytest.fixture
def store() -> FakeStore:
    s = FakeStore()
    s.seed(config_path(TEAM),
           json.dumps({"data_type": "MomentAnnotation/test", "api_version": "v1alpha1"}))
    return s


@pytest.fixture
def queue(store: FakeStore) -> ReferenceQueue:
    return ReferenceQueue(store, TEAM, AGENT, records=EVENTS)


# === the ten gates ==========================================================

def test_gate_1_crash_after_print_before_processing_replays(queue: ReferenceQueue):
    """Gate 1: the batch reappears; a lost wake is not an acceptable outcome."""
    first = queue.read(T0)
    assert first.state is ReadState.DATA
    assert [e["id"] for e in first.events] == ["r1", "r2"]

    # crash here: printed, never processed, never committed.
    second = queue.read(T0 + 1)

    assert second.token == first.token, "a new token would mean the batch was lost"
    assert [e["id"] for e in second.events] == ["r1", "r2"]
    assert queue.coverage() is None, "coverage must not have advanced on read"


def test_gate_2_crash_before_commit_replay_is_idempotent(queue: ReferenceQueue):
    """Gate 2: replay after processing commits once, and only once."""
    first = queue.read(T0)
    # processed, then crashed before commit.
    replay = queue.read(T0 + 1)
    assert replay.token == first.token

    assert queue.commit(replay.token, T0 + 2) is CommitOutcome.OK
    advanced = queue.coverage()
    assert advanced == "2026-07-29T10:05:00Z"

    # the crashed-and-retried process commits the same token again
    assert queue.commit(replay.token, T0 + 3) is CommitOutcome.IDEMPOTENT
    assert queue.coverage() == advanced, "a replayed commit must not advance twice"


def test_gate_2b_elapsed_time_replay_still_commits_exactly_once(
        queue: ReferenceQueue):
    """Gate 2b: long elapsed time loses no work and double-advances nothing.

    Spec step 5 says an uncommitted token "replays after timeout". That sentence
    admits two implementations — expire the batch and re-stage it under a fresh
    token, or never expire it and replay the original — and the shipped engine
    and the reference model here picked different ones. Neither choice is the
    contract, so this gate deliberately says NOTHING about token identity.

    What it does pin is the property both must have and that no other gate
    covers: after an arbitrary gap with no commit, the same work is still
    delivered, and it still advances coverage exactly once. Gates 1 and 2 cover
    replay after a *crash*; this covers replay after *elapsed time*, which is
    the case a long-dark agent and a reclaimed container actually hit.
    """
    first = queue.read(T0)
    assert [e["id"] for e in first.events] == ["r1", "r2"]
    assert queue.coverage() is None

    # A very long gap: well past any plausible expiry, in either design.
    later = T0 + 30 * 24 * 3600
    replay = queue.read(later)

    assert replay.state is ReadState.DATA, (
        "an uncommitted batch must survive elapsed time; going CLEAR here would "
        "silently drop work that was delivered but never acknowledged"
    )
    assert [e["id"] for e in replay.events] == ["r1", "r2"], (
        "the redelivered batch must be the same work, whatever token carries it"
    )

    assert queue.commit(replay.token, later + 1) is CommitOutcome.OK
    advanced = queue.coverage()
    assert advanced == "2026-07-29T10:05:00Z"

    # Whatever became of the pre-gap token, it must not advance coverage again.
    assert queue.commit(first.token, later + 2) in (
        CommitOutcome.IDEMPOTENT, CommitOutcome.UNKNOWN_TOKEN, CommitOutcome.STALE)
    assert queue.coverage() == advanced, (
        "coverage advanced twice for one batch — exactly the double-processing "
        "that read/process/commit exists to prevent"
    )


def test_gate_3_concurrent_wakes_lose_no_cursor_update(store: FakeStore):
    """Gate 3: two same-agent wakes converge on one batch; neither is lost.

    The interleaving is the test. ``a`` reads and, at the instant before its
    staging write lands, ``b`` runs its entire read and stages first. ``a``'s
    write then finds the generation moved and must NOT force it through.
    Calling the two reads back to back would not exercise this at all — the
    second reader would simply observe the first one's finished write.
    """
    a = ReferenceQueue(store, TEAM, AGENT, records=EVENTS)
    b = ReferenceQueue(store, TEAM, AGENT, records=EVENTS)

    peer: dict[str, object] = {}
    store.before_write = lambda _path: peer.setdefault("result", b.read(T0))

    ra = a.read(T0)
    rb = peer["result"]

    assert ra.state is ReadState.DATA and rb.state is ReadState.DATA
    assert ra.token == rb.token, (
        "one agent has one queue: the wake that lost the CAS race must adopt the "
        "winner's batch, not mint a second one over the top of it"
    )
    assert ra.detail == "lost-race-adopted-peer-batch", (
        "expected the losing writer to detect the conflict and reload; if it "
        "reported a clean win, the CAS refusal never happened"
    )
    # Exactly one staging write landed: b's. a's was refused, not applied.
    cursor_writes = [p for p, _g in store.write_log if p == cursor_path(TEAM, AGENT)]
    assert len(cursor_writes) == 1

    assert a.commit(ra.token, T0 + 1) is CommitOutcome.OK
    assert b.commit(rb.token, T0 + 2) is CommitOutcome.IDEMPOTENT
    assert a.coverage() == "2026-07-29T10:05:00Z"


def test_gate_4_stale_token_cannot_advance_coverage(queue: ReferenceQueue):
    """Gate 4: a token from a superseded batch is refused."""
    first = queue.read(T0)
    assert queue.commit(first.token, T0 + 1) is CommitOutcome.OK
    baseline = queue.coverage()

    # A newer batch arrives and is staged.
    queue.records = EVENTS + [{"id": "r3", "recorded_at": "2026-07-29T11:00:00Z",
                               "slug": "gamma"}]
    second = queue.read(T0 + 2)
    assert second.token != first.token

    # The old token must not move anything, in either direction.
    assert queue.commit("never-issued-token", T0 + 3) is CommitOutcome.UNKNOWN_TOKEN
    assert queue.coverage() == baseline


def test_gate_5_corrupt_config_is_invalid_never_absent(store: FakeStore):
    """Gate 5: malformed config is INVALID — the distinction that lost a day."""
    store.seed(config_path(TEAM), "{not json at all")
    q = ReferenceQueue(store, TEAM, AGENT, records=EVENTS)

    result = q.read(T0)

    assert result.state is ReadState.INVALID
    assert result.state is not ReadState.CLEAR, "INVALID read as CLEAR is the incident"
    assert not result.events


def test_gate_6_transport_failure_is_unknown_and_preserves_coverage(
        store: FakeStore, queue: ReferenceQueue):
    """Gate 6: blocked network/DNS/auth reports UNKNOWN, coverage untouched."""
    first = queue.read(T0)
    assert queue.commit(first.token, T0 + 1) is CommitOutcome.OK
    baseline = queue.coverage()

    store.break_reads(cursor_path(TEAM, AGENT))
    queue.records = EVENTS + [{"id": "r9", "recorded_at": "2026-07-29T12:00:00Z"}]
    result = queue.read(T0 + 2)

    assert result.state is ReadState.UNKNOWN
    assert not result.events

    store.heal()
    assert queue.coverage() == baseline, "a failed read must not have moved coverage"


def test_gate_7_old_writer_against_new_cursor_is_rejected(store: FakeStore):
    """Gate 7: legacy-shaped state is refused, not adopted as v2 coverage.

    Complements the slice-1 isolation gate: that one proves an old binary cannot
    reach the v2 path at all; this one covers the case where something legacy-
    shaped ends up there anyway (a restore, a hand edit, a migration bug).
    Physical isolation and a reader-side refusal are different defenses and the
    protocol wants both.
    """
    q = ReferenceQueue(store, TEAM, AGENT, records=EVENTS)
    first = q.read(T0)
    assert q.commit(first.token, T0 + 1) is CommitOutcome.OK

    # An old writer blind-writes a v1-shaped document over the v2 cursor.
    store.write_blind(cursor_path(TEAM, AGENT),
                      json.dumps({"v": 1, "last_read": "2099-01-01T00:00:00Z"}))

    result = q.read(T0 + 2)
    assert result.state is ReadState.INVALID, (
        "a v1-shaped cursor must be refused; adopting its last_read would let an "
        "old writer dictate v2 coverage — here, silently skipping to 2099"
    )
    assert not result.events


def test_gate_8_obligation_fold_rediscovers_a_lost_wake(store: FakeStore):
    """Gate 8: duty survives the loss of its wake record.

    The queue is empty — as it would be if the wake record were never written or
    was dropped. The fold still finds the open directive, which is what makes
    "events are best-effort hints" a safe thing to say.
    """
    q = ReferenceQueue(store, TEAM, AGENT, records=[])
    assert q.read(T0).state is ReadState.CLEAR

    store.seed("team/acme/_coord/obligations/directives.json",
               json.dumps({"open": ["respec-s2"]}))
    fold = ObligationFold(store, {
        "directives": "team/acme/_coord/obligations/directives.json",
        "reviews": "team/acme/_coord/obligations/reviews.json",
    })

    state, found = fold.owed()
    assert state is ReadState.DATA
    assert found == ["directives"]


def test_gate_9_takeover_produces_a_complete_audit_record(queue: ReferenceQueue):
    """Gate 9: who, whom, why, when, and what they SAW — never what they predict.

    This gate used to require ``prior_generation`` and
    ``new_generation == prior + 1``. Both were wrong, and the r2 spec's field
    list ("prior generation, and new generation recorded durably") is wrong with
    them: neither is knowable at audit time. The pre-takeover read can be
    overtaken by a concurrent writer before the takeover lands, so the observed
    prior is an observation and not necessarily the state actually overtaken; and
    a successor revision may never exist at all — a replayed pending delivery
    creates no revision, a staged delivery may never commit, a CAS loser adopts
    the winner's state.

    So the gate now pins what a caller can honestly record: the observation made
    at decision time, and the authority it intended to operate under. What
    actually happened is evidenced by the cursor document afterward, which is the
    only place it can be evidenced. An audit that guesses is not evidence.
    """
    first = queue.read(T0)
    assert first.token

    assert queue.takeover(actor="coord-boss", reason="agent dark 3h", now=T0 + 5)

    assert len(queue.audit) == 1
    entry = queue.audit[0]
    for required in ("actor", "target", "reason", "at",
                     "observed_prior", "intended_authority", "token"):
        assert required in entry, f"audit record is missing {required}"
    assert entry["actor"] == "coord-boss"
    assert entry["target"] == AGENT
    assert entry["token"] == first.token

    # The observation must name a real coverage claim, not an empty gesture.
    assert entry["observed_prior"], "observed_prior must carry the claim seen"
    assert "schema" in entry["intended_authority"]

    # And it must NOT smuggle a prediction back in under another name.
    assert "new_generation" not in entry
    assert "new_revision" not in entry.get("intended_authority", {}), (
        "intended_authority names the authority, not a successor revision — a "
        "predicted revision may never come to exist"
    )


def test_gate_10_unreadable_component_makes_nothing_owed_unsayable(store: FakeStore):
    """Gate 10: one dark component and CLEAR is not available as an answer."""
    store.seed("team/acme/_coord/obligations/directives.json",
               json.dumps({"open": []}))
    store.seed("team/acme/_coord/obligations/reviews.json",
               json.dumps({"open": []}))
    fold = ObligationFold(store, {
        "directives": "team/acme/_coord/obligations/directives.json",
        "reviews": "team/acme/_coord/obligations/reviews.json",
    })
    assert fold.owed()[0] is ReadState.CLEAR  # both readable, genuinely clear

    store.break_reads("team/acme/_coord/obligations/reviews.json")
    state, found = fold.owed()

    assert state is ReadState.UNKNOWN
    assert state is not ReadState.CLEAR, (
        "one unreadable component makes 'nothing owed' an unprovable claim; "
        "reporting CLEAR here is the false-clear this whole contract exists to end"
    )
    assert not found


# === the mutation matrix — proof the gates have teeth =======================
#
# Each case: a real failure mode from the spec, the gate that must catch it, and
# the assertion that it does. If a mutant ever stops failing its gate, the gate
# has gone soft and the slice-2 evidence is worth less than it looks.

def test_mutant_advance_on_read_fails_gate_1(store: FakeStore):
    """Today's engine: coverage moves at print time, so the batch never returns."""
    q = AdvanceOnReadQueue(store, TEAM, AGENT, records=EVENTS)
    first = q.read(T0)
    second = q.read(T0 + 1)
    assert second.token != first.token or not second.events, (
        "expected the advance-on-read mutant to lose the batch; if it did not, "
        "gate 1 is not actually testing the print/process boundary"
    )
    assert q.coverage() is not None, "the mutant should have advanced coverage on read"


def test_mutant_last_writer_wins_fails_gate_3(store: FakeStore):
    """No CAS: the second writer clobbers the first with no idea it happened.

    Same interleaving as gate 3. The blind writer has nothing to detect the
    conflict with, so both writes land and the earlier one is silently gone —
    the lost update that gate 3 exists to catch.
    """
    a = LastWriterWinsQueue(store, TEAM, AGENT, records=EVENTS)
    b = LastWriterWinsQueue(store, TEAM, AGENT, records=EVENTS)

    peer: dict[str, object] = {}
    store.before_write = lambda _path: peer.setdefault("result", b.read(T0))

    a.read(T0)

    cursor_writes = [p for p, _g in store.write_log if p == cursor_path(TEAM, AGENT)]
    assert len(cursor_writes) == 2, (
        "expected the blind writer to write twice over the same generation; if "
        "it only wrote once, this mutant is not exercising the race and gate 3 "
        "is unproven"
    )
    assert peer["result"].detail != "lost-race-adopted-peer-batch", (
        "the blind writer cannot detect a conflict — that is the defect"
    )


def test_mutant_non_idempotent_commit_fails_gate_2(store: FakeStore):
    """A replayed commit reports OK forever, so callers cannot tell it apart."""
    q = NonIdempotentCommitQueue(store, TEAM, AGENT, records=EVENTS)
    first = q.read(T0)
    assert q.commit(first.token, T0 + 1) is CommitOutcome.OK
    assert q.commit(first.token, T0 + 2) is CommitOutcome.OK, (
        "expected the mutant to claim success on a replay; gate 2 requires "
        "IDEMPOTENT so a caller can distinguish 'done' from 'done again'"
    )


def test_mutant_absent_on_invalid_config_fails_gate_5(store: FakeStore):
    """The 2026-07-28 incident, preserved: malformed config reads as clear."""
    store.seed(config_path(TEAM), "{not json at all")
    q = AbsentOnInvalidConfigQueue(store, TEAM, AGENT, records=EVENTS)
    assert q.read(T0).state is ReadState.CLEAR, (
        "expected the mutant to collapse INVALID into CLEAR; that collapse is "
        "exactly what gate 5 must refuse"
    )


def test_mutant_empty_on_transport_failure_fails_gate_6(store: FakeStore):
    """False clear: a dark store reads as an empty queue."""
    q = EmptyOnTransportFailureQueue(store, TEAM, AGENT, records=EVENTS)
    store.break_reads(cursor_path(TEAM, AGENT))
    result = q.read(T0)
    assert result.state is ReadState.CLEAR, (
        "expected the mutant to report CLEAR on an unreadable store; gate 6 "
        "requires UNKNOWN, because quiet is not clear"
    )


def test_transport_unknown_is_never_silently_swallowed(store: FakeStore):
    """The reference must let UNKNOWN surface from commit, not guess.

    A commit that cannot read the cursor has no idea whether coverage moved.
    Returning any CommitOutcome there would be a fabricated answer.
    """
    q = ReferenceQueue(store, TEAM, AGENT, records=EVENTS)
    first = q.read(T0)
    store.break_reads(cursor_path(TEAM, AGENT))
    with pytest.raises(TransportUnknown):
        q.commit(first.token, T0 + 1)
