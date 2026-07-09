"""Tests for the pure projection fold (coord_engine.annotate.project).

The fold turns reconcile's task ``transitions`` into timeline AnnotationSpecs +
an advanced cursor. The load-bearing property is idempotency against the typed
ingest endpoint, which has NO server-side dedup and is async: a re-run (cases c/d)
must never re-emit a transition already projected. Everything is pure + stdlib.
"""

from __future__ import annotations

import json

from coord_engine import annotate, cli
from coord_engine.annotate import AnnotationSpec, project
from coord_engine_test_helpers import FakeTransport


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
    # ts spaced within the skew margin so all three stay in the retained window.
    txns = [
        _txn("T-1", "create", "2026-07-09T09:00:00Z", title="Alpha"),
        _txn("T-2", "update", "2026-07-09T09:05:00Z", title="Beta"),
        _txn("T-3", "complete", "2026-07-09T09:10:00Z", title="Gamma"),
    ]
    specs, new_cursor = project(txns, _fresh_cursor(), team=TEAM, now=NOW)
    assert len(specs) == 3
    assert all(isinstance(s, AnnotationSpec) for s in specs)
    # watermark advanced to the newest ts; every in-window id recorded in seen_ids
    assert new_cursor["last_ts"] == "2026-07-09T09:10:00Z"
    assert sorted(new_cursor["seen_ids"]) == sorted(s.id for s in specs)


def test_seen_ids_pruned_by_time_not_count():
    # ts spread WIDER than the skew margin: only the ids whose ts is within
    # SKEW_MARGIN_SECONDS of the advanced watermark are retained; older emitted
    # ids are dropped (the boundary already suppresses their re-fire).
    txns = [
        _txn("T-1", "create", "2026-07-09T09:00:00Z"),   # 70 min back -> pruned
        _txn("T-2", "update", "2026-07-09T09:55:00Z"),   # 15 min back -> kept (== margin)
        _txn("T-3", "complete", "2026-07-09T10:10:00Z"),  # watermark -> kept
    ]
    specs, new_cursor = project(txns, _fresh_cursor(), team=TEAM, now=NOW)
    assert len(specs) == 3  # all three still EMITTED
    ids = {s.task_id: s.id for s in specs}
    assert new_cursor["last_ts"] == "2026-07-09T10:10:00Z"
    # T-1 (older than the margin) pruned out; T-2/T-3 retained
    assert ids["T-1"] not in new_cursor["seen_ids"]
    assert ids["T-2"] in new_cursor["seen_ids"]
    assert ids["T-3"] in new_cursor["seen_ids"]
    # seen_ts carries the ts of each retained id (exact prune, no drift)
    assert new_cursor["seen_ts"][ids["T-3"]] == "2026-07-09T10:10:00Z"


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


# --- skew-lookback boundary: same-ts newcomer, burst replay, unparseable ts -

def test_same_ts_newcomer_still_emits():
    # THE (a) fix: a genuinely-new transition whose ts EQUALS the watermark must
    # still land. Strict ``> watermark`` would drop it forever; the skew lookback
    # (>= watermark - margin) plus the seen_ids miss lets the newcomer through.
    prior_id = annotate._stable_id(TEAM, "T-old", "create", "2026-07-09T11:00:00Z")
    cursor = {"last_ts": "2026-07-09T11:00:00Z", "seen_ids": [prior_id],
              "seen_ts": {prior_id: "2026-07-09T11:00:00Z"}}
    newcomer = _txn("T-new", "update", "2026-07-09T11:00:00Z", title="Same tick")
    specs, new_cursor = project([newcomer], cursor, team=TEAM, now=NOW)
    assert [s.task_id for s in specs] == ["T-new"]
    assert specs[0].ts == "2026-07-09T11:00:00Z"


