"""--json purity (Phase-1 part 3): under --json, stdout is ALWAYS one parseable
JSON value — no prose, ever. Every degraded/notice line becomes a JSON row or a
reserved key, or goes to stderr. A consumer piping `--json` must never have a
result corrupted by a human-facing notice.

Induced across every fold verb (status, board, needs-me, inbox, briefing, threads)
by forcing each into a degraded path. `threads` is the known leak (it streamed
JSON-Lines — N values, not one); the rest are guarded so the new blocked-on-human
section cannot regress them.
"""

import json

from coord_engine import cli, reconcile
from coord_engine_test_helpers import FakeTransport


def _one_json_value(out):
    """Parse stdout as exactly ONE JSON value; raise if it is JSON-Lines / prose."""
    return json.loads(out)  # json.loads rejects trailing data (JSONL, prose)


def _corrupt_index(t):
    """summaries.json present but UNPARSEABLE -> `_load_rows_status` ok=False, the
    read-degraded path, on every aggregate-backed read."""
    t.put("team/r/_coord/summaries.json", "{ this is not json")


def test_status_json_one_value_under_read_degraded(capsys):
    t = FakeTransport(); _corrupt_index(t)
    capsys.readouterr()
    assert cli.main(["status", "r", "--json"], transport=t) == 0
    v = _one_json_value(capsys.readouterr().out)
    assert "read-degraded" in v


def test_board_json_one_value_under_read_degraded(capsys):
    t = FakeTransport(); _corrupt_index(t)
    capsys.readouterr()
    assert cli.main(["board", "r", "--json"], transport=t) == 0
    v = _one_json_value(capsys.readouterr().out)
    assert "read-degraded" in v


def test_needs_me_json_one_value_under_read_degraded(capsys):
    t = FakeTransport(); _corrupt_index(t)
    capsys.readouterr()
    assert cli.main(["needs-me", "r", "--agent", "alice", "--json"], transport=t) == 0
    v = _one_json_value(capsys.readouterr().out)
    assert any(r.get("type") == "read-degraded" for r in v)


def test_inbox_json_one_value_under_read_degraded(capsys):
    t = FakeTransport(); _corrupt_index(t)
    capsys.readouterr()
    assert cli.main(["inbox", "r", "--agent", "alice", "--json"], transport=t) == 0
    v = _one_json_value(capsys.readouterr().out)
    assert any(r.get("type") == "inbox-degraded" for r in v)


def test_briefing_json_one_value_under_read_degraded(capsys):
    t = FakeTransport(); _corrupt_index(t)
    capsys.readouterr()
    assert cli.main(["briefing", "r", "--agent", "alice", "--json"], transport=t) == 0
    v = _one_json_value(capsys.readouterr().out)
    assert "read_degraded" in v


def test_needs_me_json_pure_under_review_budget_pressure(capsys, monkeypatch):
    # The brief's named leak: needs-me under budget pressure must stay one value.
    monkeypatch.setenv("COORD_REVIEW_FOLD_BUDGET", "0.0001")
    t = FakeTransport()
    t.put("team/r/task/a.md",
          "---\ntype: Task\ntitle: A\nstatus: active\nassignee: alice\n"
          "timestamp: 2026-07-01T00:00:00Z\n---\nb")
    for i in range(4):
        t.put(f"team/r/review/pr{i}.md",
              "---\ntype: Review\nrequired: [alice]\n---\nr")
        t.put(f"team/r/review/pr{i}/verdicts/bob.md",
              "---\ntype: Verdict\nverdict: approve\n---\nv")
    reconcile.reconcile(t, "r", now="2026-07-20T00:00:00Z", today="2026-07-20", host="h")
    capsys.readouterr()
    assert cli.main(["needs-me", "r", "--agent", "alice", "--json"], transport=t) == 0
    v = _one_json_value(capsys.readouterr().out)
    assert isinstance(v, list)


def test_threads_json_is_one_value(capsys):
    # KNOWN leak (red today): `threads --json` streamed one JSON object PER LINE, so
    # with 2+ dropped threads stdout was N values and `json.loads(out)` raised.
    t = FakeTransport()
    for name in ("a", "b"):
        t.put(f"team/r/task/{name}.md",
              f"---\ntype: Task\ntitle: {name}\nstatus: active\nassignee: ash\n"
              f"timestamp: 2020-01-01T00:00:00Z\ntags: []\n---\nb")
    reconcile.reconcile(t, "r", now="2026-07-20T00:00:00Z", today="2026-07-20", host="h")
    capsys.readouterr()
    assert cli.main(["threads", "r", "--for", "ash", "--json"], transport=t) == 0
    out = capsys.readouterr().out
    v = _one_json_value(out)  # must not raise
    assert isinstance(v, list) and len(v) >= 2


