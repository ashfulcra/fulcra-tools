import json

from coord_reconcile import cli
from tests.test_reconcile import FakeTransport, _task


def test_cli_reconcile_then_status_and_board(capsys):
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    t.put("team/r/task/b.md", _task("Bravo", "waiting"))

    assert cli.main(["reconcile", "r"], transport=t) == 0
    assert "2 tasks" in capsys.readouterr().out

    assert cli.main(["status", "r", "--json"], transport=t) == 0
    counts = json.loads(capsys.readouterr().out)
    assert counts == {"active": 1, "waiting": 1}

    assert cli.main(["board", "r"], transport=t) == 0
    out = capsys.readouterr().out
    assert "ACTIVE (1)" in out and "Alpha" in out


def test_cli_needs_me(capsys):
    t = FakeTransport()
    t.put("team/r/task/a.md",
          "---\ntype: Task\ntitle: Mine\nstatus: active\nassignee: me\n---\n")
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    assert cli.main(["needs-me", "r", "--agent", "me"], transport=t) == 0
    assert "Mine" in capsys.readouterr().out


def test_cli_search(capsys):
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Widget fixer", "active"))
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    assert cli.main(["search", "r", "widget"], transport=t) == 0
    assert "Widget fixer" in capsys.readouterr().out


def test_cli_status_no_aggregate_hint(capsys):
    t = FakeTransport()
    assert cli.main(["status", "empty"], transport=t) == 0
    assert "run `reconcile` first" in capsys.readouterr().out
