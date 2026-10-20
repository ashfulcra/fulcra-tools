"""`review merge-ready` — the verification half of a merge, never the merge (directive 082d799e).

G6 ruling (coord-boss triage 2026-08-11): merge-time judgment stays with the merger; the engine checks that the
review is APPROVED at an exact head, that the PR head == the register head == the head each verdict reviewed, and
that CI is green, and reports MERGE-READY to the merger's queue. Every unreadable input is UNKNOWN (rc 1), never
NOT READY (rc 2) and never READY (rc 0).
"""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from coord_engine import cli, merge_ready
from coord_engine_test_helpers import FakeTransport

HEAD = "a" * 40
OTHER = "b" * 40
URL = "https://github.com/o/r/pull/7"
GREEN = [{"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
         {"__typename": "StatusContext", "context": "lint", "state": "SUCCESS"}]


@pytest.fixture(autouse=True)
def _pin(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: _dt.datetime(2026, 10, 19, 20, 0, tzinfo=_dt.timezone.utc))


def _review(t, slug="pr-7", *, head=HEAD, of=URL, required=("alice",)):
    req = "".join(f"  - {r}\n" for r in required)
    head_line = f"head: {head}\n" if head else ""
    t.put(f"team/r/review/{slug}.md",
          f"---\ntype: Review\nschema: review-request/v2\nof: {of}\n{head_line}required:\n{req}---\nr")


def _verdict(t, reviewer="alice", *, slug="pr-7", head=HEAD, attested=None, verdict="approve"):
    name = f"{head}--{reviewer}--2026-10-19T19:00:00Z-aaaaaaaa.md"
    t.put(f"team/r/review/{slug}/verdicts/{name}",
          f"---\ntype: Verdict\nreviewer: {reviewer}\nhead: {attested or head}\nverdict: {verdict}\n---\nok")


def _forge(state="OPEN", head=HEAD, checks=GREEN, draft=False, calls=None):
    def runner(args):
        if calls is not None:
            calls.append(args)
        return json.dumps({"state": state, "isDraft": draft, "headRefOid": head, "statusCheckRollup": checks})
    return runner


def _run(t, *argv, runner=None):
    parser = cli.build_parser()
    args = parser.parse_args(["review", "merge-ready", "r", *argv])
    args.runner = runner
    return args.func(args, t)


def _ready(t):
    _review(t)
    _verdict(t)


def test_approved_at_the_pr_head_with_green_ci_is_merge_ready(capsys):
    t = FakeTransport()
    _ready(t)
    calls = []
    assert _run(t, "pr-7", runner=_forge(calls=calls)) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"MERGE-READY pr-7: {URL} at {HEAD}")
    assert calls == [["gh", "pr", "view", URL, "--json", merge_ready.PR_FIELDS]]


def test_merge_ready_never_merges():
    t = FakeTransport()
    _ready(t)
    calls = []
    _run(t, "pr-7", runner=_forge(calls=calls))
    assert all(c[:3] == ["gh", "pr", "view"] for c in calls)


@pytest.mark.parametrize("forge_kw, reason", [
    ({"head": OTHER}, f"PR head {OTHER} is not the reviewed head {HEAD}"),
    ({"state": "MERGED"}, "PR is MERGED, not OPEN"),
    ({"draft": True}, "PR is a draft"),
    ({"checks": []}, "CI reported no checks"),
    ({"checks": [{"name": "test", "status": "IN_PROGRESS", "conclusion": None}]}, "CI pending: test"),
    ({"checks": [{"name": "test", "status": "COMPLETED", "conclusion": "FAILURE"},
                 {"name": "slow", "status": "QUEUED"}]}, "CI failed: test"),
    ({"checks": [{"__typename": "StatusContext", "context": "lint", "state": "ERROR"}]}, "CI failed: lint"),
])
def test_each_forge_check_blocks_and_is_named(capsys, forge_kw, reason):
    t = FakeTransport()
    _ready(t)
    assert _run(t, "pr-7", runner=_forge(**forge_kw)) == 2
    out = capsys.readouterr().out
    assert out.startswith("NOT READY pr-7")
    assert reason in out


def test_a_pending_required_reviewer_blocks(capsys):
    t = FakeTransport()
    _review(t, required=("alice", "bob"))
    _verdict(t)
    assert _run(t, "pr-7", runner=_forge()) == 2
    assert "review is PENDING (awaiting: bob)" in capsys.readouterr().out


def test_changes_requested_blocks(capsys):
    t = FakeTransport()
    _review(t)
    _verdict(t, verdict="changes")
    assert _run(t, "pr-7", runner=_forge()) == 2
    assert "changes requested: alice" in capsys.readouterr().out


