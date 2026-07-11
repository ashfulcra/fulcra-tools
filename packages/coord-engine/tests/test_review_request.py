"""CLI `review request` — requester-side durability (watcher-doctrine Task 1).

A review request must write EXACTLY the doc the tally/needs-me readers consume,
so a named required reviewer sees a durable `pending_required` obligation that
stays until their verdict file exists. Regression cover for the production
failure where a reviewer acked directives and the obligation vanished.
"""

import json
import time

from coord_engine import cli, okf, reconcile
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


class DocReadFailsTransport(CountingTransport):
    """Doc reads return None (the Task-1 `read()` timeout contract — it never
    raises) while verdict reads succeed: the false-settle incident shape."""

    def __init__(self):
        super().__init__()
        self.doc_reads_fail = True

    def read(self, path):
        if (self.doc_reads_fail and path.startswith("team/r/review/")
                and path.endswith(".md") and "/verdicts/" not in path):
            self.reads.append(path)
            return None  # timeout: content unknown, no exception raised
        return super().read(path)


def _trap(t, slug="pr-t"):
    """Open a required-alice review, then plant a READABLE stray approval —
    with the doc unreadable, required=None + one approval = false APPROVED."""
    cli.main(["review", "request", "r", slug, "--of", "url",
              "--reviewer", "alice"], transport=t)
    t.put(f"team/r/review/{slug}/verdicts/bob.md",
          "---\ntype: Verdict\nreviewer: bob\nverdict: approve\n---\n")


def test_doc_read_timeout_never_writes_false_settled(capsys):
    # Regression (review REJECTED finding): doc read -> None => required=None
    # => tally APPROVED off one readable approval => durable false .settled.
    t = DocReadFailsTransport()
    _trap(t)
    capsys.readouterr()
    out = cli._pending_reviews_for(t, "r", "alice")
    assert "team/r/review/pr-t/verdicts/.settled" not in t.store, \
        "a doc-read timeout must NEVER settle the review"
    # the slug is unknown, not silently dropped: surfaced via skipped
    deg = [r for r in out if r.get("type") == "review-fold-degraded"]
    assert len(deg) == 1 and deg[0]["skipped"] == 1, deg
    assert not [r for r in out if r.get("type") == "review-pending"], \
        "unknown is not pending either — it is skipped, visibly"


def test_review_status_doc_read_timeout_fails_loud_no_marker(capsys):
    t = DocReadFailsTransport()
    _trap(t)
    capsys.readouterr()
    assert cli.main(["review", "status", "r", "pr-t", "--json"], transport=t) == 1
    cap = capsys.readouterr()
    assert "unreadable" in cap.err
    assert "APPROVED" not in cap.out, "must not print a clean APPROVED on unknown"
    assert "team/r/review/pr-t/verdicts/.settled" not in t.store


def test_recovered_transport_tallies_pending_again(capsys):
    # After the transient failure clears, the same slug tallies to the truth.
    t = DocReadFailsTransport()
    _trap(t)
    capsys.readouterr()
    cli._pending_reviews_for(t, "r", "alice")  # degraded pass, writes nothing
    t.doc_reads_fail = False                   # transport recovers
    out = cli._pending_reviews_for(t, "r", "alice")
    pend = [r for r in out if r.get("type") == "review-pending"]
    assert len(pend) == 1 and pend[0]["name"] == "pr-t" \
        and pend[0]["pending_required"] == ["alice"]
    cli.main(["review", "status", "r", "pr-t", "--json"], transport=t)
    assert json.loads(capsys.readouterr().out)["state"] == "PENDING"


