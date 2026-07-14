"""Three-surface feedback sweep + watch registry + needs-me wiring (fulcra-agent-forge).

The motivating failure was real: a formal GitHub review went unseen because a
watcher polled conversation comments only. This suite exercises the sweep across
all three surfaces (reviews / inline / conversation comments) through the runner
seam so fixtures inject the gh JSON, plus the watch registry and needs-me/ack
wiring that surface unacked feedback to the responsible coord agent.
"""

import json

from coord_engine import cli, forge, okf
from coord_engine.cli import build_parser
from coord_engine_test_helpers import FakeTransport

NOW = "2026-07-08T12:00:00Z"

# clock-pin support (see #378):
import pytest
from datetime import datetime, timezone
PINNED_NOW = datetime(2026, 7, 8, 12, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_module_clock(monkeypatch):
    """Pin cli._now to PINNED_NOW (just after the module NOW).

    Fixtures stamp data relative to NOW, but folds/verbs compute windows and
    staleness off cli._now() against the REAL clock — so once wall-clock time
    crossed NOW + a window this suite flipped RED for good (the repo's
    date-boundary CI-flake class; template: #378 test_threads). Remedy: pin the
    clock, never weaken assertions. Tests that MOVE time monkeypatch cli._now
    themselves, overriding this."""
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)

URL = "https://github.com/o/r/pull/42"
URL2 = "https://github.com/o/r/pull/99"


# --- fixtures -------------------------------------------------------------

def _fixtures(pr_author="prauthor", reviews=None, inline=None, comments=None):
    """The three gh payloads, in their real (differing) JSON shapes."""
    return {
        "reviews": json.dumps({"author": {"login": pr_author}, "reviews": reviews or []}),
        "inline": json.dumps(inline or []),
        "comments": json.dumps({"comments": comments or []}),
    }


def _runner_for(fx):
    """Fake runner dispatching on the args of each of the three calls."""
    def run(args):
        if "api" in args:
            return fx["inline"]
        if "--json" in args:
            j = args[args.index("--json") + 1]
            if "reviews" in j:
                return fx["reviews"]
            if j == "comments":
                return fx["comments"]
        return None
    return run


def _watch(t, agent="bob", url=URL, team="r"):
    slug = forge.pr_slug(url)
    t.put(f"team/{team}/_coord/forge/watch/{slug}.md",
          okf.render_frontmatter({"type": "Watch", "url": url, "agent": agent, "ts": NOW}))


def _review_doc(t, slug="pr-42", artifact=URL, requested_by="alice", team="r", key="of"):
    """A review doc. Default ``key="of"`` mimics what the real ``review request``
    verb (``cmd_review_request``) actually writes — the PR url under ``of``, no
    ``artifact`` key. ``key="artifact"`` exercises the legacy hand-written shape
    that forge discovery must still honor."""
    t.put(f"team/{team}/review/{slug}.md",
          f"---\ntype: Review\nschema: review-request/v1\n"
          f"requested_by: {requested_by}\n{key}: {artifact}\n---\n")


_REVIEW = {"id": "PRR_1", "author": {"login": "rev"}, "state": "CHANGES_REQUESTED",
           "body": "Fix the failing test", "submittedAt": "2026-07-08T10:00:00Z"}
_INLINE = {"node_id": "PRRC_1", "user": {"login": "rev"}, "body": "nit: rename",
           "created_at": "2026-07-08T10:05:00Z", "path": "a.py", "line": 3}
_COMMENT = {"id": "IC_1", "author": {"login": "bob2"}, "body": "any update?",
            "createdAt": "2026-07-08T10:10:00Z"}


# --- (a) watch registers + idempotent -------------------------------------

