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

from coord_engine_test_helpers import FakeTransport
from coord_engine import cli, review_gc


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


def test_probe_reports_a_fabricated_sha_as_absent(monkeypatch):
    """Absent -> False, but ONLY where this checkout could prove absence.

    This assertion used to be unconditional, and CI is what proved it wrong:
    `actions/checkout@v4` clones with `fetch-depth: 1` by default, so the CI
    runner's own working copy is a shallow clone. The correct answer there is
    None, and the old test encoded the assumption the shallow-clone P0 is about
    — that a repository can always tell you an object does not exist.

    Worth stating plainly rather than skipping past: the data-loss path was live
    in CI. Anything running `review gc --apply` from a stock GitHub Actions
    checkout would have retired reviews whose heads are alive.

    The full-clone branch stays covered in CI by
    `test_an_ordinary_local_repo_still_proves_absence`, which builds its own
    complete repository instead of depending on how the runner checked us out.
    """
    import subprocess

    from coord_engine import cli, handoff

    root = handoff.repo_root()
    if root is None:
        pytest.skip("not running inside a git repository")
    shallow = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"], cwd=str(root),
        capture_output=True, text=True).stdout.strip()
    # The SOURCE boundary is mocked. Round 1 of PR 551 left these tests calling
    # the live remote and `gh`, so they passed for me and failed for the
    # reviewer — the exact environment-dependence coord-opus-worker named and I
    # had endorsed one wake earlier. What is under test is the LOCAL branch.
    monkeypatch.setattr(cli, "_remote_head_exists", lambda sha: False)
    expected = False if shallow == "false" else None
    assert cli._git_head_probe()("0" * 40) is expected


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
    # OMITTED, and the scan counts COMPLETE: the pass did resolve this slug, it
    # simply has nothing a consumer wants. Emitting a new state instead is what
    # broke round 2.
    assert row is None
    assert complete is True


def test_no_new_state_leaks_into_the_validated_schema():
    """Round 2's fix added `review.RETIRED`; the validated consumer rejected it.
    Omission needs no new state at all, so the schema stays exactly as the
    consumer already accepts it."""
    from coord_engine import review
    assert not hasattr(review, "RETIRED")


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


def test_apply_writes_the_marker_where_the_readers_look(monkeypatch):
    """End-to-end: --apply must put the marker in the verdicts prefix the two
    readers list, not merely somewhere plausible.

    The probe is PINNED here. Unpinned, this test asked the ambient repository
    whether "aaaa…" exists, so its result depended on how the host had cloned
    us: dead in a full clone, UNKNOWN in a shallow one — where gc correctly
    retires nothing and the assertion fails. CI hid that behind `--maxfail=1`
    (it aborted on an earlier failure in this file, so this test never ran).
    What is under test is the marker's PATH, not the host's clone shape.
    """
    import argparse
    from coord_engine import cli

    monkeypatch.setattr(cli, "_git_head_probe", lambda: (lambda sha: False))
    t = RegisterTransport({"dead-slug": {
        "doc": DOC.format(head="a" * 40), "verdicts": []}})
    args = argparse.Namespace(team="fulcra", apply=True, sender="tester")
    cli.cmd_review_gc(args, t)
    assert any(p.endswith("/review/dead-slug/verdicts/.gc-closed")
               for p in t.written), t.written


def test_a_projection_containing_a_retired_entry_SURVIVES_validation():
    """THE round-2 blocker, tested end to end rather than producer-only.

    Round 2 emitted retired rows as `state: RETIRED, settled: true`.
    `_validated_review_projection` accepts only PENDING/APPROVED/CHANGES and
    rejects any settled row that is not APPROVED, so the FIRST retired entry
    invalidated the entire section and every consumer fell back to the raw
    scan — defeating the durable path the verb exists to restore. Testing the
    producer alone did not catch it; this consumes the projection."""
    from coord_engine import cli, projection
    from coord_engine.budget import Deadline

    t = RegisterTransport({
        "dead-slug": {"doc": DOC.format(head="a" * 40),
                      "verdicts": [".gc-closed"]},
        "live-slug": {"doc": DOC.format(head="b" * 40), "verdicts": []},
    })
    section = projection.build_review_projection(
        t, "fulcra", now="2026-08-07T00:00:00Z", prior=None,
        settled_index=set(), deadline=Deadline.open(60.0))
    names = {str(r.get("name")) for r in (section.get("rows") or [])}
    assert not any("dead-slug" in n for n in names), names
    assert any("live-slug" in n for n in names), names

    validated = cli._validated_review_projection(section)
    assert validated is not None, ("the retired entry invalidated the whole "
                                   "section — exactly the round-2 defect")


