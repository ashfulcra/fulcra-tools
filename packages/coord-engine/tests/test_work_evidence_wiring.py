"""The work axis must reach `presence show`, not just exist in the fold.

Twice before I have built a decision function, unit-tested it, and left it
unconnected to the acting path — the tests passed while production was
unchanged. So these tests drive the REAL command and assert on what a reader
actually sees.
"""

from __future__ import annotations

from coord_engine import cli
from coord_engine.transport import TransportError

TEAM = "r"


class _Store:
    """Minimal transport: dict of path -> list_dir rows, with mtimes rendered in
    the store's real TWELVE-HOUR format so the parse is exercised, not bypassed."""

    def __init__(self, dirs, shards, raising=()):
        self.dirs, self.shards, self.raising = dirs, shards, set(raising)

    def list_dir(self, path):
        if path in self.raising:
            raise TransportError(f"unreadable: {path}")
        return self.dirs.get(path)

    def read(self, path):
        return self.shards.get(path)

    def write(self, path, text):
        return True


def _index(store):
    """Index only — the `ok` flag is asserted separately where it matters."""
    return cli._work_evidence_index(store, TEAM)[0]


def test_a_verdict_attributes_work_to_its_REVIEWER():
    """`<head>--<reviewer>.md` is the only attribution a verdict carries, and
    filing one is not a verb — this is the codex case that started all of it."""
    store = _Store(
        dirs={
            f"team/{TEAM}/review": [{"name": "pr-1/", "is_dir": True}],
            f"team/{TEAM}/review/pr-1/verdicts": [
                {"name": "abc123--codex-reviewer.md",
                 "mtime": "2026-08-09 11:20AM UTC"},
            ],
            f"team/{TEAM}/_coord/agents": [],
        },
        shards={})
    assert _index(store).get("codex-reviewer") == "2026-08-09T11:20:00Z"


def test_a_report_doc_attributes_work_to_its_AUTHOR():
    """The other verb-less path: a report written straight to the store."""
    store = _Store(
        dirs={
            f"team/{TEAM}/review": [],
            f"team/{TEAM}/_coord/agents": [{"name": "coord-opus-worker/"}],
            f"team/{TEAM}/_coord/agents/coord-opus-worker/reports": [
                {"name": "2026-08-09-a-report.md", "mtime": "2026-08-09 09:57AM UTC"},
            ],
        },
        shards={})
    assert _index(store).get("coord-opus-worker") == "2026-08-09T09:57:00Z"


def test_newest_wins_across_the_midnight_hour():
    """The 12-hour trap, pinned. String-ordered, `01:14AM` sorts BEFORE
    `12:13AM` and the older entry wins — 12AM sorts after every other hour."""
    store = _Store(
        dirs={
            f"team/{TEAM}/review": [{"name": "pr-1/"}],
            f"team/{TEAM}/review/pr-1/verdicts": [
                {"name": "a--rev.md", "mtime": "2026-08-09 12:13AM UTC"},
                {"name": "b--rev.md", "mtime": "2026-08-09 01:14AM UTC"},
            ],
            f"team/{TEAM}/_coord/agents": [],
        },
        shards={})
    assert _index(store).get("rev") == "2026-08-09T01:14:00Z", (
        "01:14AM is LATER than 12:13AM; a lexical comparison inverts them")


def test_an_unreadable_listing_yields_no_absence_claim():
    """UNKNOWN, never 'did nothing'. A raising listing must contribute nothing
    rather than an entry asserting the agent is idle."""
    store = _Store(
        dirs={f"team/{TEAM}/_coord/agents": []},
        shards={},
        raising=[f"team/{TEAM}/review"])
    idx, ok = cli._work_evidence_index(store, TEAM)
    assert idx == {}
    assert ok is False, (
        "a raising listing cannot license an absence reading; ok must be False "
        "so the fold declines to call anyone idle")


