"""`linear-assignments` routes Linear changes to the fleet, and can never write.

Three properties are under test, in descending order of what they would cost if
they broke.

1. NOTHING REACHES LINEAR BUT A QUERY. Same rail as `linear-inbox`, and it is
   tested the same way: not "we do not send mutations" but "a mutation cannot
   be sent", asserted on the transport that would actually carry it.

2. NO DELIVERY IS EVER GUESSED. A name that does not resolve, resolves to more
   than one identity, or resolves to a peer the roster says is unreachable by
   `tell` goes to the coordinator for triage. The roster itself failing to load
   is UNKNOWN, not "nobody resolves" — the difference is a confident triage
   verdict on every card versus an honest "I could not read the roster".

3. THE WATERMARK NEVER SKIPS. It may repeat — delivery is at-least-once and the
   directive says so — but a row that was never delivered is never behind the
   mark. Every failure path here is checked for what it leaves owed, because
   the two transport splits this fleet has already paid for were both a
   watermark that moved past work that had not happened.

Hermetic: no network, no coord-engine, no store. Every boundary is a fake that
records what it was asked to do.
"""

import json

import pytest

from coord_tracker_bridge.assignments import (
    AMBIGUOUS,
    NEW,
    NOT_ADDRESSABLE,
    POSSIBLE_REPEAT,
    REPEAT,
    RESOLVED,
    UNASSIGNED,
    UNRESOLVED,
    AssignmentState,
    AssignmentsUnknown,
    Candidate,
    Cursor,
    DispatchFailed,
    EngineTellDispatcher,
    FulcraRosterReader,
    Route,
    RosterUnreadable,
    StateUnreadable,
    advance,
    build_client,
    directive_body,
    fingerprint,
    parse_roster,
    parse_timestamp,
    plan_assignments,
    render_plan,
    run_assignments,
    seed,
)
from coord_tracker_bridge.inbox import InboxItem, ReadOnlyTransport, WriteRefused
from coord_tracker_bridge.linear import GraphQLResponse, LinearClient

TEAM = "team-abc"

#: The roster document as it stands in the coord store, reproduced for its
#: SHAPE. The parser is written against this exact table layout, so a tidier
#: imaginary table would pin a contract the real file does not honour.
#:
#: The notes column is neutralised. It carries a machine name in the real
#: document, and coord-boss ruled on that class of value one day before this
#: file was written: agent labels are borderline, a hostname fragment is over
#: the line for a PUBLIC repo. The notes column is not read by the parser, so
#: nothing under test depends on the words removed.
ROSTER = """# Fleet roster — nickname resolution

| Nickname (Ash) | Agent name (bus address) | Notes |
|---|---|---|
| Tycho | coord-boss | persistent fleet coordinator (cloud session) |
| Fabio | coord-fable-worker | reviewer/worker |
| Opie | coord-opus-worker | worker; Linear lane (2026-08) |
| collect maintainer | collect-maintainer | host-resident |
| infra maintainer / home-network maintainer | home-network-maintainer | one-shot wakes |

External mesh peers (NOT fleet agents — reachable only via mesh outbox
channels, never via coord-engine tell):

| Name | Identity | Address |
|---|---|---|
| Leif / Treecle | leifs-agent, uid c7c8bf86 | his mesh outbox channel |
| Webster | Fulcra Webflow agent, uid c936a72a | his mesh outbox channel |
"""


def _item(ident, *, who=None, state="Todo", when="2026-08-19T12:00:00.000Z",
          title=None, url=None, state_present=True):
    return InboxItem(
        identifier=ident,
        title=title or f"card {ident}",
        state=state,
        state_type="unstarted",
        assignee=who,
        labels=(),
        url=url if url is not None else f"https://linear.app/x/issue/{ident}",
        updated_at=when,
        state_present=state_present,
    )


def _node(ident, *, who=None, state="Todo", when="2026-08-19T12:00:00.000Z"):
    return {
        "id": "issue-" + ident, "identifier": ident, "title": f"card {ident}",
        "url": f"https://linear.app/x/issue/{ident}", "updatedAt": when,
        "state": {"name": state, "type": "unstarted"},
        "assignee": ({"displayName": who} if who else None),
        "labels": {"nodes": []},
    }


class FakeTransport:
    def __init__(self, pages):
        self.pages = list(pages)
        self.posted = []

    def post(self, payload):
        self.posted.append(dict(payload))
        return GraphQLResponse(200, self.pages.pop(0), {})


def _page(nodes, *, has_next=False, cursor=None):
    return {"data": {"issues": {"nodes": nodes,
                                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}}}}


