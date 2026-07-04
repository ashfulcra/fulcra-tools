import json

from coord_engine import cli, forge
from coord_engine_test_helpers import FakeTransport

NOW = "2026-07-02T15:00:00Z"


def test_parse_pr_url():
    assert forge.parse_pr_url("https://github.com/o/r/pull/42") == "https://github.com/o/r/pull/42"
    assert forge.parse_pr_url("see https://github.com/o/r/pull/42#issuecomment-1") \
        == "https://github.com/o/r/pull/42"
    assert forge.parse_pr_url("https://gitlab.com/o/r/-/merge_requests/1") is None
    assert forge.parse_pr_url(None) is None


def _team_with_review(t, slug="pr-42", artifact="https://github.com/o/r/pull/42"):
    t.put(f"team/r/review/{slug}.md",
          f"---\ntype: Review\ntitle: R\nartifact: {artifact}\n---\n")


def test_mirror_open_pr_writes_state_shard_idempotently():
    t = FakeTransport()
    _team_with_review(t)
    runner = lambda a: json.dumps({"state": "OPEN", "mergedAt": None, "reviewDecision": None})
    res = forge.mirror(t, "r", now=NOW, runner=runner)
    assert res == {"checked": 1, "mirrored": 1, "verdicts": 0}
    assert "team/r/_coord/evidence/pr-42/state-OPEN.md" in t.store
    # second pass, same state: no duplicate shard
    assert forge.mirror(t, "r", now=NOW, runner=runner)["mirrored"] == 0


def test_mirror_merged_pr_auto_approves_and_review_status_reflects(capsys):
    t = FakeTransport()
    _team_with_review(t)
    runner = lambda a: json.dumps({"state": "MERGED", "mergedAt": "2026-07-02T14:00:00Z",
                                   "reviewDecision": "APPROVED"})
    res = forge.mirror(t, "r", now=NOW, runner=runner)
    assert res["verdicts"] == 1
    assert "team/r/review/pr-42/verdicts/forge.md" in t.store
    # the review tally now folds the forge approval
    assert cli.main(["review", "status", "r", "pr-42", "--json"], transport=t) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["state"] == "APPROVED" and "forge" in got["approvals"]
    # idempotent: second merged pass writes nothing new
    assert forge.mirror(t, "r", now=NOW, runner=runner) == \
        {"checked": 1, "mirrored": 0, "verdicts": 0}


def test_mirror_degrades_on_gh_failure_and_non_pr_artifacts():
    t = FakeTransport()
    _team_with_review(t, slug="doc-review", artifact="internal doc, no forge")
    _team_with_review(t)
    res = forge.mirror(t, "r", now=NOW, runner=lambda a: None)   # gh failing
    assert res == {"checked": 1, "mirrored": 0, "verdicts": 0}   # non-PR skipped, PR untouched
    assert not [p for p in t.store if "_coord/evidence" in p]


def test_cli_forge_mirror_command(capsys):
    t = FakeTransport()
    _team_with_review(t)
    import argparse
    from coord_engine.cli import build_parser
    p = build_parser()
    args = p.parse_args(["forge", "mirror", "r"])
    args.runner = lambda a: json.dumps({"state": "OPEN", "mergedAt": None})
    assert args.func(args, t) == 0
    assert "1 PR review(s) checked" in capsys.readouterr().out


def test_repo_allowlist_blocks_foreign_pr():
    assert forge.parse_pr_url("https://github.com/evil/other/pull/9", repo="o/r") is None
    assert forge.parse_pr_url("https://github.com/o/r/pull/9", repo="o/r") \
        == "https://github.com/o/r/pull/9"
    assert forge.parse_pr_url("https://github.com/O/R/pull/9", repo="o/r") is not None  # case-insensitive


def test_mirror_repo_filter_skips_foreign_review():
    t = FakeTransport()
    _team_with_review(t, slug="foreign", artifact="https://github.com/evil/other/pull/9")
    runner = lambda a: json.dumps({"state": "MERGED", "mergedAt": "2026-07-02T14:00:00Z"})
    res = forge.mirror(t, "r", now=NOW, runner=runner, repo="o/r")
    assert res == {"checked": 0, "mirrored": 0, "verdicts": 0}
    assert "team/r/review/foreign/verdicts/forge.md" not in t.store   # no wrong-repo auto-approve
