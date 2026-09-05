"""asks-fold rendering: the operator must see what CLEARS a block, not just who it waits on.

Regression origin (2026-08-13): `cmd_asks` rendered `blocked_on or next_action`
and never `unlock`. Because `task block` REQUIRES a blocked_on, that field is
never falsy on a blocked row — so a correctly-authored `--unlock` was invisible
in the one fold built for the human who has to act on it. A real M1 ask reached
the operator as `ask: user:ash` and nothing else.
"""
import argparse
from types import SimpleNamespace

from coord_engine import cli


def _render(rows, capsys, monkeypatch, human="ash"):
    """Drive cmd_asks over ROWS and return its stdout.

    Patched via monkeypatch, NOT by assigning to cli.*: these two names are used
    by most of the suite, and a bare assignment leaks the stub into every test
    that runs after this file.
    """
    args = argparse.Namespace(team="acme", json=False, human=human)
    monkeypatch.setattr(cli, "_load_rows_status", lambda transport, team: (rows, True, ""))
    monkeypatch.setattr(cli.query, "asks",
                        lambda rows, *, now, human: [dict(r, age_hours=1.0) for r in rows])
    cli.cmd_asks(args, SimpleNamespace())
    return capsys.readouterr().out


def _row(**kw):
    base = {"name": "slug-1", "owner": "coord-boss", "priority": "P1",
            "title": "A blocked thing", "status": "blocked"}
    base.update(kw)
    return base


def test_authored_unlock_reaches_the_operator(capsys, monkeypatch):
    """THE REGRESSION: blocked_on is set (as it always is), and the authored
    unlock must still render. An `ask or unlock` fallback fails this."""
    out = _render([_row(blocked_on="user:ash", unlock="grant the share-create permission")], capsys, monkeypatch)
    assert "ask: user:ash" in out
    assert "unlock: grant the share-create permission" in out


def test_derived_unlock_is_not_echoed(capsys, monkeypatch):
    """`--on-user` synthesises `answer from <who>`; printing it back is a line
    with no information, so it stays suppressed."""
    out = _render([_row(blocked_on="user:ash", unlock="answer from ash")], capsys, monkeypatch)
    assert "unlock:" not in out
    assert "ask: user:ash" in out


def test_derived_unlock_detected_when_the_question_rides_in_on_user(capsys, monkeypatch):
    """The working convention puts the whole question in --on-user, so
    blocked_on is `user:<question>` and unlock echoes it. Still no echo line."""
    q = "ash - the cron box needs one adopt run"
    out = _render([_row(blocked_on=f"user:{q}", unlock=f"answer from {q}")], capsys, monkeypatch)
    assert "unlock:" not in out


def test_truncation_is_visible(capsys, monkeypatch):
    """A silently-clipped ask reads as a complete one."""
    out = _render([_row(blocked_on="user:ash", unlock="x" * 300)], capsys, monkeypatch)
    unlock_line = [ln for ln in out.splitlines() if "unlock:" in ln][0]
    assert unlock_line.rstrip().endswith("…")
    assert len(unlock_line.split("unlock: ", 1)[1]) == cli._ASK_FIELD_WIDTH


def test_short_values_are_not_marked(capsys, monkeypatch):
    out = _render([_row(blocked_on="user:ash", unlock="short one")], capsys, monkeypatch)
    assert "…" not in out


def test_clip_boundary_is_exact():
    """At exactly the width nothing is lost, so nothing is marked."""
    assert cli._clip("y" * cli._ASK_FIELD_WIDTH) == "y" * cli._ASK_FIELD_WIDTH
    over = cli._clip("y" * (cli._ASK_FIELD_WIDTH + 1))
    assert over.endswith("…") and len(over) == cli._ASK_FIELD_WIDTH


def test_row_without_unlock_still_renders(capsys, monkeypatch):
    """Legacy rows predate the field; they must not gain an empty line."""
    out = _render([_row(blocked_on="user:ash")], capsys, monkeypatch)
    assert "ask: user:ash" in out
    assert "unlock:" not in out


def test_next_action_fallback_survives(capsys, monkeypatch):
    """Pre-existing behaviour: no blocked_on falls back to next_action."""
    out = _render([_row(blocked_on="", next_action="do the thing")], capsys, monkeypatch)
    assert "ask: do the thing" in out