def test_presence_show_renders_both_facts_end_to_end(capsys):
    """The wiring test. Drives the real command and asserts on the printed row:
    a stale beat plus fresh work must show BOTH ages and carry NO nudge."""
    shard = ("---\ntype: Presence\nagent: codex-reviewer\n"
             "timestamp: 2026-08-03T11:49:00Z\n---\n")
    store = _Store(
        dirs={
            # NB trailing slash: `_presence_prefix` returns "team/<t>/presence/".
            f"team/{TEAM}/presence/": [{"name": "codex-reviewer.md"}],
            f"team/{TEAM}/review": [{"name": "pr-1/"}],
            f"team/{TEAM}/review/pr-1/verdicts": [
                {"name": "abc--codex-reviewer.md", "mtime": "2026-08-09 11:20AM UTC"},
            ],
            f"team/{TEAM}/_coord/agents": [],
        },
        shards={f"team/{TEAM}/presence/codex-reviewer.md": shard})

    assert cli.main(["presence", "show", TEAM], transport=store) == 0
    out = capsys.readouterr().out
    assert "codex-reviewer" in out
    # These assertions must be ones ONLY A REAL MEASUREMENT can satisfy.
    # The first cut asserted `"beat" in out and "work" in out` and passed with
    # the wiring deleted: an unmeasured row still reads "stale 6d (beat) · work
    # evidence UNKNOWN", which contains both words and carries no nudge. The
    # test was structurally incapable of detecting the disconnection it existed
    # to catch — caught by mutating the call site, which is the only thing that
    # would have caught it.
    assert "but filed work" in out, (
        "the row must carry the MEASURED work age; if this reads UNKNOWN the "
        f"index never reached the fold:\n{out}")
    assert "UNKNOWN" not in out, (
        f"work evidence was available and must not render as unmeasured:\n{out}")
    assert "6d" in out, f"the stale beat must still be stated plainly:\n{out}"
    assert "nudge" not in out, (
        f"an agent with fresh work must not be nudged; got:\n{out}")


def test_presence_show_BOUNDS_its_scan_and_degrades_to_partial(capsys, monkeypatch):
    """codex-reviewer, 591 r3: the deadline must reach the real command.

    Unbounded, `presence show` listed every review's `verdicts/` directory — 435
    on the live store — synchronously, to decorate a roster. The existing budget
    tests only exercised the helper and briefing, so a missing deadline on this
    path was invisible to them.

    Driven end to end with the budget set to zero: the scan must stop, and the
    row must render PARTIAL rather than claiming the agent has no work.
    """
    # Patch the budget FUNCTION, not the env var: `config.env_float` is a
    # positive-finite knob by policy, so COORD_PRESENCE_WORK_BUDGET=0 silently
    # falls back to the 20s default and would make this test pass for the wrong
    # reason. A negative budget opens an already-expired Deadline, so the stop
    # is deterministic rather than a race against a tiny timeout.
    monkeypatch.setattr(cli, "_presence_work_budget", lambda: -1.0)
    shard = ("---\ntype: Presence\nagent: codex-reviewer\n"
             "timestamp: 2026-08-03T11:49:00Z\n---\n")
    listed: list[str] = []

    class _Counting(_Store):
        def list_dir(self, path):
            listed.append(path)
            return super().list_dir(path)

    store = _Counting(
        dirs={
            f"team/{TEAM}/presence/": [{"name": "codex-reviewer.md"}],
            f"team/{TEAM}/review": [{"name": "pr-1/"}],
            f"team/{TEAM}/review/pr-1/verdicts": [
                {"name": "abc--codex-reviewer.md", "mtime": "2026-08-09 11:20AM UTC"},
            ],
            f"team/{TEAM}/_coord/agents": [],
        },
        shards={f"team/{TEAM}/presence/codex-reviewer.md": shard})

    assert cli.main(["presence", "show", TEAM], transport=store) == 0
    out = capsys.readouterr().out
    assert "scan incomplete" in out, (
        "an expired budget must render PARTIAL, not a confident absence:\n" + out)
    assert "no work found" not in out, (
        "an unfinished scan must never claim the agent has no work:\n" + out)
    assert "nudge" not in out, (
        "PARTIAL withholds the imperative — nothing was established:\n" + out)
    # And it must actually STOP: no verdicts directory was walked.
    assert not any("verdicts" in p for p in listed), (
        f"the scan continued past its deadline: {listed}")


