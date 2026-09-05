"""Tasks 12-14 of the coord-fold plan (docs/superpowers/plans/2026-09-04-coord-fold.md): the old-side bridge.

12 - seed export: one bus-v4 `open` per slug the old fold says the agent owes; idempotent via a marker.
13 - dual-emit: every v3 write mirrored onto bus-v4 from the one chokepoint; no v4 config -> no-op; never fails v3.
14 - comparator + cutover-ready: (slug, pri, ptr) tuples; AGREE/DIVERGE; the trailing run decides readiness.

Every test drives the CLI or the chokepoint and asserts on what was WRITTEN, never on a return value alone.
"""
from __future__ import annotations

import argparse
import json

import pytest

from coord_engine import cli, dual_emit, records

V4 = "team/r/_coord/bus-v4/records.json"
V4_CFG = json.dumps({"data_type": "MomentAnnotation/v4", "api_version": "v1alpha1"})
V3_CFG = {"data_type": "MomentAnnotation/v3", "api_version": "v1alpha1"}


class FakeTransport:
    """docs: path -> text; records: every record_write; fail: paths whose reads answer 'error'."""

    def __init__(self, docs=None, fail=()):
        self.docs = dict(docs or {})
        self.records: list[tuple[str, str, dict, str]] = []
        self.fail = set(fail)
        self.refuse_record = False

    def read_classified(self, path, *, deadline=None):
        if path in self.fail:
            return None, "error"
        if path in self.docs:
            return self.docs[path], "ok"
        return None, "absent"

    def read(self, path):
        return self.read_classified(path)[0]

    def write(self, path, content):
        self.docs[path] = content
        return True

    def record_write(self, data_type, api_version, note, source, recorded_at=None, tags=None):
        if self.refuse_record:
            return False
        self.records.append((data_type, api_version, json.loads(note), source))
        return True


# ---------------------------------------------------------------- Task 13: dual-emit


def _emit(t, kind="directive", team="r", **kw):
    return records.emit_event(t, V3_CFG, sender="boss", to="me", kind=kind, priority="P1", slug="s1",
                              ptr="team/r/task/s1.md", team=team, **kw)


def test_a_v3_write_is_mirrored_onto_bus_v4_with_the_eight_field_payload():
    t = FakeTransport({V4: V4_CFG})
    assert _emit(t) is True
    v3 = [r for r in t.records if r[0] == "MomentAnnotation/v3"]
    v4 = [r for r in t.records if r[0] == "MomentAnnotation/v4"]
    assert len(v3) == 1 and len(v4) == 1
    p = v4[0][2]
    assert set(p) == {"v", "at", "from", "to", "kind", "slug", "pri", "ptr"}
    assert p["v"] == 1 and p["kind"] == "open" and p["from"] == "boss" and p["to"] == "me"
    assert p["slug"] == "s1" and p["pri"] == "P1" and p["ptr"] == "team/r/task/s1.md"


@pytest.mark.parametrize("v3_kind,v4_kind", [("directive", "open"), ("response", "close"), ("claim", "claim"),
                                             ("verdict", "note")])
def test_the_kind_map_is_exactly_the_documented_one(v3_kind, v4_kind):
    t = FakeTransport({V4: V4_CFG})
    _emit(t, kind=v3_kind)
    assert [r[2]["kind"] for r in t.records if r[0] == "MomentAnnotation/v4"] == [v4_kind]


def test_no_v4_config_means_no_mirror_and_the_v3_write_still_lands():
    """Mutation named by the plan: hardcode a cfg when absent -> this FAILS."""
    t = FakeTransport({})
    assert _emit(t) is True
    assert [r[0] for r in t.records] == ["MomentAnnotation/v3"]


def test_an_unreadable_v4_config_is_not_a_mirror_target():
    t = FakeTransport({}, fail={V4})
    assert _emit(t) is True
    assert [r[0] for r in t.records] == ["MomentAnnotation/v3"]


def test_a_mirror_failure_never_fails_the_v3_write(monkeypatch):
    t = FakeTransport({V4: V4_CFG})
    calls = {"n": 0}
    real = t.record_write

    def flaky(data_type, *a, **kw):
        if data_type == "MomentAnnotation/v4":
            raise RuntimeError("v4 down")
        return real(data_type, *a, **kw)

    monkeypatch.setattr(t, "record_write", flaky)
    assert _emit(t) is True                                     # v3 delivery is unaffected
    assert [r[0] for r in t.records] == ["MomentAnnotation/v3"]


def test_a_write_without_team_context_is_not_mirrored():
    t = FakeTransport({V4: V4_CFG})
    _emit(t, team=None)
    assert [r[0] for r in t.records] == ["MomentAnnotation/v3"]


