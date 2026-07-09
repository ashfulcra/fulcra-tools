"""Tests for the pure projection fold (coord_engine.annotate.project).

The fold turns reconcile's task ``transitions`` into timeline AnnotationSpecs +
an advanced cursor. The load-bearing property is idempotency against the typed
ingest endpoint, which has NO server-side dedup and is async: a re-run (cases c/d)
must never re-emit a transition already projected. Everything is pure + stdlib.
"""

from __future__ import annotations

from coord_engine import annotate
from coord_engine.annotate import AnnotationSpec, project


TEAM = "demo"
NOW = "2026-07-09T12:00:00Z"


def _txn(task_id, kind, ts, *, title=None, assignee=None, next_action=None):
    """A structured transition row (the shape the fold consumes)."""
    row = {"task_id": task_id, "kind": kind, "ts": ts}
    if title is not None:
        row["title"] = title
    if assignee is not None:
        row["assignee"] = assignee
    if next_action is not None:
        row["next_action"] = next_action
    return row


def _fresh_cursor():
    return {"last_ts": None, "seen_ids": []}


# --- (a) empty transitions -> no specs, cursor unchanged --------------------

def test_empty_transitions_no_specs_cursor_unchanged():
    cursor = {"last_ts": "2026-07-08T00:00:00Z", "seen_ids": ["deadbeef"]}
    specs, new_cursor = project([], cursor, team=TEAM, now=NOW)
    assert specs == []
    assert new_cursor == cursor


def test_empty_transitions_fresh_cursor_normalized():
    specs, new_cursor = project([], None, team=TEAM, now=NOW)
    assert specs == []
    assert new_cursor == {"last_ts": None, "seen_ids": []}


# --- (b) N transitions -> N specs, ids deterministic + stable across re-run -

def test_n_transitions_yield_n_specs():
    txns = [
        _txn("T-1", "create", "2026-07-09T09:00:00Z", title="Alpha"),
        _txn("T-2", "update", "2026-07-09T10:00:00Z", title="Beta"),
        _txn("T-3", "complete", "2026-07-09T11:00:00Z", title="Gamma"),
    ]
    specs, new_cursor = project(txns, _fresh_cursor(), team=TEAM, now=NOW)
    assert len(specs) == 3
    assert all(isinstance(s, AnnotationSpec) for s in specs)
    # watermark advanced to the newest ts; every id recorded in seen_ids
    assert new_cursor["last_ts"] == "2026-07-09T11:00:00Z"
    assert sorted(new_cursor["seen_ids"]) == sorted(s.id for s in specs)


def test_ids_deterministic_and_stable_across_reruns():
    txns = [
        _txn("T-1", "create", "2026-07-09T09:00:00Z", title="Alpha"),
        _txn("T-2", "update", "2026-07-09T10:00:00Z", title="Beta"),
    ]
    specs_a, _ = project(txns, _fresh_cursor(), team=TEAM, now=NOW)
    specs_b, _ = project(txns, _fresh_cursor(), team=TEAM, now="2099-01-01T00:00:00Z")
    # same inputs (from the same starting cursor) -> byte-identical ids,
    # independent of ``now`` (id keys on team/task_id/kind/ts only).
    assert [s.id for s in specs_a] == [s.id for s in specs_b]


def test_id_keys_on_team_task_kind_ts():
    base = _txn("T-1", "create", "2026-07-09T09:00:00Z", title="Alpha")
    (s0,), _ = project([base], _fresh_cursor(), team=TEAM, now=NOW)
    # changing any component of the key changes the id
    (s_team,), _ = project([base], _fresh_cursor(), team="other", now=NOW)
    (s_task,), _ = project([_txn("T-2", "create", "2026-07-09T09:00:00Z")],
                           _fresh_cursor(), team=TEAM, now=NOW)
    (s_kind,), _ = project([_txn("T-1", "update", "2026-07-09T09:00:00Z")],
                           _fresh_cursor(), team=TEAM, now=NOW)
    (s_ts,), _ = project([_txn("T-1", "create", "2026-07-09T09:30:00Z")],
                         _fresh_cursor(), team=TEAM, now=NOW)
    assert len({s0.id, s_team.id, s_task.id, s_kind.id, s_ts.id}) == 5


# --- (c) re-run with advanced cursor -> zero new specs (idempotency) --------

def test_rerun_with_advanced_cursor_emits_nothing():
    txns = [
        _txn("T-1", "create", "2026-07-09T09:00:00Z", title="Alpha"),
        _txn("T-2", "update", "2026-07-09T10:00:00Z", title="Beta"),
    ]
    specs1, cursor1 = project(txns, _fresh_cursor(), team=TEAM, now=NOW)
    assert len(specs1) == 2
    specs2, cursor2 = project(txns, cursor1, team=TEAM, now=NOW)
    assert specs2 == []
    # cursor is stable under the idempotent re-run
    assert cursor2 == cursor1


