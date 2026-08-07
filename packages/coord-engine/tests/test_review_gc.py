"""``review gc`` — retiring register entries that can never settle.

The tests that carry this file are the REFUSALS. gc deletes review obligations,
so the expensive failure is not "rot survives another day", it is "a live review
was retired and the work someone was waiting on vanished". Every path that
cannot prove absence must keep the entry:

- an UNKNOWN head probe (no git, no repo, an error, a weird stderr),
- an unreadable review doc,
- a doc with no head at all,
- a settled review, which is a real outcome rather than rot.

The other load-bearing case is coord-boss's counterexample: an entry SUPERSEDED
BY ROUTING has a LIVE head and still can never settle. My first design said
"never touch an entry with a live head", which would have left exactly that
entry taxing every projection pass forever. Non-settleability, not
head-liveness.
"""
from __future__ import annotations

import json

import pytest

from coord_engine import review_gc


def entry(slug="s", head="a" * 40, superseded_by=None, settled=False,
          gc_closed=False):
    return review_gc.Entry(slug=slug, head=head, superseded_by=superseded_by,
                           settled=settled, gc_closed=gc_closed)


ALIVE = lambda sha: True          # noqa: E731
ABSENT = lambda sha: False        # noqa: E731
CANNOT_TELL = lambda sha: None    # noqa: E731


# --- the two retirable classes --------------------------------------------

def test_a_head_that_affirmatively_does_not_exist_is_retirable():
    v = review_gc.classify(entry(), head_exists=ABSENT)
    assert v.state == review_gc.DEAD_HEAD and v.retirable


def test_a_declared_supersession_is_retirable_even_with_a_LIVE_head():
    """coord-boss's counterexample. A re-routed review keeps a live head and can
    never settle, because the register refuses to mutate a required set on an
    existing slug."""
    v = review_gc.classify(
        entry(superseded_by="pr-538-...-independent"), head_exists=ALIVE)
    assert v.state == review_gc.SUPERSEDED and v.retirable
    assert "can never verdict" in v.reason


def test_supersession_is_declared_never_inferred():
    """Nothing in the register distinguishes re-routed from still-waiting, so a
    doc that does not SAY it was superseded is left alone — guessing here
    retires reviews whose reviewer is merely slow."""
    v = review_gc.classify(entry(superseded_by=None), head_exists=ALIVE)
    assert not v.retirable


# --- the refusals ----------------------------------------------------------

def test_an_unresolvable_head_is_UNKNOWN_and_never_retired():
    v = review_gc.classify(entry(), head_exists=CANNOT_TELL)
    assert v.state == review_gc.UNKNOWN and not v.retirable
    assert "keeps it alive" in v.reason


def test_a_live_head_is_never_retired():
    v = review_gc.classify(entry(), head_exists=ALIVE)
    assert v.state == review_gc.LIVE and not v.retirable


def test_a_doc_with_no_head_is_malformed_not_dead():
    """A human wrote these bytes; an engine that 'cleans up' unparseable
    evidence destroys the only record of what went wrong."""
    v = review_gc.classify(entry(head=None), head_exists=ABSENT)
    assert v.state == review_gc.UNKNOWN and not v.retirable
    assert "malformed" in v.reason


def test_a_settled_review_is_left_alone_even_with_a_dead_head():
    """Settled is a real outcome. Overwriting its marker would erase it, and it
    is not what is taxing the projection budget."""
    v = review_gc.classify(entry(settled=True), head_exists=ABSENT)
    assert v.state == review_gc.SETTLED and not v.retirable


def test_an_already_retired_entry_is_not_retired_twice():
    v = review_gc.classify(entry(gc_closed=True), head_exists=ABSENT)
    assert v.state == review_gc.ALREADY_CLOSED and not v.retirable


# --- the marker ------------------------------------------------------------

def test_the_terminal_marker_is_NOT_settled():
    """The register must keep 'reviewed and approved' separable from 'abandoned
    when the subsystem was retired' — that distinction is what the
    unsatisfiable-obligations report needed to make its case."""
    assert review_gc.GC_MARKER == ".gc-closed"
    assert review_gc.GC_MARKER != ".settled"