def test_a_verdict_attesting_another_head_does_not_count(capsys):
    t = FakeTransport()
    _review(t)
    _verdict(t, attested=OTHER)
    assert _run(t, "pr-7", runner=_forge()) == 2
    out = capsys.readouterr().out
    assert "review is PENDING" in out and "verdicts attesting another head" in out


def test_a_headless_register_cannot_bind_a_verdict_to_the_code(capsys):
    t = FakeTransport()
    _review(t, head=None)
    t.put("team/r/review/pr-7/verdicts/alice.md", "---\ntype: Verdict\nreviewer: alice\nverdict: approve\n---\nok")
    assert _run(t, "pr-7", runner=_forge()) == 2
    assert "names no head" in capsys.readouterr().out


def test_a_non_pr_artifact_is_not_ready(capsys):
    t = FakeTransport()
    _review(t, of="a design doc")
    _verdict(t)
    assert _run(t, "pr-7", runner=_forge()) == 2
    assert "is not a GitHub PR" in capsys.readouterr().err


def test_repo_allowlist_rejects_another_repository(capsys):
    t = FakeTransport()
    _ready(t)
    assert _run(t, "pr-7", "--repo", "o/other", runner=_forge()) == 2
    assert "in o/other" in capsys.readouterr().err


def test_an_absent_review_is_not_ready_not_unknown(capsys):
    assert _run(FakeTransport(), "nope", runner=_forge()) == 2
    assert "no such review" in capsys.readouterr().err


def test_an_unanswered_forge_is_unknown_and_retryable(capsys):
    t = FakeTransport()
    _ready(t)
    assert _run(t, "pr-7", runner=lambda args: None) == 1
    assert capsys.readouterr().err.rstrip().endswith("retry")


def test_an_unreadable_verdict_shard_is_unknown_never_ready(capsys):
    class Lossy(FakeTransport):
        def read(self, path):
            if "/verdicts/" in path:
                return None
            return super().read(path)

    t = Lossy()
    _ready(t)
    assert _run(t, "pr-7", runner=_forge()) == 1
    assert "UNKNOWN, retry" in capsys.readouterr().err


def test_notify_reports_ready_to_the_merger_once_per_head(capsys):
    t = FakeTransport()
    _ready(t)
    assert _run(t, "pr-7", "--notify", "boss", "--from", "maint", runner=_forge()) == 0
    reports = [p for p in t.store if p.startswith("team/r/task/merge-ready-pr-7-")]
    assert len(reports) == 1
    doc = t.store[reports[0]]
    assert "assignee: boss" in doc and HEAD in doc
    # Same head again: deduped, still one report.
    assert _run(t, "pr-7", "--notify", "boss", "--from", "maint", runner=_forge()) == 0
    assert len([p for p in t.store if p.startswith("team/r/task/merge-ready-pr-7-")]) == 1


def test_notify_sends_nothing_when_not_ready():
    t = FakeTransport()
    _ready(t)
    assert _run(t, "pr-7", "--notify", "boss", runner=_forge(head=OTHER)) == 2
    assert not [p for p in t.store if p.startswith("team/r/task/")]


def test_json_carries_the_decision(capsys):
    t = FakeTransport()
    _ready(t)
    assert _run(t, "pr-7", "--json", runner=_forge(head=OTHER)) == 2
    got = json.loads(capsys.readouterr().out)
    assert (got["ready"], got["head"], got["pr_head"], got["ci"], got["pr"]) == (False, HEAD, OTHER, "green", URL)


def test_json_unknown_is_one_object_never_a_verdict(capsys):
    t = FakeTransport()
    _ready(t)
    assert _run(t, "pr-7", "--json", runner=lambda args: None) == 1
    got = json.loads(capsys.readouterr().out)
    assert (got["ready"], got["verdict"]) == (False, "unknown")
    assert got["reasons"][0].endswith("retry")


def test_an_undelivered_report_is_unknown_and_retryable(capsys, monkeypatch):
    t = FakeTransport()
    _ready(t)
    monkeypatch.setattr(cli, "_write_directive", lambda *a, **k: 1)
    assert _run(t, "pr-7", "--notify", "boss", runner=_forge()) == 1
    assert "NOT delivered — retry" in capsys.readouterr().err


@pytest.mark.parametrize("runs", [1, 2])
def test_json_notify_stdout_is_one_value_fresh_and_deduped(capsys, runs):
    t = FakeTransport()
    _ready(t)
    for _ in range(runs):
        capsys.readouterr()
        assert _run(t, "pr-7", "--json", "--notify", "boss", "--from", "maint", runner=_forge()) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["verdict"] == "ready" and got["reported"]["to"] == "boss"


