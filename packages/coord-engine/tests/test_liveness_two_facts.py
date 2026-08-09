"""Beat recency and work evidence are TWO facts, and the nudge needs both.

PR 590 closed the verb gap, but a reviewer's core act — filing a verdict — is
not a verb at all: `review request` prints "file verdict at …" and the reviewer
writes that shard directly. So the agents whose measurements motivated all of
this (codex-reviewer at `stale 6d` with a verdict 3.5h old; coord-opus-worker at
`stale 42h` with a report 4.8h old) are still rendered dark by a beat-only
signal. This is the axis that sees them.

coord-boss's guardrails, taken verbatim:
  1. render BOTH facts, never fuse them into one state;
  2. the nudge imperative only when BOTH are stale;
  3. work evidence must be read-derived mtimes, never inference.

Guardrail 3 is why `work_ts` is a parameter here rather than something this
module goes and computes: the fold stays pure and the caller supplies a measured
timestamp.

`work_scan` took two corrections to get right, both worth stating.

First cut: "no timestamp" meant UNKNOWN and suppressed the nudge — which
silently muted the signal for every caller that does not measure (briefing,
broadcast_roster, the continuity audit), trading a false positive for total
signal loss, so a genuinely dark agent would have stopped being surfaced.

Second cut: two states (measured / not) could not express a scan that was
ATTEMPTED and ran out of budget. Reporting that as "not measured" reverts to the
legacy nudge — the precise false nudge this axis exists to prevent — and
briefing, whose scan IS budgeted, is exactly where it would land. Hence three:
NONE (legacy, unchanged), COMPLETE (absence is a finding; nudge), PARTIAL (fail
safe: no imperative, and say which kind of ignorance it is).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from coord_engine import cli, presence

NOW = "2026-08-09T12:00:00Z"
PINNED_NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_module_clock(monkeypatch):
    """Repo convention (`test_clock_pin_convention`): a module with a top-level
    NOW pins the clock. Every case here passes `now=NOW` into a pure fold, so
    nothing currently reads the real clock — but pinning costs nothing and stops
    a later case from silently drifting against wall time, which is the repo's
    documented flake."""
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)


def _shard(ts: str, agent: str = "a") -> dict:
    return {"agent": agent, "timestamp": ts}


def test_a_stale_beat_with_fresh_work_is_not_nudged():
    """The codex case. Six days without a beat, a verdict filed three hours
    ago: this agent is working and must not be told to go poke itself."""
    lv = presence.liveness(_shard("2026-08-03T11:49:00Z"), now=NOW,
                           work_ts="2026-08-09T08:30:00Z", work_scan=presence.WORK_SCAN_COMPLETE)
    assert "nudge" not in lv["annotation"], (
        f"nudged an agent that filed work 3.5h ago: {lv['annotation']!r}")


def test_both_facts_are_rendered_never_fused():
    """Guardrail 1. The reader must be able to see the disagreement — a single
    merged label is what made the original signal unfalsifiable."""
    # 09:00 against a 12:00 now — exactly 3h, so the assertion is not hostage to
    # how `_ago_label` rounds a half hour.
    lv = presence.liveness(_shard("2026-08-03T11:49:00Z"), now=NOW,
                           work_ts="2026-08-09T09:00:00Z", work_scan=presence.WORK_SCAN_COMPLETE)
    ann = lv["annotation"]
    assert "6d" in ann and "beat" in ann, f"beat age missing from {ann!r}"
    assert "3h" in ann and "work" in ann, f"work age missing from {ann!r}"
    # The freshness axis itself stays untouched: this agent's BEAT really is
    # stale, and the fold must keep saying so rather than laundering it.
    assert lv["freshness"] == "stale"


def test_the_nudge_survives_when_both_are_stale():
    """The signal must still fire for an agent that is genuinely gone —
    otherwise the fix has only replaced a false positive with a false negative."""
    lv = presence.liveness(_shard("2026-08-01T00:00:00Z"), now=NOW,
                           work_ts="2026-08-01T06:00:00Z", work_scan=presence.WORK_SCAN_COMPLETE)
    assert "nudge" in lv["annotation"], (
        f"an agent stale in BOTH axes must still be nudged: {lv['annotation']!r}")


def test_an_unmeasured_caller_keeps_the_old_behaviour_exactly():
    """The regression I nearly shipped.

    briefing, broadcast_roster and the continuity audit call the fold WITHOUT a
    work index. My first version rendered those rows "work evidence UNKNOWN" and
    withheld the nudge — which would have muted the stale signal fleet-wide for
    every caller that never opted in. A dark agent must still be surfaced by a
    caller that cannot measure.
    """
    lv = presence.liveness(_shard("2026-08-03T11:49:00Z"), now=NOW)
    assert lv["annotation"] == "stale 6d — nudge", (
        "an un-opted-in caller's row must be byte-identical to the pre-work-axis "
        f"rendering; got {lv['annotation']!r}")


def test_a_partial_scan_withholds_the_nudge_and_says_why():
    """coord-boss's briefing ruling forced this third state into existence.

    briefing is BUDGETED, so its scan can run out mid-way. Reporting that as
    "nobody measured" silently reverts to the legacy nudge — the exact false
    nudge the axis exists to prevent, landing in the fold that feeds dispatch.
    A partial scan must fail SAFE and say which kind of ignorance it is.
    """
    lv = presence.liveness(_shard("2026-08-03T11:49:00Z"), now=NOW,
                           work_ts=None, work_scan=presence.WORK_SCAN_PARTIAL)
    assert "nudge" not in lv["annotation"], (
        f"a partial scan cannot license an imperative: {lv['annotation']!r}")
    assert "scan incomplete" in lv["annotation"]


def test_a_partial_scan_does_not_nudge_even_on_a_stale_finding():
    """The subtle half. A stale artifact under a partial scan is not evidence of
    inactivity — the newer artifact that would refute it may be in the part
    never scanned."""
    lv = presence.liveness(_shard("2026-08-01T00:00:00Z"), now=NOW,
                           work_ts="2026-08-01T06:00:00Z",
                           work_scan=presence.WORK_SCAN_PARTIAL)
    assert "nudge" not in lv["annotation"], (
        f"stale-but-incomplete is still UNKNOWN: {lv['annotation']!r}")


def test_measured_and_empty_still_nudges_and_says_which_it_is():
    """"We looked and found nothing" is a real finding, not an unknown — so the
    imperative stands, and the row distinguishes it from "nobody looked"."""
    lv = presence.liveness(_shard("2026-08-03T11:49:00Z"), now=NOW,
                           work_ts=None, work_scan=presence.WORK_SCAN_COMPLETE)
    ann = lv["annotation"]
    assert "nudge" in ann, f"a measured-empty agent must still be nudged: {ann!r}"
    assert "no work found" in ann, (
        f"the row must say the scan ran and found nothing: {ann!r}")


def test_a_live_beat_is_unaffected_by_work_evidence():
    """No regression for the ordinary case: a beating agent was never the
    problem, and its row should not grow noise."""
    lv = presence.liveness(_shard("2026-08-09T11:55:00Z"), now=NOW,
                           work_ts="2026-08-09T11:00:00Z", work_scan=presence.WORK_SCAN_COMPLETE)
    assert lv["state"] == "live"
    assert "nudge" not in lv["annotation"]


def test_work_evidence_never_launders_a_lapsed_session():
    """Dormancy is a DECLARED window, not a freshness band. An agent working
    past its declared end is LAPSED+active — the honest reading the W2 truth
    table already insists on — so work evidence must not hide the lapse."""
    shard = {"agent": "a", "timestamp": "2026-08-09T11:55:00Z",
             "engagement": {"mode": "session", "until": "2026-08-09T06:00:00Z"}}
    lv = presence.liveness(shard, now=NOW, work_ts="2026-08-09T11:00:00Z",
                           work_scan=presence.WORK_SCAN_COMPLETE)
    assert lv["state"] == presence.LAPSED
    assert "LAPSED" in lv["annotation"]


def test_work_evidence_is_optional_so_existing_callers_are_unchanged():
    """Every current caller passes no work axis; their rows must be byte-identical
    to before, or this lands as a silent fleet-wide render change."""
    shard = _shard("2026-08-03T11:49:00Z")
    assert (presence.liveness(shard, now=NOW)
            == presence.liveness(shard, now=NOW, work_ts=presence.WORK_TS_ABSENT))


def test_a_fresh_finding_is_conclusive_even_under_a_partial_scan():
    """The asymmetry, pinned. Finding recent work PROVES the agent is working;
    no unscanned remainder can unprove it. Only absence and staleness need a
    complete scan — which is why briefing, whose scan usually goes PARTIAL,
    still reports real work instead of hiding it behind an UNKNOWN."""
    lv = presence.liveness(_shard("2026-08-03T11:49:00Z"), now=NOW,
                           work_ts="2026-08-09T09:00:00Z",
                           work_scan=presence.WORK_SCAN_PARTIAL)
    assert "but filed work" in lv["annotation"], (
        f"a fresh positive finding must survive a partial scan: {lv['annotation']!r}")
    assert "nudge" not in lv["annotation"]
