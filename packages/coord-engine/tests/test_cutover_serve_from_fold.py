"""The bus-v4 cutover switch (coord_engine.cutover): what `obligations` and `needs-me` answer from.

Truths, each a failure it prevents:
* No switch -> files, exactly today's answer. An unreadable or malformed switch -> UNKNOWN (rc 3) and NOTHING is
  consulted: after the flip, answering from files on a switch you cannot read is split-brain (codex-reviewer P0).
* serve=fold never invokes the task index, role resolution or the review listing (codex-coder P0): sentinels
  on every file-plane entry point prove it.
* serve=fold -> the agent's coord-fold checkpoint IS the answer; the file plane is not consulted for the rows.
* serve=fold with no checkpoint -> UNKNOWN (rc 3), never CLEAR: an unseeded identity does not owe nothing.
* `cutover set` writes the switch once and prints before/after; `--serve files` is the rollback, same verb.
"""
from __future__ import annotations

import argparse
import json

import pytest

from coord_engine import cli, cutover, obligations as obligations_mod

TEAM = "acme"
AGENT = "me"
SWITCH = cutover.switch_path(TEAM)
CKPT = cutover.checkpoint_path(TEAM, AGENT)


class FakeTransport:
    def __init__(self, docs=None, *, error_paths=()):
        self.docs = dict(docs or {})
        self.error_paths = set(error_paths)
        self.writes = []

    def read_classified(self, path, *, deadline=None):
        if path in self.error_paths:
            return None, "error"
        if path in self.docs:
            return self.docs[path], "ok"
        return None, "absent"

    def read(self, path, **kw):
        return self.read_classified(path)[0]

    def write(self, path, content):
        self.docs[path] = content
        self.writes.append(path)
        return True


def _ckpt(open_rows, *, cursor="2026-09-06T13:26:01+00:00", generation=71):
    return json.dumps({"v": 1, "cursor": cursor, "open": open_rows, "unread_events": 0, "unreadable_pointers": [],
                       "seen": [], "generation": generation, "writer": "me:abc"})


def _switch(serve, by="coord-boss", at="2026-09-06T20:00:00Z"):
    return cutover.switch_doc(serve=serve, by=by, reason="test", at=at)


ROWS = {"do-the-thing-1234abcd": {"pri": "P1", "from": "boss", "to": AGENT, "ptr": "team/acme/task/do-the-thing-1234abcd.md", "at": "2026-09-06T10:00:00Z"},
        "star-row-deadbeef": {"pri": "P2", "from": "boss", "to": "*", "ptr": "team/acme/task/star-row-deadbeef.md", "at": "2026-09-06T09:00:00Z"}}


# ---- the switch --------------------------------------------------------------------------------------------

def test_no_switch_means_files():
    assert cutover.read_switch(FakeTransport(), TEAM) == ("files", "no cutover switch on the bus; serving from files")


@pytest.mark.parametrize("body", ["not json", "[]", json.dumps({"v": 1}), json.dumps({"v": 2, "serve": "fold"}),
                                  json.dumps({"v": 1, "serve": "FOLD"}), json.dumps({"v": 1, "serve": "stream"})])
def test_a_malformed_switch_is_unknown_authority_never_files_never_fold(body):
    """Bytes exist and are not a switch: after the flip this could be a corrupted fold switch, so answering from
    files would be split-brain (codex-reviewer P0). Only an ABSENT switch selects files."""
    serve, why = cutover.read_switch(FakeTransport({SWITCH: body}), TEAM)
    assert serve == "unknown" and "authority unknown" in why


def test_an_unreadable_switch_is_unknown_authority_and_says_so():
    serve, why = cutover.read_switch(FakeTransport(error_paths=[SWITCH]), TEAM)
    assert serve == "unknown" and "error" in why


def test_a_switch_read_that_raises_is_unknown_authority():
    class Boom:
        def read_classified(self, path, **kw):
            raise RuntimeError("transport down")
    serve, why = cutover.read_switch(Boom(), TEAM)
    assert serve == "unknown" and "transport down" in why


def test_only_a_well_formed_fold_switch_serves_from_the_fold():
    serve, why = cutover.read_switch(FakeTransport({SWITCH: _switch("fold")}), TEAM)
    assert serve == "fold" and "coord-boss" in why
    assert cutover.read_switch(FakeTransport({SWITCH: _switch("files")}), TEAM)[0] == "files"


