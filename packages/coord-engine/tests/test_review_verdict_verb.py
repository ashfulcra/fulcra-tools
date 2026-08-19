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
from coord_engine_test_helpers import FakeTransport, needs_me_rows

TEAM = "r"
SLUG = "pr-1-thing"
HEAD = "a" * 40
REVIEWER = "codex-reviewer"


def _open_review(t, monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_AGENT", "asker")
    assert cli.main(["review", "request", TEAM, SLUG, "--of", "PR #1",
                     "--reviewer", REVIEWER, "--head", HEAD],
                    transport=t) == 0


PREFIX = f"team/{TEAM}/review/{SLUG}/verdicts/"


def _plain_path():
    """The hand-written form — still first-class, never written by the verb."""
    return PREFIX + review.verdict_filename(REVIEWER, head=HEAD)


def _append_shards(t):
    """Shards the VERB wrote: `<head>--<reviewer>--<iso>-<digest>.md`."""
    return sorted(k for k in t.store
                  if k.startswith(PREFIX + f"{HEAD}--{REVIEWER}--"))


def _verdict_path(t):
    """Whatever shard the verb produced, for tests that assert on its content."""
    shards = _append_shards(t)
    assert shards, f"the verb wrote no append shard; store: {sorted(t.store)}"
    return shards[-1]


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
    # The verb writes the APPEND form, not the literal name `review request`
    # advertises — that changed with coord-boss's revision of constraint (a),
    # because a shared name cannot be written safely on this store. What must
    # still hold is ATTRIBUTION: same directory, same (head, reviewer), so the
    # register reads it without a special case.
    path = _verdict_path(t)
    assert path.startswith(PREFIX + f"{HEAD}--{REVIEWER}--"), path
    assert PREFIX in printed, "the advertised directory must be unchanged"
    assert review.parse_verdict_filename(
        path.split("/")[-1], head=HEAD)[0] == REVIEWER


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
    t.put(_plain_path(), okf.render_frontmatter({
        "type": "Verdict", "reviewer": REVIEWER, "head": HEAD,
        "verdict": "approve"}) + "\nAPPROVED by hand.\n")
    assert cli.main(["review", "status", TEAM, SLUG], transport=t) == 0


def test_a_CONCURRENT_verdict_is_never_overwritten(monkeypatch):
    """codex-reviewer, 595 r2, reproduced: with a shared slot, a CHANGES that
    landed between this command's check and its write was overwritten by APPROVE
    at rc 0.

    Append-only removes the shared slot entirely: the verb's name is unique to
    its own write, so there is nothing to overwrite and no check to lose a race
    against. Both shards exist afterwards and the fold decides.
    """
    t = FakeTransport()
    _open_review(t, monkeypatch)
    # A concurrent CHANGES lands first, in the plain hand-written form.
    t.put(_plain_path(), okf.render_frontmatter({
        "type": "Verdict", "reviewer": REVIEWER, "head": HEAD,
        "verdict": "changes", "ts": "2026-08-10T05:00:00Z"}) + "\nblocked.\n")
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    assert cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD,
                     "--verdict", "approve"], transport=t) == 0
    fm = okf.parse_frontmatter(t.store[_plain_path()])
    assert review.normalize_verdict(fm.get("verdict")) == "changes", (
        "the concurrent CHANGES was destroyed — the exact failure append-only "
        "exists to make impossible")
    assert _append_shards(t), "the verb's own shard is missing"


def test_the_NEWEST_shard_wins_across_both_forms(monkeypatch, capsys):
    """A plain shard and an append shard for the same reviewer: newest counts.
    The plain form is dated by its frontmatter ts, the append form by its
    name."""
    t = FakeTransport()
    _open_review(t, monkeypatch)
    t.put(_plain_path(), okf.render_frontmatter({
        "type": "Verdict", "reviewer": REVIEWER, "head": HEAD,
        "verdict": "changes", "ts": "2020-01-01T00:00:00Z"}) + "\nold.\n")
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD,
              "--verdict", "approve"], transport=t)
    capsys.readouterr()
    assert cli.main(["review", "status", TEAM, SLUG], transport=t) == 0
    out = capsys.readouterr().out
    assert "APPROVED" in out, f"the newer approve did not win:\n{out}"