def test_the_marker_carries_its_own_evidence():
    v = review_gc.classify(entry(), head_exists=ABSENT)
    doc = json.loads(review_gc.marker_body(v, now="2026-08-07T00:00:00Z",
                                           by="coord-opus-worker"))
    assert doc["schema"] == review_gc.GC_SCHEMA
    assert doc["state"] == review_gc.DEAD_HEAD
    assert "does not exist" in doc["reason"]
    assert doc["closed_by"] == "coord-opus-worker"


# --- the plan --------------------------------------------------------------

def test_the_plan_reports_unknowns_explicitly():
    """A pass that quietly skipped what it could not classify would look
    identical to one with nothing to skip."""
    out = review_gc.render_plan(
        review_gc.plan([entry(slug="a")], head_exists=CANNOT_TELL),
        applying=False)
    assert "keep a" in out and "UNKNOWN" in out


def test_the_dry_run_says_it_is_a_dry_run():
    out = review_gc.render_plan(
        review_gc.plan([entry(slug="a")], head_exists=ABSENT), applying=False)
    assert "would retire" in out and "--apply" in out


def test_applying_drops_the_dry_run_language():
    out = review_gc.render_plan(
        review_gc.plan([entry(slug="a")], head_exists=ABSENT), applying=True)
    assert "RETIRE a" in out and "--apply" not in out


def test_a_clean_register_says_nothing_retirable():
    out = review_gc.render_plan(
        review_gc.plan([entry(slug="a")], head_exists=ALIVE), applying=False)
    assert "nothing retirable" in out


def test_summarize_counts_every_state():
    verdicts = review_gc.plan(
        [entry(slug="dead"), entry(slug="settled", settled=True)],
        head_exists=ABSENT)
    counts = review_gc.summarize(verdicts)
    assert counts == {review_gc.DEAD_HEAD: 1, review_gc.SETTLED: 1}


# --- the git probe ---------------------------------------------------------

def test_probe_returns_None_without_a_repository(tmp_path, monkeypatch):
    from coord_engine import cli
    monkeypatch.chdir(tmp_path)
    assert cli._git_head_probe()("a" * 40) is None


@pytest.mark.parametrize("bad", ["", "not-a-sha", "zz", "../etc"])
def test_probe_refuses_non_sha_input(bad):
    from coord_engine import cli
    assert cli._git_head_probe()(bad) is None


def test_probe_finds_a_real_object_in_this_repository():
    """Positive control: without this, every other probe test would pass on a
    probe that always returned None."""
    import subprocess
    from coord_engine import cli, handoff
    if handoff.repo_root() is None:
        pytest.skip("not running inside a git repository")
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                          text=True, cwd=str(handoff.repo_root()))
    if head.returncode != 0:
        pytest.skip("git rev-parse failed")
    assert cli._git_head_probe()(head.stdout.strip()) is True


def test_probe_reports_a_fabricated_sha_as_absent():
    from coord_engine import cli, handoff
    if handoff.repo_root() is None:
        pytest.skip("not running inside a git repository")
    assert cli._git_head_probe()("0" * 40) is False


# --- v1 docs hide the head in prose ---------------------------------------

def test_a_v1_head_is_extracted_when_unambiguous():
    """review-request/v1 carries no `head:` key — it rides in the `of:` prose.
    Those are the OLD entries, which is to say most of the rot."""
    of = ("https://github.com/o/r/pull/461 @ head "
          "756b682dbd58dbb207a8f7f1b4df21f2ffe9ae96 — wake-router addendum 1")
    assert review_gc.head_from_prose(of) == \
        "756b682dbd58dbb207a8f7f1b4df21f2ffe9ae96"


def test_two_different_shas_in_the_prose_yield_nothing():
    """Reading the WRONG sha out of prose would retire a live review — the one
    outcome worse than leaving the rot in place. Ambiguity stays UNKNOWN."""
    assert review_gc.head_from_prose(
        f"head {'a' * 40} superseding head {'b' * 40}") is None


def test_the_same_sha_repeated_is_not_ambiguous():
    assert review_gc.head_from_prose(
        f"head {'a' * 40} … head {'a' * 40}") == "a" * 40


def test_a_bare_sha_without_the_word_head_is_not_a_head():
    """A PR title can contain a hex string; only an explicit 'head <sha>'
    counts."""
    assert review_gc.head_from_prose(f"PR 461 {'a' * 40}") is None