class FakeDispatcher:
    def __init__(self, fail_on=()):
        self.sent = []
        self.fail_on = set(fail_on)

    def deliver(self, route):
        if route.identifier in self.fail_on:
            raise DispatchFailed(f"boom on {route.identifier}")
        self.sent.append(route)


def _roster():
    return parse_roster(ROSTER)


# --- 1. THE HARD RAIL -----------------------------------------------------

def test_the_client_this_verb_builds_cannot_send_a_mutation():
    inner = FakeTransport([_page([])])
    client = build_client("key", lambda _key: inner)
    with pytest.raises(WriteRefused):
        client.transport.post({"query": "mutation X { issueUpdate(id:1){success} }"})
    assert inner.posted == [], "nothing may reach the network"


def test_a_full_run_posts_only_query_documents(tmp_path):
    inner = FakeTransport([_page([_node("ENG-1", who="Opie")])])
    client = LinearClient(ReadOnlyTransport(inner))
    dispatcher = FakeDispatcher()
    run_assignments(
        client, team_id=TEAM, state_path=tmp_path / "s.json",
        roster_reader=lambda: ROSTER, coordinator="coord-boss",
        dispatcher=dispatcher, do_seed=True,
    )
    assert inner.posted, "the run must actually have read the board"
    for payload in inner.posted:
        assert payload["query"].lstrip().startswith("query "), payload["query"][:40]


# --- 2. TIME --------------------------------------------------------------

def test_a_naive_timestamp_is_unusable_not_assumed_utc():
    """The coord-mesh lesson. An assumed offset silently reorders rows against
    an aware watermark, and rows that sort wrong are rows that get skipped."""
    assert parse_timestamp("2026-08-19T12:00:00") is None


def test_z_and_offset_timestamps_are_equal_when_they_are_the_same_instant():
    assert parse_timestamp("2026-08-19T12:00:00Z") == parse_timestamp(
        "2026-08-19T14:00:00+02:00")


@pytest.mark.parametrize("value", [None, 7, "", "   ", "not a time", ["2026"]])
def test_unusable_timestamps_are_none(value):
    assert parse_timestamp(value) is None


# --- 3. THE CURSOR --------------------------------------------------------

def test_a_cold_cursor_is_after_everything():
    assert Cursor().is_after(parse_timestamp("2020-01-01T00:00:00Z"), "A-1")


def test_the_id_ledger_makes_the_boundary_exact():
    """A bare timestamp cannot express 'three of the four rows stamped 12:00'."""
    mark = parse_timestamp("2026-08-19T12:00:00Z")
    cursor = Cursor(t=mark, ids=frozenset({"A-1", "A-2"}))
    assert not cursor.is_after(mark, "A-1"), "already seen at the mark"
    assert cursor.is_after(mark, "A-3"), "at the mark but never seen"
    assert not cursor.is_after(parse_timestamp("2026-08-19T11:00:00Z"), "A-9")
    assert cursor.is_after(parse_timestamp("2026-08-19T13:00:00Z"), "A-1")


# --- 4. DURABLE STATE -----------------------------------------------------

def test_an_absent_state_file_is_a_genuine_cold_start(tmp_path):
    state = AssignmentState.load(tmp_path / "nope.json")
    assert state.seeded is False and state.cursor.t is None and state.observed == {}


def test_state_round_trips(tmp_path):
    path = tmp_path / "s.json"
    original = AssignmentState(
        seeded=True,
        cursor=Cursor(t=parse_timestamp("2026-08-19T12:00:00Z"), ids=frozenset({"A-1"})),
        observed={"A-1": ("Opie", "Todo"), "A-2": (None, "Done")},
        delivered={fingerprint("A-1", "Opie", "Todo")},
    )
    original.save(path)
    loaded = AssignmentState.load(path)
    assert loaded == original


@pytest.mark.parametrize("payload", [
    "not json at all",
    json.dumps([]),
    json.dumps({"schema_version": 99}),
    json.dumps({"schema_version": 1, "cursor": {"t": None, "ids": []},
                "observed": {}, "delivered": []}),                    # seeded missing
    json.dumps({"schema_version": 1, "seeded": True, "observed": {}, "delivered": []}),
    json.dumps({"schema_version": 1, "seeded": True, "cursor": {"t": None, "ids": "A-1"},
                "observed": {}, "delivered": []}),
    json.dumps({"schema_version": 1, "seeded": True,
                "cursor": {"t": "2026-08-19T12:00:00", "ids": []},     # NAIVE
                "observed": {}, "delivered": []}),
    json.dumps({"schema_version": 1, "seeded": True, "cursor": {"t": None, "ids": []},
                "observed": {"A-1": {"assignee": "Opie"}}, "delivered": []}),
    json.dumps({"schema_version": 1, "seeded": True, "cursor": {"t": None, "ids": []},
                "observed": {}, "delivered": [7]}),
])
def test_a_corrupt_state_file_is_unknown_never_a_fresh_start(tmp_path, payload):
    """Read as a cold start it re-seeds over real history; read as empty history
    it re-delivers the whole board. Neither default asserts nothing."""
    path = tmp_path / "s.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(StateUnreadable):
        AssignmentState.load(path)