def test_supersession_is_REPORTED_never_silent(monkeypatch, capsys):
    """coord-boss constraint 4. A reader told APPROVED while shards were quietly
    discarded has the same affirmative falsehood this cycle was about."""
    t = FakeTransport()
    _open_review(t, monkeypatch)
    t.put(_plain_path(), okf.render_frontmatter({
        "type": "Verdict", "reviewer": REVIEWER, "head": HEAD,
        "verdict": "changes", "ts": "2020-01-01T00:00:00Z"}) + "\nold.\n")
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD,
              "--verdict", "approve"], transport=t)
    capsys.readouterr()
    cli.main(["review", "status", TEAM, SLUG, "--json"], transport=t)
    out = capsys.readouterr().out
    assert "superseded_verdicts" in out, (
        f"folded-away shards were not reported:\n{out}")


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
    assert not _append_shards(t), "an unparseable verdict was written anyway"


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
    orphan = PREFIX + f"{REVIEWER}.md"
    assert orphan not in t.store, (
        "wrote a headless shard the tally will ignore, while reporting success")
    if rc == 0:
        assert _append_shards(t), (
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
    assert not [k for k in t.store if f"{'b' * 40}--{REVIEWER}" in k]


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



# --- coord-boss constraint 5: EVERY register reader learns the fold ---------

def test_the_PROJECTION_folds_too_or_a_stale_CHANGES_blocks_forever():
    """The reader I nearly left behind.

    `projection.py` built ONE ENTRY PER FILE and never folded. With append-only
    shards a reviewer can hold several, so a superseded CHANGES and its newer
    APPROVE both reached `review.tally` — where a single blocker dominates. The
    stale CHANGES would have blocked that review permanently, in a fold nobody
    would think to look at.
    """
    rows = [
        {"reviewer": REVIEWER, "verdict": "changes", "name": "old.md",
         "sort_key": "2020-01-01T00:00:00Z"},
        {"reviewer": REVIEWER, "verdict": "approve", "name": "new.md",
         "sort_key": "2026-08-10T05:00:00Z"},
    ]
    kept, folded = review.fold_newest_per_reviewer(rows)
    assert [r["verdict"] for r in kept] == ["approve"]
    assert folded == 1
    assert review.tally(
        [{"reviewer": r["reviewer"], "verdict": r["verdict"]} for r in kept],
        required=[REVIEWER])["state"] == review.APPROVED


def test_the_fold_is_DETERMINISTIC_when_two_shards_share_an_instant():
    """Two hosts folding the same directory must agree. Same ts, so the name
    breaks the tie — and it breaks it the same way everywhere."""
    a = {"reviewer": "r", "verdict": "approve", "name": "aaa.md",
         "sort_key": "2026-08-10T05:00:00Z"}
    b = {"reviewer": "r", "verdict": "changes", "name": "bbb.md",
         "sort_key": "2026-08-10T05:00:00Z"}
    assert (review.fold_newest_per_reviewer([a, b])[0][0]["name"]
            == review.fold_newest_per_reviewer([b, a])[0][0]["name"])


# --- codex 595 r3: the READ side must see a correction ----------------------

def test_a_correction_after_APPROVAL_invalidates_the_settle_CACHE(monkeypatch, capsys):
    """codex-reviewer, 595 r3, blocker one — reproduced exactly.

    APPROVE -> `review status` writes `.settled` -> a later CHANGES correction
    lands with rc 0 and both shards on disk — and the projection, which treats
    any marker hit as immutable approval without reading shards, still says
    APPROVED. The same-head correction contract I had just shipped was false the
    moment a prior result settled.
    """
    t = FakeTransport()
    _open_review(t, monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD,
              "--verdict", "approve"], transport=t)
    cli.main(["review", "status", TEAM, SLUG], transport=t)      # writes .settled
    marker = f"team/{TEAM}/review/{SLUG}/verdicts/.settled"
    assert marker in t.store, "precondition: the cache must exist"

    capsys.readouterr()
    rc = cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD,
                   "--verdict", "changes", "--note", "found a blocker"],
                  transport=t)
    assert rc == 0, capsys.readouterr().err
    assert marker not in t.store, (
        "the stale APPROVED cache survived a CHANGES correction — every reader "
        "that short-circuits on the marker still reports APPROVED")


