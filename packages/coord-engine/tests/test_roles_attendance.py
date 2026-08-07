"""A lapsed lease is not proof that nobody is doing the job.

Escalation used to key on lease timestamps alone, so the predicate was "has a
lease been renewed lately" while the alarm it raised read "is anybody doing this
job." Those diverged for four days: codex-reviewer's lease went stale while it
filed verdicts hourly, and the detector produced a P1 per role per day, every
one of them false.

A standing alarm that is always wrong is worse than no alarm — it is how the
real one gets ignored.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from coord_engine import cli, roles

NOW = "2026-08-07T08:00:00Z"
PINNED_NOW = datetime(2026, 8, 7, 8, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_module_clock(monkeypatch):
    """Pin cli._now to PINNED_NOW, per the repo clock convention.

    These tests pass `now`/`since` explicitly, so nothing here reads the real
    clock today — but the convention exists because that stops being true
    quietly, and a real-clock boundary flake has bitten this repo three times.
    """
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)
STALE = [{"agent": "codex-reviewer", "timestamp": "2026-08-03T08:00:00Z"}]
FRESH = [{"agent": "codex-reviewer", "timestamp": "2026-08-07T07:50:00Z"}]


def _esc(leases, **kw):
    return roles.escalation_due(leases, now=NOW, sla_hours=12.0, **kw)


def test_stale_lease_escalates_when_attendance_is_unchecked():
    """Unchecked is not 'absent'. The default still escalates — silence about a
    lapsed lease would be its own failure — it just must not claim more."""
    assert _esc(STALE) is True
    assert _esc(STALE, attended=None) is True


def test_a_served_role_does_not_escalate_as_unattended():
    """THE regression: 4-day-stale lease, verdict filed an hour ago."""
    assert _esc(STALE, attended=True) is False


def test_checked_and_genuinely_absent_still_escalates():
    """The fix must not swallow real vacancies."""
    assert _esc(STALE, attended=False) is True


def test_attendance_does_not_resurrect_dormant_or_deduped_cases():
    assert _esc(STALE, attended=False, dormant=True) is False
    assert _esc(STALE, attended=False, marker_exists_today=True) is False


def test_a_live_lease_never_escalates_regardless_of_attendance():
    for attended in (True, False, None):
        assert _esc(FRESH, attended=attended) is False


class _Store:
    """Minimal transport double: {prefix: [entries]}."""

    def __init__(self, tree):
        self.tree = tree
        self.calls = []

    def list_dir(self, prefix):
        self.calls.append(prefix)
        return self.tree.get(prefix, [])


def _attended(tree, holders, since, **kw):
    from coord_engine import cli

    return cli._role_attended(_Store(tree), "fulcra", holders, since=since, **kw)


def test_scan_finds_a_holder_verdict_by_filename():
    from datetime import datetime, timezone

    since = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)
    tree = {
        "team/fulcra/review/": [{"name": "some-slug", "is_dir": True}],
        "team/fulcra/review/some-slug/verdicts/": [
            {"name": "abc123--codex-reviewer.md",
             "mtime": "2026-08-07 07:37AM UTC"},
        ],
    }
    attended, scanned, total = _attended(tree, ["codex-reviewer"], since)
    assert attended is True
    assert (scanned, total) == (1, 1)


def test_a_truncated_scan_is_unknown_not_absent():
    """Budget cut-offs must never harden into 'nobody worked' — the same
    UNKNOWN-is-not-empty rule the lease listing already follows."""
    from datetime import datetime, timezone

    since = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)
    tree = {
        "team/fulcra/review/": [
            {"name": f"slug-{i}", "is_dir": True} for i in range(5)
        ],
    }
    attended, scanned, total = _attended(tree, ["codex-reviewer"], since, budget=2)
    assert attended is None, "a truncated sweep may not assert absence"
    assert (scanned, total) == (2, 5)


def test_a_complete_clean_sweep_may_say_absent():
    from datetime import datetime, timezone

    since = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)
    tree = {
        "team/fulcra/review/": [{"name": "slug-0", "is_dir": True}],
        "team/fulcra/review/slug-0/verdicts/": [
            {"name": "abc--someone-else.md", "mtime": "2026-08-07 07:00AM UTC"},
        ],
    }
    attended, scanned, total = _attended(tree, ["codex-reviewer"], since)
    assert attended is False
    assert (scanned, total) == (1, 1)


def test_an_old_verdict_does_not_count_as_attendance():
    from datetime import datetime, timezone

    since = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)
    tree = {
        "team/fulcra/review/": [{"name": "slug-0", "is_dir": True}],
        "team/fulcra/review/slug-0/verdicts/": [
            {"name": "abc--codex-reviewer.md", "mtime": "2026-08-01 07:00AM UTC"},
        ],
    }
    assert _attended(tree, ["codex-reviewer"], since)[0] is False


def test_no_holders_is_unknown_and_costs_nothing():
    from datetime import datetime, timezone

    since = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)
    store = _Store({})
    from coord_engine import cli

    assert cli._role_attended(store, "fulcra", [], since=since) == (None, 0, 0)
    assert store.calls == [], "an empty holder list must not hit the transport"


def test_an_undatable_holder_verdict_is_unknown_not_absent():
    """Skipping a verdict we cannot date would count it as 'no work in the
    window' — the false-absent this change exists to remove."""
    from datetime import datetime, timezone

    since = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)
    tree = {
        "team/fulcra/review/": [{"name": "slug-0", "is_dir": True}],
        "team/fulcra/review/slug-0/verdicts/": [
            {"name": "abc--codex-reviewer.md", "mtime": "not a timestamp"},
        ],
    }
    assert _attended(tree, ["codex-reviewer"], since)[0] is None
