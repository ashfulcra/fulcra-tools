from coord_engine import aggregate


def test_build_aggregate_shape():
    agg = aggregate.build_aggregate(
        "research", [{"id": "a"}], generated_at="2026-07-01T00:00:00Z",
        reconcile_host="host-x",
    )
    assert agg["schema"] == "coord.teams.summaries.v1"
    assert agg["team"] == "research"
    assert agg["reconcile_host"] == "host-x"
    assert agg["rows"] == [{"id": "a"}]
    assert agg["warnings"] == []


def test_aggregate_rows_tolerates_garbage():
    assert aggregate.aggregate_rows(None) == []
    assert aggregate.aggregate_rows({"rows": "nope"}) == []
    assert aggregate.aggregate_rows({"rows": [{"id": "a"}]}) == [{"id": "a"}]


def test_diff_creation():
    out = aggregate.diff_rows([], [{"id": "a", "name": "a", "title": "A", "status": "active"}])
    assert len(out) == 1
    assert "Creation" in out[0] and "[A](a.md)" in out[0] and "active" in out[0]


def test_diff_status_transition():
    prior = [{"id": "a", "name": "a", "title": "A", "status": "active"}]
    new = [{"id": "a", "name": "a", "title": "A", "status": "done"}]
    out = aggregate.diff_rows(prior, new)
    assert len(out) == 1
    assert "Update" in out[0] and "active → done" in out[0]


def test_diff_removal():
    prior = [{"id": "a", "name": "a", "title": "A", "status": "active"}]
    out = aggregate.diff_rows(prior, [])
    assert len(out) == 1
    assert "Deprecation" in out[0]


def test_diff_content_only_change_not_logged():
    # same status, different description -> no log entry (it's in file version history)
    prior = [{"id": "a", "name": "a", "title": "A", "status": "active", "description": "old"}]
    new = [{"id": "a", "name": "a", "title": "A", "status": "active", "description": "new"}]
    assert aggregate.diff_rows(prior, new) == []


def test_rows_by_id_skips_idless():
    rows = [{"id": "a"}, {"no_id": 1}, {"id": "b"}]
    assert set(aggregate.rows_by_id(rows)) == {"a", "b"}