def test_burst_replay_beyond_window_does_not_reemit():
    # THE (b) fix: an already-emitted transition whose id has been pruned out of
    # seen_ids (its ts is now far behind the watermark) must NOT re-emit when it
    # replays — the boundary suppresses it even though seen_ids no longer holds it.
    old = _txn("T-1", "create", "2026-07-09T09:00:00Z", title="Alpha")
    (spec1,), cursor1 = project([old], _fresh_cursor(), team=TEAM, now=NOW)
    # A fresh emit far in the future advances the watermark well past the margin,
    # pruning the old id out of the retained window.
    newer = _txn("T-2", "update", "2026-07-09T12:00:00Z", title="Beta")
    _, cursor2 = project([newer], cursor1, team=TEAM, now=NOW)
    assert spec1.id not in cursor2["seen_ids"]  # evicted by the time-prune
    # The old transition replays (async duplicate). It is > the margin behind the
    # watermark, so the boundary drops it — no double-write despite the eviction.
    specs3, _ = project([old], cursor2, team=TEAM, now=NOW)
    assert specs3 == []


def test_unparseable_ts_does_not_raise_and_does_not_drop():
    # An unparseable (non-normalized) ts must never crash and never be silently
    # dropped — it degrades to "emit / keep" rather than vanishing from the timeline.
    bad = {"task_id": "T-x", "kind": "update", "ts": "not-a-timestamp"}
    # fresh cursor: emits
    specs, cursor = project([bad], _fresh_cursor(), team=TEAM, now=NOW)
    assert [s.task_id for s in specs] == ["T-x"]
    # even with a real watermark set, an unparseable txn ts is kept (not dropped)
    cursor2 = {"last_ts": "2026-07-09T11:00:00Z", "seen_ids": []}
    specs2, _ = project([bad], cursor2, team=TEAM, now=NOW)
    assert [s.task_id for s in specs2] == ["T-x"]


def test_generator_transitions_are_consumed_not_treated_as_empty():
    # LOW-2: a generator source must be projected, not silently dropped.
    gen = (_txn(f"T-{i}", "update", f"2026-07-09T09:0{i}:00Z") for i in range(3))
    specs, _ = project(gen, _fresh_cursor(), team=TEAM, now=NOW)
    assert len(specs) == 3


# ===========================================================================
# CLI paths: resolution gate + project (end-to-end idempotency) + status
# ===========================================================================

def _task_ts(title, status, ts):
    return (f"---\ntype: Task\ntitle: {title}\nstatus: {status}\n"
            f"timestamp: {ts}\n---\nbody")


def _stub_writer(monkeypatch, *, ok=True):
    """Replace the real fulcra_common writer seam with a recorder. Returns the
    list of AnnotationSpecs the CLI tried to emit."""
    seen = []
    def fake(spec, *, agent):
        seen.append(spec)
        return ok
    monkeypatch.setattr(cli, "_emit_projection_spec", fake)
    return seen


# --- resolution config axis (validated; stored on the bus) ------------------

def test_cli_resolution_sets_level_and_status_reads_it(capsys):
    t = FakeTransport()
    assert cli.main(["annotate", "resolution", "r", "transitions"], transport=t) == 0
    assert annotate.read_resolution(t, "r") == "transitions"
    assert cli.main(["annotate", "status", "r"], transport=t) == 0
    out = capsys.readouterr().out
    assert "resolution=transitions" in out and "last_ts=(none)" in out


def test_cli_resolution_unknown_level_exits_2_and_writes_nothing(capsys):
    t = FakeTransport()
    assert cli.main(["annotate", "resolution", "r", "verbose"], transport=t) == 2
    err = capsys.readouterr().err
    assert "unknown resolution; known: off, transitions" in err
    assert annotate.resolution_path("r") not in t.store  # rejected before any write


def test_cli_resolution_off_is_accepted(capsys):
    t = FakeTransport()
    assert cli.main(["annotate", "resolution", "r", "off"], transport=t) == 0
    assert annotate.read_resolution(t, "r") == "off"


# --- project gate: off / absent -> refuse, exit 0, zero writes --------------