def test_watch_registers_updates_idempotently_and_unwatch_removes():
    t = FakeTransport()
    p = build_parser()
    args = p.parse_args(["forge", "watch", "r", URL, "--agent", "bob"])
    assert args.func(args, t) == 0
    wp = "team/r/_coord/forge/watch/o-r-42.md"
    assert wp in t.store
    fm = okf.parse_frontmatter(t.store[wp])
    assert fm["agent"] == "bob" and fm["url"] == URL
    # duplicate watch = idempotent update, not error
    args2 = p.parse_args(["forge", "watch", "r", URL, "--agent", "carol"])
    assert args2.func(args2, t) == 0
    assert okf.parse_frontmatter(t.store[wp])["agent"] == "carol"
    # unwatch removes
    argsu = p.parse_args(["forge", "unwatch", "r", URL])
    assert argsu.func(argsu, t) == 0
    assert wp not in t.store
    # unwatch of an absent watch is a clean no-op, not an error
    assert argsu.func(p.parse_args(["forge", "unwatch", "r", URL]), t) == 0


# --- (b) sweep writes shards from all three surfaces ----------------------

def test_sweep_writes_shards_from_all_three_surfaces():
    t = FakeTransport()
    _watch(t, agent="bob")
    fx = _fixtures(reviews=[_REVIEW], inline=[_INLINE], comments=[_COMMENT])
    res = forge.feedback_sweep(t, "r", runner=_runner_for(fx))
    assert res["prs"] == 1 and res["items"] == 3 and res["skipped"] == []
    base = "team/r/_coord/forge/feedback/o-r-42/"
    assert base + "review-PRR_1.md" in t.store
    assert base + "inline-PRRC_1.md" in t.store
    assert base + "comment-IC_1.md" in t.store
    fm = okf.parse_frontmatter(t.store[base + "review-PRR_1.md"])
    assert fm["surface"] == "review" and fm["author"] == "rev" and fm["pr"] == URL
    assert fm["excerpt"].startswith("Fix the failing test")
    inl = okf.parse_frontmatter(t.store[base + "inline-PRRC_1.md"])
    assert inl["surface"] == "inline" and inl["author"] == "rev"
    com = okf.parse_frontmatter(t.store[base + "comment-IC_1.md"])
    assert com["surface"] == "comment" and com["author"] == "bob2"


def test_sweep_discovers_review_artifact_prs_without_a_watch():
    t = FakeTransport()
    _review_doc(t)  # PR is a review artifact, not watched
    fx = _fixtures(reviews=[_REVIEW])
    res = forge.feedback_sweep(t, "r", runner=_runner_for(fx))
    assert res["prs"] == 1 and res["items"] == 1
    assert "team/r/_coord/forge/feedback/o-r-42/review-PRR_1.md" in t.store


# --- (c) re-run converges (identical listing) -----------------------------

def test_rerun_converges_to_identical_store_listing():
    t = FakeTransport()
    _watch(t, agent="bob")
    fx = _fixtures(reviews=[_REVIEW], inline=[_INLINE], comments=[_COMMENT])
    forge.feedback_sweep(t, "r", runner=_runner_for(fx))
    before = sorted(t.store)
    snapshot = dict(t.store)
    forge.feedback_sweep(t, "r", runner=_runner_for(fx))
    assert sorted(t.store) == before
    assert t.store == snapshot  # byte-identical, not just same keys


# --- (d) self-authored items skipped --------------------------------------

def test_self_authored_items_are_skipped_case_insensitively():
    t = FakeTransport()
    _watch(t, agent="bob")
    fx = _fixtures(
        pr_author="prauthor",
        reviews=[{"id": "PRR_self", "author": {"login": "prauthor"},
                  "state": "COMMENTED", "body": "my own note", "submittedAt": NOW}],
        comments=[{"id": "IC_self", "author": {"login": "PrAuthor"},
                   "body": "self", "createdAt": NOW},
                  {"id": "IC_other", "author": {"login": "bob2"},
                   "body": "real feedback", "createdAt": NOW}],
    )
    res = forge.feedback_sweep(t, "r", runner=_runner_for(fx))
    assert res["items"] == 1
    base = "team/r/_coord/forge/feedback/o-r-42/"
    assert base + "comment-IC_other.md" in t.store
    assert base + "comment-IC_self.md" not in t.store
    assert base + "review-PRR_self.md" not in t.store


# --- (e) needs-me shows unacked count, ack clears, new re-surfaces ---------

