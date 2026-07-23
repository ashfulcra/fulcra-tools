"""Listener caller-head starvation regression (wake-router W8).

The literal-agent/wildcard inbox is the delivery-critical head.  It must complete
under a dedicated budget even when the shared history/role tail is already spent.
Head and tail degradation are intentionally distinct and fail-visible.
"""

from datetime import datetime, timezone

import pytest

from coord_engine import budget, cli, okf, reconcile
from coord_engine.transport import TransportError
from coord_engine_test_helpers import FakeTransport


TEAM = "r"
NOW = "2026-07-22T00:00:00Z"
PINNED_NOW = datetime(2026, 7, 22, 0, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_module_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)


def _state():
    return {"inbox_ids": [], "response_keys": [], "slug_owned": {},
            "verdict_keys": [], "review_requested": {}, "settled_reviews": [],
            "degraded": {s: False for s in cli._LISTEN_SOURCES}}


def _put_direct(t, slug="mine"):
    fm = {"type": "Task", "id": slug, "title": "Urgent direct work",
          "status": "proposed", "priority": "P1", "owner": "coord-boss",
          "assignee": "codex-coder"}
    t.put(cli._task_path(TEAM, slug), okf.render_frontmatter(fm) + "\n")


def test_direct_head_survives_fully_spent_shared_tail(monkeypatch):
    """The live regression: a zero-remaining shared tail cannot hide direct work."""
    monkeypatch.setenv("COORD_LISTEN_TAIL_BUDGET", "0.000001")
    t = FakeTransport()
    _put_direct(t)
    reconcile.reconcile(t, TEAM, now=NOW, today="2026-07-22", host="h")

    events, failures = cli._listen_tick(t, TEAM, "codex-coder", _state())

    assert [e["slug"] for e in events if e["type"] == "directive"] == ["mine"]
    assert "tail" in failures
    assert "listen-tail-degraded" in " ".join(failures["tail"])
    assert "listen-head-degraded" not in str(failures)


def test_unreadable_head_is_distinct_from_tail_truncation(monkeypatch):
    class OverlayFails(FakeTransport):
        def list_dir(self, prefix):
            if prefix == reconcile.task_prefix(TEAM):
                raise TransportError("head boom")
            return super().list_dir(prefix)

    monkeypatch.setenv("COORD_LISTEN_TAIL_BUDGET", "0.000001")
    t = OverlayFails()
    _put_direct(t)
    # Write a summaries index without invoking reconcile's task listing.
    t.put(reconcile.summaries_path(TEAM), "[]")

    _events, failures = cli._listen_tick(t, TEAM, "codex-coder", _state())

    assert "listen-head-degraded" in " ".join(failures["inbox"])
    assert "listen-tail-degraded" in " ".join(failures["tail"])


def test_direct_head_is_consumed_before_role_tail(monkeypatch):
    """Head id advances even when the role tail is cut before its first probe."""
    monkeypatch.setenv("COORD_LISTEN_TAIL_BUDGET", "0.000001")
    t = FakeTransport()
    _put_direct(t)
    role_fm = {"type": "Task", "id": "role-work", "title": "Role work",
               "status": "proposed", "priority": "P2", "owner": "boss",
               "assignee": "maintainer"}
    t.put(cli._task_path(TEAM, "role-work"), okf.render_frontmatter(role_fm) + "\n")
    reconcile.reconcile(t, TEAM, now=NOW, today="2026-07-22", host="h")
    state = _state()

    cli._listen_tick(t, TEAM, "codex-coder", state)

    assert state["inbox_ids"] == ["mine"]
    assert "role-work" not in state["inbox_ids"]


def test_row_load_head_budget_cut_is_loud_and_recovery_delivers(monkeypatch):
    """A slow summaries read spends the protected clock before ack scanning.

    The cut is head-degraded, advances no id, and the recovery tick delivers the
    still-unseen directive. Fake monotonic time makes the boundary deterministic.
    """
    class SlowSummary(FakeTransport):
        def __init__(self):
            super().__init__()
            self.clock = 0.0
            self.slow = True

        def read(self, path):
            if self.slow and path == reconcile.summaries_path(TEAM):
                self.clock += 2.0
            return super().read(path)

    t = SlowSummary()
    monkeypatch.setattr(budget.time, "monotonic", lambda: t.clock)
    monkeypatch.setenv("COORD_LISTEN_HEAD_BUDGET", "1")
    _put_direct(t)
    reconcile.reconcile(t, TEAM, now=NOW, today="2026-07-22", host="h")
    state = _state()

    events, failures = cli._listen_tick(t, TEAM, "codex-coder", state)

    assert events == []
    assert state["inbox_ids"] == []
    assert "listen-head-degraded" in " ".join(failures["inbox"])

    t.slow = False
    events2, failures2 = cli._listen_tick(t, TEAM, "codex-coder", state)
    assert [e["slug"] for e in events2 if e["type"] == "directive"] == ["mine"]
    assert "inbox" not in failures2
    assert state["inbox_ids"] == ["mine"]
