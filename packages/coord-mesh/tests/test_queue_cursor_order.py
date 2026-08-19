"""The queue cursor must anchor to the NEWEST row, and the platform returns OLDEST first.

FIELD OBSERVATION (coord-boss, 2026-08-18 22:16Z): a tick's `mesh queue` returned
the complete known 24h backlog — 8 events, all previously handled — after eight
consecutive 0-event reads between 15:45Z and 21:13Z. They offered two candidate
explanations, designed retention overlap or a seen-id eviction edge. It is
neither.

MEASURED: `fulcra-api get-records` returns rows in ASCENDING recorded_at order,
oldest first. Verified on a 203-row, 12-hour read of the live channel — strictly
ascending, not descending.

`cmd_queue` walked those rows doing `newest = newest or rid`, which takes
``rows[0]``. Under ascending order that is the OLDEST row in the window, so the
variable named `newest` held the oldest id and the cursor was anchored to the
wrong end of the window. Two symptoms follow, and only one of them is visible:

  - THE LOUD ONE: when the cursor row finally ages out of the window, `rid ==
    cursor` never matches, the loop never breaks, and every addressed event in
    the window is shown again. That is the 8-event backlog.
  - THE SILENT ONE, which is worse: between those replays, every read breaks at
    ``rows[0]`` immediately, because the cursor IS ``rows[0]``. The verb prints
    "0 event(s)" and exits 0 while real addressed events sit unshown further
    down the same window. A package whose thesis is that a read must never
    report quiet when it means blind was doing exactly that.

Re-delivery under at-least-once is legal and stays legal. Silently showing
nothing is not, and a replay that cannot say why it replayed is not either.
"""
import pytest

from coord_mesh import cli, envelope

UID = "a24a9667-c2c6-4bbf-9a0f-36ea0afcb521"
MINE = "d64bbe9b-4902-42e9-a607-7db51ebc6379"
CH = "MomentAnnotation/d04f357e-b556-4298-ad1e-4ce307d54041"


def _row(rid, slug, when):
    """A row with a REAL timestamp.

    The first version of this file passed ``recorded_at=rid`` — "r1", "r2",
    "r3". codex-coder caught it on 051109f: those fixtures hard-code the order
    the author wanted and encode no time at all, so they could not have caught
    transport-order drift, which is the very thing the cursor depends on. The
    fixtures now carry the shape the platform actually emits.
    """
    import json
    return {"id": rid, "recorded_at": when, "sources": ["peer"],
            "note": json.dumps(envelope.build(to_user=MINE, kind="response",
                                              slug=slug))}


T1 = "2026-08-18T20:00:00+00:00"
T2 = "2026-08-18T21:00:00.500000+00:00"   # microseconds: both forms occur live
T3 = "2026-08-18T22:00:00+00:00"

#: Ascending, oldest first — the order the platform returned when measured.
ROWS = [_row("r1", "oldest", T1), _row("r2", "middle", T2),
        _row("r3", "newest", T3)]


@pytest.fixture
def registry(monkeypatch, tmp_path):
    """Real cursor read/write against a temp registry, so the test exercises
    the round trip rather than a stub's idea of it."""
    store = {"spaces": {}}
    monkeypatch.setattr(cli.peers, "load", lambda *a, **k: store)
    monkeypatch.setattr(cli.peers, "save", lambda *a, **k: None)
    return store


def _run(monkeypatch, rows, extra=()):
    monkeypatch.setattr(cli.transport, "get_records",
                        lambda *a, **k: cli.transport.Result(
                            cli.transport.OK, rows=list(rows)))
    return cli.main(["--channel", CH, "queue", "--me", MINE, "--peer", UID,
                     *extra])


def test_cursor_anchors_to_the_newest_row_not_the_oldest(capsys, monkeypatch, registry):
    """THE REGRESSION. A first read shows everything and remembers the NEWEST id."""
    assert _run(monkeypatch, ROWS) == cli.RC_OK
    assert "3 event(s)" in capsys.readouterr().out
    assert cli.peers.get_cursor(registry, "default", UID) == "r3", (
        "cursor anchored to the wrong end of the window — this is the defect "
        "that produced the 8-event replay and the silent 0-event reads"
    )


