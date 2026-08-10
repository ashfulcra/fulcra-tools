"""`review verdict` — filing a verdict becomes an engine write.

This closes the class the whole presence cycle kept circling. A reviewer was
invisible to activity because their core act had NO VERB: `review request`
printed a path and told them to write the shard themselves, so filing a verdict
touched no chokepoint, refreshed no presence, and produced no work event. 590
fixed verb coverage, 591 added the work axis, 593 made the sweep affordable,
594 added events — and none of them could see a reviewer, because a reviewer
never entered the process.

coord-boss's two compatibility constraints (ruling f40069c0), both pinned here:

  (a) THE VERB IS SUGAR OVER THE SAME ARTIFACT. It writes exactly the canonical
      `<head>--<reviewer>.md` shard at the path `review request` already prints,
      so tally / settle / retention see no new shape. Nothing downstream learns
      that a verb exists.
  (b) DIRECT SHARD-WRITING REMAINS VALID. codex writes shards directly today and
      must not break the day this ships. The verb is additive; its ADOPTION is
      what upgrades a reviewer from pointer-less to a work event of kind
      `review`.
"""

from __future__ import annotations

from coord_engine import cli, okf, review
from coord_engine.transport import TransportError
from coord_engine_test_helpers import FakeTransport

TEAM = "r"
SLUG = "pr-1-thing"
HEAD = "a" * 40
REVIEWER = "codex-reviewer"


def _open_review(t, monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_AGENT", "asker")
    assert cli.main(["review", "request", TEAM, SLUG, "--of", "PR #1",
                     "--reviewer", REVIEWER, "--head", HEAD],
                    transport=t) == 0


def _verdict_path(t):
    return f"team/{TEAM}/review/{SLUG}/verdicts/" + review.verdict_filename(
        REVIEWER, head=HEAD)


# --- constraint (a): exactly the canonical artifact --------------------------

def test_the_verb_writes_EXACTLY_the_path_review_request_printed(monkeypatch, capsys):
    """The filename is the attribution — `<head>--<reviewer>.md`. If the verb
    invented its own path, the register would not see the verdict at all and the
    review would sit PENDING behind a shard nobody reads."""
    t = FakeTransport()
    _open_review(t, monkeypatch)
    printed = capsys.readouterr().out
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    assert cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD,
                     "--verdict", "approve", "--note", "looks right"],
                    transport=t) == 0
    path = _verdict_path(t)
    assert path in t.store, f"verdict not at the canonical path; store: {sorted(t.store)}"
    assert path.split("/")[-1] in printed, (
        "the verb must write the filename `review request` advertised")


def test_the_tally_reads_the_verb_written_shard_with_no_special_case(monkeypatch):
    """Constraint (a) proved where it matters: `review status` must reach
    APPROVED from a verb-written shard exactly as from a hand-written one."""
    t = FakeTransport()
    _open_review(t, monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD,
              "--verdict", "approve"], transport=t)
    assert cli.main(["review", "status", TEAM, SLUG], transport=t) == 0


def test_a_verb_written_shard_is_byte_compatible_with_a_hand_written_one(monkeypatch):
    """The frontmatter the register keys on — reviewer, head, verdict — must
    match what a reviewer writing by hand produces."""
    t = FakeTransport()
    _open_review(t, monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD,
              "--verdict", "changes", "--note", "one blocker"], transport=t)
    fm = okf.parse_frontmatter(t.store[_verdict_path(t)])
    assert fm.get("reviewer") == REVIEWER
    assert fm.get("head") == HEAD
    # TWO VOCABULARIES, and they are easy to confuse: `normalize_verdict`
    # returns the SHARD value ("approve"/"changes"), while `review.CHANGES` is a
    # TALLY STATE ("APPROVED"/"CHANGES"/"PENDING"). A shard carries the former.
    assert review.normalize_verdict(fm.get("verdict")) == "changes"


# --- constraint (b): the direct path keeps working ---------------------------

def test_a_HAND_WRITTEN_shard_still_tallies_after_the_verb_exists(monkeypatch):
    """codex writes shards directly today. The verb is additive: adding it must
    not make the direct path a second-class citizen, or every reviewer breaks
    the day it ships."""
    t = FakeTransport()
    _open_review(t, monkeypatch)
    t.put(_verdict_path(t), okf.render_frontmatter({
        "type": "Verdict", "reviewer": REVIEWER, "head": HEAD,
        "verdict": "approve"}) + "\nAPPROVED by hand.\n")
    assert cli.main(["review", "status", TEAM, SLUG], transport=t) == 0


def test_the_verb_REFUSES_to_overwrite_an_existing_verdict(monkeypatch, capsys):
    """A verdict is evidence, not a draft. Silently replacing one would let a
    second run erase a CHANGES that a merge decision already depended on."""
    t = FakeTransport()
    _open_review(t, monkeypatch)
    t.put(_verdict_path(t), okf.render_frontmatter({
        "type": "Verdict", "reviewer": REVIEWER, "head": HEAD,
        "verdict": "changes"}) + "\nblocked.\n")
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    rc = cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD,
                   "--verdict", "approve"], transport=t)
    assert rc != 0
    fm = okf.parse_frontmatter(t.store[_verdict_path(t)])
    # TWO VOCABULARIES, and they are easy to confuse: `normalize_verdict`
    # returns the SHARD value ("approve"/"changes"), while `review.CHANGES` is a
    # TALLY STATE ("APPROVED"/"CHANGES"/"PENDING"). A shard carries the former.
    assert review.normalize_verdict(fm.get("verdict")) == "changes", (
        "an existing verdict was overwritten")