# --- 5. THE ROSTER --------------------------------------------------------

def test_nicknames_and_agent_names_both_resolve():
    roster = _roster()
    assert roster.resolve("Tycho") == (RESOLVED, "coord-boss")
    assert roster.resolve("Opie") == (RESOLVED, "coord-opus-worker")
    assert roster.resolve("coord-fable-worker") == (RESOLVED, "coord-fable-worker")
    assert roster.resolve("  opie  ") == (RESOLVED, "coord-opus-worker")


def test_a_slash_separated_cell_registers_every_alias():
    roster = _roster()
    assert roster.resolve("infra maintainer") == (RESOLVED, "home-network-maintainer")
    assert roster.resolve("home-network maintainer") == (RESOLVED, "home-network-maintainer")


def test_a_mesh_peer_is_never_told():
    """The roster says in as many words that these are not reachable via
    coord-engine tell. A directive addressed to one looks delivered on our side
    and never arrives on theirs, which is worse than no delivery."""
    roster = _roster()
    for name in ("Leif", "Treecle", "Webster"):
        disposition, _ = roster.resolve(name)
        assert disposition == NOT_ADDRESSABLE


def test_an_unknown_name_is_unresolved_not_a_nearest_match():
    assert _roster().resolve("Ashley")[0] == UNRESOLVED


def test_no_assignee_is_unassigned():
    roster = _roster()
    assert roster.resolve(None) == (UNASSIGNED, None)
    assert roster.resolve("   ") == (UNASSIGNED, None)


def test_a_name_in_both_tables_resolves_to_nobody():
    roster = parse_roster(ROSTER.replace("| Webster |", "| Fabio |"))
    assert roster.resolve("Fabio")[0] == AMBIGUOUS


@pytest.mark.parametrize("text", ["", "   ", "# heading only\n\nno tables here\n"])
def test_a_roster_that_yields_no_fleet_agents_is_unreadable(text):
    """NOT 'nobody resolves'. That would file a confident triage verdict on
    every card in Ash's board on the strength of a failed read."""
    with pytest.raises(RosterUnreadable):
        parse_roster(text)


def test_a_prose_cell_in_the_agent_column_does_not_invent_an_address():
    roster = parse_roster(ROSTER.replace("| Fabio | coord-fable-worker |",
                                         "| Fabio | ask coord-boss |"))
    assert roster.resolve("Fabio")[0] == UNRESOLVED


def test_the_store_reader_raises_rather_than_returning_none():
    """`FulcraTeamsTransport.read` returns None for both 'absent' and 'the
    command failed' — the exact collapse this lane keeps finding."""
    def runner(argv, timeout):
        return 1, "", "no such file"
    with pytest.raises(RosterUnreadable):
        FulcraRosterReader(runner=runner)()


def test_the_store_reader_asks_for_the_documented_path():
    seen = {}

    def runner(argv, timeout):
        seen["argv"] = tuple(argv)
        return 0, ROSTER, ""
    FulcraRosterReader(runner=runner)()
    assert seen["argv"] == (
        "fulcra-api", "file", "download", "team/fulcra/_coord/roster-nicknames.md", "-")


# --- 6. PLANNING ----------------------------------------------------------

def test_a_row_with_no_usable_updatedat_is_unknown_for_the_whole_run():
    """`updatedAt` is optional in linear-inbox and required here, and that is
    what load-bearing means: called old the row is never delivered, called new
    it is delivered forever."""
    with pytest.raises(AssignmentsUnknown):
        plan_assignments([_item("A-1", when=None)], AssignmentState(seeded=True),
                         _roster(), coordinator="coord-boss")


def test_a_row_whose_state_was_never_read_is_unknown():
    """A Linear workflow state may legitimately be named 'Unknown', so the
    placeholder cannot be compared as if it were a reading."""
    with pytest.raises(AssignmentsUnknown):
        plan_assignments([_item("A-1", state="unknown", state_present=False)],
                         AssignmentState(seeded=True), _roster(), coordinator="coord-boss")