def test_unreadable_verdict_blocks_settle_marker(capsys):
    # Same defect class one level down: a listed verdict whose READ returns None
    # could be a hidden CHANGES — an APPROVED tally over it must not be cached.
    class VerdictReadFails(CountingTransport):
        def read(self, path):
            if path == "team/r/review/pr-v/verdicts/carol.md":
                self.reads.append(path)
                return None
            return super().read(path)

    t = VerdictReadFails()
    cli.main(["review", "request", "r", "pr-v", "--of", "url",
              "--reviewer", "alice"], transport=t)
    t.put("team/r/review/pr-v/verdicts/alice.md",
          "---\ntype: Verdict\nreviewer: alice\nverdict: approve\n---\n")
    t.put("team/r/review/pr-v/verdicts/carol.md",   # exists, content unreadable
          "---\ntype: Verdict\nreviewer: carol\nverdict: changes\n---\n")
    capsys.readouterr()
    cli._pending_reviews_for(t, "r", "alice")
    assert "team/r/review/pr-v/verdicts/.settled" not in t.store
    # F1: an unreadable verdict shard makes the tally a floor (carol's CHANGES is
    # hidden) — `review status` must fail closed, not print a false APPROVED rc 0.
    assert cli.main(["review", "status", "r", "pr-v", "--json"], transport=t) == 1
    cap = capsys.readouterr()
    assert "verdict shard unreadable" in cap.err
    assert "APPROVED" not in cap.out, "must not print a clean state on an unknown tally"
    assert "team/r/review/pr-v/verdicts/.settled" not in t.store


def test_forge_style_doc_without_required_never_settles(capsys):
    # Legacy/forge review docs carry no `required:` — legitimately APPROVED but
    # NOT cacheable (an empty required list is indistinguishable from the
    # doc-read-failure shape, so it never earns a marker; state is unaffected).
    t = FakeTransport()
    t.put("team/r/review/pr-f.md", "---\ntype: Review\ntitle: R\n---\n")
    t.put("team/r/review/pr-f/verdicts/forge.md",
          "---\ntype: Verdict\nreviewer: forge\nverdict: approve\n---\n")
    capsys.readouterr()
    cli._pending_reviews_for(t, "r", "alice")
    assert "team/r/review/pr-f/verdicts/.settled" not in t.store
    assert cli.main(["review", "status", "r", "pr-f", "--json"], transport=t) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "APPROVED"
    assert "team/r/review/pr-f/verdicts/.settled" not in t.store


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


def test_single_slug_many_verdicts_bounded_by_budget(capsys):
    # F2: the budget was checked only BETWEEN slugs, so ONE review with many
    # verdict shards read every shard unbounded (N x transport.timeout) with no
    # degraded marker. The deadline must be threaded into the per-verdict loop:
    # the fold stops mid-slug, counts it skipped, and surfaces the marker.
    t = SlowTransport(delay=0.02)
    cli.main(["review", "request", "r", "pr-big", "--of", "url",
              "--reviewer", "alice"], transport=t)
    for i in range(40):
        t.put(f"team/r/review/pr-big/verdicts/rev{i:02d}.md",
              f"---\ntype: Verdict\nreviewer: rev{i:02d}\nverdict: approve\n---\n")
    capsys.readouterr()
    t.reads.clear()
    start = time.monotonic()
    out = cli._pending_reviews_for(t, "r", "alice", deadline_seconds=0.05)
    elapsed = time.monotonic() - start
    # Bounded: reading all 40 shards would take ~0.8s; the budget must cut it off.
    assert elapsed < 0.4, f"per-slug verdict reads must respect the budget, took {elapsed:.3f}s"
    shard_reads = [r for r in t.reads if "/verdicts/rev" in r]
    assert len(shard_reads) < 40, f"budget must stop mid-slug, read {len(shard_reads)}/40"
    deg = [r for r in out if r.get("type") == "review-fold-degraded"]
    assert len(deg) == 1, "a mid-slug budget breach must surface a degraded marker"
    # coherent accounting: the cut-off slug is counted (scanned) AND skipped.
    assert deg[0]["total"] == 1 and deg[0]["scanned"] == 1 and deg[0]["skipped"] == 1, deg[0]