def test_agent_reports_are_scanned_BEFORE_the_review_sweep():
    """Order is load-bearing, so it is pinned.

    Measured on the live store right after 591 shipped: 35 agent directories vs
    438 review directories, one listing each. With reviews first, the sweep ate
    the whole budget every time — at 120s (6x the shipped budget) the scan was
    still incomplete, having attributed work to THREE agents. PARTIAL withholds
    the nudge, so a fix aimed at false nudges produced no nudges at all.

    Reordering took the same 20s budget from 2 agents to 11.
    """
    order: list[str] = []

    class _Ordered(_Store):
        def list_dir(self, path):
            order.append(path)
            return super().list_dir(path)

    store = _Ordered(
        dirs={
            f"team/{TEAM}/_coord/agents": [{"name": "a1/"}],
            f"team/{TEAM}/_coord/agents/a1/reports": [
                {"name": "r.md", "mtime": "2026-08-09 09:00AM UTC"}],
            f"team/{TEAM}/review": [{"name": "pr-1/"}],
            f"team/{TEAM}/review/pr-1/verdicts": [
                {"name": "abc--rev.md", "mtime": "2026-08-09 10:00AM UTC"}],
        },
        shards={})
    cli._work_evidence_index(store, TEAM)
    agents_at = order.index(f"team/{TEAM}/_coord/agents")
    review_at = order.index(f"team/{TEAM}/review")
    assert agents_at < review_at, (
        "the cheap half (35 listings) must run before the expensive one (438), "
        f"or the budget never reaches it: {order}")


def test_a_budget_that_dies_in_the_review_sweep_still_kept_the_agent_evidence():
    """The behavioural half: partway-through is exactly the live case.

    The scan is PARTIAL on the real store either way, so what matters is WHICH
    half completed. Agent evidence must survive a cutoff that lands in the
    review sweep — that is the whole reason for the order.
    """
    class _DieAfterAgents(_Store):
        def list_dir(self, path):
            # Expire the budget the moment the review sweep begins.
            if path == f"team/{TEAM}/review":
                cli.time.monotonic = lambda: 1e9      # type: ignore[assignment]
            return super().list_dir(path)

    # The deadline is an ABSOLUTE monotonic instant, so it must be in the real
    # future — my first cut passed 1.0, which is already long past, and the scan
    # expired before its first listing (empty index, test red for the wrong
    # reason).
    real_monotonic = cli.time.monotonic
    deadline = real_monotonic() + 60.0
    try:
        store = _DieAfterAgents(
            dirs={
                f"team/{TEAM}/_coord/agents": [{"name": "a1/"}],
                f"team/{TEAM}/_coord/agents/a1/reports": [
                    {"name": "r.md", "mtime": "2026-08-09 09:00AM UTC"}],
                f"team/{TEAM}/review": [{"name": "pr-1/"}],
                f"team/{TEAM}/review/pr-1/verdicts": [
                    {"name": "abc--rev.md", "mtime": "2026-08-09 10:00AM UTC"}],
            },
            shards={})
        idx, ok = cli._work_evidence_index(store, TEAM, deadline=deadline)
    finally:
        cli.time.monotonic = real_monotonic           # type: ignore[assignment]

    assert idx.get("a1") == "2026-08-09T09:00:00Z", (
        f"agent evidence was lost to a cutoff in the review sweep: {idx}")
    assert ok is False, "a cut-off scan must report PARTIAL, never complete"
