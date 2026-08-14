from coord_engine import cli, review
from coord_engine_test_helpers import FakeTransport


def _v(reviewer, verdict):
    return {"reviewer": reviewer, "verdict": verdict}


def test_normalize_verdict():
    assert review.normalize_verdict("approve") == "approve"
    assert review.normalize_verdict("LGTM") == "approve"
    assert review.normalize_verdict("request-changes") == "changes"
    assert review.normalize_verdict("meh") is None


def test_pending_when_no_verdicts():
    assert review.tally([])["state"] == review.PENDING


def test_approved_on_single_approve():
    assert review.tally([_v("a", "approve")])["state"] == review.APPROVED


def test_changes_dominates():
    t = review.tally([_v("a", "approve"), _v("b", "changes")])
    assert t["state"] == review.CHANGES
    assert t["changes"] == ["b"]


def test_last_verdict_per_reviewer_wins():
    # reviewer flips changes -> approve
    t = review.tally([_v("a", "changes"), _v("a", "approve")])
    assert t["state"] == review.APPROVED


def test_required_reviewers_gate_approval():
    t = review.tally([_v("a", "approve")], required=["a", "b"])
    assert t["state"] == review.PENDING
    assert t["pending_required"] == ["b"]
    t2 = review.tally([_v("a", "approve"), _v("b", "approve")], required=["a", "b"])
    assert t2["state"] == review.APPROVED


def test_garbage_verdicts_ignored():
    t = review.tally([{"reviewer": "a"}, {"verdict": "approve"}, "nope", _v("b", "approve")])
    assert t["state"] == review.APPROVED
    assert t["approvals"] == ["b"]


# --- uncounted verdicts must be LOUD, not silent (coord-maintainer + ---
# --- collect-maintainer, 2026-08-08) --------------------------------------

def _req(required="bob", head=None, rnd=None):
    lines = ["---", "type: Review", "schema: review-request/v2",
             "required:", f"  - {required}"]
    if head:
        lines += [f"head: {head}", f"round: {rnd or 1}"]
    return "\n".join(lines + ["---", "the request"])


def test_a_date_prefixed_shard_is_reported_not_silently_skipped(capsys):
    """The live failure: collect-maintainer filed a real, careful verdict as
    `<date>--<reviewer>.md` on a headless review. `--` means "superseded head",
    so it was dropped BEFORE the file was opened, and `review status` then
    reported `pending_required: [collect-maintainer]` — the affirmative claim
    that they had not voted, about a file sitting right there."""
    t = FakeTransport()
    t.put("team/r/review/pr-x.md", _req())
    t.put("team/r/review/pr-x/verdicts/2026-08-08--bob.md",
          "---\nreviewer: bob\nverdict: approve\n---\nlgtm")
    capsys.readouterr()
    rc = cli.main(["review", "status", "r", "pr-x"], transport=t)
    err = capsys.readouterr().err
    assert rc == 3, "a verdict that exists but cannot be counted is not a clean tally"
    assert "2026-08-08--bob.md" in err and "NOT counted" in err
    assert "<head>--<reviewer>.md" in err, "say what a correct name looks like"


def test_a_superseded_head_shard_stays_silent(capsys):
    """The other side of the same rule, and the reason it is not just 'warn on
    every `--`': a keyed review legitimately carries shards for rounds this fold
    is not looking at. Making those noisy would train everyone to ignore the
    warning that matters."""
    head, old = "a" * 40, "b" * 40
    t = FakeTransport()
    t.put("team/r/review/pr-y.md", _req(head=head, rnd=2))
    t.put(f"team/r/review/pr-y/verdicts/{old}--bob.md",
          f"---\nreviewer: bob\nverdict: approve\nhead: {old}\n---\nold round")
    capsys.readouterr()
    rc = cli.main(["review", "status", "r", "pr-y"], transport=t)
    err = capsys.readouterr().err
    assert "NOT counted" not in err, "a superseded round is out of scope, not broken"
    assert rc == 0


def test_an_unrecognised_verdict_token_is_reported_with_the_vocabulary(capsys):
    """`approve-with-required-changes` is an unmistakable vote. Normalising it
    to None and landing in the same PENDING as no verdict at all is the engine
    choosing the least informative reading of a clear intent."""
    t = FakeTransport()
    t.put("team/r/review/pr-z.md", _req())
    t.put("team/r/review/pr-z/verdicts/bob.md",
          "---\nreviewer: bob\nverdict: approve-with-required-changes\n---\nok")
    capsys.readouterr()
    rc = cli.main(["review", "status", "r", "pr-z"], transport=t)
    err = capsys.readouterr().err
    assert rc == 3
    assert "approve-with-required-changes" in err and "bob.md" in err
    assert "lgtm" in err, "name the accepted tokens or the reviewer guesses again"
    assert "read" in err, "distinguish 'unreadable' from 'read but not understood'"


def test_the_reported_vocabulary_comes_from_the_tally_not_a_copy():
    """A hand-copied list in the error message drifts from the one that decides,
    and a stale list is worse than none: it sends the reviewer to re-file with
    another token that also does not count."""
    vocab = review.accepted_vocabulary()
    for token in sorted(review._APPROVE | review._CHANGES):
        assert token in vocab, f"{token} counts but is not offered to the reviewer"