def test_needs_me_surfaces_unacked_ack_clears_and_new_item_resurfaces(capsys):
    t = FakeTransport()
    _watch(t, agent="bob")
    fx = _fixtures(reviews=[_REVIEW], comments=[_COMMENT])  # 2 items
    forge.feedback_sweep(t, "r", runner=_runner_for(fx))

    assert cli.main(["needs-me", "r", "--agent", "bob", "--json"], transport=t) == 0
    got = json.loads(capsys.readouterr().out)
    fb = [r for r in got if r.get("type") == "forge-feedback"]
    assert len(fb) == 1 and fb[0]["count"] == 2 and fb[0]["pr_slug"] == "o-r-42"
    assert set(fb[0]["authors"]) == {"rev", "bob2"}

    # ack one item via the EXISTING inbox --ack verb against its item id
    item = "review-PRR_1"
    assert item in fb[0]["items"]
    assert cli.main(["inbox", "r", "--ack", item, "--agent", "bob"], transport=t) == 0
    capsys.readouterr()

    assert cli.main(["needs-me", "r", "--agent", "bob", "--json"], transport=t) == 0
    got2 = json.loads(capsys.readouterr().out)
    fb2 = [r for r in got2 if r.get("type") == "forge-feedback"]
    assert len(fb2) == 1 and fb2[0]["count"] == 1  # acked item dropped

    # a NEW node id after the ack re-surfaces; the acked one stays hidden
    fx2 = _fixtures(reviews=[_REVIEW],
                    comments=[_COMMENT, {"id": "IC_2", "author": {"login": "carol"},
                                         "body": "new note", "createdAt": NOW}])
    forge.feedback_sweep(t, "r", runner=_runner_for(fx2))
    assert cli.main(["needs-me", "r", "--agent", "bob", "--json"], transport=t) == 0
    got3 = json.loads(capsys.readouterr().out)
    fb3 = [r for r in got3 if r.get("type") == "forge-feedback"][0]
    assert fb3["count"] == 2 and "review-PRR_1" not in fb3["items"]
    assert "comment-IC_2" in fb3["items"]


def test_needs_me_surfaces_to_review_requester_for_artifact_pr(capsys):
    t = FakeTransport()
    _review_doc(t, requested_by="alice")  # not watched; requester = alice
    forge.feedback_sweep(t, "r", runner=_runner_for(_fixtures(reviews=[_REVIEW])))
    assert cli.main(["needs-me", "r", "--agent", "alice", "--json"], transport=t) == 0
    got = json.loads(capsys.readouterr().out)
    fb = [r for r in got if r.get("type") == "forge-feedback"]
    assert len(fb) == 1 and fb[0]["count"] == 1
    # an unrelated agent sees nothing
    assert cli.main(["needs-me", "r", "--agent", "nobody", "--json"], transport=t) == 0
    got2 = json.loads(capsys.readouterr().out)
    assert [r for r in got2 if r.get("type") == "forge-feedback"] == []


# --- (e2) of/artifact discovery: the real `review request` writes `of` --------

def test_of_keyed_review_is_discovered_by_mirror_swept_and_surfaces_to_requester(capsys):
    """The real `review request` verb writes the PR url under `of` (not
    `artifact`). Forge discovery must read `of`, or every CLI-opened review is
    invisible to the mirror and the sweep — a pre-existing v1 bug. This drives
    the doc through the ACTUAL verb so the frontmatter is exactly what ships."""
    t = FakeTransport()
    p = build_parser()
    # open the review through the real verb: `of` = PR url, no `artifact` key
    a = p.parse_args(["review", "request", "r", "pr-42", "--of", URL,
                      "--reviewer", "carol", "--from", "alice"])
    assert a.func(a, t) == 0
    capsys.readouterr()  # drain the verb's stdout so it doesn't pollute --json
    fm = okf.parse_frontmatter(t.store["team/r/review/pr-42.md"])
    assert fm.get("of") == URL and "artifact" not in fm  # exact shipped shape

    # mirror discovers it via `of`
    mres = forge.mirror(t, "r", now=NOW,
                        runner=lambda args: json.dumps(
                            {"state": "OPEN", "mergedAt": None, "reviewDecision": None}))
    assert mres["checked"] == 1

    # sweep discovers it via `of` and writes the shard
    fres = forge.feedback_sweep(t, "r", runner=_runner_for(_fixtures(reviews=[_REVIEW])))
    assert fres["prs"] == 1 and fres["items"] == 1
    assert "team/r/_coord/forge/feedback/o-r-42/review-PRR_1.md" in t.store

    # the requester (alice) gets the needs-me item
    assert cli.main(["needs-me", "r", "--agent", "alice", "--json"], transport=t) == 0
    fb = [r for r in json.loads(capsys.readouterr().out)
          if r.get("type") == "forge-feedback"]
    assert len(fb) == 1 and fb[0]["count"] == 1 and fb[0]["pr_slug"] == "o-r-42"


