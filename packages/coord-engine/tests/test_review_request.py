"""CLI `review request` — requester-side durability (watcher-doctrine Task 1).

A review request must write EXACTLY the doc the tally/needs-me readers consume,
so a named required reviewer sees a durable `pending_required` obligation that
stays until their verdict file exists. Regression cover for the production
failure where a reviewer acked directives and the obligation vanished.
"""

import json
import time

from coord_engine import cli, okf
from coord_engine.transport import TransportError
from coord_engine_test_helpers import FakeTransport


# --- bounded-review-fold fixtures (Task 2) -----------------------------------

class CountingTransport(FakeTransport):
    """FakeTransport that records every read/list path for cost assertions."""

    def __init__(self):
        super().__init__()
        self.reads: list[str] = []
        self.lists: list[str] = []

    def read(self, path):
        self.reads.append(path)
        return super().read(path)

    def list_dir(self, prefix):
        self.lists.append(prefix)
        return super().list_dir(prefix)


class SlowTransport(CountingTransport):
    """Every read/list sleeps — models a degraded transport for budget tests."""

    def __init__(self, delay=0.03):
        super().__init__()
        self.delay = delay

    def read(self, path):
        time.sleep(self.delay)
        return super().read(path)

    def list_dir(self, prefix):
        time.sleep(self.delay)
        return super().list_dir(prefix)


def _approve(t, slug, reviewer="rev"):
    """Open a single-reviewer review and file that reviewer's approval, leaving
    the review terminal-APPROVED with no pending_required."""
    cli.main(["review", "request", "r", slug, "--of", "url",
              "--reviewer", reviewer], transport=t)
    t.put(f"team/r/review/{slug}/verdicts/{reviewer}.md",
          f"---\ntype: Verdict\nreviewer: {reviewer}\nverdict: approve\n---\n")


def test_settled_slug_skipped_with_zero_reads(capsys):
    # First fold computes the APPROVED tally and drops the .settled marker...
    t = CountingTransport()
    _approve(t, "pr-set")
    capsys.readouterr()
    cli._pending_reviews_for(t, "r", "someone")
    assert "team/r/review/pr-set/verdicts/.settled" in t.store, "fold must settle it"
    # ...so a second fold skips the slug with ZERO reads of its doc/verdicts.
    t.reads.clear()
    cli._pending_reviews_for(t, "r", "someone")
    slug_reads = [p for p in t.reads if "pr-set" in p]
    assert slug_reads == [], f"settled slug must cost zero reads, got {slug_reads}"


def test_pending_slug_fully_tallied_and_unmarked(capsys):
    t = FakeTransport()
    cli.main(["review", "request", "r", "pr-pend", "--of", "url",
              "--reviewer", "alice"], transport=t)
    capsys.readouterr()
    out = cli._pending_reviews_for(t, "r", "alice")
    pend = [r for r in out if r.get("type") == "review-pending"]
    assert len(pend) == 1 and pend[0]["name"] == "pr-pend"
    assert "team/r/review/pr-pend/verdicts/.settled" not in t.store, \
        "a pending review must NOT be settled"


def test_review_status_writes_settled_marker(capsys):
    t = FakeTransport()
    _approve(t, "pr-rs")
    capsys.readouterr()
    assert cli.main(["review", "status", "r", "pr-rs", "--json"], transport=t) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "APPROVED"
    assert "team/r/review/pr-rs/verdicts/.settled" in t.store


def test_review_status_ignores_wrong_stale_marker(capsys):
    # A corrupt marker claiming APPROVED must NOT fool a direct query.
    t = FakeTransport()
    cli.main(["review", "request", "r", "pr-w", "--of", "url",
              "--reviewer", "alice", "--reviewer", "bob"], transport=t)
    t.put("team/r/review/pr-w/verdicts/.settled",
          "---\nschema: review-settled/v1\nstate: APPROVED\n---\n")
    capsys.readouterr()
    cli.main(["review", "status", "r", "pr-w", "--json"], transport=t)
    res = json.loads(capsys.readouterr().out)
    assert res["state"] == "PENDING", "review status must compute truth, not trust the marker"
    assert set(res["pending_required"]) == {"alice", "bob"}