def test_report_identity_is_the_head_not_the_ci_detail():
    t = FakeTransport()
    _ready(t)
    third = GREEN + [{"__typename": "CheckRun", "name": "extra", "status": "COMPLETED", "conclusion": "SUCCESS"}]
    assert _run(t, "pr-7", "--notify", "boss", "--from", "maint", runner=_forge()) == 0
    assert _run(t, "pr-7", "--notify", "boss", "--from", "maint", runner=_forge(checks=third)) == 0
    assert len([p for p in t.store if p.startswith("team/r/task/merge-ready-pr-7-")]) == 1


def test_a_new_head_is_a_new_report():
    t = FakeTransport()
    _ready(t)
    assert _run(t, "pr-7", "--notify", "boss", "--from", "maint", runner=_forge()) == 0
    _review(t, head=OTHER)
    _verdict(t, head=OTHER)
    assert _run(t, "pr-7", "--notify", "boss", "--from", "maint", runner=_forge(head=OTHER)) == 0
    assert len([p for p in t.store if p.startswith("team/r/task/merge-ready-pr-7-")]) == 2


# --- the GitHub CLI probe: a `gh` on PATH is not proof (docs-QA 2026-10-20) -----------------------------------

from coord_engine import forge as forge_mod

BUILTIN = ("gh: this gh is a built-in GitHub client, not the GitHub CLI: `gh api <REST endpoint>` works here, "
           "and there is no other gh command.\n")


def _host_gh(monkeypatch, *, path="/usr/bin/gh", rc=0, out="gh version 2.62.0 (2024-11-14)\n", err="",
             raises=False):
    """Fake the host under the probe: what `which gh` finds and what `gh --version` prints. Any other gh call
    is recorded so a test can prove the verb never reached the forge."""
    calls = []
    monkeypatch.setattr(forge_mod.shutil, "which", lambda name: path if name == "gh" else None)

    def run_bounded(argv, timeout):
        calls.append(argv)
        if raises:
            raise OSError("probe failed")
        if argv[1:] == ["--version"]:
            return rc, out, err
        return 1, "", "unexpected gh call"
    monkeypatch.setattr(forge_mod._transport, "run_bounded", run_bounded)
    return calls


@pytest.mark.parametrize("host, state", [
    (dict(), "ok"),
    (dict(path=None), "absent"),
    (dict(rc=1, out="", err=BUILTIN), "other"),  # the built-in client, as measured 2026-10-20
    (dict(out=BUILTIN), "other"),
    (dict(rc=2, out=""), "other"),
    (dict(raises=True), "unknown"),
])
def test_github_cli_probe_classifies_the_host(monkeypatch, host, state):
    _host_gh(monkeypatch, **host)
    assert forge_mod.github_cli()[0] == state


def test_merge_ready_on_a_gh_that_is_not_the_github_cli_is_unknown_without_retry(capsys, monkeypatch):
    calls = _host_gh(monkeypatch, rc=1, out="", err=BUILTIN)
    t = FakeTransport()
    _ready(t)
    assert _run(t, "pr-7") == 1
    err = capsys.readouterr().err
    assert "not the GitHub CLI" in err and "retry" not in err, err
    assert all(argv[1:] == ["--version"] for argv in calls), "the forge was never asked"


def test_merge_ready_when_the_probe_does_not_finish_is_unknown_and_retryable(capsys, monkeypatch):
    _host_gh(monkeypatch, raises=True)
    t = FakeTransport()
    _ready(t)
    assert _run(t, "pr-7") == 1
    assert capsys.readouterr().err.rstrip().endswith("retry")


def test_merge_ready_json_names_the_missing_cli(capsys, monkeypatch):
    _host_gh(monkeypatch, path=None)
    t = FakeTransport()
    _ready(t)
    assert _run(t, "pr-7", "--json") == 1
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "unknown" and "no `gh` on PATH" in out["reasons"][0]


@pytest.mark.parametrize("verb", ["mirror", "feedback"])
def test_forge_sweeps_degrade_on_a_gh_that_is_not_the_github_cli(capsys, monkeypatch, verb):
    calls = _host_gh(monkeypatch, rc=1, out="", err=BUILTIN)
    args = cli.build_parser().parse_args(["forge", verb, "r"])
    assert args.func(args, FakeTransport()) == 0
    assert "not the GitHub CLI" in capsys.readouterr().err
    assert all(argv[1:] == ["--version"] for argv in calls), "nothing was swept"
