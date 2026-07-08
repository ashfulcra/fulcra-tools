"""Drive the fixture engagement through all seven phases end to end.

This is the executable version of the spec's lifecycle: every phase writes its
expected artifact, transitions validate, and resume stays truthful throughout.
"""

import os

from fde_engine import engagement, resume
from fde_engine_test_helpers import FakeTransport

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample-plan.md")

TS = [f"2026-07-08T1{i}:00:00Z" for i in range(8)]  # strictly increasing


def test_full_engagement_lifecycle():
    t = FakeTransport()
    slug = "sourdough-coach"

    # intake: init + source material + brief
    engagement.init_engagement(t, slug, "Sourdough Coach", now=TS[0])
    with open(FIXTURE, encoding="utf-8") as fh:
        t.write(f"fde/engagements/{slug}/intake/sample-plan.md", fh.read())
    t.write(f"fde/engagements/{slug}/intake/brief.md",
            "# Brief\nGoals: coach bakers. Actors: individual bakers.\n"
            "Assumptions to test: one-tap logging drives consistency.\n")

    # interview
    engagement.set_phase(t, slug, "interview", now=TS[1])
    t.write(f"fde/engagements/{slug}/interview/plan.md",
            "# Topic map\nP1 tenancy: whose account holds bake data?\n")
    t.write(f"fde/engagements/{slug}/interview/findings.md",
            "Tenancy: each baker owns their data -> user-owned accounts.\n")

    # architecture
    engagement.set_phase(t, slug, "architecture", now=TS[2])
    t.write(f"fde/engagements/{slug}/architecture.md",
            "# Architecture\nFeedings: moment annotation. Bakes: duration.\n"
            "Temp: numeric series. Photos: file library.\n"
            "Gap register: no webhooks -> poll data-updates.\n"
            "Tenancy: user-owned (each baker's own Fulcra account).\n")

    # plan
    engagement.set_phase(t, slug, "plan", now=TS[3])
    t.write(f"fde/engagements/{slug}/plan.md",
            "# Prototype plan\n1. Verify one-tap feeding log round-trip.\n"
            "2. Deployment rehearsal: install flow on a clean machine.\n"
            "# Production plan (provisional)\nM1 data layer, M2 guidance.\n")

    # prototype: findings force a loop back to plan, then forward again
    engagement.set_phase(t, slug, "prototype", now=TS[4])
    t.write(f"fde/engagements/{slug}/prototype/verification.md",
            "1. one-tap round-trip: PASS\n2. deploy rehearsal: FAIL (auth)\n")
    engagement.set_phase(t, slug, "plan", now=TS[5])        # backward edge
    engagement.set_phase(t, slug, "prototype", now=TS[6])   # fixed, retry
    engagement.set_phase(t, slug, "build", now=TS[7])

    # build + retro
    t.write(f"fde/engagements/{slug}/build/log.md", "M1 complete.\n")
    engagement.set_phase(t, slug, "retro", now="2026-07-08T18:00:00Z")
    t.write(f"fde/engagements/{slug}/retro.md",
            "Repeatable: tenancy question always lands in interview P1.\n")

    st = engagement.status(t, slug)
    assert st["phase"] == "retro"
    assert all(st["artifacts"].values()), f"missing artifacts: {st['artifacts']}"
    history_phases = [e.split()[0] for e in st["phase_history"]]
    assert history_phases == [
        "intake", "interview", "architecture", "plan", "prototype",
        "plan", "prototype", "build", "retro",
    ]

    brief = resume.resume_brief(t, slug)
    assert "retro" in brief and "playbook" in brief
