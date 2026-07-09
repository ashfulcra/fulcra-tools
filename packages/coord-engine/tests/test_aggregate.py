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


# --- diff_transitions: the ADDITIVE structured sibling of diff_rows -----------

def _row(rid, status, *, title=None, ts="2026-07-09T09:00:00Z", **extra):
    r = {"id": rid, "name": rid, "title": title or rid.upper(),
         "status": status, "timestamp": ts}
    r.update(extra)
    return r


def test_diff_rows_bullets_are_byte_identical_to_before():
    # GUARDRAIL: adding diff_transitions must not perturb diff_rows' output.
    prior = [_row("a", "active"), _row("b", "active")]
    new = [_row("a", "done"), _row("c", "proposed")]  # a: update, b: removed, c: created
    # order: new.items() first (a=update, c=create), then removals (b)
    assert aggregate.diff_rows(prior, new) == [
        "* **Update**: [A](a.md) active → done.",
        "* **Creation**: [C](c.md) created (proposed).",
        "* **Deprecation**: [B](b.md) removed.",
    ]


def test_diff_transitions_carries_updated_at_as_ts():
    prior = [_row("a", "active", ts="2026-07-09T08:00:00Z")]
    new = [_row("a", "done", ts="2026-07-09T10:30:00Z")]
    (t,) = aggregate.diff_transitions(prior, new)
    assert t == {"task_id": "a", "kind": "update", "ts": "2026-07-09T10:30:00Z",
                 "title": "A"}


def test_diff_transitions_categories_mirror_diff_rows():
    prior = [_row("a", "active"), _row("b", "active")]
    new = [_row("a", "done"), _row("c", "proposed")]
    kinds = {t["task_id"]: t["kind"] for t in aggregate.diff_transitions(prior, new)}
    assert kinds == {"a": "update", "b": "deprecate", "c": "create"}


def test_diff_transitions_optional_fields_and_content_edit_ignored():
    # optional assignee/next_action ride along; a content-only edit is NOT a txn
    prior = [_row("a", "active", description="old")]
    new = [_row("a", "active", description="new"),
           _row("b", "proposed", assignee="claude:s", next_action="ship")]
    txns = aggregate.diff_transitions(prior, new)
    assert [t["task_id"] for t in txns] == ["b"]  # content-only edit on a skipped
    assert txns[0]["assignee"] == "claude:s" and txns[0]["next_action"] == "ship"
    # no assignee/next_action keys when the row lacks them
    only = aggregate.diff_transitions([], [_row("z", "proposed")])[0]
    assert "assignee" not in only and "next_action" not in only


def test_diff_transitions_ts_normalized_to_utc_z():
    # a non-Z / offset ts is normalized to zero-padded UTC-Z; unparseable passes
    # through; missing -> "".
    assert aggregate._normalize_ts("2026-07-09T05:00:00-04:00") == "2026-07-09T09:00:00Z"
    assert aggregate._normalize_ts("2026-07-09T09:00:00Z") == "2026-07-09T09:00:00Z"
    assert aggregate._normalize_ts("not-a-ts") == "not-a-ts"
    assert aggregate._normalize_ts(None) == ""
    assert aggregate._normalize_ts("") == ""