# --- shallow / partial clones cannot prove absence -------------------------
#
# P0 (coord-opus-worker, 2026-08-07): `git cat-file -e` answers "is this object
# HERE", not "does this object EXIST". In a clone that legitimately lacks
# history those diverge, and the gc treats False as authoritative grounds to
# retire a review. Reproduced against a real `--depth 1` clone: probing a
# merged, current-main commit exits 128 with "fatal: Not a valid object name",
# which the classifier read as FALSE — a LIVE head reported affirmatively dead.


def _shallow_clone(tmp_path):
    """A real depth-1 clone of this repository, or skip."""
    import subprocess
    from coord_engine import handoff

    root = handoff.repo_root()
    if root is None:
        pytest.skip("not running inside a git repository")
    dst = tmp_path / "shallow"
    cp = subprocess.run(
        ["git", "clone", "--depth", "1", "file://" + str(root), str(dst)],
        capture_output=True, timeout=180)
    if cp.returncode != 0:
        pytest.skip("could not create a shallow clone here")
    return dst


def test_a_shallow_clone_never_reports_absence(tmp_path, monkeypatch):
    """THE regression. A sha this clone does not hold must read UNKNOWN, not
    dead — otherwise gc destroys reviews whose heads are alive."""
    import subprocess
    from coord_engine import cli, handoff

    dst = _shallow_clone(tmp_path)
    monkeypatch.setattr(handoff, "repo_root", lambda: dst)

    assert subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"], cwd=str(dst),
        capture_output=True, text=True).stdout.strip() == "true", "not shallow"

    probe = cli._git_head_probe()
    # A well-formed sha that is certainly not in a depth-1 clone.
    assert probe("0" * 40) is None, (
        "a shallow clone claimed authoritative absence — this is the data-loss "
        "path: gc would retire a review whose head is alive"
    )


def test_a_shallow_clone_still_confirms_what_it_does_hold(tmp_path, monkeypatch):
    """The guard must not blind the probe: presence is still provable, so gc
    keeps working on the objects a shallow clone actually has."""
    import subprocess
    from coord_engine import cli, handoff

    dst = _shallow_clone(tmp_path)
    monkeypatch.setattr(handoff, "repo_root", lambda: dst)

    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(dst),
                          capture_output=True, text=True)
    if head.returncode != 0:
        pytest.skip("git rev-parse failed in the clone")
    assert cli._git_head_probe()(head.stdout.strip()) is True


def test_a_full_clone_still_proves_absence(monkeypatch):
    """The over-correction guard: with the SOURCE confirming absence, gc must
    still be able to say 'gone', or it can never retire anything and the
    register rots.

    The source is mocked. Round 1 left this calling the live remote and `gh`,
    so it passed on my host and failed on the reviewer's — environment
    dependence in the very file whose subject is environment dependence.
    """
    from coord_engine import cli, handoff

    if handoff.repo_root() is None:
        pytest.skip("not running inside a git repository")
    import subprocess
    shallow = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=str(handoff.repo_root()), capture_output=True, text=True)
    if shallow.stdout.strip() != "false":
        pytest.skip("this checkout is itself shallow")
    monkeypatch.setattr(cli, "_remote_head_exists", lambda sha: False)
    assert cli._git_head_probe()("0" * 40) is False

    # ...and the same local state with the source UNABLE to answer is UNKNOWN.
    monkeypatch.setattr(cli, "_remote_head_exists", lambda sha: None)
    assert cli._git_head_probe()("0" * 40) is None