def test_a_MERGED_marker_is_evidence_and_survives_a_late_verdict(monkeypatch, capsys):
    """The other half of the 572/588 distinction: `.settled` with state MERGED
    is proof a PR landed, not a cache. A late verdict must not destroy it."""
    t = FakeTransport()
    _open_review(t, monkeypatch)
    marker = f"team/{TEAM}/review/{SLUG}/verdicts/.settled"
    t.put(marker, okf.render_frontmatter({
        "schema": "review-settled/v1", "state": "MERGED",
        "merge_sha": "c" * 40}) + "\nmerged.\n")
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD,
              "--verdict", "changes"], transport=t)
    assert marker in t.store, "merge EVIDENCE was deleted by a late verdict"
    assert "EVIDENCE" in capsys.readouterr().err


def test_the_PROJECTION_orders_a_ts_less_plain_shard_by_MTIME(monkeypatch):
    """codex-reviewer, 595 r3, blocker two — driven through the projection.

    The direct tally dated a plain shard by frontmatter ts then LISTING MTIME;
    the projection stopped at frontmatter. So a plain shard with no `ts` sorted
    as EMPTY there and lost to an older append shard: one reader said CHANGES
    and the other APPROVED for the same directory.

    My first version of this test only compared the two `_store_mtime_iso`
    helpers — which both still existed when I deleted the projection's USE of
    one, so it passed against the bug. Drive the reader, not its parts.
    """
    from coord_engine import projection as pj
    from coord_engine.budget import Deadline

    head = HEAD
    doc = okf.render_frontmatter({
        "type": "Review", "schema": "review-request/v2", "of": "PR #1",
        "head": head, "required": [REVIEWER], "requested_by": "asker"}) + "\n"
    older_append = (f"{head}--{REVIEWER}--2026-08-10T01:00:00Z-aaaaaaaa.md")
    plain = f"{head}--{REVIEWER}.md"

    class _T:
        store = {
            f"team/{TEAM}/review/{SLUG}.md": doc,
            f"{PREFIX}{older_append}": okf.render_frontmatter({
                "type": "Verdict", "reviewer": REVIEWER, "head": head,
                "verdict": "approve", "ts": "2026-08-10T01:00:00Z"}) + "\nok\n",
            # NO frontmatter ts — only the listing mtime dates it, and it is NEWER.
            f"{PREFIX}{plain}": okf.render_frontmatter({
                "type": "Verdict", "reviewer": REVIEWER, "head": head,
                "verdict": "changes"}) + "\nblocker\n",
        }

        def read(self, path):
            return self.store.get(path)

        def list_dir(self, prefix):
            rows = []
            for k in self.store:
                if k.startswith(prefix) and "/" not in k[len(prefix):]:
                    mt = ("2026-08-10 02:00AM UTC" if k.endswith(plain)
                          else "2026-08-10 01:00AM UTC")
                    rows.append({"name": k[len(prefix):], "mtime": mt})
            return rows

    row, ok = pj._scan_review_slug(
        _T(), TEAM, SLUG, {"name": f"{SLUG}.md"},
        now="2026-08-10T05:00:00Z", deadline=Deadline(None))
    assert ok and row is not None, "the projection refused to scan"
    assert row["state"] == review.CHANGES, (
        f"the projection ordered a ts-less plain shard as empty and lost the "
        f"newer CHANGES to an older APPROVE: {row}")


