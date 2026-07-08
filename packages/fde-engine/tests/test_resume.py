"""Deterministic resume brief — what a fresh session reads first."""

from fde_engine import engagement, resume
from fde_engine_test_helpers import FakeTransport

NOW = "2026-07-08T17:00:00Z"
LATER = "2026-07-08T18:00:00Z"


def _engaged_transport():
    t = FakeTransport()
    engagement.init_engagement(t, "x", "Sourdough Coach", now=NOW)
    t.write("fde/engagements/x/intake/brief.md", "# Brief\nGoal: coach bakers.\n")
    engagement.set_phase(t, "x", "interview", now=LATER)
    t.write("fde/engagements/x/interview/plan.md",
            "# Topics\n1. Whose data?\n2. Tenancy.\n")
    return t


def test_brief_contains_identity_phase_artifacts_and_next():
    brief = resume.resume_brief(_engaged_transport(), "x")
    assert "Sourdough Coach" in brief
    assert "phase: interview" in brief
    assert "[x] intake/brief.md" in brief          # present artifact
    assert "[ ] interview/findings.md" in brief    # missing artifact
    assert "architecture" in brief                 # the next-move hint


def test_brief_tails_the_current_phase_primary_artifact():
    brief = resume.resume_brief(_engaged_transport(), "x")
    assert "Whose data?" in brief


def test_brief_is_deterministic():
    t = _engaged_transport()
    assert resume.resume_brief(t, "x") == resume.resume_brief(t, "x")