def test_fold_budget_emits_degraded_marker(capsys):
    t = SlowTransport(delay=0.03)
    for i in range(4):
        cli.main(["review", "request", "r", f"pr-{i}", "--of", "url",
                  "--reviewer", "alice"], transport=t)
    capsys.readouterr()
    out = cli._pending_reviews_for(t, "r", "alice", deadline_seconds=0.01)
    deg = [r for r in out if r.get("type") == "review-fold-degraded"]
    assert len(deg) == 1, "budget breach must append exactly one degraded marker"
    assert deg[0]["total"] == 4
    assert 1 <= deg[0]["scanned"] < 4, deg[0]


def test_briefing_and_needs_me_degraded_exit_zero_with_text(capsys, monkeypatch):
    monkeypatch.setenv("COORD_REVIEW_FOLD_BUDGET", "0.01")
    t = SlowTransport(delay=0.03)
    for i in range(4):
        cli.main(["review", "request", "r", f"pr-{i}", "--of", "url",
                  "--reviewer", "alice"], transport=t)
    capsys.readouterr()

    assert cli.main(["needs-me", "r", "--agent", "alice"], transport=t) == 0
    assert "review fold degraded" in capsys.readouterr().out

    assert cli.main(["briefing", "r", "--agent", "alice"], transport=t) == 0
    assert "review fold degraded" in capsys.readouterr().out

    cli.main(["needs-me", "r", "--agent", "alice", "--json"], transport=t)
    got = json.loads(capsys.readouterr().out)
    assert any(r.get("type") == "review-fold-degraded" for r in got), \
        "json path must surface the marker as-is"


def test_single_slug_transport_error_skipped_and_counted(capsys):
    class OneSlugFails(CountingTransport):
        def list_dir(self, prefix):
            if prefix == "team/r/review/pr-bad/verdicts/":
                raise TransportError("boom")
            return super().list_dir(prefix)

    t = OneSlugFails()
    cli.main(["review", "request", "r", "pr-bad", "--of", "url",
              "--reviewer", "alice"], transport=t)
    cli.main(["review", "request", "r", "pr-good", "--of", "url",
              "--reviewer", "alice"], transport=t)
    capsys.readouterr()
    out = cli._pending_reviews_for(t, "r", "alice")
    assert any(r.get("type") == "review-pending" and r.get("name") == "pr-good"
               for r in out), "a sibling slug's timeout must not hide pr-good"
    deg = [r for r in out if r.get("type") == "review-fold-degraded"]
    assert len(deg) == 1 and deg[0]["skipped"] == 1, deg


def test_request_creates_doc_at_tally_path(capsys):
    # (a) the request writes to the SAME path _review_doc_path/_review_tally read.
    t = FakeTransport()
    assert cli.main(
        ["review", "request", "r", "PR 9: fix auth", "--of",
         "https://github.com/x/y/pull/9", "--reviewer", "reviewer"],
        transport=t,
    ) == 0
    doc = t.read("team/r/review/pr-9-fix-auth.md")
    assert doc is not None, "request must write the doc at the tally's expected path"
    fm = okf.parse_frontmatter(doc)
    assert fm["schema"] == "review-request/v1"
    assert fm["of"] == "https://github.com/x/y/pull/9"
    assert fm["required"] == ["reviewer"]
    assert fm.get("requested_by")
    assert fm.get("ts")
    out = capsys.readouterr().out
    # echo: slug + the verdict-file path a reviewer must write
    assert "pr-9-fix-auth" in out
    assert "team/r/review/pr-9-fix-auth/verdicts/reviewer.md" in out


def test_slug_like_argument_preserved(capsys):
    t = FakeTransport()
    cli.main(["review", "request", "r", "pr-42", "--of", "desc",
              "--reviewer", "amy"], transport=t)
    assert t.read("team/r/review/pr-42.md") is not None