def test_threads_json_degraded_marker_is_in_the_value(capsys):
    # The threads degraded marker must ride INSIDE the single value, not as a
    # trailing extra JSON document. An intent candidate whose task doc read fails
    # leaves the intent window UNKNOWN -> the threads source degrades visibly.
    class T(FakeTransport):
        armed = False

        def read(self, path):
            if path == "team/r/task/a.md" and self.armed:
                return None  # intent window UNKNOWN -> ok=False
            return super().read(path)

    t = T()
    # Fresh timestamp so bounded retention does not archive it before the fold runs.
    t.put("team/r/task/a.md",
          "---\ntype: Task\ntitle: A\nstatus: proposed\nassignee: ash\n"
          "timestamp: 2026-07-20T00:00:00Z\ntags: [\"intent:ash\"]\n---\nb")
    reconcile.reconcile(t, "r", now="2026-07-20T00:00:00Z", today="2026-07-20", host="h")
    t.armed = True
    capsys.readouterr()
    assert cli.main(["threads", "r", "--for", "ash", "--json"], transport=t) == 0
    out = capsys.readouterr().out
    v = _one_json_value(out)
    assert any(o.get("type") == "threads-degraded" for o in v), v


# --- close the class, not just the two reported instances -------------------
# coord-boss filed the leak 2026-07-21 against `threads` (JSON-Lines) and
# `needs-me` (prose markers under budget pressure). Both are fixed and pinned
# above. But the rule — "under --json, stdout is ALWAYS one parseable JSON
# value" — applies to EVERY --json verb, and only six of twenty-one were pinned.
# The other fifteen were correct by accident of nobody having touched them.
#
# A sweep on 2026-08-08 found zero live leaks across the verbs below under BOTH
# induced conditions, verified against a positive control (injecting a prose
# print into needs-me's JSON branch makes the sweep flag it). These tests exist
# so that stays true: they are regression pins on a class that is currently
# clean, not a fix for one that is not.

_JSON_VERBS = [
    ("agents",        ["agents", "r", "--json"]),
    ("asks",          ["asks", "r", "--json"]),
    ("digest",        ["digest", "r", "--json"]),
    ("health",        ["health", "r", "--json"]),
    ("obligations",   ["obligations", "r", "--agent", "alice", "--json"]),
    ("search",        ["search", "r", "a", "--json"]),
    ("presence-show", ["presence", "show", "r", "--json"]),
    ("stash-list",    ["stash", "list", "r", "--json"]),
    ("board",         ["board", "r", "--json"]),
    ("inbox",         ["inbox", "r", "--agent", "alice", "--json"]),
    ("needs-me",      ["needs-me", "r", "--agent", "alice", "--json"]),
    ("briefing",      ["briefing", "r", "--agent", "alice", "--json"]),
    ("threads",       ["threads", "r", "--for", "alice", "--json"]),
]

#: Every fold budget, squeezed to nothing. Budget pressure is the condition the
#: original report named, and it is the one that produces degraded markers on
#: paths a happy-path test never reaches.
_ALL_BUDGETS = (
    "COORD_BRIEFING_BUDGET", "COORD_FORGE_SWEEP_BUDGET", "COORD_OBLIGATION_BUDGET",
    "COORD_OVERLAY_BUDGET", "COORD_PROJECTION_BUILD_BUDGET",
    "COORD_REVIEW_FOLD_BUDGET", "COORD_ROLE_FOLD_BUDGET",
    "COORD_THREADS_FOLD_BUDGET",
)


def _seeded_store():
    """A store with enough reviews/roles/intents that a squeezed budget actually
    truncates a fold — an empty store cannot produce the markers under test."""
    t = FakeTransport()
    t.put("team/r/task/a.md",
          "---\ntype: Task\ntitle: A\nstatus: active\nassignee: alice\n"
          "timestamp: 2026-07-20T00:00:00Z\ntags: [\"intent:alice\"]\n---\nb")
    for i in range(6):
        t.put(f"team/r/review/pr{i}.md", "---\ntype: Review\nrequired: [alice]\n---\nr")
        t.put(f"team/r/review/pr{i}/verdicts/bob.md",
              "---\ntype: Verdict\nverdict: approve\n---\nv")
    for i in range(4):
        t.put(f"team/r/roles/role{i}.md", "---\ntype: Role\nholder: alice\n---\nx")
    reconcile.reconcile(t, "r", now="2026-07-20T00:00:00Z", today="2026-07-20",
                        host="h")
    return t


import pytest  # noqa: E402


@pytest.mark.parametrize("label,argv", _JSON_VERBS, ids=[v[0] for v in _JSON_VERBS])
def test_every_json_verb_stays_one_value_under_budget_pressure(
        label, argv, capsys, monkeypatch):
    for var in _ALL_BUDGETS:
        monkeypatch.setenv(var, "0.0001")
    t = _seeded_store()
    capsys.readouterr()
    try:
        cli.main(argv, transport=t)
    except SystemExit:                      # a verb may exit nonzero; purity still holds
        pass
    out = capsys.readouterr().out
    if out.strip():
        _one_json_value(out)                # raises on prose or JSON-Lines


@pytest.mark.parametrize("label,argv", _JSON_VERBS, ids=[v[0] for v in _JSON_VERBS])
def test_every_json_verb_stays_one_value_under_read_degraded(
        label, argv, capsys):
    t = FakeTransport()
    _corrupt_index(t)
    capsys.readouterr()
    try:
        cli.main(argv, transport=t)
    except SystemExit:
        pass
    out = capsys.readouterr().out
    if out.strip():
        _one_json_value(out)