def test_events_after_the_cursor_are_shown_not_swallowed(capsys, monkeypatch, registry):
    """THE SILENT SYMPTOM: with the cursor at the oldest row, the old loop broke
    at rows[0] and reported 0 while two addressed events sat below it."""
    cli.peers.set_cursor(registry, "default", UID, "r1")
    assert _run(monkeypatch, ROWS) == cli.RC_OK
    out = capsys.readouterr().out
    assert "2 event(s)" in out, out
    assert "middle" in out and "newest" in out
    assert "oldest" not in out, "the cursor row itself must not be re-shown"


def test_nothing_new_really_means_nothing_new(capsys, monkeypatch, registry):
    cli.peers.set_cursor(registry, "default", UID, "r3")
    assert _run(monkeypatch, ROWS) == cli.RC_OK
    assert "0 event(s)" in capsys.readouterr().out


def test_a_cursor_that_aged_out_replays_the_window_and_SAYS_SO(capsys, monkeypatch, registry):
    """Re-delivery is legal under at-least-once; replaying in silence is not.

    This is the case coord-boss hit. The events are shown again — correct — but
    the operator must be told the cursor was not in the window, or the only way
    to learn it is an investigation like the one that produced this test."""
    cli.peers.set_cursor(registry, "default", UID, "aged-out-id")
    assert _run(monkeypatch, ROWS) == cli.RC_OK
    cap = capsys.readouterr()
    assert "3 event(s)" in cap.out
    assert "aged out" in cap.err or "not in the window" in cap.err, cap.err


def test_no_advance_leaves_the_cursor_alone(monkeypatch, registry):
    cli.peers.set_cursor(registry, "default", UID, "r1")
    _run(monkeypatch, ROWS, extra=["--no-advance"])
    assert cli.peers.get_cursor(registry, "default", UID) == "r1"


def test_an_unidentifiable_row_stops_the_whole_peer_not_just_that_row(capsys, monkeypatch, registry):
    """DELIBERATE BEHAVIOUR CHANGE, pinned here so it is not mistaken for drift.

    The previous loop skipped an id-less row and kept folding the rest. But the
    cursor is a POSITION in an ordered stream: if one row cannot be identified,
    we do not know where the rows after it sit relative to it, so any slice we
    show is a claim about position we cannot support. The peer degrades to
    UNKNOWN instead — the same rule the rest of this package follows, applied to
    the one place that was still guessing."""
    cli.peers.set_cursor(registry, "default", UID, "r1")
    rows = [ROWS[0], {"recorded_at": "r2", "sources": [], "note": "{}"}, ROWS[2]]
    assert _run(monkeypatch, rows) == cli.RC_UNKNOWN
    cap = capsys.readouterr()
    assert "cannot be placed" in cap.err and "missing id" in cap.err
    assert "UNREADABLE" in cap.err and "not empty" in cap.err
    # And the cursor must not have moved past a stream we could not read.
    assert cli.peers.get_cursor(registry, "default", UID) == "r1"


def test_advance_lands_on_the_last_row_after_a_replay(monkeypatch, registry):
    """After an aged-out replay the cursor must land on the NEWEST row, so the
    next read is quiet for the right reason rather than by accident."""
    cli.peers.set_cursor(registry, "default", UID, "aged-out-id")
    _run(monkeypatch, ROWS)
    assert cli.peers.get_cursor(registry, "default", UID) == "r3"


# --- the order is PROVEN per read, never assumed (codex-coder on 051109f) ----

# DELIBERATE CHANGE OF CONTRACT (codex-coder on bd747f37). The previous round
# degraded a descending or disordered window to UNKNOWN. That was still letting
# the transport decide the order and merely refusing when it looked wrong — and
# it did not help at all for TIED timestamps, where every permutation looked
# fine and the last of the tied group was an arbitrary anchor. We now sort by a
# total key we own, (recorded_at, id), so a transport-order change cannot
# choose the cursor. These tests assert the new contract.