def test_cli_project_refuses_when_resolution_absent(capsys, monkeypatch):
    t = FakeTransport()
    seen = _stub_writer(monkeypatch)
    assert cli.main(["annotate", "project", "r"], transport=t) == 0
    assert "projection off" in capsys.readouterr().out
    assert seen == []                                   # nothing emitted
    assert annotate.cursor_path("r") not in t.store     # cursor untouched


def test_cli_project_refuses_when_resolution_off(capsys, monkeypatch):
    t = FakeTransport()
    t.put(annotate.resolution_path("r"), "off\n")
    seen = _stub_writer(monkeypatch)
    assert cli.main(["annotate", "project", "r"], transport=t) == 0
    assert seen == []


# --- end-to-end idempotency THROUGH the CLI (real fold + stubbed writer) -----

def _reconcile_then(t, monkeypatch, *, ok=True):
    """Opt in, seed a task, reconcile (writes pending), stub the writer."""
    t.put(annotate.resolution_path("r"), "transitions\n")
    t.put("team/r/task/a.md", _task_ts("Alpha", "active", "2026-07-09T09:00:00Z"))
    assert cli.main(["reconcile", "r"], transport=t) == 0
    return _stub_writer(monkeypatch, ok=ok)


def test_cli_project_emits_once_then_rerun_emits_zero(capsys, monkeypatch):
    t = FakeTransport()
    seen = _reconcile_then(t, monkeypatch)
    # first project: the creation transition lands once
    assert cli.main(["annotate", "project", "r"], transport=t) == 0
    assert "projected 1/1" in capsys.readouterr().out
    assert [s.task_id for s in seen] == ["a"]
    assert annotate.cursor_path("r") in t.store   # cursor advanced (all emitted)
    # re-run over the SAME pending + advanced cursor: zero new emits (idempotency
    # against the no-dedup endpoint, proven end-to-end through the CLI)
    assert cli.main(["annotate", "project", "r"], transport=t) == 0
    assert "projected 0/0" in capsys.readouterr().out
    assert [s.task_id for s in seen] == ["a"]      # writer NOT called again


def test_cli_project_cursor_round_trips(monkeypatch):
    t = FakeTransport()
    _reconcile_then(t, monkeypatch)
    cli.main(["annotate", "project", "r"], transport=t)
    cur = json.loads(t.store[annotate.cursor_path("r")])
    assert cur["last_ts"] == "2026-07-09T09:00:00Z"
    assert len(cur["seen_ids"]) == 1


# --- writer absent -> degrade to exit 0, cursor NOT advanced (retry later) --

def test_cli_project_writer_absent_degrades_exit_0(capsys, monkeypatch):
    t = FakeTransport()
    seen = _reconcile_then(t, monkeypatch, ok=False)  # writer returns False
    assert cli.main(["annotate", "project", "r"], transport=t) == 0
    assert "projected 0/1" in capsys.readouterr().out
    assert seen and seen[0].task_id == "a"            # it TRIED to emit
    # cursor left unadvanced so a later run (working writer) still projects it
    assert annotate.cursor_path("r") not in t.store


def test_cli_project_missing_fulcra_common_is_not_fatal(monkeypatch):
    # the real seam: fulcra_common may be entirely absent on a stdlib-only host.
    # _emit_projection_spec must swallow the ImportError -> False, exit 0.
    import builtins
    real_import = builtins.__import__
    def blocked(name, *a, **k):
        if name.startswith("fulcra_common"):
            raise ImportError("no fulcra_common on this host")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", blocked)

    class _Spec:
        note = "x"; tags = ["agent-tasks", "create"]; ts = "2026-07-09T09:00:00Z"
    assert cli._emit_projection_spec(_Spec(), agent="h") is False


def test_cli_status_json_reports_cursor(capsys, monkeypatch):
    t = FakeTransport()
    _reconcile_then(t, monkeypatch)
    cli.main(["annotate", "project", "r"], transport=t)
    capsys.readouterr()
    assert cli.main(["annotate", "status", "r", "--json"], transport=t) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["resolution"] == "transitions"
    assert payload["projecting"] is True
    assert payload["last_ts"] == "2026-07-09T09:00:00Z"
    assert payload["seen_ids"] == 1