# --- the point of the whole exercise ----------------------------------------

def test_filing_a_verdict_NOW_records_a_work_event(monkeypatch):
    """The reason this verb exists.

    A reviewer was invisible to every liveness signal because filing a verdict
    was not a verb. Now it is one, so it flows through the 590 chokepoint and
    leaves a 594 work event — no new plumbing, just the verb existing.
    """
    t = FakeTransport()
    _open_review(t, monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD,
              "--verdict", "approve"], transport=t)
    events = [p for p in t.store
              if p.startswith(f"team/{TEAM}/_coord/agents/{REVIEWER}/work/")]
    assert events, (
        "filing a verdict left no work event — the reviewer is still invisible, "
        f"which is the entire thing this verb exists to fix. paths: {sorted(t.store)}")


def test_an_UNKNOWN_verdict_value_is_refused(monkeypatch):
    """The vocabulary is APPROVED/CHANGES. A shard carrying anything else would
    read as unparseable to the tally and stall the review silently."""
    t = FakeTransport()
    _open_review(t, monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    assert cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD,
                     "--verdict", "maybe"], transport=t) != 0
    assert _verdict_path(t) not in t.store


# --- codex 595 r1: the verb must satisfy the ACTIVE round --------------------

def test_omitting_head_on_a_KEYED_review_does_not_silently_orphan_the_verdict(monkeypatch):
    """codex-reviewer, 595 r1, blocker one.

    `--head` was optional and the register was never read, so omitting it wrote
    `<reviewer>.md`, printed success, returned 0 and emitted reviewer activity —
    while the tally ignored that headless shard and the reviewer stayed in
    `pending_required`. A confident false success: the reviewer believes they
    voted and the round still waits on them.
    """
    t = FakeTransport()
    _open_review(t, monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    rc = cli.main(["review", "verdict", TEAM, SLUG, "--verdict", "approve"],
                  transport=t)
    # Either resolve the active head, or refuse — never write an orphan.
    orphan = f"team/{TEAM}/review/{SLUG}/verdicts/{REVIEWER}.md"
    assert orphan not in t.store, (
        "wrote a headless shard the tally will ignore, while reporting success")
    if rc == 0:
        assert _verdict_path(t) in t.store, (
            "claimed success without discharging the active round")


def test_a_head_that_is_NOT_the_registers_current_head_is_refused(monkeypatch):
    """A stale or unrelated head records a shard that cannot discharge the
    current round — the same false success, just spelled differently."""
    t = FakeTransport()
    _open_review(t, monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    rc = cli.main(["review", "verdict", TEAM, SLUG, "--head", "b" * 40,
                   "--verdict", "approve"], transport=t)
    assert rc != 0, "a non-current head was accepted"
    assert f"team/{TEAM}/review/{SLUG}/verdicts/{'b' * 40}--{REVIEWER}.md" \
        not in t.store


def test_an_UNREADABLE_register_fails_closed(monkeypatch):
    """UNKNOWN is not permission. If the register cannot be read, the verb
    cannot know which round it is voting in, so it must not guess."""
    class _NoDoc(FakeTransport):
        def read(self, path):
            if path.endswith(f"/review/{SLUG}.md"):
                return None          # ambiguous: missing OR transient failure
            return super().read(path)

    t = _NoDoc()
    _open_review(t, monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    rc = cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD,
                   "--verdict", "approve"], transport=t)
    assert rc != 0, "voted into a round it could not verify"


# --- codex 595 r1: the refusal must not rest on an ambiguous read ------------

def test_an_AMBIGUOUS_read_of_an_existing_verdict_fails_closed(monkeypatch):
    """codex-reviewer, 595 r1, blocker two — and my own memory used against me.

    The check was `existing = transport.read(path); if existing:`, and this
    transport returns None for BOTH absence and a transient failure. So an
    existing CHANGES shard that happened to be unreadable read as absent, and
    APPROVE was written over evidence. `transport-read-none-is-ambiguous` is a
    rule I wrote down after this exact class bit me twice; I then coded the
    thing it warns against.

    Confirmed absence comes from the RAISING listing, and anything else is
    UNKNOWN, which must refuse.
    """
    class _ShardUnreadable(FakeTransport):
        def read(self, path):
            if path.endswith(f"--{REVIEWER}.md"):
                return None          # present but unreadable
            return super().read(path)

    t = _ShardUnreadable()
    _open_review(t, monkeypatch)
    t.put(_verdict_path(t), okf.render_frontmatter({
        "type": "Verdict", "reviewer": REVIEWER, "head": HEAD,
        "verdict": "changes"}) + "\nblocked.\n")
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    rc = cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD,
                   "--verdict", "approve"], transport=t)
    assert rc != 0, "overwrote evidence after an ambiguous read"
    fm = okf.parse_frontmatter(t.store[_verdict_path(t)])
    assert review.normalize_verdict(fm.get("verdict")) == "changes", (
        "the existing CHANGES was replaced by APPROVE")


def test_an_UNREADABLE_verdicts_LISTING_also_fails_closed(monkeypatch):
    """The listing is the authority for presence, so when IT is unknown the
    verb has no basis to claim the slot is free."""
    class _NoListing(FakeTransport):
        def list_dir(self, prefix):
            if prefix.endswith("/verdicts/"):
                raise TransportError("listing down")
            return super().list_dir(prefix)

    t = _NoListing()
    _open_review(t, monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    assert cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD,
                     "--verdict", "approve"], transport=t) != 0