# --- (d) transition already in seen_ids -> skipped even if ts >= watermark --

def test_seen_id_skipped_even_when_ts_at_or_after_watermark():
    txn = _txn("T-1", "create", "2026-07-09T09:00:00Z", title="Alpha")
    (spec,), _ = project([txn], _fresh_cursor(), team=TEAM, now=NOW)
    # A cursor whose watermark is BEHIND the txn ts (clock-skew / rewind) but
    # whose seen_ids already contains this id. ts >= watermark, yet the seen
    # guard must still suppress the double-fire.
    skewed = {"last_ts": "2026-07-01T00:00:00Z", "seen_ids": [spec.id]}
    specs, new_cursor = project([txn], skewed, team=TEAM, now=NOW)
    assert specs == []
    assert spec.id in new_cursor["seen_ids"]


# --- (e) malformed row skipped, others projected, cursor advances past it ---

def test_malformed_row_skipped_others_projected():
    txns = [
        _txn("T-1", "create", "2026-07-09T09:00:00Z", title="Alpha"),
        {"kind": "update", "ts": "2026-07-09T10:00:00Z"},  # no task_id -> malformed
        "not-a-dict",                                        # wrong type -> malformed
        {"task_id": "T-3", "ts": "2026-07-09T10:30:00Z"},   # no kind -> malformed
        {"task_id": "T-4", "kind": "update"},                # no ts -> malformed
        _txn("T-5", "complete", "2026-07-09T11:00:00Z", title="Epsilon"),
    ]
    specs, new_cursor = project(txns, _fresh_cursor(), team=TEAM, now=NOW)
    assert [s.task_id for s in specs] == ["T-1", "T-5"]
    # cursor advanced past the good rows (never gets stuck on malformed input)
    assert new_cursor["last_ts"] == "2026-07-09T11:00:00Z"


def test_project_never_raises_on_garbage():
    # None cursor, garbage transitions, missing fields — must degrade, not raise.
    specs, new_cursor = project(
        [None, 42, {}, {"task_id": "x"}], "not-a-cursor", team=TEAM, now=NOW
    )
    assert specs == []
    assert new_cursor == {"last_ts": None, "seen_ids": []}


# --- (f) note contains only served keys; title text lives inside note -------

def test_note_carries_title_kind_assignee_next_action():
    txn = _txn("T-1", "pickup", "2026-07-09T09:00:00Z",
               title="Fix the widget", assignee="claude:sess", next_action="ship it")
    (spec,), _ = project([txn], _fresh_cursor(), team=TEAM, now=NOW)
    # title text is folded INTO note (title is not a served MomentAnnotation key)
    assert "Fix the widget" in spec.note
    assert "pickup" in spec.note
    assert "claude:sess" in spec.note
    assert "ship it" in spec.note
    # served-set discipline: the spec carries no free-standing ``title`` key
    assert not hasattr(spec, "title")


def test_tags_are_definition_plus_kind():
    txn = _txn("T-1", "update", "2026-07-09T09:00:00Z", title="Alpha")
    (spec,), _ = project([txn], _fresh_cursor(), team=TEAM, now=NOW)
    assert spec.tags == [annotate.DEFINITION_TAG, "update"]


def test_spec_field_set_is_bounded():
    txn = _txn("T-1", "create", "2026-07-09T09:00:00Z", title="Alpha")
    (spec,), _ = project([txn], _fresh_cursor(), team=TEAM, now=NOW)
    assert set(vars(spec)) == {"id", "note", "tags", "kind", "task_id", "ts"}


# --- seen_ids window bound --------------------------------------------------

def test_seen_ids_pruned_to_window():
    many = [
        _txn(f"T-{i}", "update", f"2026-07-09T00:00:{i:02d}Z")
        for i in range(annotate.SEEN_IDS_WINDOW + 25)
    ]
    specs, new_cursor = project(many, _fresh_cursor(), team=TEAM, now=NOW)
    assert len(specs) == annotate.SEEN_IDS_WINDOW + 25
    # cursor stays bounded — only the most-recent window of ids is retained
    assert len(new_cursor["seen_ids"]) == annotate.SEEN_IDS_WINDOW
    # the retained ids are the most recent ones (tail of the emit order)
    assert new_cursor["seen_ids"] == [s.id for s in specs][-annotate.SEEN_IDS_WINDOW:]
