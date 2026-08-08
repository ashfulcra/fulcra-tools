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


# --- the ACTING path: cmd_escalate is what actually mints the P1 ----------
#
# Round-1 defect: attendance was wired into `roles status` only. That improved
# what an operator READS while the sweep kept emitting the same false
# "unattended" tasks. The diagnostic is not the actuator — these tests go
# through `escalate` for that reason.

from coord_engine_test_helpers import FakeTransport  # noqa: E402

STALE_LEASE = "---\ntype: Lease\nagent: codex-reviewer\ntimestamp: 2026-08-03T08:00:00Z\n---\n"
ROLE_DOC = "---\ntype: Role\nsla_hours: 12\nmaintainer: ash\n---\n"


def _team_with_stale_lease():
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md", ROLE_DOC)
    t.put("team/r/roles/reviewer/leases/codex-reviewer.md", STALE_LEASE)
    return t


def _tasks(t):
    return [p for p in t.store if "/task/" in p]


def test_escalate_suppresses_when_a_holder_filed_a_verdict(capsys):
    """THE regression codex asked for: stale lease + recent holder verdict,
    end to end through the sweep. No P1 may be minted."""
    t = _team_with_stale_lease()
    v = "team/r/review/some-pr/verdicts/abc--codex-reviewer.md"
    t.put(v, "---\nverdict: approved\n---\n")
    t.mtimes[v] = "2026-08-07 07:37AM UTC"

    assert cli.main(["escalate", "r"], transport=t) == 0
    err = capsys.readouterr().err
    assert "Escalation suppressed" in err
    assert "the LEASE lapsed, the job did not" in err
    assert not _tasks(t), "a served role must not mint an unattended P1"


def test_escalate_still_mints_for_a_genuinely_absent_role(capsys):
    """The fix must not swallow real vacancies — and must SAY it checked."""
    t = _team_with_stale_lease()
    v = "team/r/review/some-pr/verdicts/abc--somebody-else.md"
    t.put(v, "---\nverdict: approved\n---\n")
    t.mtimes[v] = "2026-08-07 07:37AM UTC"

    assert cli.main(["escalate", "r"], transport=t) == 0
    minted = _tasks(t)
    assert len(minted) == 1
    assert minted[0].startswith("team/r/task/role-vacant-"), "slug family is a contract"
    body = t.store[minted[0]]
    assert "UNATTENDED past" in body
    assert "COMPLETE verdict sweep" in body


def test_escalate_never_claims_unattended_when_it_could_not_check(capsys):
    """No reviews to scan -> attendance UNKNOWN. It must still escalate (a
    lapsed lease matters) but must not assert that nobody is working."""
    t = _team_with_stale_lease()

    assert cli.main(["escalate", "r"], transport=t) == 0
    minted = _tasks(t)
    assert len(minted) == 1
    body = t.store[minted[0]]
    assert "lease lapsed past" in body
    assert "UNVERIFIED" in body
    assert "UNATTENDED past" not in body, (
        "an unchecked sweep may not assert absence — this exact wording is what "
        "made four days of false P1s read as fact"
    )


def test_an_empty_review_listing_is_unknown_not_absent():
    """A complete sweep of an EMPTY set is not evidence of absence — a wrong
    prefix looks exactly like this."""
    from datetime import datetime, timezone

    since = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)
    assert _attended({}, ["codex-reviewer"], since) == (None, 0, 0)


# --- the attendance scan is paid ONCE per sweep -----------------------------
# coord-boss, 2026-08-08: `escalate` timed out on two consecutive watchdogs
# (rc 143 at 170s and 175s), so the fleet's vacancy check was unavailable 12h.
# Profiled: `_role_attended` was 47.3s of 98.2s, entirely transport — it rebuilt
# its scan per role, 41 sequential listings each. NOT `_held_roles_for_rows`,
# which is where we both expected to find it.

def _counting_transport(base_cls, n_reviews=6):
    class T(base_cls):
        def __init__(self):
            super().__init__()
            self.review_root_lists = 0
            self.verdict_lists = 0

        def list_dir(self, prefix):
            if prefix == "team/r/review/":
                self.review_root_lists += 1
            elif prefix.startswith("team/r/review/") and prefix.endswith("/verdicts/"):
                self.verdict_lists += 1
            return super().list_dir(prefix)
    return T()