def test_only_rows_past_the_watermark_are_candidates():
    state = AssignmentState(
        seeded=True, cursor=Cursor(t=parse_timestamp("2026-08-19T12:00:00Z")))
    plan = plan_assignments(
        [_item("A-1", who="Opie", when="2026-08-19T11:00:00Z"),
         _item("A-2", who="Opie", when="2026-08-19T13:00:00Z")],
        state, _roster(), coordinator="coord-boss")
    assert [c.item.identifier for c in plan.candidates] == ["A-2"]
    assert plan.considered == 2


def test_a_card_that_moved_without_changing_assignee_or_state_is_not_routed():
    """Linear bumps updatedAt for a retitle. Routing on that is the noise the
    design names as a defect in its own right."""
    state = AssignmentState(seeded=True, observed={"A-1": ("Opie", "Todo")})
    plan = plan_assignments([_item("A-1", who="Opie", state="Todo", title="renamed")],
                            state, _roster(), coordinator="coord-boss")
    assert plan.routes == ()
    assert plan.unchanged == ("A-1",)


@pytest.mark.parametrize("before,after", [
    (("Opie", "Todo"), ("Fabio", "Todo")),
    (("Opie", "Todo"), ("Opie", "In Progress")),
    (None, ("Opie", "Todo")),
])
def test_an_assignee_or_state_change_is_routed(before, after):
    state = AssignmentState(seeded=True, observed=({"A-1": before} if before else {}))
    plan = plan_assignments([_item("A-1", who=after[0], state=after[1])],
                            state, _roster(), coordinator="coord-boss")
    assert len(plan.routes) == 1
    assert plan.routes[0].previous == before


def test_unresolved_and_unassigned_go_to_the_coordinator_never_to_a_guess():
    plan = plan_assignments(
        [_item("A-1", who="Ashley"), _item("A-2", who=None), _item("A-3", who="Webster")],
        AssignmentState(seeded=True), _roster(), coordinator="coord-boss")
    assert {r.identifier: (r.disposition, r.target) for r in plan.routes} == {
        "A-1": (UNRESOLVED, "coord-boss"),
        "A-2": (UNASSIGNED, "coord-boss"),
        "A-3": (NOT_ADDRESSABLE, "coord-boss"),
    }


def test_a_repeat_of_an_already_delivered_fingerprint_is_flagged():
    state = AssignmentState(
        seeded=True, delivered={fingerprint("A-1", "Opie", "Todo")})
    plan = plan_assignments([_item("A-1", who="Opie", state="Todo")],
                            state, _roster(), coordinator="coord-boss")
    assert plan.routes[0].repeat == REPEAT


def test_an_attempt_with_an_unknown_outcome_is_a_POSSIBLE_repeat():
    """Neither of its neighbours. Called new it under-claims, called a repeat
    it over-claims; the fingerprint sat in `attempted` because a dispatch
    raised, and a raise is not evidence the write did not land."""
    state = AssignmentState(
        seeded=True, attempted={fingerprint("A-1", "Opie", "Todo")})
    plan = plan_assignments([_item("A-1", who="Opie", state="Todo")],
                            state, _roster(), coordinator="coord-boss")
    assert plan.routes[0].repeat == POSSIBLE_REPEAT


def test_a_confirmed_delivery_outranks_a_stale_attempt_marker():
    state = AssignmentState(
        seeded=True,
        delivered={fingerprint("A-1", "Opie", "Todo")},
        attempted={fingerprint("A-1", "Opie", "Todo")})
    plan = plan_assignments([_item("A-1", who="Opie", state="Todo")],
                            state, _roster(), coordinator="coord-boss")
    assert plan.routes[0].repeat == REPEAT


def test_candidates_are_in_total_order():
    plan = plan_assignments(
        [_item("B-2", who="Opie", when="2026-08-19T12:00:00Z"),
         _item("B-1", who="Opie", when="2026-08-19T12:00:00Z"),
         _item("A-9", who="Opie", when="2026-08-19T11:00:00Z")],
        AssignmentState(seeded=True), _roster(), coordinator="coord-boss")
    assert [c.item.identifier for c in plan.candidates] == ["A-9", "B-1", "B-2"]


# --- 7. ADVANCING ---------------------------------------------------------

def _cand(ident, when, who="Opie", state="Todo"):
    item = _item(ident, who=who, state=state, when=when)
    return Candidate(item=item, updated_at=parse_timestamp(when), route=None)


def test_the_watermark_records_every_id_at_the_boundary():
    state = advance(AssignmentState(seeded=True), [
        _cand("A-1", "2026-08-19T12:00:00Z"),
        _cand("A-2", "2026-08-19T12:00:00Z"),
        _cand("A-3", "2026-08-19T11:00:00Z"),
    ])
    assert state.cursor.t == parse_timestamp("2026-08-19T12:00:00Z")
    assert state.cursor.ids == frozenset({"A-1", "A-2"})


