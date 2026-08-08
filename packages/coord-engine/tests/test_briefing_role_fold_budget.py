"""The briefing role fold must respect the shared add-on window.

`add_on = Deadline.open(_briefing_budget())` is an ABSOLUTE deadline. Its
consumers are presence, pending-reviews, forge and a resume check. The role fold
sits BETWEEN the open and the last three, so a fold that ignores add_on does not
merely overrun its own cap -- it burns the shared window and starves the
sections that DO respect it, while a comment below it asserts the add-on stack
is bounded.

Found by coord-opus-worker AFTER I had published a sweep marking this site
CLEAN. My check asked whether add_on bound the section it was opened for
(presence) and stopped; it never asked whether an unbudgeted phase sits between
the open and the OTHER consumers.
"""

from __future__ import annotations

import argparse

import pytest

from coord_engine import cli
from coord_engine_test_helpers import FakeTransport

TEAM = "t"
AGENT = "worker-a"


def _seen_deadline(monkeypatch, *, briefing_budget, role_budget):
    seen = {}
    real = cli._held_roles_for_rows

    def spy(*a, **kw):
        seen["deadline_seconds"] = kw.get("deadline_seconds")
        return real(*a, **kw)

    monkeypatch.setattr(cli, "_held_roles_for_rows", spy)
    monkeypatch.setattr(cli, "_briefing_budget", lambda: briefing_budget)
    monkeypatch.setattr(cli, "_role_fold_budget", lambda: role_budget)
    args = argparse.Namespace(team=TEAM, agent=AGENT, json=True, all=False)
    try:
        cli.cmd_briefing(args, FakeTransport())
    except Exception:
        pass  # the bundle may fail elsewhere; we only care what the fold received
    return seen.get("deadline_seconds")


def test_the_role_fold_cannot_outlive_the_shared_add_on_window(monkeypatch):
    """A generous role cap must be clipped to what the shared window has left."""
    got = _seen_deadline(monkeypatch, briefing_budget=5.0, role_budget=999.0)
    assert got is not None, "the role fold ran unbounded inside the shared window"
    assert got <= 5.0, (
        f"role fold got {got}s against a 5s shared window — it can starve "
        f"pending-reviews, forge and the resume check, which do respect add_on"
    )


def test_the_role_fold_keeps_its_OWN_cap_when_the_window_is_wider(monkeypatch):
    """Composition, not replacement: a huge briefing budget must not licence an
    unbounded role pass. Whichever instant arrives first wins."""
    got = _seen_deadline(monkeypatch, briefing_budget=999.0, role_budget=20.0)
    assert got is not None and got <= 20.0, (
        f"role fold got {got}s — its own cap must still apply"
    )
