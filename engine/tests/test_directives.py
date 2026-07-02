from coord_engine import directives

NOW = "2026-07-02T12:00:00Z"


def _r(name, status="proposed", assignee=None, priority="P2", not_before=None):
    return {"id": name, "name": name, "title": name, "status": status,
            "assignee": assignee, "priority": priority, "not_before": not_before,
            "timestamp": NOW, "tags": []}


def test_parse_when_iso_passthrough():
    assert directives.parse_when("2026-08-01T00:00:00Z", now=NOW) == "2026-08-01T00:00:00Z"
    assert directives.parse_when("2026-08-01", now=NOW) == "2026-08-01"


def test_parse_when_relative():
    assert directives.parse_when("2h", now=NOW) == "2026-07-02T14:00:00Z"
    assert directives.parse_when("3d", now=NOW) == "2026-07-05T12:00:00Z"
    assert directives.parse_when("30m", now=NOW) == "2026-07-02T12:30:00Z"
    assert directives.parse_when("junk", now=NOW) is None
    assert directives.parse_when("5x", now=NOW) is None


def test_inbox_direct_and_wildcard_minus_acked():
    rows = [
        _r("mine", assignee="amy"),
        _r("all", assignee="*"),
        _r("acked", assignee="amy"),
        _r("other", assignee="bob"),
        _r("closed", status="done", assignee="amy"),
    ]
    got = [r["name"] for r in directives.inbox(rows, {"acked": ["amy"]}, "amy", now=NOW)]
    assert set(got) == {"mine", "all"}


def test_inbox_wildcard_ack_hides_per_agent():
    rows = [_r("all", assignee="*")]
    assert directives.inbox(rows, {"all": ["amy"]}, "amy", now=NOW) == []
    assert [r["name"] for r in directives.inbox(rows, {"all": ["amy"]}, "bob", now=NOW)] == ["all"]


def test_inbox_not_before_gate_and_backlog():
    rows = [
        _r("later", assignee="amy", not_before="2026-08-01T00:00:00Z"),
        _r("idea", assignee=directives.BACKLOG),
    ]
    assert directives.inbox(rows, {}, "amy", now=NOW) == []
    got = directives.inbox(rows, {}, directives.BACKLOG, now=NOW, include_backlog=True)
    assert [r["name"] for r in got] == ["idea"]


def test_inbox_priority_sorted():
    rows = [_r("low", assignee="amy", priority="P3"), _r("hot", assignee="amy", priority="P0")]
    assert [r["name"] for r in directives.inbox(rows, {}, "amy", now=NOW)] == ["hot", "low"]


def test_broadcast_state_with_and_without_roster():
    row = _r("all", assignee="*")
    st = directives.broadcast_state(row, ["amy"], ["amy", "bob"])
    assert st["complete"] is False and st["pending"] == ["bob"]
    st2 = directives.broadcast_state(row, ["amy", "bob"], ["amy", "bob"])
    assert st2["complete"] is True and st2["pending"] == []
    st3 = directives.broadcast_state(row, ["amy"], None)   # presence absent
    assert st3["complete"] is None and st3["pending"] is None


def test_renotify_priority_ceiling():
    rows = [
        _r("p0", assignee="amy", priority="P0"),
        _r("p1", assignee="amy", priority="P1"),
        _r("p2", assignee="amy", priority="P2"),
    ]
    got = [r["name"] for r in directives.renotify(rows, {}, "amy", now=NOW)]
    assert got == ["p0", "p1"]