def test_legacy_artifact_keyed_review_doc_is_still_discovered(capsys):
    """A hand-written doc keyed with `artifact` (no `of`) must keep working —
    the fallback arm of the of/artifact lookup."""
    t = FakeTransport()
    _review_doc(t, requested_by="alice", key="artifact")  # legacy shape
    mres = forge.mirror(t, "r", now=NOW,
                        runner=lambda args: json.dumps(
                            {"state": "OPEN", "mergedAt": None, "reviewDecision": None}))
    assert mres["checked"] == 1
    fres = forge.feedback_sweep(t, "r", runner=_runner_for(_fixtures(reviews=[_REVIEW])))
    assert fres["prs"] == 1 and fres["items"] == 1
    assert cli.main(["needs-me", "r", "--agent", "alice", "--json"], transport=t) == 0
    fb = [r for r in json.loads(capsys.readouterr().out)
          if r.get("type") == "forge-feedback"]
    assert len(fb) == 1 and fb[0]["count"] == 1


# --- (e3) author unknown → self-skip not applied, but reported ----------------

def test_author_unknown_reports_note_but_still_ingests():
    """When the reviews call fails, the PR author is unknown, so self-skip can't
    be applied. Per over-capture preference we still ingest the inline/comment
    items — but the sweep records a note so the silent weakening is visible."""
    t = FakeTransport()
    _watch(t, agent="bob")

    def run(args):
        if "api" in args:  # inline succeeds
            return json.dumps([_INLINE])
        if "--json" in args:
            j = args[args.index("--json") + 1]
            if "reviews" in j:
                return None  # reviews call fails → pr_author unknown
            if j == "comments":
                return json.dumps({"comments": [_COMMENT]})
        return None

    res = forge.feedback_sweep(t, "r", runner=run)
    assert res["items"] == 2  # inline + comment still ingested
    # reviews surface was down (hence author unknown) — BOTH facts are noted:
    # the unavailable surface AND its self-skip consequence.
    assert res["notes"] == ["o-r-42: review surface unavailable",
                            "o-r-42: author unknown — self-skip not applied"]
    assert res["skipped"] == []  # not skipped — items were written
    assert "team/r/_coord/forge/feedback/o-r-42/inline-PRRC_1.md" in t.store


def test_partial_surface_failure_is_noted_while_healthy_surfaces_ingest():
    """A partial gh failure (one surface None, others healthy) must not report
    clean — the motivating blind spot was exactly a persistently failing
    reviews surface going unseen. The healthy surfaces still ingest; each
    unavailable surface lands a note. All-three-None stays in ``skipped``."""
    t = FakeTransport()
    _watch(t, agent="bob", url=URL)   # o-r-42: reviews down, comments healthy
    _watch(t, agent="bob", url=URL2)  # o-r-99: every surface down → skipped

    def run(args):
        joined = " ".join(args)
        if "/pull/42" in joined or "pulls/42" in joined:
            if "api" in args:
                return None  # inline surface down too
            if "--json" in args:
                j = args[args.index("--json") + 1]
                if "reviews" in j:
                    return None  # reviews surface down
                if j == "comments":
                    return json.dumps({"comments": [_COMMENT]})  # comments healthy
        return None  # o-r-99: all three surfaces down

    res = forge.feedback_sweep(t, "r", runner=run)
    # comment from the healthy surface still lands
    assert res["items"] == 1
    assert "team/r/_coord/forge/feedback/o-r-42/comment-IC_1.md" in t.store
    # the reviews-unavailable line is present (partial failure surfaced, not clean)
    assert "o-r-42: review surface unavailable" in res["notes"]
    assert "o-r-42: inline surface unavailable" in res["notes"]
    # the all-three-None PR stays in skipped, NOT notes
    assert any("o-r-99" in s for s in res["skipped"])
    assert not any("o-r-99" in n for n in res["notes"])