def test_single_slow_verdict_read_overrun_marks_slug_skipped(capsys):
    # P1-B (codex r2): the deadline was checked only BEFORE each verdict read, so
    # ONE stalled read that sleeps past the budget still completed and the slug
    # returned a clean `fully_scanned` row — the budget was blown with no degraded
    # marker. The check must also run AFTER the blocking read: a read that pushes
    # us over budget marks the slug not-fully-scanned (skipped) and surfaces the
    # degraded marker. Overshoot is bounded by ONE transport timeout.
    class SlowVerdictRead(CountingTransport):
        def read(self, path):
            if "/verdicts/" in path and path.endswith(".md"):
                time.sleep(0.2)  # the single stalled read that overruns the budget
            return super().read(path)

    t = SlowVerdictRead()
    # two required reviewers; bob's verdict shard exists (a shard to read), alice
    # is still pending -> at HEAD this yields a clean review-pending row for alice.
    cli.main(["review", "request", "r", "pr-stall", "--of", "url",
              "--reviewer", "alice", "--reviewer", "bob"], transport=t)
    t.put("team/r/review/pr-stall/verdicts/bob.md",
          "---\ntype: Verdict\nreviewer: bob\nverdict: approve\n---\n")
    capsys.readouterr()
    out = cli._pending_reviews_for(t, "r", "alice", deadline_seconds=0.05)
    deg = [r for r in out if r.get("type") == "review-fold-degraded"]
    assert len(deg) == 1, f"a single over-budget read must surface a degraded marker: {out}"
    assert deg[0]["scanned"] == 1 and deg[0]["skipped"] == 1, deg[0]
    assert not any(r.get("type") == "review-pending" for r in out), \
        "a slug whose read blew the budget must NOT return a clean pending row"


def test_review_status_removes_stale_marker_on_pending(capsys):
    # F4: a `.settled` marker planted on a since-reopened (still-PENDING) review
    # is provably stale. `review status` recomputes the truth AND best-effort
    # deletes the marker, so the next fan-out fold sees the pending obligation
    # again instead of settled-skipping it.
    t = FakeTransport()
    cli.main(["review", "request", "r", "pr-st", "--of", "url",
              "--reviewer", "alice", "--reviewer", "bob"], transport=t)
    t.put("team/r/review/pr-st/verdicts/.settled",
          "---\nschema: review-settled/v1\nstate: APPROVED\n---\n")
    capsys.readouterr()
    assert cli.main(["review", "status", "r", "pr-st", "--json"], transport=t) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "PENDING"
    assert "team/r/review/pr-st/verdicts/.settled" not in t.store, \
        "a provably-stale marker must be self-healed away on direct query"
    # the fold now sees the obligation rather than skipping a 'settled' slug.
    out = cli._pending_reviews_for(t, "r", "alice")
    assert any(r.get("type") == "review-pending" and r.get("name") == "pr-st"
               for r in out), "next fold must surface the pending obligation"


class VerdictsListFails(CountingTransport):
    """The verdicts-prefix LISTING raises (the prefix is unlistable under a
    degraded transport — its very membership is unknown, not empty) while the
    doc and everything else read fine. Distinct from an EMPTY verdicts dir
    (list_dir returns []), which is a legitimate no-verdicts PENDING."""

    def __init__(self):
        super().__init__()
        self.list_fails = True

    def list_dir(self, prefix):
        if self.list_fails and prefix.endswith("/verdicts/"):
            raise TransportError("boom")
        return super().list_dir(prefix)


def test_review_status_verdicts_listing_failure_fails_loud_keeps_marker(capsys):
    # F-listing: the verdicts LISTING raised, but `_review_tally` swallowed it and
    # fell back to entries=[] -> vreads_ok VACUOUSLY True -> two fail-closed
    # violations on a direct query: (1) a false PENDING printed rc 0 (clean output
    # on a failed listing), and (2) the F4 self-heal DELETES a legitimate .settled
    # marker off that vacuous non-settleable tally. Now: rc 1 in the same register
    # as the doc/shard-unreadable cases, and the marker is left untouched.
    t = VerdictsListFails()
    cli.main(["review", "request", "r", "pr-ll", "--of", "url",
              "--reviewer", "alice"], transport=t)
    # a legitimate settled marker that must SURVIVE an unreadable-listing query
    t.put("team/r/review/pr-ll/verdicts/.settled",
          "---\nschema: review-settled/v1\nstate: APPROVED\n---\n")
    capsys.readouterr()
    assert cli.main(["review", "status", "r", "pr-ll", "--json"], transport=t) == 1
    cap = capsys.readouterr()
    assert "verdicts listing unreadable" in cap.err
    assert "PENDING" not in cap.out and "APPROVED" not in cap.out, \
        "a failed listing must not print any clean state"
    assert "team/r/review/pr-ll/verdicts/.settled" in t.store, \
        "a failed listing must NOT delete a legitimate .settled marker"