def test_the_verdict_scan_is_built_once_not_per_role(monkeypatch):
    """The fix, stated as a behaviour: attendance cost is constant in the number
    of roles. Before, a sweep with N acting roles re-listed the review root N
    times and re-walked every verdict prefix N times."""
    from coord_engine import cli
    t = _counting_transport(FakeTransport)
    for i in range(4):
        t.put(f"team/r/review/pr{i}.md", "---\ntype: Review\n---\nr")
        t.put(f"team/r/review/pr{i}/verdicts/aaa--alice.md",
              "---\ntype: Verdict\nverdict: approve\n---\nv")
    # three roles, each with a lapsed lease -> three acting-path candidates
    for r in ("r1", "r2", "r3"):
        t.put(f"team/r/roles/{r}.md",
              "---\ntype: Role\nstatus: active\nsla_hours: 1\n---\nx")
        t.put(f"team/r/roles/{r}/leases/alice-1.md",
              "---\ntype: Lease\nagent: alice\ntimestamp: 2020-01-01T00:00:00Z\n---\nl")
    cli.main(["escalate", "r"], transport=t)
    assert t.review_root_lists == 1, (
        f"the review root was listed {t.review_root_lists}x — the scan is being "
        f"rebuilt per role again")
    assert t.verdict_lists <= 4, (
        f"{t.verdict_lists} verdict listings for 4 reviews — the per-role "
        f"multiplication is back")


def test_the_ordinary_count_cap_is_coverage_not_degradation(capsys):
    """412 review dirs on the live store against a cap of 40: partial coverage is
    the DESIGNED state. An rc-3 alarm on every single run is worth as much as no
    alarm, so rc 3 is reserved for a wall-clock cut."""
    from coord_engine import cli
    t = FakeTransport()
    for i in range(60):                       # more reviews than the count budget
        t.put(f"team/r/review/pr{i}.md", "---\ntype: Review\n---\nr")
        t.put(f"team/r/review/pr{i}/verdicts/aaa--alice.md",
              "---\ntype: Verdict\nverdict: approve\n---\nv")
    capsys.readouterr()
    rc = cli.main(["escalate", "r"], transport=t)
    err = capsys.readouterr().err
    assert rc == 0, "the normal count cap must not raise a degraded rc"
    assert "attendance=40/60" in err, err
    assert "DEGRADED" not in err


def test_a_wall_clock_cut_DOES_fail_closed(capsys, monkeypatch):
    """The count budget alone cannot bound wall-clock — that is how a 40-dir scan
    became a two-minute call. A deadline cut is the real anomaly and fails closed."""
    from coord_engine import cli
    # A tiny-but-positive budget RACES an in-memory transport (six listings can
    # finish inside 0.1ms), so pin the clock instead of hoping: the deadline is
    # already in the past on the first check.
    monkeypatch.setattr(cli.Deadline, "expired", lambda self: True)
    t = FakeTransport()
    for i in range(6):
        t.put(f"team/r/review/pr{i}.md", "---\ntype: Review\n---\nr")
        t.put(f"team/r/review/pr{i}/verdicts/aaa--alice.md",
              "---\ntype: Verdict\nverdict: approve\n---\nv")
    capsys.readouterr()
    rc = cli.main(["escalate", "r"], transport=t)
    err = capsys.readouterr().err
    assert rc == 3, "a deadline-cut attendance scan must fail closed"
    assert "DEGRADED" in err and "wall-clock budget" in err


# --- the cut reason must not leak between scans -----------------------------
# codex-reviewer, PR 570 r1: `_ATT_CUT_BY_DEADLINE` was module state updated
# ONLY on the normal return, so the two early returns inherited the PREVIOUS
# invocation's value. An identical unreadable scan then produced a different
# `escalate` rc depending on whether some earlier scan had timed out. I flagged
# the module state as a smell in my own review request and shipped it anyway;
# the cut reason now rides in the return tuple, per scan.

from coord_engine.transport import TransportError  # noqa: E402


class _RootUnreadable(FakeTransport):
    def list_dir(self, prefix):
        if prefix == "team/r/review/":
            raise TransportError("root unreadable")
        return super().list_dir(prefix)


def _seeded_reviews(t, n=3):
    for i in range(n):
        t.put(f"team/r/review/pr{i}.md", "---\ntype: Review\n---\nr")
        t.put(f"team/r/review/pr{i}/verdicts/aaa--alice.md",
              "---\ntype: Verdict\nverdict: approve\n---\nv")
    return t