def _partial_repo(tmp_path, remote_name):
    """A NON-shallow repo configured as a partial clone via ``remote_name``."""
    import subprocess

    d = tmp_path / f"partial-{remote_name}"
    d.mkdir()
    def g(*a):
        return subprocess.run(["git", *a], cwd=str(d), capture_output=True)
    if g("init", "-q").returncode != 0:
        pytest.skip("git init failed")
    g("config", "user.email", "t@example.invalid")
    g("config", "user.name", "t")
    (d / "f").write_text("x")
    g("add", "f")
    if g("commit", "-qm", "c").returncode != 0:
        pytest.skip("git commit failed")
    g("config", f"remote.{remote_name}.promisor", "true")
    g("config", f"remote.{remote_name}.partialclonefilter", "blob:none")
    return d


@pytest.mark.parametrize("remote_name", ["origin", "upstream", "fork"])
def test_a_partial_clone_never_reports_absence_whatever_the_remote_is_called(
        tmp_path, monkeypatch, remote_name):
    """codex round 1: git does not require the promisor remote to be named
    `origin`. Checking `remote.origin.*` specifically left the destructive path
    open for any other name — the same mistake as the bug itself, one level up:
    testing ONE INSTANCE of a thing instead of the thing."""
    from coord_engine import cli, handoff

    d = _partial_repo(tmp_path, remote_name)
    monkeypatch.setattr(handoff, "repo_root", lambda: d)
    assert cli._git_head_probe()("0" * 40) is None, (
        f"a partial clone with promisor '{remote_name}' claimed authoritative "
        f"absence"
    )


def test_extensions_partialclone_alone_is_enough_to_refuse(tmp_path, monkeypatch):
    """The canonical marker git itself writes. Present without any
    remote.*.promisor entry, it still means objects arrive lazily."""
    import subprocess
    from coord_engine import cli, handoff

    d = tmp_path / "ext-partial"
    d.mkdir()
    def g(*a):
        return subprocess.run(["git", *a], cwd=str(d), capture_output=True)
    if g("init", "-q").returncode != 0:
        pytest.skip("git init failed")
    g("config", "user.email", "t@example.invalid")
    g("config", "user.name", "t")
    (d / "f").write_text("x")
    g("add", "f")
    if g("commit", "-qm", "c").returncode != 0:
        pytest.skip("git commit failed")
    g("config", "extensions.partialClone", "somewhere")

    monkeypatch.setattr(handoff, "repo_root", lambda: d)
    assert cli._git_head_probe()("0" * 40) is None


def test_an_ordinary_local_repo_defers_to_the_source(tmp_path, monkeypatch):
    """A plain full repo with no partial-clone config passes the SHALLOW guard
    and still cannot answer alone — absence is the source's to confirm."""
    import subprocess
    from coord_engine import cli, handoff

    d = tmp_path / "plain"
    d.mkdir()
    def g(*a):
        return subprocess.run(["git", *a], cwd=str(d), capture_output=True)
    if g("init", "-q").returncode != 0:
        pytest.skip("git init failed")
    g("config", "user.email", "t@example.invalid")
    g("config", "user.name", "t")
    (d / "f").write_text("x")
    g("add", "f")
    if g("commit", "-qm", "c").returncode != 0:
        pytest.skip("git commit failed")

    monkeypatch.setattr(handoff, "repo_root", lambda: d)
    # CONTRACT CHANGED, and this is the third pre-existing test in this file to
    # have encoded the defect: "a complete local repo can prove absence" is the
    # belief that reported six live heads as dead. A repo with no reachable
    # source cannot prove anything about absence, however complete it is.
    monkeypatch.setattr(cli, "_remote_ref_tips", lambda: None)
    monkeypatch.setattr(cli, "_fetch_probe_head_exists", lambda sha: None)
    monkeypatch.setattr(cli, "_forge_head_exists", lambda sha: None)
    assert cli._git_head_probe()("0" * 40) is None

    # ...and absence IS provable once the SOURCE answers. This is the
    # over-correction guard in its new, correct form: without it gc can never
    # retire anything and the register rots, which is the problem gc exists for.
    monkeypatch.setattr(cli, "_fetch_probe_head_exists", lambda sha: None)
    monkeypatch.setattr(cli, "_forge_head_exists", lambda sha: False)
    assert cli._git_head_probe()("0" * 40) is False