def test_the_watermark_never_moves_backwards():
    mark = parse_timestamp("2026-08-19T12:00:00Z")
    state = AssignmentState(seeded=True, cursor=Cursor(t=mark, ids=frozenset({"A-1"})))
    moved = advance(state, [_cand("A-0", "2026-08-19T09:00:00Z")])
    assert moved.cursor.t == mark and moved.cursor.ids == frozenset({"A-1"})


def test_ids_at_an_unchanged_mark_accumulate_rather_than_replace():
    mark = parse_timestamp("2026-08-19T12:00:00Z")
    state = AssignmentState(seeded=True, cursor=Cursor(t=mark, ids=frozenset({"A-1"})))
    moved = advance(state, [_cand("A-2", "2026-08-19T12:00:00Z")])
    assert moved.cursor.ids == frozenset({"A-1", "A-2"})


def test_only_dispatched_routes_are_fingerprinted_as_delivered():
    route = Route(identifier="A-1", title="t", url=None, assignee="Opie", state="Todo",
                  updated_at=parse_timestamp("2026-08-19T12:00:00Z"),
                  disposition=RESOLVED, target="coord-opus-worker",
                  repeat=NEW, previous=None)
    state = advance(AssignmentState(seeded=True),
                    [_cand("A-1", "2026-08-19T12:00:00Z"),
                     _cand("A-2", "2026-08-19T12:00:00Z")],
                    dispatched=[route])
    assert state.delivered == {fingerprint("A-1", "Opie", "Todo")}


def test_a_confirmed_success_clears_the_ambiguity_marker():
    fp = fingerprint("A-1", "Opie", "Todo")
    route = Route(identifier="A-1", title="t", url=None, assignee="Opie", state="Todo",
                  updated_at=parse_timestamp("2026-08-19T12:00:00Z"),
                  disposition=RESOLVED, target="coord-opus-worker",
                  repeat=POSSIBLE_REPEAT, previous=None)
    state = advance(AssignmentState(seeded=True, attempted={fp}),
                    [_cand("A-1", "2026-08-19T12:00:00Z")], dispatched=[route])
    assert state.delivered == {fp} and state.attempted == set()


def test_seed_adopts_the_whole_board_and_delivers_nothing():
    state = seed(AssignmentState(), [
        _item("A-1", who="Opie", when="2026-08-19T11:00:00Z"),
        _item("A-2", who=None, when="2026-08-19T12:00:00Z"),
    ])
    assert state.seeded is True
    assert state.observed == {"A-1": ("Opie", "Todo"), "A-2": (None, "Todo")}
    assert state.cursor.t == parse_timestamp("2026-08-19T12:00:00Z")
    assert state.delivered == set()


# --- 8. THE DIRECTIVE -----------------------------------------------------

def test_the_directive_carries_the_card_identifier_and_url():
    route = Route(identifier="ENG-42", title="Fix the thing",
                  url="https://linear.app/x/issue/ENG-42", assignee="Opie",
                  state="Todo", updated_at=parse_timestamp("2026-08-19T12:00:00Z"),
                  disposition=RESOLVED, target="coord-opus-worker",
                  repeat=NEW, previous=("Fabio", "Todo"))
    summary, next_action = directive_body(route)
    assert "ENG-42" in summary
    assert "https://linear.app/x/issue/ENG-42" in summary
    assert "Fabio" in summary, "the previous observation belongs in the directive"
    assert "RE-DELIVERY" not in summary
    assert next_action


def test_a_possible_redelivery_says_POSSIBLE_in_the_directive():
    route = Route(identifier="ENG-42", title="t", url=None, assignee="Opie",
                  state="Todo", updated_at=parse_timestamp("2026-08-19T12:00:00Z"),
                  disposition=RESOLVED, target="coord-opus-worker",
                  repeat=POSSIBLE_REPEAT, previous=None)
    summary, _ = directive_body(route)
    assert "POSSIBLE RE-DELIVERY" in summary
    assert "may already exist" in summary


def test_a_redelivery_says_so_in_the_directive_itself():
    """At-least-once with a silent duplicate is indistinguishable from a second
    real assignment, so the reader is told which it is."""
    route = Route(identifier="ENG-42", title="t", url=None, assignee="Opie",
                  state="Todo", updated_at=parse_timestamp("2026-08-19T12:00:00Z"),
                  disposition=RESOLVED, target="coord-opus-worker",
                  repeat=REPEAT, previous=None)
    summary, _ = directive_body(route)
    assert "RE-DELIVERY" in summary