def test_needs_me_text_output_renders_forge_line(capsys):
    t = FakeTransport()
    _watch(t, agent="bob")
    forge.feedback_sweep(t, "r", runner=_runner_for(_fixtures(reviews=[_REVIEW])))
    assert cli.main(["needs-me", "r", "--agent", "bob"], transport=t) == 0
    out = capsys.readouterr().out
    assert "[FORGE] feedback on o-r-42" in out and "from rev" in out


# --- (f) gh failure on one PR reported + pass continues -------------------

def test_gh_failure_on_one_pr_is_reported_and_pass_continues():
    t = FakeTransport()
    _watch(t, agent="bob", url=URL)    # o-r-42 -> gh fails
    _watch(t, agent="bob", url=URL2)   # o-r-99 -> gh succeeds

    def run(args):
        joined = " ".join(args)
        if "/pull/99" in joined or "pulls/99" in joined:
            if "api" in args:
                return json.dumps([])
            if "--json" in args:
                j = args[args.index("--json") + 1]
                if "reviews" in j:
                    return json.dumps({"author": {"login": "pa"}, "reviews": [
                        {"id": "PRR_9", "author": {"login": "z"},
                         "state": "CHANGES_REQUESTED", "body": "fix", "submittedAt": NOW}]})
                if j == "comments":
                    return json.dumps({"comments": []})
        return None  # the failing PR: every call returns None

    res = forge.feedback_sweep(t, "r", runner=run)
    assert res["prs"] == 2 and res["items"] == 1
    assert any("o-r-42" in s for s in res["skipped"])
    # the healthy PR still wrote its shard despite the sibling's failure
    assert "team/r/_coord/forge/feedback/o-r-99/review-PRR_9.md" in t.store


# --- (g) v1 mirror behavior untouched -------------------------------------

def test_mirror_return_contract_is_unchanged():
    t = FakeTransport()
    _review_doc(t, slug="pr-42")
    runner = lambda a: json.dumps({"state": "OPEN", "mergedAt": None, "reviewDecision": None})
    assert forge.mirror(t, "r", now=NOW, runner=runner) == \
        {"checked": 1, "mirrored": 1, "verdicts": 0}


# --- CLI verbs ------------------------------------------------------------

def test_cli_forge_feedback_command(capsys):
    t = FakeTransport()
    _watch(t, agent="bob")
    p = build_parser()
    args = p.parse_args(["forge", "feedback", "r"])
    args.runner = _runner_for(_fixtures(reviews=[_REVIEW]))
    assert args.func(args, t) == 0
    out = capsys.readouterr().out
    assert "feedback" in out.lower()
    assert "team/r/_coord/forge/feedback/o-r-42/review-PRR_1.md" in t.store


def test_cli_forge_mirror_also_sweeps_feedback(capsys):
    t = FakeTransport()
    _review_doc(t, slug="pr-42")

    def run(args):
        if "state" in " ".join(args):
            return json.dumps({"state": "OPEN", "mergedAt": None})
        if "api" in args:
            return json.dumps([])
        if "--json" in args:
            j = args[args.index("--json") + 1]
            if "reviews" in j:
                return json.dumps({"author": {"login": "pa"}, "reviews": [_REVIEW]})
            if j == "comments":
                return json.dumps({"comments": []})
        return None

    p = build_parser()
    args = p.parse_args(["forge", "mirror", "r"])
    args.runner = run
    assert args.func(args, t) == 0
    out = capsys.readouterr().out
    assert "PR review(s) checked" in out  # v1 mirror line intact
    assert "team/r/_coord/forge/feedback/o-r-42/review-PRR_1.md" in t.store
