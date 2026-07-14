from coord_engine import roles

NOW = "2026-07-01T18:00:00Z"

# clock-pin support (see #378):
import pytest
from datetime import datetime, timezone
from coord_engine import cli
PINNED_NOW = datetime(2026, 7, 1, 18, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_module_clock(monkeypatch):
    """Pin cli._now to PINNED_NOW (just after the module NOW).

    Fixtures stamp data relative to NOW, but folds/verbs compute windows and
    staleness off cli._now() against the REAL clock — so once wall-clock time
    crossed NOW + a window this suite flipped RED for good (the repo's
    date-boundary CI-flake class; template: #378 test_threads). Remedy: pin the
    clock, never weaken assertions. Tests that MOVE time monkeypatch cli._now
    themselves, overriding this."""
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)


def _lease(agent, hours_ago):
    # timestamp = NOW - hours_ago
    from datetime import datetime, timedelta, timezone
    n = datetime(2026, 7, 1, 18, 0, 0, tzinfo=timezone.utc)
    ts = (n - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")
    return {"agent": agent, "timestamp": ts}


def test_age_hours():
    assert roles.age_hours("2026-07-01T12:00:00Z", NOW) == 6.0
    assert roles.age_hours(None, NOW) == float("inf")
    assert roles.age_hours("garbage", NOW) == float("inf")


def test_held_when_fresh_lease():
    assert roles.classify([_lease("a", 1)], now=NOW, sla_hours=24) == roles.HELD


def test_vacant_when_all_stale():
    assert roles.classify([_lease("a", 30)], now=NOW, sla_hours=24) == roles.VACANT


def test_vacant_when_no_leases():
    assert roles.classify([], now=NOW, sla_hours=24) == roles.VACANT


def test_unknown_when_unreadable():
    assert roles.classify(None, now=NOW, sla_hours=24) == roles.UNKNOWN


def test_contested_only_when_exclusive_and_multiple_fresh():
    two_fresh = [_lease("a", 1), _lease("b", 2)]
    assert roles.classify(two_fresh, now=NOW, sla_hours=24, policy="exclusive") == roles.CONTESTED
    # shared policy tolerates multiple holders
    assert roles.classify(two_fresh, now=NOW, sla_hours=24, policy="shared") == roles.HELD


def test_contested_ignores_stale_second_holder():
    leases = [_lease("a", 1), _lease("b", 40)]  # b stale
    assert roles.classify(leases, now=NOW, sla_hours=24, policy="exclusive") == roles.HELD


def test_escalation_due_when_vacant_and_no_marker():
    assert roles.escalation_due([_lease("a", 30)], now=NOW, sla_hours=24) is True


def test_escalation_not_due_when_marker_exists():
    assert roles.escalation_due([_lease("a", 30)], now=NOW, sla_hours=24,
                                marker_exists_today=True) is False


def test_escalation_not_due_when_held():
    assert roles.escalation_due([_lease("a", 1)], now=NOW, sla_hours=24) is False


# --- dormancy (deliberately-parked roles) ---

def test_dormant_state_future_is_dormant():
    # NOW is 2026-07-01; a 2026-08-05 park is in the future -> dormant, no error.
    assert roles.dormant_state("2026-08-05T09:00:00Z", now=NOW) == (True, False)


def test_dormant_state_past_is_not_dormant():
    assert roles.dormant_state("2026-06-01T00:00:00Z", now=NOW) == (False, False)


def test_dormant_state_absent_is_not_dormant():
    assert roles.dormant_state(None, now=NOW) == (False, False)
    assert roles.dormant_state("", now=NOW) == (False, False)
    assert roles.dormant_state("   ", now=NOW) == (False, False)


def test_dormant_state_garbage_is_parse_error_not_dormant():
    # Fail OPEN toward escalation: a typo must never silently suppress. Report the
    # parse error so the caller can note it; never treat garbage as dormant.
    assert roles.dormant_state("not-a-date", now=NOW) == (False, True)


def test_escalation_suppressed_when_dormant():
    # A vacant-past-SLA role that is dormant must NOT escalate.
    assert roles.escalation_due([_lease("a", 30)], now=NOW, sla_hours=24,
                                dormant=True) is False


def test_escalation_still_due_when_not_dormant():
    assert roles.escalation_due([_lease("a", 30)], now=NOW, sla_hours=24,
                                dormant=False) is True