def test_the_dispatcher_uses_tell_not_a_bare_send():
    seen = {}

    def runner(argv, timeout):
        seen["argv"] = tuple(argv)
        return 0, "", ""
    route = Route(identifier="ENG-42", title="t", url=None, assignee="Opie",
                  state="Todo", updated_at=parse_timestamp("2026-08-19T12:00:00Z"),
                  disposition=RESOLVED, target="coord-opus-worker",
                  repeat=NEW, previous=None)
    EngineTellDispatcher(team="fulcra", sender="coord-opus-worker", runner=runner).deliver(route)
    argv = seen["argv"]
    assert argv[:5] == ("coord-engine", "tell", "fulcra", "coord-opus-worker",
                        "Linear ENG-42 assigned to you — Todo")
    assert "--from" in argv and "coord-opus-worker" in argv


def test_a_nonzero_tell_is_a_dispatch_failure_not_a_delivery():
    def runner(argv, timeout):
        return 2, "", "engine said no"
    route = Route(identifier="ENG-42", title="t", url=None, assignee="Opie",
                  state="Todo", updated_at=parse_timestamp("2026-08-19T12:00:00Z"),
                  disposition=RESOLVED, target="coord-opus-worker",
                  repeat=NEW, previous=None)
    with pytest.raises(DispatchFailed):
        EngineTellDispatcher(team="fulcra", sender="x", runner=runner).deliver(route)


# --- 9. THE RUN -----------------------------------------------------------

def _run(tmp_path, nodes, *, roster=ROSTER, dispatcher=None, **kwargs):
    client = LinearClient(ReadOnlyTransport(FakeTransport([_page(nodes)])))
    return run_assignments(
        client, team_id=TEAM, state_path=tmp_path / "s.json",
        roster_reader=(roster if callable(roster) else (lambda: roster)),
        coordinator="coord-boss",
        dispatcher=dispatcher if dispatcher is not None else FakeDispatcher(),
        **kwargs)


def test_an_unreadable_board_is_unknown_never_no_changes(tmp_path):
    client = LinearClient(ReadOnlyTransport(FakeTransport([{"data": {"issues": None}}])))
    outcome = run_assignments(
        client, team_id=TEAM, state_path=tmp_path / "s.json",
        roster_reader=lambda: ROSTER, coordinator="coord-boss")
    assert outcome.code == 3
    assert "UNKNOWN" in outcome.text
    assert not (tmp_path / "s.json").exists(), "a failed read must not move the mark"


def test_an_unreadable_roster_is_unknown_and_delivers_nothing(tmp_path):
    def boom():
        raise RosterUnreadable("store is down")
    dispatcher = FakeDispatcher()
    outcome = _run(tmp_path, [_node("A-1", who="Opie")], roster=boom,
                   dispatcher=dispatcher, deliver=True)
    assert outcome.code == 3 and dispatcher.sent == []


def test_a_cold_start_refuses_to_deliver(tmp_path):
    """The near-miss that shaped this lane was a plan that would have pushed
    ~503 creates into a curated board. A cold watermark has the same shape with
    the bus as the target."""
    dispatcher = FakeDispatcher()
    outcome = _run(tmp_path, [_node(f"A-{i}", who="Opie") for i in range(5)],
                   dispatcher=dispatcher, deliver=True)
    assert outcome.code == 2 and "REFUSING" in outcome.text
    assert dispatcher.sent == []
    assert not (tmp_path / "s.json").exists()


def test_seeding_records_the_baseline_and_sends_nothing(tmp_path):
    dispatcher = FakeDispatcher()
    outcome = _run(tmp_path, [_node("A-1", who="Opie")], dispatcher=dispatcher, do_seed=True)
    assert outcome.code == 0 and dispatcher.sent == []
    state = AssignmentState.load(tmp_path / "s.json")
    assert state.seeded and state.observed == {"A-1": ("Opie", "Todo")}


def test_preview_is_the_default_and_consumes_nothing(tmp_path):
    AssignmentState(seeded=True).save(tmp_path / "s.json")
    before = (tmp_path / "s.json").read_text()
    dispatcher = FakeDispatcher()
    outcome = _run(tmp_path, [_node("A-1", who="Opie")], dispatcher=dispatcher)
    assert outcome.code == 0 and dispatcher.sent == []
    assert (tmp_path / "s.json").read_text() == before, "a preview may not move the mark"
    assert "A-1" in outcome.text


