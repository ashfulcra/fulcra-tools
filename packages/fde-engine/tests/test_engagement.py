"""Engagement lifecycle over the transport."""

import pytest

from fde_engine import engagement, model
from fde_engine_test_helpers import FakeTransport

NOW = "2026-07-08T17:00:00Z"
LATER = "2026-07-08T18:00:00Z"


def test_init_writes_engagement_doc_at_canonical_path():
    t = FakeTransport()
    meta = engagement.init_engagement(t, "sourdough-coach", "Sourdough Coach", now=NOW)
    assert meta["phase"] == "intake"
    assert meta["phase_history"] == [f"intake {NOW}"]
    stored = t.read("fde/engagements/sourdough-coach/engagement.md")
    assert model.parse_engagement(stored) == meta


def test_init_refuses_to_clobber_an_existing_engagement():
    t = FakeTransport()
    engagement.init_engagement(t, "x", "X", now=NOW)
    with pytest.raises(engagement.EngagementError):
        engagement.init_engagement(t, "x", "X again", now=LATER)


def test_load_returns_none_for_missing_engagement():
    assert engagement.load_engagement(FakeTransport(), "ghost") is None


def test_set_phase_validates_and_records_history():
    t = FakeTransport()
    engagement.init_engagement(t, "x", "X", now=NOW)
    meta = engagement.set_phase(t, "x", "interview", now=LATER)
    assert meta["phase"] == "interview"
    assert meta["updated_at"] == LATER
    assert meta["phase_history"][-1] == f"interview {LATER}"
    # persisted, not just returned
    assert engagement.load_engagement(t, "x")["phase"] == "interview"


def test_set_phase_rejects_invalid_transition():
    t = FakeTransport()
    engagement.init_engagement(t, "x", "X", now=NOW)
    with pytest.raises(engagement.EngagementError):
        engagement.set_phase(t, "x", "build", now=LATER)


def test_set_phase_rejects_missing_engagement():
    with pytest.raises(engagement.EngagementError):
        engagement.set_phase(FakeTransport(), "ghost", "interview", now=NOW)