# --- blindness must be loud, and --apply must refuse it --------------------
#
# The guard made gc SAFE in a clone that cannot prove absence: every head reads
# UNKNOWN, nothing is retirable, nothing is written. It also made it SILENT --
# "retired 0" reads exactly like a clean register. That is this P0's own shape
# one layer up: absence of a finding standing in for a finding of absence.


def _blind_probe():
    p = lambda sha: None          # noqa: E731 - a stand-in probe
    p.absence_is_trustworthy = False
    return p


def _seeing_probe():
    p = lambda sha: False         # noqa: E731
    p.absence_is_trustworthy = True
    return p


def test_a_blind_clone_says_so_on_a_dry_run(monkeypatch, capsys):
    import argparse
    from coord_engine import cli

    monkeypatch.setattr(cli, "_git_head_probe", _blind_probe)
    t = RegisterTransport({"dead-slug": {
        "doc": DOC.format(head="a" * 40), "verdicts": []}})
    rc = cli.cmd_review_gc(
        argparse.Namespace(team="fulcra", apply=False, sender="tester"), t)
    err = capsys.readouterr().err
    assert rc == 0, "a dry run from a blind clone is still allowed"
    assert "CANNOT PROVE ABSENCE" in err
    assert "BLIND, not clean" in err


def test_apply_refuses_from_a_blind_clone_and_writes_nothing(monkeypatch, capsys):
    """The technical control. A hold that is only announced is a social
    control; this makes the destructive verb refuse on its own."""
    import argparse
    from coord_engine import cli

    monkeypatch.setattr(cli, "_git_head_probe", _blind_probe)
    t = RegisterTransport({"dead-slug": {
        "doc": DOC.format(head="a" * 40), "verdicts": []}})
    rc = cli.cmd_review_gc(
        argparse.Namespace(team="fulcra", apply=True, sender="tester"), t)
    assert rc == 2, "refusal must be a distinct non-zero rc, not a quiet 0"
    assert "refusing --apply" in capsys.readouterr().err
    assert not t.written, t.written


def test_a_seeing_clone_is_unaffected(monkeypatch, capsys):
    """Over-correction guard: the warning and the refusal must not fire where
    absence IS provable, or gc can never retire anything again."""
    import argparse
    from coord_engine import cli

    monkeypatch.setattr(cli, "_git_head_probe", _seeing_probe)
    t = RegisterTransport({"dead-slug": {
        "doc": DOC.format(head="a" * 40), "verdicts": []}})
    rc = cli.cmd_review_gc(
        argparse.Namespace(team="fulcra", apply=True, sender="tester"), t)
    err = capsys.readouterr().err
    assert "CANNOT PROVE ABSENCE" not in err
    assert "refusing --apply" not in err
    assert rc == 0
    assert any(p.endswith("/review/dead-slug/verdicts/.gc-closed")
               for p in t.written), t.written


def test_the_real_probe_publishes_its_capability():
    """The flag must actually be set by the real constructor, not only by the
    doubles above — otherwise these tests pass against a probe that never
    exposes it and cmd_review_gc silently defaults to 'trustworthy'."""
    from coord_engine import cli

    probe = cli._git_head_probe()
    assert hasattr(probe, "absence_is_trustworthy")
    assert isinstance(probe.absence_is_trustworthy, bool)


# --- local absence is not absence: prove it at the SOURCE ------------------
#
# Measured on the live register: 6 of 6 entries the classifier called dead were
# ALIVE at the source, from a FULL non-shallow non-partial clone. A standard
# clone fetches branches, not refs/pull/*, and a review register is full of PR
# heads. Completeness of the clone was never the right test.


def test_an_advertised_ref_tip_proves_presence(monkeypatch):
    from coord_engine import cli

    monkeypatch.setattr(cli, "_remote_ref_tips", lambda: {"a" * 40})
    monkeypatch.setattr(cli, "_forge_head_exists",
                        lambda sha: pytest.fail("must not need the API for a tip"))
    assert cli._remote_head_exists("a" * 40) is True


