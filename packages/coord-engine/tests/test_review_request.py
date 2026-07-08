"""CLI `review request` — requester-side durability (watcher-doctrine Task 1).

A review request must write EXACTLY the doc the tally/needs-me readers consume,
so a named required reviewer sees a durable `pending_required` obligation that
stays until their verdict file exists. Regression cover for the production
failure where a reviewer acked directives and the obligation vanished.
"""

import json

from coord_engine import cli, okf
from coord_engine_test_helpers import FakeTransport


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