def test_switch_doc_refuses_a_bad_value_or_an_empty_reason():
    with pytest.raises(ValueError):
        cutover.switch_doc(serve="stream", by="x", reason="r", at="t")
    with pytest.raises(ValueError):
        cutover.switch_doc(serve="fold", by="x", reason="   ", at="t")


# ---- the fold rows -----------------------------------------------------------------------------------------

def test_fold_rows_come_from_the_checkpoint_in_needs_me_shape_with_a_source_row():
    rows, why = cutover.fold_rows(FakeTransport({CKPT: _ckpt(ROWS)}), TEAM, AGENT)
    assert rows is not None and "generation 71" in why
    ids = [r["id"] for r in rows if "id" in r]
    assert ids == ["do-the-thing-1234abcd", "star-row-deadbeef"]                   # P1 before P2
    first = rows[0]
    assert first["priority"] == "P1" and first["status"] == "open" and first["assignee"] == AGENT
    assert first["ptr"] == "team/acme/task/do-the-thing-1234abcd.md" and first["served_from"] == "coord-fold"
    src = rows[-1]
    assert src["type"] == "needs-me-source" and src["source"] == "projection" and src["fold"] == "coord-fold"
    assert src["as_of"] == "2026-09-06T13:26:01+00:00"


@pytest.mark.parametrize("docs,expect", [
    ({}, "absent"),
    ({CKPT: "nope"}, "not JSON"),
    ({CKPT: json.dumps({"v": 2, "open": {}})}, "malformed"),
    ({CKPT: json.dumps({"v": 1, "open": {"x": "not-a-row"}})}, "non-object row"),
])
def test_a_checkpoint_that_cannot_answer_is_none_with_the_reason(docs, expect):
    rows, why = cutover.fold_rows(FakeTransport(docs), TEAM, AGENT)
    assert rows is None and expect in why


def test_an_unreadable_checkpoint_is_none():
    rows, why = cutover.fold_rows(FakeTransport(error_paths=[CKPT]), TEAM, AGENT)
    assert rows is None and "error" in why


# ---- obligations and needs-me: the public reads ------------------------------------------------------------

class _FilePlaneTouched(AssertionError):
    pass


def _sentinel(*a, **kw):
    raise _FilePlaneTouched("FILE_PLANE_WAS_CONSULTED")


def _arm_file_plane_sentinels(monkeypatch):
    """Every file-plane entry point raises. Under serve=fold and serve=unknown none may be reached
    (codex-coder P0: a diagnostic sentinel on _load_rows_status fired for both public reads on 6445cde8)."""
    for name in ("_load_rows_status", "_held_roles_for_rows", "_needs_me_rows", "_pending_reviews_for",
                 "_blocked_on_human_section"):
        monkeypatch.setattr(cli, name, _sentinel)
    monkeypatch.setattr(cli, "_forge_feedback_for", lambda *a, **kw: [])


def _obl_args(**kw):
    return argparse.Namespace(team=TEAM, agent=AGENT, json=True, **kw)


def _obligations(transport, capsys):
    rc = cli.cmd_obligations(_obl_args(), transport)
    out = capsys.readouterr().out
    return rc, json.loads([l for l in out.splitlines() if l.startswith("{")][-1])


def _nm_args(**kw):
    return argparse.Namespace(team=TEAM, agent=AGENT, json=True, all=False, envelope_only=False, **kw)


def _needs_me(transport, capsys):
    rc = cli.cmd_needs_me(_nm_args(), transport)
    out = capsys.readouterr().out
    return rc, json.loads([l for l in out.splitlines() if l.startswith("{")][-1])


def test_obligations_served_from_the_fold_never_touch_the_file_plane(monkeypatch, capsys):
    _arm_file_plane_sentinels(monkeypatch)
    rc, payload = _obligations(FakeTransport({SWITCH: _switch("fold"), CKPT: _ckpt(ROWS)}), capsys)
    assert rc == 0 and payload["state"] == "DATA" and payload["owed_count"] >= 2, payload
    assert set(payload["consulted"]) == set(obligations_mod.OBLIGATION_COMPONENTS) and not payload["degraded"]


def test_obligations_served_from_the_fold_with_no_checkpoint_are_unknown_never_clear(monkeypatch, capsys):
    _arm_file_plane_sentinels(monkeypatch)
    rc, payload = _obligations(FakeTransport({SWITCH: _switch("fold")}), capsys)
    assert rc == 3 and payload["state"] == "UNKNOWN", payload
    assert any("has not seeded its fold" in str(v) for v in payload["details"].values()), payload["details"]