@pytest.mark.parametrize("bad", [None, 42, "", "head deadbeef"])
def test_prose_extraction_survives_junk(bad):
    assert review_gc.head_from_prose(bad) is None


# --- the round-1 blocker: the marker must be CONSUMED, not just written ----

class RegisterTransport:
    """Enough store to exercise both review readers over one register."""

    def __init__(self, slugs):
        # slugs: {slug: {"doc": str, "verdicts": [names]}}
        self.slugs = slugs
        self.written: dict[str, str] = {}

    def list_dir(self, prefix):
        if prefix.endswith("/review/"):
            return [{"name": f"{s}.md"} for s in sorted(self.slugs)]
        for slug, data in self.slugs.items():
            if prefix.endswith(f"/review/{slug}/verdicts/"):
                names = list(data.get("verdicts") or [])
                if f"review/{slug}/verdicts/.gc-closed" in "".join(self.written):
                    pass
                return [{"name": n} for n in names]
        return []

    def read(self, path):
        for slug, data in self.slugs.items():
            if path.endswith(f"/review/{slug}.md"):
                return data["doc"]
        return None

    def write(self, path, content):
        self.written[path] = content
        # Reflect the write, so a later listing sees it — the whole point of
        # this test is that the marker becomes VISIBLE to the readers.
        for slug in self.slugs:
            if path.endswith(f"/review/{slug}/verdicts/.gc-closed"):
                self.slugs[slug].setdefault("verdicts", []).append(".gc-closed")
        return True


DOC = ("---\ntype: Review\nschema: review-request/v2\nrequested_by: boss\n"
       "of: PR 1\nrequired:\n  - codex-reviewer\nhead: {head}\n---\nbody\n")


def test_a_retired_slug_leaves_the_projection_scan():
    """THE round-1 blocker. gc wrote `.gc-closed` and both readers still only
    knew `.settled`, so a 'retired' entry was still scanned, still tallied
    pending, and still consumed the exact budget the verb exists to recover."""
    from coord_engine import projection, review

    from coord_engine.budget import Deadline

    t = RegisterTransport({"dead-slug": {
        "doc": DOC.format(head="a" * 40), "verdicts": [".gc-closed"]}})
    row, complete = projection._scan_review_slug(
        t, "fulcra", "dead-slug", {"name": "dead-slug.md"},
        now="2026-08-07T00:00:00Z", deadline=Deadline.open(60.0))
    assert complete
    assert row["state"] == review.RETIRED
    assert row["pending_required"] == []
    assert row["state"] != review.APPROVED  # never claim it was reviewed


def test_retired_is_a_distinct_state_from_approved():
    from coord_engine import review
    assert review.RETIRED != review.APPROVED


def test_a_retired_slug_leaves_the_pending_review_fold():
    """The other reader named in the verdict. A retired entry can never become
    pending for anybody, so the fold must skip it exactly as it skips settled —
    that skip is what actually recovers the budget."""
    from coord_engine import cli

    t = RegisterTransport({
        "dead-slug": {"doc": DOC.format(head="a" * 40),
                      "verdicts": [".gc-closed"]},
        "live-slug": {"doc": DOC.format(head="b" * 40), "verdicts": []},
    })
    rows = cli._pending_reviews_for(t, "fulcra", "codex-reviewer")
    rows = rows[0] if isinstance(rows, tuple) else rows
    blob = repr(rows)
    # The retired slug must be absent from the fold entirely; the live one must
    # still be there, so the skip is targeted rather than a fold that broke.
    assert "dead-slug" not in blob
    assert "live-slug" in blob


def test_apply_writes_the_marker_where_the_readers_look():
    """End-to-end: --apply must put the marker in the verdicts prefix the two
    readers list, not merely somewhere plausible."""
    import argparse
    from coord_engine import cli

    t = RegisterTransport({"dead-slug": {
        "doc": DOC.format(head="a" * 40), "verdicts": []}})
    args = argparse.Namespace(team="fulcra", apply=True, sender="tester")
    cli.cmd_review_gc(args, t)
    assert any(p.endswith("/review/dead-slug/verdicts/.gc-closed")
               for p in t.written), t.written