def test_a_head_mismatched_verdict_is_reported_not_silently_skipped(capsys):
    """collect-maintainer argued the third skip and was right about the family.

    Their stated mechanism was wrong — they expected it to MANUFACTURE a vote,
    and it does not: the shard is skipped, so the vote is LOST. But the
    user-visible failure is identical to the two already fixed. A well-formed
    verdict from alice sits in the directory, is read, is discarded, and the
    register reports `pending_required: [alice]` at rc 0 while her file is
    right there.

    The skip itself stays — a verdict must independently attest the exact
    commit it reviewed, or a copied round-1 shard discharges round 2. Only the
    silence changes.
    """
    h2, h1 = "b" * 40, "a" * 40
    t = FakeTransport()
    t.put("team/r/review/pr-q.md",
          "---\ntype: Review\nschema: review-request/v2\nrequired:\n  - alice\n"
          f"head: {h2}\nround: 2\n---\nreq")
    t.put(f"team/r/review/pr-q/verdicts/{h2}--alice.md",
          f"---\nreviewer: alice\nverdict: approve\nhead: {h1}\n---\nlgtm")
    capsys.readouterr()
    rc = cli.main(["review", "status", "r", "pr-q"], transport=t)
    out, err = capsys.readouterr()
    assert "PENDING" in out, "the skip stands — a stale shard must not discharge a round"
    assert rc == 3, "but it is no longer a CLEAN pending"
    assert h1 in err and "not this round's head" in err
    assert "its author believes they voted" in err


def test_a_matching_head_verdict_stays_silent_and_counts(capsys):
    """The control. Without it the test above passes on a build that shouts
    about every verdict, which is the failure mode of every diagnostic that
    stops discriminating."""
    h = "c" * 40
    t = FakeTransport()
    t.put("team/r/review/pr-ok.md",
          "---\ntype: Review\nschema: review-request/v2\nrequired:\n  - alice\n"
          f"head: {h}\nround: 1\n---\nreq")
    t.put(f"team/r/review/pr-ok/verdicts/{h}--alice.md",
          f"---\nreviewer: alice\nverdict: approve\nhead: {h}\n---\nlgtm")
    capsys.readouterr()
    rc = cli.main(["review", "status", "r", "pr-ok"], transport=t)
    out, err = capsys.readouterr()
    assert "APPROVED" in out and rc == 0
    assert "not this round's head" not in err


def test_the_unattributable_line_names_the_rule_that_skipped_it(capsys):
    """coord-boss's addendum: the unrecognized-verdict half already names its
    reason; the filename half should too, or the next debugging session starts
    from 'skipped, but why' again."""
    t = FakeTransport()
    t.put("team/r/review/pr-x2.md", _req())
    t.put("team/r/review/pr-x2/verdicts/2026-08-08--bob.md",
          "---\nreviewer: bob\nverdict: approve\n---\nlgtm")
    capsys.readouterr()
    cli.main(["review", "status", "r", "pr-x2"], transport=t)
    err = capsys.readouterr().err
    assert "RULE:" in err and "40/64-hex" in err


def test_a_keyed_shard_on_a_HEADLESS_review_is_uncounted_and_loud(capsys):
    """codex-reviewer, 576 r2: my predicate asked a question about the FILENAME
    alone, and a filename is only meaningful against the review it sits under.

    On a headless review there are no rounds, so `<valid-head>--bob.md` names a
    round that cannot exist here. The r2 build skipped it in silence — PENDING,
    awaiting bob, rc 0 — with bob's approve verdict sitting in the directory.
    The exact false negative this whole change exists to remove, one branch
    deeper than I looked."""
    h = "a" * 40
    t = FakeTransport()
    t.put("team/r/review/pr-h.md", _req())          # no head: -> headless
    t.put(f"team/r/review/pr-h/verdicts/{h}--bob.md",
          f"---\nreviewer: bob\nverdict: approve\nhead: {h}\n---\nlgtm")
    capsys.readouterr()
    rc = cli.main(["review", "status", "r", "pr-h"], transport=t)
    err = capsys.readouterr().err
    assert rc == 3, "a present-but-uncounted verdict is not a clean tally"
    assert f"{h}--bob.md" in err and "NOT counted" in err


def test_a_superseded_shard_on_a_KEYED_review_stays_silent(capsys):
    """The control that keeps the fix honest. Returning True unconditionally for
    every `--` filename would pass the test above and make every multi-round
    review noisy — which is how a warning stops being read."""
    active, old = "b" * 40, "a" * 40
    t = FakeTransport()
    t.put("team/r/review/pr-k.md", _req(head=active, rnd=2))
    t.put(f"team/r/review/pr-k/verdicts/{old}--bob.md",
          f"---\nreviewer: bob\nverdict: approve\nhead: {old}\n---\nold round")
    t.put(f"team/r/review/pr-k/verdicts/{active}--bob.md",
          f"---\nreviewer: bob\nverdict: approve\nhead: {active}\n---\nthis round")
    capsys.readouterr()
    rc = cli.main(["review", "status", "r", "pr-k"], transport=t)
    out, err = capsys.readouterr()
    assert "APPROVED" in out and rc == 0
    assert "NOT counted" not in err, "a superseded round is out of scope, not broken"