def test_obligations_with_an_unreadable_switch_are_unknown_and_consult_nothing(monkeypatch, capsys):
    """codex-reviewer P0, the integrated negative control: switch read error + an empty file plane used to read
    rc=0 CLEAR. Now rc 3 UNKNOWN, and the file plane is not even consulted."""
    _arm_file_plane_sentinels(monkeypatch)
    rc, payload = _obligations(FakeTransport(error_paths=[SWITCH]), capsys)
    assert rc == 3 and payload["state"] == "UNKNOWN" and payload["owed_count"] == 0, payload
    assert all("authority unknown" in str(v) for v in payload["details"].values()), payload["details"]
    rc, payload = _obligations(FakeTransport({SWITCH: "corrupt"}), capsys)
    assert rc == 3 and payload["state"] == "UNKNOWN"


def test_obligations_with_no_switch_still_answer_from_the_file_plane(monkeypatch, capsys):
    """The pre-cutover path is untouched: the file-plane fold is consulted, the checkpoint is not."""
    called = []
    monkeypatch.setattr(cli, "_load_rows_status", lambda tr, team, **kw: (called.append("index") or ([], True, "")))
    monkeypatch.setattr(cli, "_held_roles_for_rows", lambda *a, **kw: (set(), []))
    monkeypatch.setattr(cli, "_needs_me_rows", lambda *a, **kw: (called.append("needs-me") or []))
    monkeypatch.setattr(cli, "_pending_reviews_for", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "_forge_feedback_for", lambda *a, **kw: [])
    cli.cmd_obligations(_obl_args(), FakeTransport({CKPT: _ckpt(ROWS)}))
    assert called == ["index", "needs-me"]


def test_needs_me_served_from_the_fold_never_touches_the_file_plane(monkeypatch, capsys):
    _arm_file_plane_sentinels(monkeypatch)
    rc, env = _needs_me(FakeTransport({SWITCH: _switch("fold"), CKPT: _ckpt(ROWS)}), capsys)
    assert rc == 0 and env["health"] == "DATA" and env["source"] == "projection", env


def test_needs_me_served_from_the_fold_with_no_checkpoint_is_unknown_never_clear(monkeypatch, capsys):
    _arm_file_plane_sentinels(monkeypatch)
    rc, env = _needs_me(FakeTransport({SWITCH: _switch("fold")}), capsys)
    assert rc == 3 and env["health"] == "UNKNOWN", env


def test_needs_me_with_an_unreadable_switch_is_unknown_and_consults_nothing(monkeypatch, capsys):
    _arm_file_plane_sentinels(monkeypatch)
    rc, env = _needs_me(FakeTransport(error_paths=[SWITCH]), capsys)
    assert rc == 3 and env["health"] == "UNKNOWN", env
    rc, env = _needs_me(FakeTransport({SWITCH: json.dumps({"v": 1, "serve": "stream"})}), capsys)
    assert rc == 3 and env["health"] == "UNKNOWN", env