def test_a_STALE_cache_written_AFTER_a_correction_is_ignored(monkeypatch, capsys):
    """codex-reviewer, 595 r4 — the interleaving, as a regression.

    1. `review status` reads the old APPROVE tally and pauses before writing.
    2. `review verdict changes` writes the correction, sees no marker, rc 0.
    3. The paused status resumes and writes `.settled` from its STALE snapshot.
    4. Readers short-circuit on the marker and answer APPROVED.

    Deleting the cache cannot prevent step 3 — the loser is whoever writes last,
    and correctness must not depend on that. The cache is bound to the evidence
    it summarised, so the resurrected marker carries a digest for a directory
    that no longer exists and every reader ignores it.
    """
    from coord_engine import projection as pj
    from coord_engine.budget import Deadline

    head = HEAD
    doc = okf.render_frontmatter({
        "type": "Review", "schema": "review-request/v2", "of": "PR #1",
        "head": head, "required": [REVIEWER], "requested_by": "asker"}) + "\n"
    approve = f"{head}--{REVIEWER}--2026-08-10T01:00:00Z-aaaaaaaa.md"
    changes = f"{head}--{REVIEWER}--2026-08-10T02:00:00Z-bbbbbbbb.md"

    class _T:
        def __init__(self):
            self.store = {
                f"team/{TEAM}/review/{SLUG}.md": doc,
                f"{PREFIX}{approve}": okf.render_frontmatter({
                    "type": "Verdict", "reviewer": REVIEWER, "head": head,
                    "verdict": "approve"}) + "\nok\n",
            }

        def read(self, path):
            return self.store.get(path)

        def list_dir(self, prefix):
            return [{"name": k[len(prefix):]} for k in sorted(self.store)
                    if k.startswith(prefix) and "/" not in k[len(prefix):]]

    t = _T()
    # (1) the paused status fingerprints ONLY the approve shard
    stale_digest = review.evidence_digest([approve])
    # (2) the correction lands
    t.store[f"{PREFIX}{changes}"] = okf.render_frontmatter({
        "type": "Verdict", "reviewer": REVIEWER, "head": head,
        "verdict": "changes"}) + "\nblocker\n"
    # (3) the stale status resumes and resurrects the cache
    t.store[f"{PREFIX}.settled"] = okf.render_frontmatter({
        "schema": "review-settled/v1", "state": review.APPROVED,
        "ts": "2026-08-10T01:30:00Z", "evidence": stale_digest})

    row, ok = pj._scan_review_slug(
        t, TEAM, SLUG, {"name": f"{SLUG}.md"},
        now="2026-08-10T05:00:00Z", deadline=Deadline(None))
    assert ok and row is not None
    assert row["state"] == review.CHANGES, (
        f"a cache resurrected from a stale snapshot was honoured, so the newest "
        f"CHANGES was invisible: {row}")


def test_a_cache_that_still_matches_its_evidence_is_HONOURED():
    """The other direction: binding must not make the cache useless. When the
    directory is unchanged the fast path still fires."""
    from coord_engine import projection as pj
    from coord_engine.budget import Deadline

    head = HEAD
    approve = f"{head}--{REVIEWER}--2026-08-10T01:00:00Z-aaaaaaaa.md"

    class _T:
        store = {
            f"team/{TEAM}/review/{SLUG}.md": okf.render_frontmatter({
                "type": "Review", "schema": "review-request/v2", "of": "PR #1",
                "head": head, "required": [REVIEWER],
                "requested_by": "asker"}) + "\n",
            f"{PREFIX}{approve}": okf.render_frontmatter({
                "type": "Verdict", "reviewer": REVIEWER, "head": head,
                "verdict": "approve"}) + "\nok\n",
        }

        def read(self, path):
            return self.store.get(path)

        def list_dir(self, prefix):
            return [{"name": k[len(prefix):]} for k in sorted(self.store)
                    if k.startswith(prefix) and "/" not in k[len(prefix):]]

    t = _T()
    t.store[f"{PREFIX}.settled"] = okf.render_frontmatter({
        "schema": "review-settled/v1", "state": review.APPROVED,
        "ts": "2026-08-10T01:30:00Z",
        "evidence": review.evidence_digest([approve, ".settled"])})
    row, ok = pj._scan_review_slug(
        t, TEAM, SLUG, {"name": f"{SLUG}.md"},
        now="2026-08-10T05:00:00Z", deadline=Deadline(None))
    assert ok and row and row["settled"] is True, (
        f"a valid cache was ignored, so the fast path is dead: {row}")


# --- 595 r5: a NAME digest cannot fingerprint a MUTABLE shard ----------------

def _plain_shard_store(verdict):
    """A directory holding ONLY the canonical plain shard — the hand-written
    form constraint (b) keeps first-class — plus a cache bound to its NAME."""
    head = HEAD
    plain = f"{head}--{REVIEWER}.md"

    class _T:
        def __init__(self):
            self.store = {
                f"team/{TEAM}/review/{SLUG}.md": okf.render_frontmatter({
                    "type": "Review", "schema": "review-request/v2",
                    "of": "PR #1", "head": head, "required": [REVIEWER],
                    "requested_by": "asker"}) + "\n",
                f"{PREFIX}{plain}": okf.render_frontmatter({
                    "type": "Verdict", "reviewer": REVIEWER, "head": head,
                    "verdict": verdict}) + "\nbody\n",
                f"{PREFIX}.settled": okf.render_frontmatter({
                    "schema": "review-settled/v1", "state": review.APPROVED,
                    "ts": "2026-08-10T01:30:00Z",
                    "evidence": review.evidence_digest([plain])}),
            }

        def read(self, path):
            return self.store.get(path)

        def list_dir(self, prefix):
            return [{"name": k[len(prefix):]} for k in sorted(self.store)
                    if k.startswith(prefix) and "/" not in k[len(prefix):]]

    return _T(), plain