def test_a_ls_remote_MISS_is_not_absence(monkeypatch):
    """THE regression for this round. ls-remote sees ref TIPS only; 2 of the 6
    live heads were reachable-but-not-tips. A miss must fall through, never
    resolve to False on its own."""
    from coord_engine import cli

    monkeypatch.setattr(cli, "_remote_ref_tips", lambda: {"b" * 40})
    monkeypatch.setattr(cli, "_fetch_probe_head_exists", lambda sha: None)
    monkeypatch.setattr(cli, "_forge_head_exists", lambda sha: True)
    assert cli._remote_head_exists("a" * 40) is True, (
        "a non-tip commit that the forge confirms is ALIVE was reported absent"
    )


def test_only_the_forge_may_answer_absent(monkeypatch):
    from coord_engine import cli

    monkeypatch.setattr(cli, "_remote_ref_tips", lambda: {"b" * 40})
    monkeypatch.setattr(cli, "_fetch_probe_head_exists", lambda sha: None)
    monkeypatch.setattr(cli, "_forge_head_exists", lambda sha: False)
    assert cli._remote_head_exists("a" * 40) is False


def test_no_remote_answer_at_all_is_UNKNOWN(monkeypatch):
    """No ls-remote, no forge -> None. gc collapsing to can-never-retire is
    honest and useless, which beats confident and wrong."""
    from coord_engine import cli

    monkeypatch.setattr(cli, "_remote_ref_tips", lambda: None)
    monkeypatch.setattr(cli, "_fetch_probe_head_exists", lambda sha: None)
    monkeypatch.setattr(cli, "_forge_head_exists", lambda sha: None)
    assert cli._remote_head_exists("a" * 40) is None


def test_the_probe_never_returns_False_from_local_absence_alone(monkeypatch):
    """End to end through _git_head_probe: a sha absent locally, with the
    source unreachable, must be UNKNOWN. This is the exact path that reported
    six live heads as dead."""
    from coord_engine import cli, handoff

    if handoff.repo_root() is None:
        pytest.skip("not running inside a git repository")
    monkeypatch.setattr(cli, "_remote_ref_tips", lambda: None)
    monkeypatch.setattr(cli, "_fetch_probe_head_exists", lambda sha: None)
    monkeypatch.setattr(cli, "_forge_head_exists", lambda sha: None)
    assert cli._git_head_probe()("0" * 40) is None


# --- the forge boundary: only ONE response is absence ----------------------
#
# codex round 1 on PR 551: a 404 can mean the repository is inaccessible, not
# that the commit is gone, and the origin parser accepted any host and then
# asked github.com about a same-named repo. Inability to SEE becoming proof of
# ABSENCE — this P0 one layer down. Measured against the live API:
#   missing commit    -> 422 "No commit found for SHA: <sha>"
#   inaccessible repo -> 404 "Not Found"


def _fake_gh(monkeypatch, tmp_path, *, origin, rc, stdout="", stderr=""):
    """Pin `gh`, the repo root and both subprocess calls the forge path makes."""
    import subprocess
    from coord_engine import cli, handoff

    monkeypatch.setattr(handoff, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli.shutil if hasattr(cli, "shutil") else __import__("shutil"),
                        "which", lambda n: "/usr/bin/gh")

    def fake_run(cmd, **kw):
        if "remote" in cmd and "get-url" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=origin + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return cli


GH = "https://github.com/ashfulcra/fulcra-tools"


def test_forge_absent_only_on_the_no_commit_message(monkeypatch, tmp_path):
    cli = _fake_gh(monkeypatch, tmp_path, origin=GH, rc=1,
                   stderr="gh: No commit found for SHA: aaa (HTTP 422)")
    assert cli._forge_head_exists("a" * 40) is False


def test_an_inaccessible_repository_404_is_UNKNOWN(monkeypatch, tmp_path):
    """THE regression. A 404 means we could not see the repo, not that the
    commit is gone — and this False would have written .gc-closed."""
    cli = _fake_gh(monkeypatch, tmp_path, origin=GH, rc=1,
                   stderr="gh: Not Found (HTTP 404)")
    assert cli._forge_head_exists("a" * 40) is None


def test_an_unrelated_422_is_UNKNOWN(monkeypatch, tmp_path):
    cli = _fake_gh(monkeypatch, tmp_path, origin=GH, rc=1,
                   stderr="gh: Validation Failed (HTTP 422)")
    assert cli._forge_head_exists("a" * 40) is None