def test_needs_me_lists_named_role_until_verdict(capsys):
    # (b) + (c): the durable marker. Role name used directly as --agent.
    t = FakeTransport()
    cli.main(["review", "request", "r", "pr-9", "--of", "url",
              "--reviewer", "reviewer"], transport=t)
    capsys.readouterr()

    assert cli.main(["needs-me", "r", "--agent", "reviewer", "--json"],
                    transport=t) == 0
    got = json.loads(capsys.readouterr().out)
    pend = [r for r in got if r.get("type") == "review-pending"]
    assert len(pend) == 1 and pend[0]["name"] == "pr-9"

    # obligation persists across repeated polls (structural, not one-shot)
    cli.main(["needs-me", "r", "--agent", "reviewer", "--json"], transport=t)
    assert [r for r in json.loads(capsys.readouterr().out)
            if r.get("type") == "review-pending"], "must stay until verdict exists"

    # verdict file appears -> obligation drops
    t.put("team/r/review/pr-9/verdicts/reviewer.md",
          "---\ntype: Verdict\nreviewer: reviewer\nverdict: approve\n---\n")
    cli.main(["needs-me", "r", "--agent", "reviewer", "--json"], transport=t)
    assert not [r for r in json.loads(capsys.readouterr().out)
                if r.get("type") == "review-pending"], "must drop once verdict filed"


def test_needs_me_routes_role_to_fresh_holder(capsys):
    # role-awareness: a request naming a ROLE surfaces to its fresh lease holder.
    t = FakeTransport()
    # huge SLA keeps the lease fresh regardless of wall-clock `now`
    t.put("team/r/roles/reviewer.md",
          "---\ntype: Role\npolicy: shared\nsla_hours: 8760000\n---\n")
    t.put("team/r/roles/reviewer/leases/amy.md",
          "---\ntype: Lease\nagent: amy\ntimestamp: 2026-07-01T00:00:00Z\n---\n")
    cli.main(["review", "request", "r", "pr-1", "--of", "url",
              "--reviewer", "reviewer"], transport=t)
    capsys.readouterr()
    cli.main(["needs-me", "r", "--agent", "amy", "--json"], transport=t)
    got = json.loads(capsys.readouterr().out)
    assert [r for r in got if r.get("type") == "review-pending"], \
        "fresh holder of the required role owes the verdict"


def test_review_status_reflects_required_gating(capsys):
    # (d): PENDING until every required reviewer has filed a verdict.
    t = FakeTransport()
    cli.main(["review", "request", "r", "pr-9", "--of", "url",
              "--reviewer", "alice", "--reviewer", "bob"], transport=t)
    capsys.readouterr()

    cli.main(["review", "status", "r", "pr-9", "--json"], transport=t)
    assert json.loads(capsys.readouterr().out)["state"] == "PENDING"

    t.put("team/r/review/pr-9/verdicts/alice.md",
          "---\ntype: Verdict\nreviewer: alice\nverdict: approve\n---\n")
    cli.main(["review", "status", "r", "pr-9", "--json"], transport=t)
    assert json.loads(capsys.readouterr().out)["state"] == "PENDING"

    t.put("team/r/review/pr-9/verdicts/bob.md",
          "---\ntype: Verdict\nreviewer: bob\nverdict: approve\n---\n")
    cli.main(["review", "status", "r", "pr-9", "--json"], transport=t)
    assert json.loads(capsys.readouterr().out)["state"] == "APPROVED"


def test_request_requires_at_least_one_reviewer(capsys):
    t = FakeTransport()
    # argparse-level: --reviewer is required; missing -> SystemExit(2)
    try:
        cli.main(["review", "request", "r", "pr-9", "--of", "url"], transport=t)
        assert False, "expected argparse to reject a request with no reviewer"
    except SystemExit as e:
        assert e.code == 2


def test_request_rejects_whitespace_only_reviewer(capsys):
    # A whitespace-only --reviewer filters to required=[] — the review would
    # then gate on nothing. Guard: exit 2, write no doc.
    t = FakeTransport()
    assert cli.main(
        ["review", "request", "r", "pr-9", "--of", "url", "--reviewer", "  "],
        transport=t,
    ) == 2
    assert t.read("team/r/review/pr-9.md") is None, "must write no doc when gating on nothing"