def test_a_REWRITTEN_plain_shard_is_not_hidden_by_its_unchanged_name(monkeypatch):
    """codex-reviewer, 595 r5, P1 — the exact-head reproduction.

    The plain `<head>--<reviewer>.md` form is permanently supported and
    hand-writable, so it can be rewritten IN PLACE. Its content changes from
    APPROVE to CHANGES; its NAME does not. A digest over names therefore still
    matches, and r4's binding accepted the cache and answered APPROVED from a
    shard that now reads CHANGES.

    No stronger identity exists on this store — no etag, no version, no content
    hash, and listing mtimes are minute-resolution. So the cache does not bind
    to mutable evidence at all: this directory is folded for real, every time.
    """
    from coord_engine import projection as pj
    from coord_engine.budget import Deadline

    t, plain = _plain_shard_store("approve")
    # The same canonical file is rewritten. Nothing about the LISTING changes.
    t.store[f"{PREFIX}{plain}"] = okf.render_frontmatter({
        "type": "Verdict", "reviewer": REVIEWER, "head": HEAD,
        "verdict": "changes"}) + "\nblocker\n"

    row, ok = pj._scan_review_slug(
        t, TEAM, SLUG, {"name": f"{SLUG}.md"},
        now="2026-08-10T05:00:00Z", deadline=Deadline(None))
    assert ok and row is not None
    assert row["state"] == review.CHANGES, (
        f"a name digest hid an in-place rewrite of the plain shard, so a "
        f"CHANGES verdict read as APPROVED: {row}")


def test_the_fanout_scan_applies_THE_SAME_rule_as_the_projection(capsys):
    """The sibling reader. `needs-me` skipped a slug on `.settled` PRESENCE
    alone — the unvalidated short-circuit r4 fixed in the projection and left
    standing one reader away. A stale cache there hides an OBLIGATION: the
    reviewer is told they owe nothing while the newest verdict is CHANGES.

    Driven through `cli.main`, because a rule proven only in the shared helper
    is a rule I have twice failed to actually connect.
    """
    import json as _j
    t = FakeTransport()
    head = HEAD
    t.put(f"team/{TEAM}/review/{SLUG}.md", okf.render_frontmatter({
        "type": "Review", "schema": "review-request/v2", "of": "PR #1",
        "head": head, "required": [REVIEWER, "second-reviewer"],
        "requested_by": "asker"}) + "\n")
    t.put(f"{PREFIX}{head}--{REVIEWER}--2026-08-10T01:00:00Z-aaaaaaaa.md",
          okf.render_frontmatter({
              "type": "Verdict", "reviewer": REVIEWER, "head": head,
              "verdict": "approve"}) + "\nok\n")
    # A PRE-BINDING cache — exactly what every build before r4, and the
    # projection's own writer, put on disk. `second-reviewer` has filed nothing.
    t.put(f"{PREFIX}.settled", okf.render_frontmatter({
        "schema": "review-settled/v1", "state": review.APPROVED,
        "ts": "2026-08-10T01:30:00Z"}))

    cli.main(["needs-me", TEAM, "--agent", "second-reviewer", "--json"],
             transport=t)
    rows = needs_me_rows(_j.loads(capsys.readouterr().out))
    assert any(r.get("name") == SLUG for r in rows
               if r.get("type") == "review-pending"), (
        f"the fan-out scan honoured an unvalidated cache and hid an owed "
        f"verdict: {rows}")


def test_a_MERGED_marker_still_short_circuits_a_mutable_directory():
    """Merge evidence is not a recomputable tally — no verdict set can
    contradict "the PR landed" — so the immutability rule must not touch it."""
    from coord_engine import projection as pj
    from coord_engine.budget import Deadline

    t, _ = _plain_shard_store("changes")
    t.store[f"{PREFIX}.settled"] = okf.render_frontmatter({
        "schema": "review-settled/v1", "state": "MERGED",
        "ts": "2026-08-10T01:30:00Z", "merge_sha": "b" * 40})
    row, ok = pj._scan_review_slug(
        t, TEAM, SLUG, {"name": f"{SLUG}.md"},
        now="2026-08-10T05:00:00Z", deadline=Deadline(None))
    assert ok and row and row["settled"] is True, (
        f"merge evidence was demoted to a cache and re-folded: {row}")