def test_review_status_recovers_after_verdicts_listing_failure(capsys):
    # Once the listing recovers, the same slug tallies to the truth (PENDING).
    t = VerdictsListFails()
    cli.main(["review", "request", "r", "pr-rec", "--of", "url",
              "--reviewer", "alice"], transport=t)
    capsys.readouterr()
    assert cli.main(["review", "status", "r", "pr-rec", "--json"], transport=t) == 1
    capsys.readouterr()
    t.list_fails = False
    assert cli.main(["review", "status", "r", "pr-rec", "--json"], transport=t) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "PENDING"


def test_fold_counts_slug_skipped_when_verdicts_listing_fails(capsys):
    # Fan-out fold alignment: a verdicts-listing failure for a slug is UNKNOWN —
    # counted skipped and surfaced via the degraded marker (same semantics as a
    # doc-read failure), never silently settled or dropped. A readable sibling
    # still surfaces its pending obligation.
    class OneSlugListFails(CountingTransport):
        def list_dir(self, prefix):
            if prefix == "team/r/review/pr-bad/verdicts/":
                raise TransportError("boom")
            return super().list_dir(prefix)

    t = OneSlugListFails()
    cli.main(["review", "request", "r", "pr-bad", "--of", "url",
              "--reviewer", "alice"], transport=t)
    cli.main(["review", "request", "r", "pr-good", "--of", "url",
              "--reviewer", "alice"], transport=t)
    capsys.readouterr()
    out = cli._pending_reviews_for(t, "r", "alice")
    assert any(r.get("type") == "review-pending" and r.get("name") == "pr-good"
               for r in out), "a sibling's listing failure must not hide pr-good"
    deg = [r for r in out if r.get("type") == "review-fold-degraded"]
    assert len(deg) == 1 and deg[0]["skipped"] == 1, deg
    assert "team/r/review/pr-bad/verdicts/.settled" not in t.store, \
        "an unlistable slug must never be settled"


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


def test_rerequest_under_read_timeout_fails_closed_no_overwrite(capsys):
    # P1/#342: a re-request whose review-doc READ times out (returns None) must
    # NOT fall through to WRITE — post-#342 that could clobber a live review under
    # a degraded transport. The I1-style listing guard sees the doc PRESENT in the
    # listing + read None -> rc 1 "unreadable, retry", never overwrite. (Changing a
    # required set re-opens only via a NEW slug; a matching request recovers once
    # the read succeeds.)
    t = FakeTransport()
    cli.main(["review", "request", "r", "pr-u", "--of", "url",
              "--reviewer", "alice", "--from", "requester"], transport=t)
    doc_before = t.read("team/r/review/pr-u.md")
    assert doc_before is not None

    class DocReadTimesOut(FakeTransport):
        def __init__(self, base):
            self.__dict__ = base.__dict__
        def read(self, path):
            # only the review-doc read times out (present-but-unreadable);
            # everything else (listing, directive writes) behaves normally.
            if (path.startswith("team/r/review/") and path.endswith(".md")
                    and "/verdicts/" not in path):
                return None
            return super().read(path)

    capsys.readouterr()
    rc = cli.main(["review", "request", "r", "pr-u", "--of", "url",
                   "--reviewer", "bob", "--from", "requester"],
                  transport=DocReadTimesOut(t))
    cap = capsys.readouterr()
    assert rc == 1
    assert "unreadable" in cap.err and "retry" in cap.err
    assert t.read("team/r/review/pr-u.md") == doc_before, \
        "a present-but-unreadable doc must never be overwritten"


def test_matching_rerequest_clears_stale_settled_marker(capsys):
    # The stale-marker self-heal (former I2) now rides the MATCHING-recovery path:
    # a re-request byte-identical in of/required/requested_by clears a lingering
    # `.settled` marker so the next fold recomputes — WITHOUT the dangerous
    # rewrite-on-read-timeout the old path relied on, and without rewriting the doc.
    t = FakeTransport()
    cli.main(["review", "request", "r", "pr-m", "--of", "url",
              "--reviewer", "alice", "--from", "requester"], transport=t)
    doc_before = t.read("team/r/review/pr-m.md")
    t.put("team/r/review/pr-m/verdicts/.settled",
          "---\nschema: review-settled/v1\nstate: APPROVED\n---\n")
    capsys.readouterr()
    rc = cli.main(["review", "request", "r", "pr-m", "--of", "url",
                   "--reviewer", "alice", "--from", "requester"], transport=t)
    assert rc == 0
    assert "matching" in capsys.readouterr().out
    assert "team/r/review/pr-m/verdicts/.settled" not in t.store, \
        "a matching recovery must clear a stale settled marker"
    assert t.read("team/r/review/pr-m.md") == doc_before, \
        "recovery must not rewrite the doc"


