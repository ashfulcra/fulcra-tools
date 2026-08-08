"""A spent budget must cost ZERO transport ops, not one.

`_held_roles_for_rows` opened its deadline and then called
`_roles_listing_names` -- an unconditional blocking `list_dir` -- with the first
`dl.expired()` check sitting AFTER it. So a caller passing an already-spent
window (briefing, after a slow earlier phase) still paid one full transport op.

Under a healthy store that is ~0.8s and irrelevant. Under the degraded
transport this bound exists for, one op is one whole timeout, charged to the
very sections the caller was protecting.

Found by coord-opus-worker reviewing PR 559, and it is the same pre-budget class
this function's own docstring names four lines above the defect.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from coord_engine import cli
from coord_engine_test_helpers import FakeTransport

TEAM = "t"
AGENT = "worker-a"
NOW = "2026-08-08T00:00:00Z"
PINNED_NOW = datetime(2026, 8, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_module_clock(monkeypatch):
    """Repo convention: a module fixing data timestamps pins the clock. Every
    test here passes `now` explicitly, so nothing reads the real clock today —
    the convention exists because that stops being true quietly."""
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)


class CountingTransport(FakeTransport):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.ops = 0

    def list_dir(self, prefix: str):
        self.ops += 1
        return super().list_dir(prefix)

    def read(self, path: str):
        self.ops += 1
        return super().read(path)


def _rows_addressed_to(role):
    return [{"id": "r1", "assignee": role, "status": "proposed",
             "priority": "P1", "title": "t", "type": "Task"}]


def test_a_spent_budget_pays_no_transport_ops_at_all():
    t = CountingTransport()
    held, unresolved = cli._held_roles_for_rows(
        t, TEAM, AGENT, _rows_addressed_to("some-role"),
        now=NOW, deadline_seconds=0.0)
    assert t.ops == 0, (
        f"a spent budget still paid {t.ops} transport op(s); under a degraded "
        f"transport that is a full timeout charged to the caller's other work"
    )
    assert held == set()
    assert "some-role" in unresolved, (
        "a fold that spent nothing knows nothing — every candidate must be "
        "UNKNOWN, never silently 'you hold no roles'"
    )


def test_a_live_budget_still_does_the_work():
    """Non-vacuity: the guard must not short-circuit a fold that has time."""
    t = CountingTransport()
    cli._held_roles_for_rows(
        t, TEAM, AGENT, _rows_addressed_to("some-role"),
        now=NOW, deadline_seconds=60.0)
    assert t.ops > 0, "the guard swallowed a fold that had budget to spend"


def test_no_candidates_is_still_free():
    """No role-shaped assignees: nothing to resolve, spent or not."""
    t = CountingTransport()
    held, unresolved = cli._held_roles_for_rows(
        t, TEAM, AGENT, [], now=NOW, deadline_seconds=0.0)
    assert (t.ops, held, unresolved) == (0, set(), set())
