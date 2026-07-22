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
