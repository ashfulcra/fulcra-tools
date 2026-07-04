from coord_engine import query


def _r(id, status="active", assignee=None, blocked_on=None, not_before=None,
       title=None, desc="", tags=None, priority="P2", ts=None):
    return {
        "id": id, "name": id, "title": title or id, "description": desc,
        "status": status, "assignee": assignee, "blocked_on": blocked_on,
        "not_before": not_before, "tags": tags or [], "priority": priority,
        "timestamp": ts,
    }


def test_status_counts():
    rows = [_r("a", "active"), _r("b", "active"), _r("c", "done")]
    assert query.status_counts(rows) == {"active": 2, "done": 1}


def test_board_groups_open_only_and_sorts():
    rows = [
        _r("a", "active", priority="P2"),
        _r("hi", "active", priority="P0"),
        _r("w", "waiting"),
        _r("d", "done"),
    ]
    b = query.board(rows)
    assert [r["id"] for r in b["active"]] == ["hi", "a"]  # P0 first
    assert [r["id"] for r in b["waiting"]] == ["w"]
    assert "done" not in b  # terminal not on board


def test_needs_me_by_assignee():
    rows = [_r("a", assignee="me"), _r("b", assignee="you")]
    got = [r["id"] for r in query.needs_me(rows, "me")]
    assert got == ["a"]


def test_needs_me_by_blocked_on():
    rows = [_r("a", assignee="x", blocked_on="waiting on me to review")]
    assert [r["id"] for r in query.needs_me(rows, "me")] == ["a"]


def test_needs_me_excludes_terminal():
    rows = [_r("a", status="done", assignee="me")]
    assert query.needs_me(rows, "me") == []


def test_needs_me_not_before_gate():
    rows = [
        _r("future", assignee="me", not_before="2026-08-01T00:00:00Z"),
        _r("now", assignee="me", not_before="2026-06-01T00:00:00Z"),
    ]
    got = [r["id"] for r in query.needs_me(rows, "me", now="2026-07-01T00:00:00Z")]
    assert got == ["now"]  # future hidden until not_before


def test_needs_me_no_now_skips_gate():
    rows = [_r("future", assignee="me", not_before="2026-08-01T00:00:00Z")]
    assert [r["id"] for r in query.needs_me(rows, "me")] == ["future"]


def test_search_matches_title_desc_tags_ci():
    rows = [
        _r("a", title="Fix the Widget", desc="", tags=[]),
        _r("b", title="unrelated", desc="mentions widget here"),
        _r("c", title="unrelated", desc="", tags=["area:widget"]),
        _r("d", title="nothing"),
    ]
    got = {r["id"] for r in query.search(rows, "WIDGET")}
    assert got == {"a", "b", "c"}


def test_search_empty_query_returns_nothing():
    assert query.search([_r("a")], "") == []