def test_needs_me_with_no_switch_still_answers_from_the_file_plane(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(cli, "_load_rows_status", lambda tr, team, **kw: (called.append("index") or ([], True, "")))
    monkeypatch.setattr(cli, "_held_roles_for_rows", lambda *a, **kw: (set(), []))
    monkeypatch.setattr(cli, "_needs_me_rows", lambda *a, **kw: (called.append("needs-me") or []))
    monkeypatch.setattr(cli, "_pending_reviews_for", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "_forge_feedback_for", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "_blocked_on_human_section", lambda *a, **kw: [])
    cli.cmd_needs_me(_nm_args(), FakeTransport({CKPT: _ckpt(ROWS)}))
    capsys.readouterr()
    assert called == ["index", "needs-me"]


# ---- the verbs ---------------------------------------------------------------------------------------------

def test_cutover_show_reports_the_switch(capsys):
    assert cli.cmd_cutover_show(argparse.Namespace(team=TEAM, json=False), FakeTransport()) == 0
    assert "serving from files" in capsys.readouterr().out
    assert cli.cmd_cutover_show(argparse.Namespace(team=TEAM, json=False), FakeTransport({SWITCH: _switch("fold")})) == 0
    assert "serving from fold" in capsys.readouterr().out


def test_cutover_set_flips_and_rolls_back_with_before_and_after_printed(capsys):
    t = FakeTransport()
    args = argparse.Namespace(team=TEAM, serve="fold", reason="ship gate rc 0, cutover-ready READY", agent="coord-boss")
    assert cli.cmd_cutover_set(args, t) == 0
    out = capsys.readouterr().out
    assert "was files" in out and "now fold" in out and t.writes == [SWITCH]
    doc = json.loads(t.docs[SWITCH])
    assert doc == {"v": 1, "serve": "fold", "at": doc["at"], "by": "coord-boss", "reason": "ship gate rc 0, cutover-ready READY"}
    back = argparse.Namespace(team=TEAM, serve="files", reason="rollback", agent="coord-boss")
    assert cli.cmd_cutover_set(back, t) == 0
    assert "was fold" in capsys.readouterr().out and cutover.read_switch(t, TEAM)[0] == "files"


def test_cutover_set_refuses_without_an_identity_and_when_the_write_does_not_confirm(monkeypatch, capsys):
    monkeypatch.delenv("FULCRA_COORD_AGENT", raising=False)
    args = argparse.Namespace(team=TEAM, serve="fold", reason="r", agent=None)
    assert cli.cmd_cutover_set(args, FakeTransport()) == 2
    class NoWrite(FakeTransport):
        def write(self, path, content):
            return False
    args = argparse.Namespace(team=TEAM, serve="fold", reason="r", agent="coord-boss")
    assert cli.cmd_cutover_set(args, NoWrite()) == 3
    assert "did not confirm" in capsys.readouterr().err


# ---- needs-me: the human-facing view ------------------------------------------------------------------------

def _needs_me_stubs(monkeypatch, *, file_rows):
    monkeypatch.setattr(cli, "_load_rows_status", lambda t, team, **kw: ([], True, ""))
    monkeypatch.setattr(cli, "_held_roles_for_rows", lambda *a, **kw: (set(), []))
    monkeypatch.setattr(cli, "_needs_me_rows", lambda *a, **kw: list(file_rows))
    monkeypatch.setattr(cli, "_pending_reviews_for", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "_forge_feedback_for", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "_blocked_on_human_section", lambda *a, **kw: [])


def _nm_args():
    return argparse.Namespace(team=TEAM, agent=AGENT, json=True, all=False, envelope_only=False)


FILE_ROW = {"id": "file-plane-row-00000001", "name": "file-plane-row-00000001", "title": "from the file plane",
            "priority": "P2", "status": "proposed", "kind": "task"}


def test_needs_me_served_from_the_fold_replaces_the_file_plane_rows(monkeypatch, capsys):
    _needs_me_stubs(monkeypatch, file_rows=[FILE_ROW, {"type": "needs-me-source", "source": "raw-scan", "reason": "x"}])
    t = FakeTransport({SWITCH: _switch("fold"), CKPT: _ckpt(ROWS)})
    rc = cli.cmd_needs_me(_nm_args(), t)
    out = capsys.readouterr().out
    payload = json.loads([l for l in out.splitlines() if l.startswith("{")][-1])
    ids = [r.get("id") for r in payload["rows"] if r.get("id")]
    assert rc == 0 and "do-the-thing-1234abcd" in ids and "file-plane-row-00000001" not in ids, payload
    assert payload["health"] == "DATA" and payload["source"] == "projection"


def test_needs_me_served_from_the_fold_without_a_checkpoint_is_unknown_rc_3(monkeypatch, capsys):
    _needs_me_stubs(monkeypatch, file_rows=[FILE_ROW])
    t = FakeTransport({SWITCH: _switch("fold")})
    rc = cli.cmd_needs_me(_nm_args(), t)
    out = capsys.readouterr().out
    payload = json.loads([l for l in out.splitlines() if l.startswith("{")][-1])
    assert rc == 3 and payload["health"] in ("UNKNOWN", "DEGRADED"), payload
    assert any(r.get("type") == cutover.FOLD_DEGRADED for r in payload["rows"])


def test_needs_me_without_a_switch_is_the_file_plane_answer(monkeypatch, capsys):
    _needs_me_stubs(monkeypatch, file_rows=[FILE_ROW])
    t = FakeTransport({CKPT: _ckpt(ROWS)})
    rc = cli.cmd_needs_me(_nm_args(), t)
    out = capsys.readouterr().out
    payload = json.loads([l for l in out.splitlines() if l.startswith("{")][-1])
    ids = [r.get("id") for r in payload["rows"] if r.get("id")]
    assert rc == 0 and ids == ["file-plane-row-00000001"], payload