def test_partial_failure_then_recovery_notifies_missing_reviewer(capsys):
    # P1 (the exact repro): bob's directive write fails on the first request, so the
    # doc lands but bob is never notified -> rc 1 "retry". At HEAD every retry died
    # at the exists-guard ("already exists" rc 1) because the doc now exists, so bob
    # stayed an invisible orphan forever. Fix: a MATCHING re-request is idempotent
    # RECOVERY — alice's already-delivered directive dedupes (rc 0), bob's missing
    # one is delivered, the doc is byte-unchanged, and each reviewer ends with
    # EXACTLY ONE canonical directive.
    fail = {"bob": True}

    class BobDirectiveFailsOnce(FakeTransport):
        def write(self, path, content):
            if (fail["bob"] and path.startswith("team/r/task/")
                    and "verdicts/bob.md" in content):
                return False  # bob's directive write times out (partial failure)
            return super().write(path, content)

    t = BobDirectiveFailsOnce()
    rc1 = cli.main(["review", "request", "r", "pr-rec", "--of", "PR#1",
                    "--reviewer", "alice", "--reviewer", "bob",
                    "--from", "requester"], transport=t)
    cap1 = capsys.readouterr()
    assert rc1 == 1 and "bob" in cap1.err and "FAILED" in cap1.err
    doc_before = t.read("team/r/review/pr-rec.md")
    assert doc_before is not None

    fail["bob"] = False  # transport recovers
    rc2 = cli.main(["review", "request", "r", "pr-rec", "--of", "PR#1",
                    "--reviewer", "alice", "--reviewer", "bob",
                    "--from", "requester"], transport=t)
    out = capsys.readouterr().out
    assert rc2 == 0, "a matching re-request after partial failure must recover"
    assert "matching" in out and "re-verified" in out
    assert t.read("team/r/review/pr-rec.md") == doc_before, \
        "recovery must NOT rewrite the doc (byte-compare)"
    # BOTH reviewers now hold EXACTLY ONE canonical directive each.
    for reviewer in ("alice", "bob"):
        hits = [p for p, c in t.store.items()
                if p.startswith("team/r/task/")
                and f"team/r/review/pr-rec/verdicts/{reviewer}.md" in c
                and f"assignee: {reviewer}" in c]
        assert len(hits) == 1, (reviewer, hits)


def test_rerequest_with_different_required_is_conflict(capsys):
    # A re-request with a DIFFERENT required set is a conflict, not a recovery:
    # changing the required set re-opens a review only via a new slug. rc 1 naming
    # what differs; the doc is never overwritten.
    t = FakeTransport()
    cli.main(["review", "request", "r", "pr-c", "--of", "url",
              "--reviewer", "alice", "--from", "requester"], transport=t)
    doc_before = t.read("team/r/review/pr-c.md")
    capsys.readouterr()
    rc = cli.main(["review", "request", "r", "pr-c", "--of", "url",
                   "--reviewer", "alice", "--reviewer", "bob",
                   "--from", "requester"], transport=t)
    cap = capsys.readouterr()
    assert rc == 1
    assert "required" in cap.err  # names the field that differs
    assert "bob" in cap.err       # and both sides of the difference
    assert t.read("team/r/review/pr-c.md") == doc_before, \
        "a conflicting re-request must never overwrite the doc"


def test_rerequest_by_different_requester_is_conflict(capsys):
    # requested_by is part of the request identity: a DIFFERENT requester re-opening
    # someone else's review is a conflict, never a silent recovery. rc 1, no rewrite.
    t = FakeTransport()
    cli.main(["review", "request", "r", "pr-cr", "--of", "url",
              "--reviewer", "alice", "--from", "alice-req"], transport=t)
    doc_before = t.read("team/r/review/pr-cr.md")
    capsys.readouterr()
    rc = cli.main(["review", "request", "r", "pr-cr", "--of", "url",
                   "--reviewer", "alice", "--from", "mallory"], transport=t)
    cap = capsys.readouterr()
    assert rc == 1
    assert "requested_by" in cap.err
    assert t.read("team/r/review/pr-cr.md") == doc_before, \
        "a different requester must not overwrite the original review doc"