def test_a_descending_response_is_sorted_not_silently_anchored(capsys, monkeypatch, registry):
    cli.peers.set_cursor(registry, "default", UID, "r1")
    assert _run(monkeypatch, list(reversed(ROWS))) == cli.RC_OK
    out, err = capsys.readouterr().out, capsys.readouterr().err
    assert cli.peers.get_cursor(registry, "default", UID) == "r3", \
        "cursor must anchor to the true newest row regardless of transport order"


def test_a_descending_response_is_reported_even_though_it_is_handled(capsys, monkeypatch, registry):
    """Handled is not the same as unremarkable: a transport whose order changed
    is worth knowing about before something else in the fleet assumes the old
    one."""
    _run(monkeypatch, list(reversed(ROWS)))
    assert "transport order differs" in capsys.readouterr().err


def test_an_internally_disordered_response_is_sorted(capsys, monkeypatch, registry):
    """One row out of place — the case a spot check misses."""
    assert _run(monkeypatch, [ROWS[0], ROWS[2], ROWS[1]]) == cli.RC_OK
    assert cli.peers.get_cursor(registry, "default", UID) == "r3"


def test_tied_timestamps_get_a_deterministic_anchor(capsys, monkeypatch, registry):
    """THE r2 FINDING. Equal timestamps are a PARTIAL order: every permutation
    of a tied group passed the old monotonic check, so the last row of that
    group — and therefore the cursor — was whatever the transport happened to
    emit last. The id breaks the tie deterministically, so both orderings of the
    same tied pair must anchor to the same record."""
    a = _row("id-aaa", "first", T2)
    b = _row("id-bbb", "second", T2)      # same instant, different id
    _run(monkeypatch, [a, b])
    one = cli.peers.get_cursor(registry, "default", UID)
    registry["spaces"] = {}
    _run(monkeypatch, [b, a])             # transport emits the tie the other way
    two = cli.peers.get_cursor(registry, "default", UID)
    assert one == two == "id-bbb", (one, two)


def test_a_row_with_no_timestamp_is_UNKNOWN(capsys, monkeypatch, registry):
    """An absent recorded_at must not sort first or last; both are guesses."""
    rows = [ROWS[0], {"id": "r2", "sources": ["peer"], "note": "{}"}, ROWS[2]]
    assert _run(monkeypatch, rows) == cli.RC_UNKNOWN


def test_an_unparseable_timestamp_is_UNKNOWN(capsys, monkeypatch, registry):
    rows = [ROWS[0], _row("r2", "middle", "not-a-time"), ROWS[2]]
    assert _run(monkeypatch, rows) == cli.RC_UNKNOWN
    assert "cannot be placed" in capsys.readouterr().err


def test_a_timezone_naive_timestamp_is_UNKNOWN(capsys, monkeypatch, registry):
    """codex-coder on bd747f37: a naive timestamp does not identify an absolute
    instant, and the old parse assumed UTC — a silent guess that reorders rows
    against a peer running anywhere else."""
    rows = [ROWS[0], _row("r2", "middle", "2026-08-18T21:00:00"), ROWS[2]]
    assert _run(monkeypatch, rows) == cli.RC_UNKNOWN
    assert "timezone-naive" in capsys.readouterr().err


def test_equal_timestamps_do_not_degrade_a_healthy_read(capsys, monkeypatch, registry):
    """Ties are ordered, not refused: two records really can share a second, and
    refusing them would degrade a healthy read."""
    rows = [_row("r1", "a", T1), _row("r2", "b", T1), _row("r3", "c", T2)]
    assert _run(monkeypatch, rows) == cli.RC_OK
    assert cli.peers.get_cursor(registry, "default", UID) == "r3"


def test_a_single_row_window_is_trivially_ordered(monkeypatch, registry):
    assert _run(monkeypatch, [ROWS[0]]) == cli.RC_OK
    assert cli.peers.get_cursor(registry, "default", UID) == "r1"