def test_delivery_dispatches_and_advances(tmp_path):
    AssignmentState(seeded=True).save(tmp_path / "s.json")
    dispatcher = FakeDispatcher()
    outcome = _run(tmp_path, [_node("A-1", who="Opie")], dispatcher=dispatcher, deliver=True)
    assert outcome.code == 0
    assert [r.target for r in dispatcher.sent] == ["coord-opus-worker"]
    state = AssignmentState.load(tmp_path / "s.json")
    assert state.cursor.t == parse_timestamp("2026-08-19T12:00:00.000Z")
    assert fingerprint("A-1", "Opie", "Todo") in state.delivered


def test_a_second_run_over_the_same_board_delivers_nothing(tmp_path):
    AssignmentState(seeded=True).save(tmp_path / "s.json")
    _run(tmp_path, [_node("A-1", who="Opie")], dispatcher=FakeDispatcher(), deliver=True)
    second = FakeDispatcher()
    outcome = _run(tmp_path, [_node("A-1", who="Opie")], dispatcher=second, deliver=True)
    assert outcome.code == 0 and second.sent == []


def test_the_cap_refuses_the_whole_run_rather_than_flooding(tmp_path):
    AssignmentState(seeded=True).save(tmp_path / "s.json")
    dispatcher = FakeDispatcher()
    outcome = _run(tmp_path, [_node(f"A-{i}", who="Opie") for i in range(4)],
                   dispatcher=dispatcher, deliver=True, cap=3)
    assert outcome.code == 2 and "cap" in outcome.text
    assert dispatcher.sent == [], "the refusal is whole; no partial flood"


def test_a_failed_dispatch_leaves_the_rest_owed(tmp_path):
    """THE TRANSPORT SPLIT, in the direction this fleet has already paid for
    twice: the mark stops at the failure, so nothing that did not go out ends
    up behind it."""
    AssignmentState(seeded=True).save(tmp_path / "s.json")
    dispatcher = FakeDispatcher(fail_on={"A-2"})
    outcome = _run(
        tmp_path,
        [_node("A-1", who="Opie", when="2026-08-19T11:00:00Z"),
         _node("A-2", who="Opie", when="2026-08-19T12:00:00Z"),
         _node("A-3", who="Opie", when="2026-08-19T13:00:00Z")],
        dispatcher=dispatcher, deliver=True)
    assert outcome.code == 3 and "UNKNOWN" in outcome.text
    assert [r.identifier for r in dispatcher.sent] == ["A-1"]
    state = AssignmentState.load(tmp_path / "s.json")
    assert state.cursor.t == parse_timestamp("2026-08-19T11:00:00Z")

    # and the rows that did not go out are still owed on the next run
    retry = FakeDispatcher()
    _run(tmp_path,
         [_node("A-1", who="Opie", when="2026-08-19T11:00:00Z"),
          _node("A-2", who="Opie", when="2026-08-19T12:00:00Z"),
          _node("A-3", who="Opie", when="2026-08-19T13:00:00Z")],
         dispatcher=retry, deliver=True)
    assert [r.identifier for r in retry.sent] == ["A-2", "A-3"]


def test_deliver_and_seed_together_are_refused(tmp_path):
    outcome = _run(tmp_path, [_node("A-1")], deliver=True, do_seed=True)
    assert outcome.code == 2


def test_delivering_without_a_dispatcher_is_refused(tmp_path):
    client = LinearClient(ReadOnlyTransport(FakeTransport([_page([])])))
    outcome = run_assignments(
        client, team_id=TEAM, state_path=tmp_path / "s.json",
        roster_reader=lambda: ROSTER, coordinator="coord-boss", deliver=True)
    assert outcome.code == 2


def test_an_unplaceable_row_stops_the_run_before_anything_is_sent(tmp_path):
    AssignmentState(seeded=True).save(tmp_path / "s.json")
    node = _node("A-1", who="Opie")
    node.pop("updatedAt")
    dispatcher = FakeDispatcher()
    outcome = _run(tmp_path, [node, _node("A-2", who="Opie")],
                   dispatcher=dispatcher, deliver=True)
    assert outcome.code == 3 and dispatcher.sent == []


# --- 10. THE FOLD ---------------------------------------------------------

def test_a_cold_start_fold_says_so_out_loud():
    plan = plan_assignments([_item("A-1", who="Opie")], AssignmentState(),
                            _roster(), coordinator="coord-boss")
    assert "COLD START" in render_plan(plan)


def test_the_fold_names_the_triage_reason():
    plan = plan_assignments([_item("A-1", who="Webster")], AssignmentState(seeded=True),
                            _roster(), coordinator="coord-boss")
    text = render_plan(plan)
    assert "coord-boss" in text and NOT_ADDRESSABLE in text


# --- 11. THE AMBIGUOUS DISPATCH ------------------------------------------
#
# codex-coder's finding at 722fcc2. `coord-engine tell` can commit the directive
# and THEN fail to report it, so a raise is not evidence that nothing was
# written. The first version of this module had two outcomes where reality has
# three, and the retry announced a possible duplicate as a first delivery.