@pytest.mark.parametrize("origin", [
    "https://gitlab.com/ashfulcra/fulcra-tools",
    "git@git.internal.example:ashfulcra/fulcra-tools.git",
    "https://github.example.com/ashfulcra/fulcra-tools",
])
def test_a_non_github_origin_is_never_answered_by_github(monkeypatch, tmp_path, origin):
    """A wrong authority is worse than no answer: asking github.com about a
    same-named repo could return either a false True or a 404."""
    cli = _fake_gh(monkeypatch, tmp_path, origin=origin, rc=1,
                   stderr="gh: No commit found for SHA: aaa (HTTP 422)")
    assert cli._forge_head_exists("a" * 40) is None


def test_a_github_hit_is_presence(monkeypatch, tmp_path):
    cli = _fake_gh(monkeypatch, tmp_path, origin=GH + ".git", rc=0,
                   stdout="a" * 40 + "\n")
    assert cli._forge_head_exists("a" * 40) is True


# --- the fetch probe: forge-agnostic, and abbreviation is a trap -----------
#
# Measured against GitHub:
#   full sha alive       -> rc 0
#   full sha fabricated  -> "remote error: upload-pack: not our ref <sha>"
#   ABBREVIATED sha      -> "couldn't find remote ref" for BOTH
# I ran the abbreviated form, got the failure, and reported it as proof that
# fetch cannot probe GitHub. It was proof that abbreviations are not fetchable.


def _fetch_env(monkeypatch, tmp_path, *, rc, stderr=""):
    import subprocess
    from coord_engine import cli, handoff
    import shutil

    monkeypatch.setattr(handoff, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/" + n)
    def fake_run(cmd, **kw):
        # the probe resolves origin first, then inits a throwaway repo; only the
        # FETCH carries the outcome under test.
        if "get-url" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="https://x/y\n", stderr="")
        if "fetch" not in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return cli


def test_fetch_probe_rc0_is_presence(monkeypatch, tmp_path):
    cli = _fetch_env(monkeypatch, tmp_path, rc=0)
    assert cli._fetch_probe_head_exists("a" * 40) is True


def test_fetch_probe_NEVER_answers_absence(monkeypatch, tmp_path):
    """coord-boss, round 3. "not our ref" is absence only if origin is the
    CANONICAL repo. In a fork checkout origin is the fork and an upstream
    refs/pull head returns exactly that string while alive — and because this
    layer runs FIRST, a False here short-circuits the origin-verified forge
    path that would have answered correctly. Presence is the contribution."""
    cli = _fetch_env(monkeypatch, tmp_path, rc=1,
                     stderr="fatal: remote error: upload-pack: not our ref aaa")
    assert cli._fetch_probe_head_exists("a" * 40) is None


def test_fetch_probe_couldnt_find_remote_ref_is_UNKNOWN(monkeypatch, tmp_path):
    """THE trap. This is what an ABBREVIATED sha returns whether the object is
    alive or dead, so it can never mean absence."""
    cli = _fetch_env(monkeypatch, tmp_path, rc=128,
                     stderr="fatal: couldn't find remote ref 48e248ce9298")
    assert cli._fetch_probe_head_exists("a" * 40) is None


def test_fetch_probe_refuses_an_abbreviated_sha_outright(monkeypatch, tmp_path):
    """Never even ask: an abbreviated sha is not a valid fetch argument, so the
    answer would be uninterpretable rather than merely unknown."""
    cli = _fetch_env(monkeypatch, tmp_path, rc=0)
    assert cli._fetch_probe_head_exists("48e248ce9298") is None


def test_the_fetch_probe_is_preferred_for_PRESENCE(monkeypatch):
    """Forge-agnostic presence first: a GitLab or self-hosted origin gets a
    real True rather than the None the GitHub-only path must return."""
    from coord_engine import cli

    monkeypatch.setattr(cli, "_remote_ref_tips", lambda: set())
    monkeypatch.setattr(cli, "_fetch_probe_head_exists", lambda sha: True)
    monkeypatch.setattr(
        cli, "_forge_head_exists",
        lambda sha: pytest.fail("must not need GitHub when fetch proved presence"))
    assert cli._remote_head_exists("a" * 40) is True