def test_request_write_timeout_fails_loud(capsys):
    # I2 (requester-side C1 mirror): a timed-out write() returns False (T1), not a
    # raise. An rc-0 "review requested" that never landed is the requester-side
    # incident. A False write must fail loud (rc 1).
    class WriteTimesOut(FakeTransport):
        def write(self, path, content):
            return False

    t = WriteTimesOut()
    rc = cli.main(["review", "request", "r", "pr-z", "--of", "url",
                   "--reviewer", "alice"], transport=t)
    cap = capsys.readouterr()
    assert rc == 1
    assert "write failed" in cap.err
    assert "requested" not in cap.out, "must not claim a review that never landed"


# --- atomic notification: request also delivers a directive per reviewer -----

def test_request_notifies_each_required_reviewer(capsys):
    # Atomicity: the doc lands AND every required reviewer gets a directive
    # through the canonical task path, so a verb-opened review fires the
    # reviewer's inbox/listen instead of relying on a hand-sent tell.
    t = FakeTransport()
    assert cli.main(["review", "request", "r", "pr-note", "--of", "PR#7",
                     "--reviewer", "alice", "--reviewer", "bob",
                     "--from", "requester"], transport=t) == 0
    assert t.read("team/r/review/pr-note.md") is not None, "the review doc must land"

    # the directive text carries the slug + the EXACT verdict-file path (the
    # fail-closed watcher contract)
    task_docs = [c for p, c in t.store.items() if p.startswith("team/r/task/")]
    for reviewer in ("alice", "bob"):
        hit = [c for c in task_docs
               if f"team/r/review/pr-note/verdicts/{reviewer}.md" in c
               and f"assignee: {reviewer}" in c]
        assert hit, f"{reviewer} must get a directive naming her verdict file"

    # real fold: after reconcile each reviewer's inbox surfaces the directive
    reconcile.reconcile(t, "r", now="2026-07-10T00:00:00Z",
                        today="2026-07-10", host="h")
    for reviewer in ("alice", "bob"):
        capsys.readouterr()
        cli.main(["inbox", "r", "--agent", reviewer, "--json"], transport=t)
        got = json.loads(capsys.readouterr().out)
        assert any("REVIEW REQUEST: pr-note" in (r.get("title") or "")
                   for r in got), (reviewer, got)


def test_doc_write_fail_writes_no_directive(capsys):
    # doc-write fail -> rc 1 and NOTHING else (no reviewer directive attempted).
    class ReviewDocWriteFails(FakeTransport):
        def write(self, path, content):
            if (path.startswith("team/r/review/") and path.endswith(".md")
                    and "/" not in path[len("team/r/review/"):]):
                return False  # the review DOC write times out
            return super().write(path, content)

    t = ReviewDocWriteFails()
    rc = cli.main(["review", "request", "r", "pr-d", "--of", "url",
                   "--reviewer", "alice"], transport=t)
    cap = capsys.readouterr()
    assert rc == 1
    assert "write failed" in cap.err
    assert not [p for p in t.store if p.startswith("team/r/task/")], \
        "a failed doc write must not deliver any reviewer directive"


def test_partial_directive_failure_is_loud_rc1(capsys):
    # doc lands, but one reviewer's directive write fails -> rc 1 naming exactly
    # what landed and what did not (partial is loud, never silent).
    class OneDirectiveWriteFails(FakeTransport):
        def write(self, path, content):
            if path.startswith("team/r/task/") and "verdicts/bob.md" in content:
                return False  # bob's directive write times out
            return super().write(path, content)

    t = OneDirectiveWriteFails()
    rc = cli.main(["review", "request", "r", "pr-p", "--of", "url",
                   "--reviewer", "alice", "--reviewer", "bob"], transport=t)
    cap = capsys.readouterr()
    assert rc == 1
    assert t.read("team/r/review/pr-p.md") is not None, "the doc still landed"
    # names what FAILED (bob) and what was DELIVERED (alice)
    assert "bob" in cap.err and "FAILED" in cap.err
    assert "alice" in cap.err
    # alice's directive really landed
    assert [p for p in t.store
            if p.startswith("team/r/task/") and "alice" in t.store[p]]


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
