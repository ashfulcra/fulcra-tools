"""The state-change guard must run in cmd_escalate, not merely exist.

I have twice shipped a decision function that was unit-tested and never
connected to the acting path. A passing test on `roles.vacancy_already_open`
says nothing about whether the sweep calls it, so these drive the real entry
point (`cli.main(["escalate", ...])`) and assert on what lands in the store.

The existing same-day suppressor is the daily marker; it cannot cover this case,
because the title embeds the date and yesterday's row has a different slug.
"""
from __future__ import annotations

import json

import pytest

from coord_engine import cli
from coord_engine_test_helpers import FakeTransport

ROLE_DOC = "---\ntype: Role\nsla_hours: 12\nmaintainer: alice\n---\n"
STALE_LEASE = ("---\ntype: Lease\nagent: bob\n"
               "timestamp: 2020-01-01T00:00:00Z\n---\n")
BUS_CONFIG = json.dumps({"data_type": "MomentAnnotation/test-bus",
                         "api_version": "v1alpha1"})

OLD_ROW = ("team/r/task/role-vacant-2020-01-01-reviewer-lease-lapsed-past-12h-"
           "sla-attendance-unver.md")


def _team() -> FakeTransport:
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md", ROLE_DOC)
    t.put("team/r/roles/reviewer/leases/bob.md", STALE_LEASE)
    t.put("team/r/_coord/bus-v3/records.json", BUS_CONFIG)
    return t


def _vacancy_docs(t) -> list[str]:
    return [p for p in t.store if "/task/role-vacant-" in p]


def test_an_open_row_from_a_PREVIOUS_DAY_suppresses_a_new_mint(capsys):
    """THE regression: 117 rows carrying 12 facts, +2-6/day fleetwide."""
    t = _team()
    t.put(OLD_ROW, "---\ntype: Task\nstatus: proposed\n---\n")
    assert cli.main(["escalate", "r"], transport=t) == 0
    assert _vacancy_docs(t) == [OLD_ROW], (
        "a second row was minted for a role whose vacancy is already on the "
        "board — the restatement adds no fact")
    assert "ALREADY OPEN" in capsys.readouterr().err, (
        "the suppressor printed nothing; silence is indistinguishable from a "
        "sweep that never ran")


def test_the_FIRST_notice_is_still_minted():
    """The guard must not silence the alarm that carries new information."""
    t = _team()
    assert cli.main(["escalate", "r"], transport=t) == 0
    assert len(_vacancy_docs(t)) == 1


def test_a_row_for_a_DIFFERENT_role_does_not_suppress():
    t = _team()
    t.put("team/r/task/role-vacant-2020-01-01-someone-else-lease-lapsed-past-"
          "12h-sla-attendance-unver.md", "---\ntype: Task\n---\n")
    cli.main(["escalate", "r"], transport=t)
    assert any("reviewer" in p for p in _vacancy_docs(t)), (
        "another role's vacancy suppressed this one — the match is not exact")


@pytest.mark.xfail(strict=True, reason=(
    "PRE-EXISTING defect, not introduced by the guard: an unreadable task "
    "listing aborts the whole escalate sweep with rc=1 from an EARLIER "
    "list_dir (the attendance scan) before this guard is reached. Verified by "
    "running this same test against pristine 124ab7fd, where it fails "
    "identically. strict=True so this flips to a FAILURE the moment somebody "
    "fixes the sweep, rather than silently passing unnoticed."))
def test_an_UNREADABLE_board_mints_rather_than_silencing(capsys):
    """Fail direction. UNKNOWN must never suppress an alarm: a duplicate row is
    strictly cheaper than a vacancy nobody is told about.

    The guard itself implements this correctly (its own list_dir is wrapped and
    falls through to minting). It is unreachable for this failure mode today
    because the sweep dies earlier — see the xfail reason."""
    t = _team()

    def _boom(prefix):
        raise OSError("listing unavailable")

    t.list_dir = _boom  # type: ignore[assignment]
    assert cli.main(["escalate", "r"], transport=t) == 0
    assert len(_vacancy_docs(t)) == 1, (
        "an unreadable listing silenced the vacancy")
    assert "guard SKIPPED" in capsys.readouterr().err


SELF_ADDRESSED_ROLE = "---\ntype: Role\nsla_hours: 12\nmaintainer: bob\n---\n"


def test_a_CLOSED_LOOP_vacancy_is_exempt_from_the_guard():
    """The notice addressed to the role's own lapsed holder reached NOBODY.

    Re-surfacing it every sweep is the deliberate retry for an undelivered
    alarm, so the state-change guard must stand down there: the restatement
    carries no new fact but IS the delivery. Locking this because my first cut
    of the guard broke it, and only an existing test caught that.
    """
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md", SELF_ADDRESSED_ROLE)
    t.put("team/r/roles/reviewer/leases/bob.md", STALE_LEASE)
    t.put("team/r/_coord/bus-v3/records.json", BUS_CONFIG)
    t.put(OLD_ROW, "---\ntype: Task\nstatus: proposed\n---\n")
    # rc is 3, not 0: a closed-loop notice is UNDELIVERED and the sweep says so.
    assert cli.main(["escalate", "r"], transport=t) == 3
    assert len(_vacancy_docs(t)) == 2, (
        "the guard suppressed a closed-loop retry — that silences the only "
        "mechanism that re-delivers an alarm nobody received")
