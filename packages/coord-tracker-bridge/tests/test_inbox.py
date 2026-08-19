"""`linear-inbox` reads Ash's board and can never write to it.

The hard rail on this lane is coord-boss's, and it exists because of a
near-miss: an earlier cutover plan would have pushed ~503 creates into a
55-issue curated board. So "this verb does not call mutations" is not the
property under test — "this verb CANNOT call one" is.

The second property is the coord-mesh one: a read that fails is UNKNOWN, never
an empty board. An empty board and an unreadable board look identical to a
caller unless the code refuses to conflate them, and here the caller is a person
deciding whether they have work.

FIXTURES ARE SYNTHETIC AND SAY SO. There is no Linear API key on this host, so
no response has ever been captured from the live API. Per the sealed-secrets and
coord-mesh lesson — a fixture whose provenance is assumed reads exactly like one
that was measured — nothing here is labelled captured. `tools/capture_inbox.py`
exists to stamp a real one on the first live read; until then these shapes are
hand-built from Linear's published schema and the package's own ISSUES_QUERY,
and the contract test that pins them against a live capture is skipped rather
than faked.
"""

import json
import os

import pytest

from coord_tracker_bridge.inbox import (
    INBOX_QUERY,
    OK,
    EMPTY,
    UNKNOWN,
    ReadOnlyTransport,
    Result,
    WriteRefused,
    fetch_inbox,
    render_fold,
    to_item,
)
from coord_tracker_bridge.linear import GraphQLResponse, LinearClient, LinearError

TEAM = "team-abc"


def _node(ident, title, *, state="Todo", stype="unstarted", who=None, labels=()):
    return {
        "id": "issue-" + ident, "identifier": ident, "title": title,
        "url": "https://linear.app/x/issue/" + ident,
        "updatedAt": "2026-08-19T12:00:00.000Z",
        "state": {"name": state, "type": stype},
        "assignee": ({"displayName": who} if who else None),
        "labels": {"nodes": [{"name": name} for name in labels]},
    }


