from coord_engine import continuity

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


def test_build_snapshot_normalizes_lists():
    s = continuity.build_snapshot(
        agent="ash", task="build-l6", objective="ship continuity", now=NOW,
        next_actions="write tests", decisions=["chose json"], open_questions=None)
    assert s["schema"] == "coord.teams.continuity.v1"
    assert s["checkpoint_id"] == "CHK-2026-07-01T18:00:00Z-build-l6"
    assert s["next_actions"] == ["write tests"]   # scalar -> list
    assert s["decisions"] == ["chose json"]
    assert s["open_questions"] == []              # None -> []
    assert s["created_at"] == NOW


def test_latest_picks_newest():
    a = continuity.build_snapshot(agent="x", task="t", objective="o",
                                  now="2026-07-01T10:00:00Z")
    b = continuity.build_snapshot(agent="x", task="t", objective="o2",
                                  now="2026-07-01T12:00:00Z")
    assert continuity.latest([a, b])["objective"] == "o2"
    assert continuity.latest([]) is None
    assert continuity.latest([{"no": "date"}]) is None


def test_latest_ignores_garbage_created_at_and_tiebreaks_deterministically():
    good = continuity.build_snapshot(agent="x", task="a", objective="good",
                                     now="2026-07-01T10:00:00Z")
    newer = continuity.build_snapshot(agent="x", task="b", objective="newer",
                                      now="2026-07-01T10:00:00Z")
    garbage = {"task": "z", "objective": "bad", "created_at": "not-a-date"}
    assert continuity.latest([good, garbage])["objective"] == "good"
    assert continuity.latest([newer, good])["task"] == "b"


def test_render_resume_none():
    assert "No continuity snapshot" in continuity.render_resume(None)


def test_render_resume_includes_fields():
    s = continuity.build_snapshot(
        agent="ash", task="t", objective="ship it", now=NOW,
        next_actions=["land PR"], open_questions=["naming?"], context_used_percent=42)
    out = continuity.render_resume(s)
    assert "objective: ship it" in out
    assert "land PR" in out and "naming?" in out
    assert "42%" in out


def test_latest_tiebreak_deterministic():
    a = {"created_at": NOW, "checkpoint_id": "CHK-a", "objective": "A"}
    b = {"created_at": NOW, "checkpoint_id": "CHK-b", "objective": "B"}
    assert continuity.latest([a, b])["objective"] == "B"
    assert continuity.latest([b, a])["objective"] == "B"


def test_latest_ignores_malformed_created_at():
    # a corrupt snapshot must not shadow valid ones (lexical 'n' > '2')
    good = {"created_at": "2026-07-01T10:00:00Z", "checkpoint_id": "CHK-g", "objective": "good"}
    corrupt = {"created_at": "not-a-date", "checkpoint_id": "CHK-x", "objective": "bad"}
    assert continuity.latest([good, corrupt])["objective"] == "good"
    assert continuity.latest([corrupt]) is None


def test_latest_orders_mixed_timezone_timestamps():
    aware = {"created_at": "2026-07-01T10:00:00Z", "checkpoint_id": "CHK-aware", "objective": "aware"}
    naive = {"created_at": "2026-07-01T11:00:00", "checkpoint_id": "CHK-naive", "objective": "naive"}
    offset = {"created_at": "2026-07-01T06:30:00-04:00", "checkpoint_id": "CHK-offset", "objective": "offset"}
    assert continuity.latest([aware, naive])["objective"] == "naive"
    assert continuity.latest([aware, offset])["objective"] == "offset"