class CommitThenErrorDispatcher:
    """Writes, then reports failure — the outcome we cannot observe."""

    def __init__(self, commit_then_fail=(), state_path=None):
        self.sent = []
        self.committed = []
        self.commit_then_fail = set(commit_then_fail)
        self.state_path = state_path
        self.state_at_dispatch = []

    def deliver(self, route):
        if self.state_path is not None:
            self.state_at_dispatch.append(
                json.loads(self.state_path.read_text(encoding="utf-8")))
        if route.identifier in self.commit_then_fail:
            self.committed.append(route)          # the directive DID land
            raise DispatchFailed(f"transport lost the reply for {route.identifier}")
        self.sent.append(route)
        self.committed.append(route)


def test_the_attempt_is_on_disk_before_the_transport_runs(tmp_path):
    """Write-ahead, and asserted from inside the dispatcher: if the marker were
    written after the call, a process killed mid-tell would leave no trace of a
    directive that may already exist."""
    path = tmp_path / "s.json"
    AssignmentState(seeded=True).save(path)
    dispatcher = CommitThenErrorDispatcher(state_path=path)
    _run(tmp_path, [_node("A-1", who="Opie")], dispatcher=dispatcher, deliver=True)
    assert dispatcher.state_at_dispatch, "the dispatcher never ran"
    assert fingerprint("A-1", "Opie", "Todo") in dispatcher.state_at_dispatch[0]["attempted"]


def test_a_commit_then_error_retries_as_a_POSSIBLE_redelivery(tmp_path):
    """codex-coder's repro, end to end: first pass commits A-1 and A-2 and then
    reports failure on A-2; the retry must not tell the agent A-2 is new."""
    path = tmp_path / "s.json"
    AssignmentState(seeded=True).save(path)
    first = CommitThenErrorDispatcher(commit_then_fail={"A-2"})
    board = [_node("A-1", who="Opie", when="2026-08-19T11:00:00Z"),
             _node("A-2", who="Opie", when="2026-08-19T12:00:00Z")]
    outcome = _run(tmp_path, board, dispatcher=first, deliver=True)
    assert outcome.code == 3
    assert [r.identifier for r in first.committed] == ["A-1", "A-2"]
    assert "UNKNOWN" in outcome.text

    retry = CommitThenErrorDispatcher()
    _run(tmp_path, board, dispatcher=retry, deliver=True)
    assert [r.identifier for r in retry.sent] == ["A-2"]
    assert retry.sent[0].repeat == POSSIBLE_REPEAT
    summary, _ = directive_body(retry.sent[0])
    assert "POSSIBLE RE-DELIVERY" in summary


def test_the_ambiguous_row_is_the_only_one_left_ambiguous(tmp_path):
    """A-1 succeeded, so it is CONFIRMED, not possible. Collapsing the two would
    make every retry after any failure claim uncertainty it does not have."""
    path = tmp_path / "s.json"
    AssignmentState(seeded=True).save(path)
    board = [_node("A-1", who="Opie", when="2026-08-19T11:00:00Z"),
             _node("A-2", who="Opie", when="2026-08-19T12:00:00Z")]
    _run(tmp_path, board, dispatcher=CommitThenErrorDispatcher(commit_then_fail={"A-2"}),
         deliver=True)
    state = AssignmentState.load(path)
    assert state.delivered == {fingerprint("A-1", "Opie", "Todo")}
    assert state.attempted == {fingerprint("A-2", "Opie", "Todo")}


def test_a_later_confirmed_send_clears_the_marker_it_set(tmp_path):
    """The marker is written for every attempt, so a clean run must not leave
    the whole board looking permanently ambiguous."""
    path = tmp_path / "s.json"
    AssignmentState(seeded=True).save(path)
    _run(tmp_path, [_node("A-1", who="Opie")], dispatcher=FakeDispatcher(), deliver=True)
    state = AssignmentState.load(path)
    assert state.attempted == set()
    assert state.delivered == {fingerprint("A-1", "Opie", "Todo")}


def test_a_v1_state_file_is_unreadable_rather_than_assumed_unambiguous(tmp_path):
    """An absent `attempted` would default to "no dispatch ever ended
    ambiguously" — a claim a file written before the concept cannot support."""
    path = tmp_path / "s.json"
    path.write_text(json.dumps({
        "schema_version": 1, "seeded": True, "cursor": {"t": None, "ids": []},
        "observed": {}, "delivered": []}), encoding="utf-8")
    with pytest.raises(StateUnreadable):
        AssignmentState.load(path)