class FakeTransport:
    """Records what would have been posted; returns canned pages."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.posted = []

    def post(self, payload):
        self.posted.append(dict(payload))
        body = self.pages.pop(0)
        return GraphQLResponse(200, body, {})


def _page(nodes, *, has_next=False, cursor=None):
    return {"data": {"issues": {"nodes": nodes,
                                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}}}}


# --- THE HARD RAIL --------------------------------------------------------

def test_the_transport_refuses_a_mutation_document():
    """THE RAIL. Not 'we do not send mutations' — 'a mutation cannot be sent'."""
    inner = FakeTransport([_page([])])
    ro = ReadOnlyTransport(inner)
    with pytest.raises(WriteRefused):
        ro.post({"operationName": "IssueCreate",
                 "query": "mutation IssueCreate($i:IssueCreateInput!){issueCreate(input:$i){success}}"})
    assert inner.posted == [], "nothing may reach the network"


def test_the_rail_reads_the_document_not_the_operation_name():
    """A mutation smuggled under a query-shaped operationName still refuses."""
    inner = FakeTransport([_page([])])
    with pytest.raises(WriteRefused):
        ReadOnlyTransport(inner).post({"operationName": "CoordInbox",
                                       "query": "mutation { issueUpdate(id:1){success} }"})
    assert inner.posted == []


def test_subscriptions_are_refused_too():
    inner = FakeTransport([_page([])])
    with pytest.raises(WriteRefused):
        ReadOnlyTransport(inner).post({"query": "subscription { issues { id } }"})


def test_a_field_merely_containing_the_word_mutation_is_not_refused():
    """The rail must not be so blunt it blocks a legitimate read — a title or
    label containing the word would otherwise take the board down."""
    inner = FakeTransport([_page([])])
    ReadOnlyTransport(inner).post({"query": "query Q { issues { title } }",
                                   "variables": {"q": "mutation testing notes"}})
    assert len(inner.posted) == 1


def test_the_inbox_query_itself_passes_the_rail():
    inner = FakeTransport([_page([])])
    ReadOnlyTransport(inner).post({"operationName": "CoordInbox", "query": INBOX_QUERY})
    assert len(inner.posted) == 1


def test_the_shipped_query_is_a_pure_query():
    assert INBOX_QUERY.lstrip().startswith("query ")
    assert "mutation" not in INBOX_QUERY.lower()


# --- UNKNOWN is never an empty board --------------------------------------

def test_a_transport_failure_is_unknown_not_empty():
    class Boom:
        def post(self, payload):
            raise LinearError("boom")

    result = fetch_inbox(LinearClient(Boom()), TEAM)
    assert result.state == UNKNOWN and result.unknown
    assert result.items == ()


def test_a_stalled_cursor_is_unknown():
    """paginate() raises on a cursor that does not advance; that must surface as
    UNKNOWN rather than as the rows read so far."""
    pages = [_page([_node("ASH-1", "one")], has_next=True, cursor="c1"),
             _page([_node("ASH-2", "two")], has_next=True, cursor="c1")]
    result = fetch_inbox(LinearClient(FakeTransport(pages)), TEAM)
    assert result.unknown, result


def test_an_unidentifiable_row_degrades_the_whole_read():
    """A board missing rows it never mentions is the same lie as an empty one."""
    pages = [_page([_node("ASH-1", "one"), {"title": "no identifier"}])]
    result = fetch_inbox(LinearClient(FakeTransport(pages)), TEAM)
    assert result.unknown
    assert "partial" in result.detail


def test_an_empty_board_is_empty_not_unknown():
    result = fetch_inbox(LinearClient(FakeTransport([_page([])])), TEAM)
    assert result.state == EMPTY and not result.unknown


def test_pagination_collects_every_page():
    pages = [_page([_node("ASH-2", "two")], has_next=True, cursor="c1"),
             _page([_node("ASH-1", "one")])]
    result = fetch_inbox(LinearClient(FakeTransport(pages)), TEAM)
    assert result.state == OK
    assert [i.identifier for i in result.items] == ["ASH-1", "ASH-2"], "sorted, deterministic"


# --- rendering ------------------------------------------------------------

def test_the_fold_says_unknown_loudly():
    text = render_fold(Result(UNKNOWN, detail="429 rate limited"), team_id=TEAM)
    assert "UNKNOWN" in text
    assert "not an empty board" in text
    assert "0 issue" not in text


def test_the_fold_distinguishes_a_real_empty_board():
    text = render_fold(Result(EMPTY), team_id=TEAM)
    assert "0 issue(s)" in text and "read succeeded" in text
    assert "UNKNOWN" not in text


def test_the_fold_renders_identifier_state_assignee_and_labels():
    result = fetch_inbox(LinearClient(FakeTransport([
        _page([_node("ASH-7", "Wire the thing", state="In Progress",
                     who="Ash", labels=("infra", "p1"))])])), TEAM)
    text = render_fold(result, team_id=TEAM)
    assert "ASH-7" in text and "In Progress" in text and "Ash" in text
    assert "[infra, p1]" in text and "Wire the thing" in text


def test_an_unassigned_issue_renders_as_unassigned():
    result = fetch_inbox(LinearClient(FakeTransport([_page([_node("ASH-9", "orphan")])])), TEAM)
    assert "unassigned" in render_fold(result, team_id=TEAM)


# --- normalization --------------------------------------------------------

def test_to_item_rejects_a_row_with_no_identifier():
    assert to_item({"title": "x"}) is None


def test_to_item_rejects_a_row_with_no_title():
    assert to_item({"identifier": "ASH-1"}) is None


def test_to_item_tolerates_missing_optional_fields():
    item = to_item({"identifier": "ASH-1", "title": "t"})
    assert item.state == "unknown" and item.assignee is None and item.labels == ()


# --- the fixture that does not exist yet ----------------------------------

CAPTURE = os.path.join(os.path.dirname(__file__), "fixtures", "real_linear_issues.json")


@pytest.mark.skipif(not os.path.exists(CAPTURE),
                    reason="no live Linear capture yet — no API key on this host; "
                           "run tools/capture_inbox.py after the first live read")
def test_the_query_fields_exist_on_a_real_response():
    """Pinned against a REAL capture once one exists, the same way coord-mesh
    pins its argv against a captured --help. Skipped rather than faked: a
    hand-written fixture labelled 'real' is the exact defect that cost
    sealed-secrets a review round."""
    with open(CAPTURE, "r", encoding="utf-8") as fh:
        captured = json.load(fh)
    assert captured.get("captured_from") == "linear.app"
    nodes = captured["response"]["data"]["issues"]["nodes"]
    assert nodes, "a capture with no issues cannot pin field names"
    for node in nodes:
        assert to_item(node) is not None


# --- the verb must actually exist and run ---------------------------------

def test_the_console_entry_point_exposes_linear_inbox():
    """codex-coder's coord-mesh finding, applied here before it can bite: a verb
    declared in argparse but unreachable passes every unit test that imports the
    module and none that runs the command."""
    import subprocess
    import sys as _sys
    cp = subprocess.run([_sys.executable, "-m", "coord_tracker_bridge.cli", "--help"],
                        capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stderr
    assert "linear-inbox" in cp.stdout


def test_the_verb_refuses_without_credentials_rather_than_pretending(monkeypatch):
    from coord_tracker_bridge import cli as _cli
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    rc = _cli.main(["linear-inbox", "--linear-team-id", "TEAM-X"])
    assert rc == 2


def test_unknown_exits_3_so_a_caller_can_tell_it_from_an_empty_board(monkeypatch, capsys):
    """rc 0 for a board we could not read would make every script that wraps
    this verb report 'no work' during an outage."""
    from coord_tracker_bridge import cli as _cli
    monkeypatch.setenv("LINEAR_API_KEY", "not-a-real-key")
    monkeypatch.setattr(_cli, "fetch_inbox",
                        lambda *a, **k: Result(UNKNOWN, detail="429"))
    monkeypatch.setattr(_cli, "HttpxGraphQLTransport", lambda key: object())
    rc = _cli.main(["linear-inbox", "--linear-team-id", "TEAM-X"])
    assert rc == 3
    assert "UNKNOWN" in capsys.readouterr().out


def test_an_empty_board_exits_0(monkeypatch, capsys):
    from coord_tracker_bridge import cli as _cli
    monkeypatch.setenv("LINEAR_API_KEY", "not-a-real-key")
    monkeypatch.setattr(_cli, "fetch_inbox", lambda *a, **k: Result(EMPTY))
    monkeypatch.setattr(_cli, "HttpxGraphQLTransport", lambda key: object())
    assert _cli.main(["linear-inbox", "--linear-team-id", "TEAM-X"]) == 0


# --- node cardinality: codex-coder's finding at 667befac -------------------

def test_a_null_node_is_UNKNOWN_not_a_clean_empty_board():
    """THE REGRESSION. `LinearClient.paginate` filters non-Mapping nodes before
    any validation of ours can see them, so a page containing a null came back
    state=empty, unknown=false, items=0 — a clean empty board for a response we
    could not read, which is the precise failure this module exists to prevent.

    The defect was inherited from a library function reused without auditing:
    the guard downstream was verified, the filter upstream was not."""
    result = fetch_inbox(LinearClient(FakeTransport([_page([None])])), TEAM)
    assert result.unknown, result
    assert result.state == UNKNOWN
    assert result.items == ()


def test_a_null_among_good_rows_still_degrades_the_whole_read():
    """Partial is the dangerous shape: the good rows make the answer look real."""
    pages = [_page([_node("ASH-1", "one"), None, _node("ASH-2", "two")])]
    result = fetch_inbox(LinearClient(FakeTransport(pages)), TEAM)
    assert result.unknown
    assert "3 node(s) read" in result.detail, result.detail


def test_a_null_on_a_LATER_page_is_not_lost():
    """Cardinality must be preserved across the page boundary too."""
    pages = [_page([_node("ASH-1", "one")], has_next=True, cursor="c1"),
             _page([None])]
    assert fetch_inbox(LinearClient(FakeTransport(pages)), TEAM).unknown


def test_a_non_list_nodes_field_is_UNKNOWN():
    pages = [{"data": {"issues": {"nodes": {"not": "a list"},
                                  "pageInfo": {"hasNextPage": False}}}}]
    assert fetch_inbox(LinearClient(FakeTransport(pages)), TEAM).unknown


def test_a_missing_issues_root_is_UNKNOWN():
    pages = [{"data": {}}]
    assert fetch_inbox(LinearClient(FakeTransport(pages)), TEAM).unknown


def test_the_null_node_case_exits_3_through_the_CLI(monkeypatch, capsys):
    """codex asked for the rc as well as the state: a script wrapping this verb
    must see 3, because rc 0 here is the outage-reported-as-no-work case."""
    from coord_tracker_bridge import cli as _cli
    monkeypatch.setenv("LINEAR_API_KEY", "not-a-real-key")
    transport = FakeTransport([_page([None])])
    monkeypatch.setattr(_cli, "HttpxGraphQLTransport", lambda key: transport)
    monkeypatch.setattr(_cli, "ReadOnlyTransport", lambda inner: inner)
    rc = _cli.main(["linear-inbox", "--linear-team-id", TEAM])
    assert rc == 3
    out = capsys.readouterr().out
    assert "UNKNOWN" in out and "not an empty board" in out


# --- shape checks must be POSITIVE, never inferred from truthiness ---------
# codex-coder on 34a7220f: `x.get(k) or {}` rescues only a FALSY value. A truthy
# non-Mapping sails past it and AttributeErrors on the next .get — a crash, not
# an UNKNOWN, which escapes the single contract this verb makes. The idiom was
# inherited from LinearClient.paginate and is mine now that I rewrote the walk.

def _page_with(page_info, nodes=()):
    return {"data": {"issues": {"nodes": list(nodes), "pageInfo": page_info}}}


@pytest.mark.parametrize("bad", [["malformed"], "next", 7, True])
def test_a_truthy_non_mapping_pageInfo_is_UNKNOWN_not_a_crash(bad):
    """codex's negative control was pageInfo=[malformed]; the whole class is
    covered, because the next malformed shape will not be a list."""
    result = fetch_inbox(LinearClient(FakeTransport([_page_with(bad)])), TEAM)
    assert result.unknown, result
    assert "pageInfo" in result.detail


def test_a_missing_pageInfo_is_simply_the_last_page():
    """Absent is not malformed: a response that omits pageInfo has no next page,
    and treating that as an error would degrade a healthy read."""
    result = fetch_inbox(LinearClient(FakeTransport([_page_with(None, [_node("ASH-1", "one")])])), TEAM)
    assert result.state == OK
    assert [i.identifier for i in result.items] == ["ASH-1"]


def test_the_malformed_pageInfo_case_exits_3_through_the_CLI(monkeypatch, capsys):
    from coord_tracker_bridge import cli as _cli
    monkeypatch.setenv("LINEAR_API_KEY", "not-a-real-key")
    transport = FakeTransport([_page_with(["malformed"])])
    monkeypatch.setattr(_cli, "HttpxGraphQLTransport", lambda key: transport)
    monkeypatch.setattr(_cli, "ReadOnlyTransport", lambda inner: inner)
    assert _cli.main(["linear-inbox", "--linear-team-id", TEAM]) == 3
    assert "UNKNOWN" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["label-string", 3, {"nodes": 1}])
def test_a_malformed_labels_collection_degrades_the_row(bad):
    """The same family, found by auditing rather than by being told: a truthy
    non-list under labels.nodes would iterate character by character and yield
    no labels — wrong rather than loud. An issue whose labels cannot be read is
    an issue that cannot be rendered faithfully."""
    node = _node("ASH-1", "one")
    node["labels"] = {"nodes": bad} if not isinstance(bad, dict) else bad
    assert fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM).unknown


def test_absent_labels_are_simply_no_labels():
    node = _node("ASH-1", "one")
    node.pop("labels")
    result = fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM)
    assert result.state == OK and result.items[0].labels == ()


# --- cardinality one level down: codex-coder at 98ac86e1 -------------------
# The issue-level null-node defect, reproduced inside the label list — written
# while fixing the issue-level one. Absent has a default; malformed never does.

def _with_labels(labels_value):
    node = _node("ASH-1", "one")
    node["labels"] = labels_value
    return node


def test_a_null_label_node_degrades_the_row_not_the_label_list():
    """codex's control: [valid, null, {no name}] used to return OK with one
    label — a confident row rendered from data we could not read."""
    node = _with_labels({"nodes": [{"name": "infra"}, None, {"id": "x"}]})
    assert fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM).unknown


def test_a_label_node_with_no_usable_name_degrades_the_row():
    node = _with_labels({"nodes": [{"name": "   "}]})
    assert fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM).unknown


@pytest.mark.parametrize("root", [["not", "an", "object"], "labels", 5, True])
def test_a_truthy_non_object_labels_root_is_UNKNOWN_not_absent(root):
    """It used to be coerced to {} and read as 'no labels'."""
    assert fetch_inbox(LinearClient(FakeTransport([_page([_with_labels(root)])])), TEAM).unknown


def test_absent_labels_and_absent_nodes_are_both_simply_no_labels():
    for value in (None, {}, {"nodes": None}):
        node = _with_labels(value) if value is not None else _node("ASH-1", "one")
        if value is None:
            node.pop("labels", None)
        result = fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM)
        assert result.state == OK, value
        assert result.items[0].labels == ()


# --- the same rule applied to every other optional sub-object -------------
# Audited rather than reported: state and assignee had the identical coercion,
# and a malformed one rendered as "unknown"/"unassigned" — a confident answer
# about a field we could not read.

@pytest.mark.parametrize("field,value", [
    ("state", ["nope"]), ("state", "Todo"), ("assignee", ["nope"]), ("assignee", "Ash"),
])
def test_a_truthy_non_object_subfield_degrades_the_row(field, value):
    node = _node("ASH-1", "one")
    node[field] = value
    assert fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM).unknown


@pytest.mark.parametrize("field", ["url", "updatedAt"])
def test_a_non_string_scalar_degrades_the_row(field):
    node = _node("ASH-1", "one")
    node[field] = {"not": "a string"}
    assert fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM).unknown


def test_absent_state_and_assignee_still_render():
    node = _node("ASH-1", "one")
    node.pop("state"); node["assignee"] = None
    result = fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM)
    assert result.state == OK
    assert result.items[0].state == "unknown" and result.items[0].assignee is None


def test_the_malformed_label_case_exits_3_through_the_CLI(monkeypatch, capsys):
    from coord_tracker_bridge import cli as _cli
    monkeypatch.setenv("LINEAR_API_KEY", "not-a-real-key")
    node = _with_labels({"nodes": [{"name": "infra"}, None]})
    transport = FakeTransport([_page([node])])
    monkeypatch.setattr(_cli, "HttpxGraphQLTransport", lambda key: transport)
    monkeypatch.setattr(_cli, "ReadOnlyTransport", lambda inner: inner)
    assert _cli.main(["linear-inbox", "--linear-team-id", TEAM]) == 3
    assert "UNKNOWN" in capsys.readouterr().out


# --- present-but-hollow is malformed, not absent: codex-coder at dd82af14 ---
# The fourth site of the same confusion, and the reason the rule is now a table
# (_REQUIRED_SUBFIELDS) rather than a check written wherever it was last named.

from coord_tracker_bridge.inbox import _REQUIRED_SUBFIELDS  # noqa: E402


@pytest.mark.parametrize("state_obj", [
    {},                              # present, entirely hollow
    {"type": "started"},             # present, no name
    {"name": "In Progress"},         # present, no type
    {"name": "  ", "type": "x"},     # present, blank name
    {"name": "x", "type": None},     # present, null type
])
def test_a_present_but_hollow_state_degrades_the_row(state_obj):
    """It used to render as state='unknown' — a confident answer about a field
    we could not read, on a row we were happy to show."""
    node = _node("ASH-1", "one")
    node["state"] = state_obj
    assert fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM).unknown


@pytest.mark.parametrize("who", [{}, {"name": "Ash"}, {"displayName": "   "}])
def test_a_present_but_hollow_assignee_degrades_the_row(who):
    """It used to render as 'unassigned', which is a claim about who owns work."""
    node = _node("ASH-1", "one")
    node["assignee"] = who
    assert fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM).unknown


def test_absent_state_and_absent_assignee_still_render():
    """The other half of the rule, kept honest: absent is not malformed."""
    node = _node("ASH-1", "one")
    node.pop("state")
    node["assignee"] = None
    result = fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM)
    assert result.state == OK
    assert result.items[0].state == "unknown"
    assert result.items[0].state_type == "unknown"
    assert result.items[0].assignee is None


def test_a_whole_state_and_assignee_render_their_values():
    node = _node("ASH-1", "one", state="In Progress", stype="started", who="Ash")
    item = fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM).items[0]
    assert item.state == "In Progress" and item.state_type == "started"
    assert item.assignee == "Ash"


def test_the_hollow_state_case_exits_3_through_the_CLI(monkeypatch, capsys):
    from coord_tracker_bridge import cli as _cli
    monkeypatch.setenv("LINEAR_API_KEY", "not-a-real-key")
    node = _node("ASH-1", "one")
    node["state"] = {"type": "started"}
    transport = FakeTransport([_page([node])])
    monkeypatch.setattr(_cli, "HttpxGraphQLTransport", lambda key: transport)
    monkeypatch.setattr(_cli, "ReadOnlyTransport", lambda inner: inner)
    assert _cli.main(["linear-inbox", "--linear-team-id", TEAM]) == 3
    assert "UNKNOWN" in capsys.readouterr().out


def test_the_required_subfield_spec_matches_what_the_query_asks_for():
    """THE ANTI-DRIFT PIN, and the structural answer to four rounds of the same
    finding. Every sub-object the query selects must appear in the spec, and
    every field the spec requires must be one the query actually asks for —
    otherwise the next field somebody adds gets validated by nobody."""
    for parent, fields in _REQUIRED_SUBFIELDS.items():
        assert parent + "{" in INBOX_QUERY, f"{parent} is specced but not queried"
        selection = INBOX_QUERY.split(parent + "{", 1)[1].split("}", 1)[0]
        for field in fields:
            assert field in selection, f"{parent}.{field} is required but not selected"
    # And the reverse: a selected sub-object with no spec is an unguarded field.
    for parent in ("state", "assignee"):
        assert parent in _REQUIRED_SUBFIELDS, f"{parent} is queried but unspecced"


# --- the invariant at scalar scope: codex-coder at 81858fd6 ----------------
# Fifth site, and the first one they named as an INVARIANT rather than a field,
# which is what I asked for: every value this module reads is either ABSENT WITH
# A DEFAULT or VALIDATED WHOLE. No third state, and no quiet promotion into one
# of the first two.

from coord_tracker_bridge.inbox import _OPTIONAL_SCALARS, _REQUIRED_SCALARS  # noqa: E402


@pytest.mark.parametrize("bad", [[], {}, 7, True, "", "   "])
def test_a_present_malformed_identifier_is_not_masked_by_the_id_fallback(bad):
    """codex's control: identifier=[] fell through `or` to `id`, so a row we
    could not identify rendered as one we could. A fallback is exactly where
    absent and malformed get confused."""
    node = _node("ASH-1", "one")
    node["identifier"] = bad
    assert fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM).unknown


def test_the_id_fallback_still_works_when_identifier_is_genuinely_absent():
    """The other half: absent is not malformed, and the fallback exists for it."""
    node = _node("ASH-1", "one")
    node.pop("identifier")
    result = fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM)
    assert result.state == OK
    assert result.items[0].identifier == "issue-ASH-1"


def test_a_null_identifier_falls_back_rather_than_degrading():
    node = _node("ASH-1", "one")
    node["identifier"] = None
    assert fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM).state == OK


def test_neither_identifier_nor_id_usable_degrades():
    node = _node("ASH-1", "one")
    node["identifier"] = None
    node["id"] = "   "
    assert fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM).unknown


@pytest.mark.parametrize("bad", ["", "   ", 7, [], {}])
def test_a_blank_or_non_string_title_degrades(bad):
    """A blank title rendered a row with an empty label — present, unusable,
    and shown anyway."""
    node = _node("ASH-1", "one")
    node["title"] = bad
    assert fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM).unknown


def test_an_absent_title_degrades_because_title_is_required():
    node = _node("ASH-1", "one")
    node.pop("title")
    assert fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM).unknown


@pytest.mark.parametrize("field", _OPTIONAL_SCALARS)
@pytest.mark.parametrize("bad", ["   ", 7, [], {}])
def test_a_present_unusable_optional_scalar_degrades(field, bad):
    node = _node("ASH-1", "one")
    node[field] = bad
    assert fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM).unknown


@pytest.mark.parametrize("field", _OPTIONAL_SCALARS)
def test_an_absent_or_null_optional_scalar_is_simply_absent(field):
    for value in (None, "__POP__"):
        node = _node("ASH-1", "one")
        if value == "__POP__":
            node.pop(field)
        else:
            node[field] = value
        result = fetch_inbox(LinearClient(FakeTransport([_page([node])])), TEAM)
        assert result.state == OK, (field, value)


def test_the_malformed_identifier_case_exits_3_through_the_CLI(monkeypatch, capsys):
    from coord_tracker_bridge import cli as _cli
    monkeypatch.setenv("LINEAR_API_KEY", "not-a-real-key")
    node = _node("ASH-1", "one")
    node["identifier"] = []
    transport = FakeTransport([_page([node])])
    monkeypatch.setattr(_cli, "HttpxGraphQLTransport", lambda key: transport)
    monkeypatch.setattr(_cli, "ReadOnlyTransport", lambda inner: inner)
    assert _cli.main(["linear-inbox", "--linear-team-id", TEAM]) == 3
    assert "UNKNOWN" in capsys.readouterr().out


def test_the_scalar_tables_match_what_the_query_asks_for():
    """Anti-drift, same shape as the sub-object pin: a scalar this module
    validates must be one the query actually selects."""
    for field in (*_REQUIRED_SCALARS, *_OPTIONAL_SCALARS):
        assert field in INBOX_QUERY, f"{field} is validated but not selected"
