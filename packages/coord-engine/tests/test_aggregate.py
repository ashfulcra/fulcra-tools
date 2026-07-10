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


def test_normalize_ts_parses_store_mtime_fallback():
    # FIX 1: the store's list-style mtime (the fallback when a task has no
    # `timestamp` frontmatter) must normalize to a parseable UTC-Z ISO — not pass
    # through raw — or the fold's skew math degrades to always-emit and seen_ids
    # grows unbounded. Format is `%Y-%m-%d %I:%M%p UTC` (transport.parse_list_output).
    assert aggregate._normalize_ts("2026-07-01 04:12PM UTC") == "2026-07-01T16:12:00Z"
    assert aggregate._normalize_ts("2026-07-09 09:00AM UTC") == "2026-07-09T09:00:00Z"
    assert aggregate._normalize_ts("2026-07-09 12:00PM UTC") == "2026-07-09T12:00:00Z"


def test_diff_transitions_ts_falls_back_to_mtime_as_iso():
    # a task row with NO `timestamp` but an mtime yields a parseable ISO-Z ts
    # (the ts contract holds for timestamp-less teams).
    new = [{"id": "a", "name": "a", "title": "A", "status": "proposed",
            "mtime": "2026-07-01 04:12PM UTC"}]
    (t,) = aggregate.diff_transitions([], new)
    assert t["ts"] == "2026-07-01T16:12:00Z"


def test_categorize_is_shared_source_of_both_diff_views():
    # FIX 3: diff_rows and diff_transitions both fold over _categorize, so they
    # can never drift on WHICH changes count (or their order). Drift-proof by
    # construction — not by a mirrored comment.
    prior = [_row("a", "active"), _row("b", "active")]
    new = [_row("a", "done"), _row("c", "proposed")]
    cats = aggregate._categorize(prior, new)
    assert [(k, r["id"]) for k, r, _p in cats] == [
        ("update", "a"), ("create", "c"), ("deprecate", "b")]
    # diff_transitions' categorization is exactly _categorize's
    assert [(t["kind"], t["task_id"]) for t in aggregate.diff_transitions(prior, new)] \
        == [(k, r["id"]) for k, r, _p in cats]
    # diff_rows renders exactly one bullet per category, in the same order
    assert len(aggregate.diff_rows(prior, new)) == len(cats)