def test_the_projection_writes_a_cache_ITS_OWN_reader_then_honours():
    """codex-reviewer, 595 r5, P2 — write-then-read, no hand-seeded marker.

    The projection composed its own marker dict and omitted `evidence`, so the
    cache it wrote failed its own validation on the very next pass: a write that
    could never pay off, repeated every reconcile. Seeding a valid cache by hand
    cannot catch that — only running the writer and then the reader can.
    """
    from coord_engine import projection as pj
    from coord_engine.budget import Deadline

    head = HEAD
    approve = f"{head}--{REVIEWER}--2026-08-10T01:00:00Z-aaaaaaaa.md"

    class _T:
        def __init__(self):
            self.store = {
                f"team/{TEAM}/review/{SLUG}.md": okf.render_frontmatter({
                    "type": "Review", "schema": "review-request/v2",
                    "of": "PR #1", "head": head, "required": [REVIEWER],
                    "requested_by": "asker"}) + "\n",
                f"{PREFIX}{approve}": okf.render_frontmatter({
                    "type": "Verdict", "reviewer": REVIEWER, "head": head,
                    "verdict": "approve"}) + "\nok\n",
            }
            self.reads: list[str] = []

        def read(self, path):
            self.reads.append(path)
            return self.store.get(path)

        def list_dir(self, prefix):
            return [{"name": k[len(prefix):]} for k in sorted(self.store)
                    if k.startswith(prefix) and "/" not in k[len(prefix):]]

        def write(self, path, text):
            self.store[path] = text
            return True

    t = _T()
    args = (TEAM, SLUG, {"name": f"{SLUG}.md"})
    row, ok = pj._scan_review_slug(t, *args, now="2026-08-10T05:00:00Z",
                                   deadline=Deadline(None))
    assert ok and row and row["settled"] is True
    assert f"{PREFIX}.settled" in t.store, "the projection wrote no cache"

    t.reads.clear()
    row2, ok2 = pj._scan_review_slug(t, *args, now="2026-08-10T06:00:00Z",
                                     deadline=Deadline(None))
    assert ok2 and row2 and row2["settled"] is True
    assert f"{PREFIX}{approve}" not in t.reads, (
        "the projection's own cache did not hit its own fast path — the "
        f"verdict shard was re-read: {t.reads}")


def test_a_MUTABLE_directory_gets_NO_cache_written():
    """A marker every reader must refuse is cost with no reader. Both writers
    decline it rather than rewriting it on every pass."""
    from coord_engine import projection as pj
    from coord_engine.budget import Deadline

    head = HEAD
    plain = f"{head}--{REVIEWER}.md"

    class _T:
        def __init__(self):
            self.store = {
                f"team/{TEAM}/review/{SLUG}.md": okf.render_frontmatter({
                    "type": "Review", "schema": "review-request/v2",
                    "of": "PR #1", "head": head, "required": [REVIEWER],
                    "requested_by": "asker"}) + "\n",
                f"{PREFIX}{plain}": okf.render_frontmatter({
                    "type": "Verdict", "reviewer": REVIEWER, "head": head,
                    "verdict": "approve"}) + "\nok\n",
            }

        def read(self, path):
            return self.store.get(path)

        def list_dir(self, prefix):
            return [{"name": k[len(prefix):]} for k in sorted(self.store)
                    if k.startswith(prefix) and "/" not in k[len(prefix):]]

        def write(self, path, text):
            self.store[path] = text
            return True

    t = _T()
    row, ok = pj._scan_review_slug(t, TEAM, SLUG, {"name": f"{SLUG}.md"},
                                   now="2026-08-10T05:00:00Z",
                                   deadline=Deadline(None))
    assert ok and row and row["state"] == review.APPROVED
    assert f"{PREFIX}.settled" not in t.store, (
        "a cache was written over mutable evidence that no reader may honour")
    assert cli._write_settled_marker(t, TEAM, SLUG, now="2026-08-10T05:00:00Z",
                                     evidence="") == "unbound-evidence"
    assert f"{PREFIX}.settled" not in t.store