def test_a_root_unreadable_scan_reports_no_deadline_cut_after_a_PRIOR_cut(monkeypatch):
    """The exact reproduction codex described: prior scan cut by deadline, then a
    root-unreadable scan. The second must report its OWN reason."""
    from coord_engine import cli
    # 1) a scan that IS cut by its deadline
    monkeypatch.setattr(cli.Deadline, "expired", lambda self: True)
    cut = cli._verdict_activity_index(_seeded_reviews(FakeTransport()), "r",
                                      deadline=cli.Deadline.open(30))
    assert cut[4] is True, "the deadline-cut scan should report a cut"
    # 2) an unrelated scan whose ROOT listing fails — not a deadline cut
    monkeypatch.undo()
    unreadable = cli._verdict_activity_index(_RootUnreadable(), "r",
                                             deadline=cli.Deadline.open(30))
    assert unreadable[4] is False, (
        "the root-unreadable scan inherited the previous scan's cut flag — the "
        "diagnostic is leaking across invocations again")
    assert unreadable[1] == 0 and unreadable[2] == 0


def test_an_unreadable_verdicts_prefix_reports_its_own_cut_state(monkeypatch):
    """The other early return: a verdicts prefix that raises. It is UNKNOWN
    (undatable=True) but it is not a wall-clock cut."""
    from coord_engine import cli

    class T(FakeTransport):
        def list_dir(self, prefix):
            if prefix.endswith("/verdicts/"):
                raise TransportError("verdicts unreadable")
            return super().list_dir(prefix)

    out = cli._verdict_activity_index(_seeded_reviews(T()), "r",
                                      deadline=cli.Deadline.open(30))
    assert out[3] is True, "an unreadable verdicts prefix is UNKNOWN evidence"
    assert out[4] is False, "it is not a deadline cut"


def test_consecutive_clean_scans_never_accumulate_a_cut():
    """And the benign direction: repeated clean scans stay clean."""
    from coord_engine import cli
    for _ in range(3):
        out = cli._verdict_activity_index(_seeded_reviews(FakeTransport()), "r",
                                          deadline=cli.Deadline.open(30))
        assert out[4] is False


# --- a vacancy alarm addressed to the absent party (coord-boss, 2026-08-08) ---

def test_a_self_addressed_vacancy_is_reported_not_silently_delivered(capsys):
    """The live loss: three daily ROLE VACANT directives for a role whose
    registered `maintainer:` was its own retired holder. The alarm about an
    absence, delivered to the absent party — a closed loop with no exit, and
    every instrument said 'escalated' because one had been written."""
    t = FakeTransport()
    t.put("team/r/roles/arc.md", "---\ntype: Role\nmaintainer: arcbot\n---\n")
    t.put("team/r/roles/arc/leases/arcbot.md",
          "---\ntype: Lease\nagent: arcbot\ntimestamp: 2026-06-01T00:00:00Z\n---\n")
    capsys.readouterr()
    assert cli.main(["escalate", "r"], transport=t) == 0
    out, err = capsys.readouterr()
    assert "escalated arc -> arcbot" in out, "still escalates; the notice is not suppressed"
    assert "IS its own lapsed holder" in err and "no exit" in err
    # and the directive itself carries it, for whoever eventually reads the bucket
    written = [v for k, v in t.store.items() if "/task/" in k]
    assert written, "a directive must still be written"
    assert any("CLOSED LOOP" in d for d in written), (
        "the written directive must carry it too — the stderr line is invisible "
        "to whoever eventually reads the bucket")


def test_a_third_party_maintainer_stays_silent(capsys):
    """The other side, and the reason this is not 'warn whenever the maintainer
    looks quiet': a normal role maintained by someone else must produce no
    noise, or the warning that matters gets tuned out."""
    t = FakeTransport()
    t.put("team/r/roles/rev.md", "---\ntype: Role\nmaintainer: coord-boss\n---\n")
    t.put("team/r/roles/rev/leases/someone.md",
          "---\ntype: Lease\nagent: someone\ntimestamp: 2026-06-01T00:00:00Z\n---\n")
    capsys.readouterr()
    assert cli.main(["escalate", "r"], transport=t) == 0
    assert "lapsed holder" not in capsys.readouterr().err


def test_the_notice_is_never_rerouted_to_a_worse_address():
    """My first version rerouted to `_human()`, and an existing test caught it
    doing harm: a role legitimately maintained by the human operator, who also
    appears as a lease agent, had its notice moved off a real person onto the
    bare 'human' default that nobody reads. Detect and report; never rewrite the
    destination — the same rule we hold for alias resolution."""
    assert cli._is_self_addressed_vacancy("ash", [{"agent": "ash"}]) is True
    assert cli._is_self_addressed_vacancy("coord-boss", [{"agent": "ash"}]) is False
    assert cli._is_self_addressed_vacancy("coord-boss", []) is False
    assert cli._is_self_addressed_vacancy("coord-boss", None) is False