def test_the_github_path_is_still_the_fallback(monkeypatch):
    from coord_engine import cli

    monkeypatch.setattr(cli, "_remote_ref_tips", lambda: set())
    monkeypatch.setattr(cli, "_fetch_probe_head_exists", lambda sha: None)
    monkeypatch.setattr(cli, "_fetch_probe_head_exists", lambda sha: None)
    monkeypatch.setattr(cli, "_forge_head_exists", lambda sha: True)
    assert cli._remote_head_exists("a" * 40) is True


def test_the_fetch_probe_does_not_touch_the_working_repository(monkeypatch, tmp_path):
    """`--dry-run` DOES write to the object store — verified: fetching a
    reachable non-tip into a --depth 1 clone left the object present and took
    .git from 4.0M to 12M. Probing in the working repo would mutate it and make
    the classifier HISTORY-DEPENDENT: run 2 answering True from run 1's
    download. So the fetch must happen somewhere disposable."""
    import subprocess
    from coord_engine import cli, handoff
    import shutil

    seen = []
    monkeypatch.setattr(handoff, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/" + n)

    def fake_run(cmd, **kw):
        seen.append((list(cmd), kw.get("cwd")))
        if "get-url" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="https://x/y\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli._fetch_probe_head_exists("a" * 40) is True

    fetches = [(c, cwd) for c, cwd in seen if "fetch" in c]
    assert fetches, seen
    for cmd, cwd in fetches:
        assert str(cwd) != str(tmp_path), (
            "the fetch ran in the WORKING repo — it would download packs into it "
            "and the next run would answer from what this one fetched"
        )
        assert "--depth=1" in cmd, "the transfer must be bounded"


# --- restore must not be hardcoded to one team's reviewer -------------------

def _archive_one_shard(t, team, month, slug, filename):
    """A doc-less cold archive: verdict shards, no request doc."""
    from coord_engine import reconcile as _rec
    t.put(f"{_rec.review_archive_prefix(team)}{month}/{slug}/verdicts/{filename}",
          "---\ntype: Verdict\nverdict: approve\n---\nlgtm")


def test_restore_works_for_any_reviewer_not_just_one_hardcoded_name(capsys):
    """The gate read `files != ["codex-reviewer.md"]`, so `review restore`
    worked for exactly one agent on exactly one team and told everyone else
    "unexpected archived verdict shape". Team-particular content in a repo that
    has to generalize — whose shard it is was never the engine's business."""
    t = FakeTransport()
    _archive_one_shard(t, "r", "2026-07", "pr-a", "some-other-reviewer.md")
    capsys.readouterr()
    rc = cli.main(["review", "restore", "r", "pr-a"], transport=t)
    out, err = capsys.readouterr()
    assert rc == 0, f"any single shard must restore:\n{out}\n{err}"
    assert "restored review pr-a" in out
    assert t.read("team/r/review/pr-a/verdicts/some-other-reviewer.md") is not None


def test_restore_says_the_result_is_an_orphan(capsys):
    """A doc-less restore recreates a review dir with verdicts and no doc — the
    exact shape that surfaces as `needs maintainer repair`. Saying so at the
    moment it happens is cheaper than the maintainer rediscovering it later."""
    t = FakeTransport()
    _archive_one_shard(t, "r", "2026-07", "pr-b", "alice.md")
    capsys.readouterr()
    assert cli.main(["review", "restore", "r", "pr-b"], transport=t) == 0
    assert "orphan" in capsys.readouterr().err


def test_restore_refuses_multiple_doc_less_shards_and_says_why(capsys):
    """The COUNT bound is deliberate and stays: restoring N shards with no doc
    makes a bigger claim than this verb can justify. The old message
    ("unexpected archived verdict shape") did not say that; this one does."""
    t = FakeTransport()
    _archive_one_shard(t, "r", "2026-07", "pr-c", "alice.md")
    _archive_one_shard(t, "r", "2026-07", "pr-c", "bob.md")
    capsys.readouterr()
    rc = cli.main(["review", "restore", "r", "pr-c"], transport=t)
    err = capsys.readouterr().err
    assert rc == 1
    assert "2 archived verdict shard(s)" in err and "no request doc" in err