# ---------------------------------------------------------------- Task 12: seed export


def _args(**kw):
    ns = argparse.Namespace(team="r", agent="me", force=False, min_run=24, min_hours=24.0, ship_check_rc=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _old_rows(monkeypatch, rows, ok=True, reason=""):
    monkeypatch.setattr(cli, "_old_open_set", lambda transport, team, agent: (rows, ok, reason))


ROWS = [
    {"id": "a", "priority": "P1", "path": "team/r/task/a.md", "owner": "boss"},
    {"id": "b", "priority": "P0", "path": "team/r/task/b.md", "owner": "boss"},
]


def test_export_open_writes_one_open_per_owed_slug_and_a_marker(monkeypatch, capsys):
    t = FakeTransport({V4: V4_CFG})
    _old_rows(monkeypatch, ROWS)
    assert cli.cmd_obligations_export_open(_args(), t) == 0
    opens = [r for r in t.records if r[0] == "MomentAnnotation/v4"]
    assert sorted(r[2]["slug"] for r in opens) == ["a", "b"]
    assert all(r[2]["kind"] == "open" and r[2]["to"] == "me" and r[3] == "me" for r in opens)
    assert {r[2]["slug"]: r[2]["ptr"] for r in opens} == {"a": "team/r/task/a.md", "b": "team/r/task/b.md"}
    marker = t.docs["team/r/_coord/bus-v4/seeded/me.md"]
    assert "written: 2" in marker and "skipped: 0" in marker
    assert "seeded 2 open(s)" in capsys.readouterr().out


def test_export_open_is_idempotent_via_the_marker(monkeypatch, capsys):
    """Mutation named by the plan: remove the marker guard -> this FAILS (four opens instead of two)."""
    t = FakeTransport({V4: V4_CFG})
    _old_rows(monkeypatch, ROWS)
    assert cli.cmd_obligations_export_open(_args(), t) == 0
    assert cli.cmd_obligations_export_open(_args(), t) == 2
    assert "already seeded" in capsys.readouterr().err
    assert len([r for r in t.records if r[0] == "MomentAnnotation/v4"]) == 2
    assert cli.cmd_obligations_export_open(_args(force=True), t) == 0                 # the explicit override re-seeds
    assert len([r for r in t.records if r[0] == "MomentAnnotation/v4"]) == 4


def test_export_open_refuses_without_v4_config_and_on_an_unknown_old_fold(monkeypatch, capsys):
    t = FakeTransport({})
    _old_rows(monkeypatch, ROWS)
    assert cli.cmd_obligations_export_open(_args(), t) == 3 and "no bus-v4 config" in capsys.readouterr().err
    t = FakeTransport({V4: V4_CFG})
    _old_rows(monkeypatch, ROWS, ok=False, reason="rows unreadable")
    assert cli.cmd_obligations_export_open(_args(), t) == 3 and "old fold is UNKNOWN" in capsys.readouterr().err
    assert t.records == []


def test_export_open_treats_an_unreadable_marker_as_unknown_not_absent(monkeypatch, capsys):
    t = FakeTransport({V4: V4_CFG}, fail={"team/r/_coord/bus-v4/seeded/me.md"})
    _old_rows(monkeypatch, ROWS)
    assert cli.cmd_obligations_export_open(_args(), t) == 3 and "unreadable" in capsys.readouterr().err
    assert t.records == []


def test_export_open_skips_a_row_without_a_pointer_and_says_so(monkeypatch, capsys):
    t = FakeTransport({V4: V4_CFG})
    _old_rows(monkeypatch, ROWS + [{"id": "c", "priority": "P2", "path": "", "owner": "boss"}])
    assert cli.cmd_obligations_export_open(_args(), t) == 2
    assert sorted(r[2]["slug"] for r in t.records) == ["a", "b"]
    assert "'c'" in t.docs["team/r/_coord/bus-v4/seeded/me.md"]


# ---------------------------------------------------------------- Task 14: comparator + cutover-ready


CKPT = "team/r/member/me/fold/checkpoint.json"
LOG = "team/r/_coord/bus-v4/compare/me.jsonl"


def _ckpt(open_rows):
    return json.dumps({"v": 1, "cursor": "x", "open": open_rows})


def test_compare_agrees_when_the_tuples_match_and_logs_it(monkeypatch, capsys):
    t = FakeTransport({CKPT: _ckpt({"a": {"pri": "P1", "ptr": "team/r/task/a.md"}, "b": {"pri": "P0", "ptr": "team/r/task/b.md"}})})
    _old_rows(monkeypatch, ROWS)
    assert cli.cmd_compare_to_fold(_args(), t) == 0
    assert capsys.readouterr().out.strip() == "AGREE n=2"
    line = json.loads(t.docs[LOG].strip())
    assert line["agree"] is True and line["n"] == 2 and line["only_old"] == [] and line["only_new"] == []


def test_compare_diverges_on_a_differing_pointer_or_priority_and_names_the_slugs(monkeypatch, capsys):
    t = FakeTransport({CKPT: _ckpt({"a": {"pri": "P1", "ptr": "team/r/task/OTHER.md"}, "b": {"pri": "P0", "ptr": "team/r/task/b.md"},
                                    "z": {"pri": "P3", "ptr": "team/r/task/z.md"}})})
    _old_rows(monkeypatch, ROWS)
    assert cli.cmd_compare_to_fold(_args(), t) == 2
    out = capsys.readouterr().out
    assert out.startswith("DIVERGE slugs=['a', 'z']")
    line = json.loads(t.docs[LOG].strip())
    assert line["agree"] is False and line["only_old"] == ["a"] and line["only_new"] == ["a", "z"]


def test_compare_is_unknown_when_either_side_cannot_be_read(monkeypatch, capsys):
    t = FakeTransport({})
    _old_rows(monkeypatch, ROWS)
    assert cli.cmd_compare_to_fold(_args(), t) == 3 and "checkpoint for me is absent" in capsys.readouterr().err
    t = FakeTransport({CKPT: _ckpt({})})
    _old_rows(monkeypatch, ROWS, ok=False, reason="rows unreadable")
    assert cli.cmd_compare_to_fold(_args(), t) == 3
    assert LOG not in t.docs                                                             # nothing logged on UNKNOWN


def _log(entries):
    return "".join(json.dumps(e) + "\n" for e in entries)


def _run(n_seq, start_hour=0):
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 9, 5, start_hour, 0, 0, tzinfo=timezone.utc)
    return [{"at": (t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "agent": "me", "agree": True, "n": n}
            for i, n in enumerate(n_seq)]


def test_cutover_ready_only_when_every_condition_holds(capsys):
    good = _run([5, 6, 6, 7, 6, 5, 5, 6, 7, 8, 7, 6, 6, 6, 7, 7, 8, 8, 7, 7, 6, 6, 7, 7] + [7], start_hour=0)   # 25 entries, spans 24h
    good[-1]["at"] = "2026-09-06T00:00:00Z"
    t = FakeTransport({LOG: _log(good), "team/r/_coord/bus-v4/drill/me.md": "drill: done"})
    assert cli.cmd_cutover_ready(_args(ship_check_rc=0), t) == 0
    assert "READY" in capsys.readouterr().out


def test_cutover_not_ready_names_each_failing_condition(capsys):
    t = FakeTransport({LOG: _log(_run([5, 5, 5]))})
    assert cli.cmd_cutover_ready(_args(), t) == 1
    out = capsys.readouterr().out
    assert "trailing AGREE run is 3 < 24" in out and "span" in out and "grow and shrink" in out
    assert "drill not recorded" in out and "ship gate not proven" in out


def test_a_diverge_breaks_the_trailing_run(capsys):
    entries = _run([5] * 30)
    entries[-2]["agree"] = False
    t = FakeTransport({LOG: _log(entries), "team/r/_coord/bus-v4/drill/me.md": "x"})
    assert cli.cmd_cutover_ready(_args(ship_check_rc=0, min_run=2, min_hours=0), t) == 1
    assert "trailing AGREE run is 1 < 2" in capsys.readouterr().out


def test_the_24h_span_check_is_real_one_minute_apart_entries_fail():
    """Mutation named by the plan: force the 24h check true -> this FAILS."""
    entries = [{"at": f"2026-09-05T00:{i:02d}:00Z", "agent": "me", "agree": True, "n": 5 + (i % 3) - 1} for i in range(30)]
    t = FakeTransport({LOG: _log(entries), "team/r/_coord/bus-v4/drill/me.md": "x"})
    assert cli.cmd_cutover_ready(_args(ship_check_rc=0), t) == 1


def test_the_window_can_be_compressed_only_by_explicit_flags(capsys):
    entries = _run([5, 6, 5], start_hour=0)
    t = FakeTransport({LOG: _log(entries), "team/r/_coord/bus-v4/drill/me.md": "x"})
    assert cli.cmd_cutover_ready(_args(ship_check_rc=0), t) == 1                      # defaults: 24 / 24h
    assert cli.cmd_cutover_ready(_args(ship_check_rc=0, min_run=3, min_hours=2.0), t) == 0
