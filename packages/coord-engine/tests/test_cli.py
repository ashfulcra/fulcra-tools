import json

from coord_engine import cli, tasks
from coord_engine_test_helpers import FakeTransport, _task


def _dslug(title, *, summary=None, next=None, assignee):
    """The canonical hash-bearing directive slug the CLI now computes — directive
    paths are ``<title-slug>-<sha256(payload)[:8]>`` (identical resends dedupe,
    distinct messages never share a slot)."""
    payload = cli._directive_payload(title, summary, next, assignee)
    return f"{tasks.slugify(title)}-{cli._payload_hash(payload)}"


def test_cli_reconcile_then_status_and_board(capsys):
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    t.put("team/r/task/b.md", _task("Bravo", "waiting"))

    assert cli.main(["reconcile", "r"], transport=t) == 0
    assert "2 tasks" in capsys.readouterr().out

    assert cli.main(["status", "r", "--json"], transport=t) == 0
    counts = json.loads(capsys.readouterr().out)
    assert counts == {"active": 1, "waiting": 1}

    assert cli.main(["board", "r"], transport=t) == 0
    out = capsys.readouterr().out
    assert "ACTIVE (1)" in out and "Alpha" in out


def test_cli_needs_me(capsys):
    t = FakeTransport()
    t.put("team/r/task/a.md",
          "---\ntype: Task\ntitle: Mine\nstatus: active\nassignee: me\n---\n")
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    assert cli.main(["needs-me", "r", "--agent", "me"], transport=t) == 0
    assert "Mine" in capsys.readouterr().out


def test_cli_search(capsys):
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Widget fixer", "active"))
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    assert cli.main(["search", "r", "widget"], transport=t) == 0
    assert "Widget fixer" in capsys.readouterr().out


def test_cli_status_no_aggregate_hint(capsys):
    t = FakeTransport()
    assert cli.main(["status", "empty"], transport=t) == 0
    assert "run `reconcile` first" in capsys.readouterr().out


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def test_cli_roles_status_held(capsys):
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md", "---\ntype: Role\npolicy: shared\nsla_hours: 24\n---\n")
    t.put("team/r/roles/reviewer/leases/ash.md",
          f"---\ntype: Lease\nagent: ash\ntimestamp: {_now_iso()}\n---\n")
    assert cli.main(["roles", "status", "r", "reviewer"], transport=t) == 0
    assert "HELD" in capsys.readouterr().out


def test_cli_roles_status_malformed_doc_is_unknown_not_default_sla(capsys):
    # The same one-line class as `_role_fresh_holders`: the guard tested
    # `raw_doc is None`, so a LISTED role whose doc is corrupt or truncated sailed
    # past it into `parse_frontmatter(...) or {}` — collapsing a long-SLA role onto
    # the 24h default and printing a confident VACANT at rc 0. A body that won't
    # parse is UNKNOWN, exactly like a read that failed: rc 1, no state asserted.
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md", "not frontmatter\n")  # listed, unusable
    t.put("team/r/roles/reviewer/leases/ash.md",  # a lease FRESH under the real SLA
          f"---\ntype: Lease\nagent: ash\ntimestamp: {_now_iso()}\n---\n")
    assert cli.main(["roles", "status", "r", "reviewer", "--json"], transport=t) == 1
    cap = capsys.readouterr()
    assert "state unknown" in cap.err
    assert "VACANT" not in cap.out, "must not assert a state off a doc it cannot read"


def test_cli_roles_escalate_skips_malformed_doc_no_false_vacancy(capsys):
    # Same class on the ACTING path, where it mints a P1: an unparseable doc for a
    # listed role must be skipped as UNKNOWN, never judged under the 24h default.
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md", "not frontmatter\n")
    t.put("team/r/roles/reviewer/leases/ash.md",  # stale ONLY under the default SLA
          "---\ntype: Lease\nagent: ash\ntimestamp: 2026-06-01T00:00:00Z\n---\n")
    assert cli.main(["escalate", "r"], transport=t) == 0
    cap = capsys.readouterr()
    assert "state unknown" in cap.err
    assert "VACANT" not in cap.out
    # and no escalation task was written off a doc we could not read
    assert not [p for p in t.store if "/task/" in p], sorted(t.store)


def test_cli_roles_status_invalid_sla_is_unknown_not_default(capsys):
    # Same fact-class as the malformed doc above, reached through the VALUE: the doc
    # parses, but its `sla_hours` was explicitly set and doesn't. Every state below
    # it (HELD/VACANT/escalation_due) would be asserted off a 24h window we invented,
    # so: rc 1, assert nothing.
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md", "---\ntype: Role\nsla_hours: abc\n---\n")
    t.put("team/r/roles/reviewer/leases/ash.md",  # stale ONLY under the default SLA
          "---\ntype: Lease\nagent: ash\ntimestamp: 2026-06-01T00:00:00Z\n---\n")
    assert cli.main(["roles", "status", "r", "reviewer", "--json"], transport=t) == 1
    cap = capsys.readouterr()
    assert "state unknown" in cap.err
    assert "VACANT" not in cap.out, "must not assert a state off an SLA it invented"


def test_cli_roles_status_absent_sla_still_defaults(capsys):
    # The distinction: an OMITTED optional `sla_hours` is a well-formed doc, not an
    # unknown one. It must still fold under the default at rc 0.
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md", "---\ntype: Role\npolicy: shared\n---\n")
    t.put("team/r/roles/reviewer/leases/ash.md",
          f"---\ntype: Lease\nagent: ash\ntimestamp: {_now_iso()}\n---\n")
    assert cli.main(["roles", "status", "r", "reviewer", "--json"], transport=t) == 0
    import json as _json
    res = _json.loads(capsys.readouterr().out)
    assert res["status"] == "HELD"
    assert res["sla_hours"] == 24.0


def test_cli_roles_escalate_skips_invalid_sla_no_p1_minted(capsys):
    # The ACTING path. A malformed SLA must never manufacture an escalation to a
    # human: under the 24h default this lease reads VACANT and mints a P1, but the
    # doc's real window is unknowable — it could make the lease perfectly fresh.
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md",
          "---\ntype: Role\nsla_hours: abc\nmaintainer: ash\n---\n")
    t.put("team/r/roles/reviewer/leases/ash.md",  # stale ONLY under the default SLA
          "---\ntype: Lease\nagent: ash\ntimestamp: 2026-06-01T00:00:00Z\n---\n")
    assert cli.main(["escalate", "r"], transport=t) == 0
    cap = capsys.readouterr()
    assert "state unknown" in cap.err
    assert "escalated" not in cap.out.replace("0 escalated", "")
    assert not [p for p in t.store if "/task/" in p], \
        "no P1 may be minted off an SLA we could not read"
    assert not [p for p in t.store if "escalation" in p], sorted(t.store)


def test_cli_roles_escalate_absent_sla_still_escalates(capsys):
    # And the over-correction guard: a doc that omits `sla_hours` is well-formed, so
    # a genuinely vacant role must STILL escalate under the default.
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md", "---\ntype: Role\nmaintainer: ash\n---\n")
    t.put("team/r/roles/reviewer/leases/ash.md",
          "---\ntype: Lease\nagent: ash\ntimestamp: 2026-06-01T00:00:00Z\n---\n")
    assert cli.main(["escalate", "r"], transport=t) == 0
    assert "escalated reviewer -> ash" in capsys.readouterr().out
    assert [p for p in t.store if "/task/" in p], "absent SLA must not suppress"


def test_cli_roles_status_vacant_escalation_due(capsys):
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md", "---\ntype: Role\nsla_hours: 24\n---\n")
    t.put("team/r/roles/reviewer/leases/ash.md",
          "---\ntype: Lease\nagent: ash\ntimestamp: 2020-01-01T00:00:00Z\n---\n")
    assert cli.main(["roles", "status", "r", "reviewer", "--json"], transport=t) == 0
    import json as _json
    res = _json.loads(capsys.readouterr().out)
    assert res["status"] == "VACANT"
    assert res["escalation_due"] is True


def test_cli_task_start_then_reconcile_shows_it(capsys):
    from coord_engine import okf
    t = FakeTransport()
    assert cli.main(["task", "start", "r", "Build the thing", "-w", "coord",
                     "--status", "active", "-p", "P1"], transport=t) == 0
    assert "created" in capsys.readouterr().out
    fm = okf.parse_frontmatter(t.store["team/r/task/build-the-thing.md"])
    assert fm["type"] == "Task" and fm["status"] == "active" and fm["priority"] == "P1"
    cli.main(["reconcile", "r"], transport=t); capsys.readouterr()
    cli.main(["board", "r"], transport=t)
    assert "Build the thing" in capsys.readouterr().out


def test_cli_task_start_refuses_duplicate(capsys):
    t = FakeTransport()
    cli.main(["task", "start", "r", "Dup"], transport=t); capsys.readouterr()
    assert cli.main(["task", "start", "r", "Dup"], transport=t) == 1
    assert "already exists" in capsys.readouterr().err


def test_cli_task_illegal_transition_fails(capsys):
    t = FakeTransport()
    cli.main(["task", "start", "r", "T", "--status", "active"], transport=t)
    cli.main(["task", "done", "r", "t", "-e", "shipped"], transport=t)
    capsys.readouterr()
    assert cli.main(["task", "update", "r", "t", "--status", "active"], transport=t) == 1
    assert "illegal transition" in capsys.readouterr().err


def test_cli_task_update_done_needs_evidence(capsys):
    t = FakeTransport()
    cli.main(["task", "start", "r", "T", "--status", "active"], transport=t)
    capsys.readouterr()
    assert cli.main(["task", "update", "r", "t", "--status", "done"], transport=t) == 1
    assert "done requires evidence" in capsys.readouterr().err
    assert cli.main(["task", "update", "r", "t", "--status", "done", "-e", "ok"], transport=t) == 0


def test_cli_review_status(capsys):
    import json as _j
    t = FakeTransport()
    t.put("team/r/review/pr-9.md", "---\ntype: Review\nrequired: alice, bob\n---\n")
    t.put("team/r/review/pr-9/verdicts/alice.md",
          "---\ntype: Verdict\nreviewer: alice\nverdict: approve\n---\n")
    assert cli.main(["review", "status", "r", "pr-9", "--json"], transport=t) == 0
    res = _j.loads(capsys.readouterr().out)
    assert res["state"] == "PENDING" and res["pending_required"] == ["bob"]
    t.put("team/r/review/pr-9/verdicts/bob.md",
          "---\ntype: Verdict\nreviewer: bob\nverdict: changes\n---\n")
    cli.main(["review", "status", "r", "pr-9", "--json"], transport=t)
    assert _j.loads(capsys.readouterr().out)["state"] == "CHANGES"


def test_cli_review_keys_by_filename_not_frontmatter(capsys):
    # a file claiming someone else's reviewer name must NOT shadow their verdict
    import json as _j
    t = FakeTransport()
    t.put("team/r/review/pr-1.md", "---\ntype: Review\nrequired: alice\n---\n")
    t.put("team/r/review/pr-1/verdicts/alice.md",
          "---\ntype: Verdict\nreviewer: alice\nverdict: changes\n---\n")
    t.put("team/r/review/pr-1/verdicts/mallory.md",   # claims to be alice, approving
          "---\ntype: Verdict\nreviewer: alice\nverdict: approve\n---\n")
    cli.main(["review", "status", "r", "pr-1", "--json"], transport=t)
    res = _j.loads(capsys.readouterr().out)
    # alice's real changes still blocks; mallory counts as her own (approve) reviewer
    assert res["state"] == "CHANGES"
    assert "alice" in res["changes"] and "mallory" in res["approvals"]


def test_cli_continuity_snapshot_and_resume(capsys):
    t = FakeTransport()
    assert cli.main(["continuity", "snapshot", "r", "ash", "build-l6",
                     "--objective", "ship it", "--next", "land PR",
                     "--open-question", "naming?", "--context-percent", "40"], transport=t) == 0
    assert "snapshot CHK-" in capsys.readouterr().out
    assert cli.main(["continuity", "resume", "r", "ash", "build-l6"], transport=t) == 0
    out = capsys.readouterr().out
    assert "objective: ship it" in out and "land PR" in out


def test_cli_continuity_resume_picks_latest_across_tasks(capsys):
    t = FakeTransport()
    cli.main(["continuity", "snapshot", "r", "ash", "old", "--objective", "older"], transport=t)
    cli.main(["continuity", "snapshot", "r", "ash", "new", "--objective", "newest"], transport=t)
    capsys.readouterr()
    # no task arg -> fold to the newest across the member's snapshots
    cli.main(["continuity", "resume", "r", "ash"], transport=t)
    assert "newest" in capsys.readouterr().out


def test_cli_continuity_slugifies_task(capsys):
    t = FakeTransport()
    cli.main(["continuity", "snapshot", "r", "ash", "feat/Sub Task", "--objective", "x"], transport=t)
    capsys.readouterr()
    assert "team/r/member/ash/continuity/feat-sub-task/latest.json" in t.store
    cli.main(["continuity", "resume", "r", "ash"], transport=t)
    assert "objective: x" in capsys.readouterr().out


def test_cli_task_block_pause_abandon_assign(capsys):
    from coord_engine import okf
    t = FakeTransport()
    cli.main(["task", "start", "r", "T", "--status", "active"], transport=t)
    assert cli.main(["task", "block", "r", "t", "--on-user", "review"], transport=t) == 0
    fm = okf.parse_frontmatter(t.store["team/r/task/t.md"])
    assert fm["status"] == "blocked" and fm["blocked_on"] == "review"
    assert fm["assignee"] == "human" and "needs:human" in fm["tags"]
    # blocked -> waiting is a legal transition
    assert cli.main(["task", "pause", "r", "t", "-n", "resume after review"], transport=t) == 0
    assert okf.parse_frontmatter(t.store["team/r/task/t.md"])["status"] == "waiting"


def test_cli_task_block_on_user_honors_env_human(monkeypatch):
    from coord_engine import okf
    monkeypatch.setenv("FULCRA_COORD_HUMAN", "ash")
    t = FakeTransport()
    cli.main(["task", "start", "r", "Human", "--status", "active"], transport=t)
    assert cli.main(["task", "block", "r", "human", "--on-user", "approve"], transport=t) == 0
    fm = okf.parse_frontmatter(t.store["team/r/task/human.md"])
    assert fm["blocked_on"] == "approve"
    assert fm["assignee"] == "ash"
    assert "needs:human" in fm["tags"]


def test_cli_task_block_requires_a_reason(capsys):
    t = FakeTransport()
    cli.main(["task", "start", "r", "B", "--status", "active"], transport=t)
    assert cli.main(["task", "block", "r", "b"], transport=t) == 1
    assert "requires --blocked-on or --on-user" in capsys.readouterr().err


def test_cli_task_pause_and_abandon(capsys):
    from coord_engine import okf
    t = FakeTransport()
    cli.main(["task", "start", "r", "P", "--status", "active"], transport=t)
    assert cli.main(["task", "pause", "r", "p", "-n", "wait for CI"], transport=t) == 0
    fm = okf.parse_frontmatter(t.store["team/r/task/p.md"])
    assert fm["status"] == "waiting" and fm["next_action"] == "wait for CI"
    assert cli.main(["task", "abandon", "r", "p", "-r", "superseded"], transport=t) == 0
    out = t.store["team/r/task/p.md"]
    assert okf.parse_frontmatter(out)["status"] == "abandoned" and "superseded" in out


def test_cli_task_assign(capsys):
    from coord_engine import okf
    t = FakeTransport()
    cli.main(["task", "start", "r", "A"], transport=t)
    assert cli.main(["task", "assign", "r", "a", "codex:h:r"], transport=t) == 0
    assert okf.parse_frontmatter(t.store["team/r/task/a.md"])["assignee"] == "codex:h:r"


def test_cli_task_assign_clears_needs_human_when_reassigned_away(monkeypatch):
    from coord_engine import okf
    monkeypatch.setenv("FULCRA_COORD_HUMAN", "ash")
    t = FakeTransport()
    cli.main(["task", "start", "r", "H", "--status", "active"], transport=t)
    cli.main(["task", "block", "r", "h", "--on-user", "decide"], transport=t)
    assert cli.main(["task", "assign", "r", "h", "agent-b"], transport=t) == 0
    fm = okf.parse_frontmatter(t.store["team/r/task/h.md"])
    assert fm["assignee"] == "agent-b"
    assert "needs:human" not in fm["tags"]


def test_cli_task_assign_keeps_needs_human_when_reassigned_to_human(monkeypatch):
    from coord_engine import okf
    monkeypatch.setenv("FULCRA_COORD_HUMAN", "ash")
    t = FakeTransport()
    cli.main(["task", "start", "r", "Keep", "--status", "active"], transport=t)
    cli.main(["task", "block", "r", "keep", "--on-user", "decide"], transport=t)
    assert cli.main(["task", "assign", "r", "keep", "ash"], transport=t) == 0
    fm = okf.parse_frontmatter(t.store["team/r/task/keep.md"])
    assert fm["assignee"] == "ash"
    assert "needs:human" in fm["tags"]


def test_cli_task_abandon_terminal_blocks_further(capsys):
    t = FakeTransport()
    cli.main(["task", "start", "r", "Z"], transport=t)
    cli.main(["task", "abandon", "r", "z", "-r", "nope"], transport=t)
    capsys.readouterr()
    assert cli.main(["task", "assign", "r", "z", "x"], transport=t) == 0  # assign w/o status change ok on terminal
    assert cli.main(["task", "pause", "r", "z", "-n", "x"], transport=t) == 1  # no transition out of terminal


def test_cli_task_abandon_note_says_reason(capsys):
    t = FakeTransport()
    cli.main(["task", "start", "r", "R"], transport=t)
    cli.main(["task", "abandon", "r", "r", "-r", "superseded"], transport=t)
    assert "(reason: superseded)" in t.store["team/r/task/r.md"]


def test_cli_task_block_rejects_both_flags(capsys):
    t = FakeTransport()
    cli.main(["task", "start", "r", "B", "--status", "active"], transport=t)
    capsys.readouterr()
    assert cli.main(["task", "block", "r", "b", "--blocked-on", "x", "--on-user", "y"], transport=t) == 1
    assert "not both" in capsys.readouterr().err


def test_cli_presence_beat_show_agents(capsys):
    import json as _j
    t = FakeTransport()
    assert cli.main(["presence", "beat", "r", "-a", "amy", "-w", "web", "-s", "shipping"], transport=t) == 0
    assert cli.main(["presence", "beat", "r", "-a", "bob"], transport=t) == 0
    capsys.readouterr()
    assert cli.main(["presence", "show", "r", "--json"], transport=t) == 0
    ros = _j.loads(capsys.readouterr().out)
    assert [x["agent"] for x in ros] == ["amy", "bob"]
    assert all(x["liveness"] == "live" for x in ros)
    # agents digest folds aggregate rows + presence
    cli.main(["task", "start", "r", "W", "--status", "active", "--assignee", "amy"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    assert cli.main(["agents", "r", "--json"], transport=t) == 0
    dig = {a["agent"]: a for a in _j.loads(capsys.readouterr().out)}
    assert dig["amy"]["open"].get("active") == 1
    assert dig["bob"]["open"] == {}


def test_cli_roles_claim_release_roundtrip(capsys):
    import json as _j
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md", "---\ntype: Role\npolicy: shared\nsla_hours: 24\n---\n")
    assert cli.main(["roles", "claim", "r", "reviewer", "-a", "amy"], transport=t) == 0
    capsys.readouterr()
    cli.main(["roles", "status", "r", "reviewer", "--json"], transport=t)
    assert _j.loads(capsys.readouterr().out)["status"] == "HELD"
    assert cli.main(["roles", "release", "r", "reviewer", "-a", "amy"], transport=t) == 0
    capsys.readouterr()
    cli.main(["roles", "status", "r", "reviewer", "--json"], transport=t)
    assert _j.loads(capsys.readouterr().out)["status"] == "VACANT"


def test_cli_roles_release_without_lease_errors(capsys):
    t = FakeTransport()
    assert cli.main(["roles", "release", "r", "reviewer", "-a", "ghost"], transport=t) == 1
    assert "no lease" in capsys.readouterr().err


def test_agent_key_collision_safe():
    from coord_engine import tasks
    a, b = "claude-code:host:repo", "claude_code/host/repo"
    assert tasks.slugify(a) == tasks.slugify(b)          # the lossy collision
    assert tasks.agent_key(a) != tasks.agent_key(b)      # keys stay distinct


def test_cli_presence_colliding_ids_both_survive(capsys):
    import json as _j
    t = FakeTransport()
    cli.main(["presence", "beat", "r", "-a", "claude-code:host:repo"], transport=t)
    cli.main(["presence", "beat", "r", "-a", "claude_code/host/repo"], transport=t)
    capsys.readouterr()
    cli.main(["presence", "show", "r", "--json"], transport=t)
    ros = _j.loads(capsys.readouterr().out)
    assert len(ros) == 2                                  # no silent clobber


def test_cli_tell_inbox_ack_flow(capsys):
    import json as _j
    t = FakeTransport()
    do_slug = _dslug("Do the thing", assignee="amy")
    all_slug = _dslug("All hands", assignee="*")
    assert cli.main(["tell", "r", "amy", "Do the thing", "-p", "P1", "--from", "boss"], transport=t) == 0
    cli.main(["broadcast", "r", "All hands"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    cli.main(["inbox", "r", "-a", "amy", "--json"], transport=t)
    got = {r["name"] for r in _j.loads(capsys.readouterr().out)}
    assert got == {do_slug, all_slug}
    # ack the direct one -> disappears for amy, broadcast still there
    cli.main(["inbox", "r", "-a", "amy", "--ack", do_slug], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    cli.main(["inbox", "r", "-a", "amy", "--json"], transport=t)
    assert {r["name"] for r in _j.loads(capsys.readouterr().out)} == {all_slug}
    # bob sees only the broadcast (do-the-thing is amy's)
    cli.main(["inbox", "r", "-a", "bob", "--json"], transport=t)
    assert [r["name"] for r in _j.loads(capsys.readouterr().out)] == [all_slug]


def test_cli_inbox_ack_hides_before_reconcile(capsys):
    import json as _j
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "Immediate hide"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()

    cli.main(["inbox", "r", "-a", "amy", "--ack",
              _dslug("Immediate hide", assignee="amy")], transport=t)
    capsys.readouterr()
    cli.main(["inbox", "r", "-a", "amy", "--json"], transport=t)
    assert _j.loads(capsys.readouterr().out) == []


def test_cli_remind_hidden_until_when(capsys):
    import json as _j
    t = FakeTransport()
    cli.main(["remind", "r", "amy", "2026-12-01T00:00:00Z", "Future chore"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    cli.main(["inbox", "r", "-a", "amy", "--json"], transport=t)
    assert _j.loads(capsys.readouterr().out) == []          # gated by not_before
    assert cli.main(["remind", "r", "amy", "bogus", "X"], transport=t) == 1


def test_cli_later_backlog_only_with_all(capsys):
    import json as _j
    t = FakeTransport()
    cli.main(["later", "r", "Someday idea"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    cli.main(["inbox", "r", "-a", "@backlog", "--json"], transport=t)
    assert _j.loads(capsys.readouterr().out) == []
    cli.main(["inbox", "r", "-a", "@backlog", "--all", "--json"], transport=t)
    assert [r["name"] for r in _j.loads(capsys.readouterr().out)] == [
        _dslug("Someday idea", assignee="@backlog")]


def test_cli_handoff_atomic_single_write(capsys):
    from coord_engine import okf
    t = FakeTransport()
    cli.main(["task", "start", "r", "H", "--status", "active"], transport=t)
    writes = []
    orig = t.write
    t.write = lambda p, c: (writes.append(p), orig(p, c))[1]
    assert cli.main(["handoff", "r", "h", "--to", "bob", "--checkpoint", "CHK-1", "-n", "resume"], transport=t) == 0
    assert writes == ["team/r/task/h.md"]                    # ONE write: atomic
    fm = okf.parse_frontmatter(t.store["team/r/task/h.md"])
    assert fm["assignee"] == "bob" and fm["checkpoint_ref"] == "CHK-1"


def test_cli_respond_closes_and_records(capsys):
    from coord_engine import okf
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "Question"], transport=t)
    capsys.readouterr()
    slug = _dslug("Question", assignee="amy")
    assert cli.main(["respond", "r", slug, "-o", "answered", "-a", "amy"], transport=t) == 0
    out = capsys.readouterr().out
    assert "closed" in out
    assert "response recorded — the owner's listen surfaces it" in out  # reply-leg breadcrumb
    assert okf.parse_frontmatter(t.store[f"team/r/task/{slug}.md"])["status"] == "done"
    assert any(p.startswith(f"team/r/_coord/responses/{slug}/") for p in t.store)


def test_cli_respond_fails_loud_on_unresolved_directive(capsys):
    """A name that resolves to no directive doc must FAIL rc-1 and write NO
    response shard. A slugified display-title matches no hash-suffixed slug, and
    the old code recorded a ghost response (rc 0) while the real directive stayed
    open in needs-me forever — fail-loud, same doctrine as review status."""
    t = FakeTransport()
    # nothing created: 'a-display-title' resolves to no task doc
    rc = cli.main(["respond", "r", "a-display-title", "-o", "answered", "-a", "amy"],
                  transport=t)
    assert rc == 1
    err = capsys.readouterr().err
    assert "a-display-title" in err  # the message names the unresolved directive
    # crucially: NO ghost response shard was written
    assert not any(p.startswith("team/r/_coord/responses/") for p in t.store)


def test_cli_respond_response_paths_do_not_collide(monkeypatch, capsys):
    from datetime import datetime, timezone
    t = FakeTransport()
    cli.main(["task", "start", "r", "Waiting", "--status", "waiting"], transport=t)
    fixed = datetime(2026, 7, 2, 13, 30, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(cli, "_now", lambda: fixed)

    assert cli.main(["respond", "r", "waiting", "-o", "noted", "-a", "amy"], transport=t) == 0
    assert cli.main(["respond", "r", "waiting", "-o", "noted", "-a", "bob"], transport=t) == 0
    capsys.readouterr()
    paths = [p for p in t.store if p.startswith("team/r/_coord/responses/waiting/")]
    assert len(paths) == 2


# --- listen breadcrumbs: every ask points at the reply/verdict leg -----------

def test_tell_prints_replies_breadcrumb_when_sender_known(capsys):
    t = FakeTransport()
    assert cli.main(["tell", "r", "amy", "Do it", "--from", "boss"], transport=t) == 0
    out = capsys.readouterr().out
    assert "replies: coord-engine listen r --agent boss" in out


def test_broadcast_prints_replies_breadcrumb_when_sender_known(capsys):
    t = FakeTransport()
    assert cli.main(["broadcast", "r", "All hands", "--from", "boss"], transport=t) == 0
    assert "replies: coord-engine listen r --agent boss" in capsys.readouterr().out


def test_remind_prints_replies_breadcrumb_when_sender_known(capsys):
    t = FakeTransport()
    assert cli.main(["remind", "r", "amy", "2h", "Soon", "--from", "boss"], transport=t) == 0
    assert "replies: coord-engine listen r --agent boss" in capsys.readouterr().out


def test_tell_sender_from_env_when_no_from_flag(capsys, monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_AGENT", "envboss")
    t = FakeTransport()
    assert cli.main(["tell", "r", "amy", "Env sender"], transport=t) == 0
    assert "replies: coord-engine listen r --agent envboss" in capsys.readouterr().out


def test_tell_no_breadcrumb_when_sender_anonymous(capsys, monkeypatch):
    # No --from and no FULCRA_COORD_AGENT: only the host fallback exists, which is
    # not an identity anyone listens as -> print NO breadcrumb (a hostname would
    # mislead the reader into `listen --agent coord-reconcile:...`).
    monkeypatch.delenv("FULCRA_COORD_AGENT", raising=False)
    t = FakeTransport()
    assert cli.main(["tell", "r", "amy", "Anon"], transport=t) == 0
    assert "replies:" not in capsys.readouterr().out


def test_later_backlog_has_no_replies_breadcrumb(capsys):
    # A @backlog capture is not an ask awaiting a reply -> no breadcrumb even with --from.
    t = FakeTransport()
    assert cli.main(["later", "r", "Someday", "--from", "boss"], transport=t) == 0
    assert "replies:" not in capsys.readouterr().out


def test_review_request_prints_await_verdicts_breadcrumb(capsys):
    t = FakeTransport()
    assert cli.main(["review", "request", "r", "pr-9", "--of", "url",
                     "--reviewer", "alice", "--from", "boss"], transport=t) == 0
    assert "await verdicts: coord-engine listen r --agent boss" in capsys.readouterr().out


def test_reconcile_gcs_orphaned_ack_shards(capsys):
    t = FakeTransport()
    t.put("team/r/task/live.md", "---\ntype: Task\ntitle: L\nstatus: active\nassignee: amy\n---\n")
    t.put("team/r/_coord/acks/live/amy.md", "---\ntype: Ack\nagent: amy\n---\n")
    t.put("team/r/_coord/acks/ghost/amy.md",
          "---\ntype: Ack\nagent: amy\ntimestamp: 2020-01-01T00:00:00Z\n---\n")
    cli.main(["reconcile", "r"], transport=t)
    assert "team/r/_coord/acks/ghost/amy.md" not in t.store   # old+datable -> GC'd
    assert "team/r/_coord/acks/live/amy.md" in t.store        # kept
    import json as _j
    agg = _j.loads(t.store["team/r/_coord/summaries.json"])
    row = next(r for r in agg["rows"] if r["name"] == "live")
    assert row["acked_by"] == ["amy"]


def test_reconcile_gc_grace_protects_recent_and_undatable(capsys):
    # GC only deletes datable shards older than the grace window
    t = FakeTransport()
    t.put("team/r/task/live.md", "---\ntype: Task\ntitle: L\nstatus: active\n---\n")
    t.put("team/r/_coord/acks/ghost/old.md",
          "---\ntype: Ack\nagent: old\ntimestamp: 2020-01-01T00:00:00Z\n---\n")
    t.put("team/r/_coord/acks/ghost/recent.md",
          f"---\ntype: Ack\nagent: recent\ntimestamp: {_now_iso()}\n---\n")
    t.put("team/r/_coord/acks/ghost/undated.md", "---\ntype: Ack\nagent: undated\n---\n")
    cli.main(["reconcile", "r"], transport=t)
    assert "team/r/_coord/acks/ghost/old.md" not in t.store       # old + datable -> GC'd
    assert "team/r/_coord/acks/ghost/recent.md" in t.store        # recent -> kept
    assert "team/r/_coord/acks/ghost/undated.md" in t.store       # undatable -> kept


def test_reconcile_gc_skips_when_no_live_tasks(capsys):
    # catastrophic/empty listing must never trigger GC
    t = FakeTransport()
    t.put("team/r/_coord/acks/ghost/old.md",
          "---\ntype: Ack\nagent: old\ntimestamp: 2020-01-01T00:00:00Z\n---\n")
    cli.main(["reconcile", "r"], transport=t)
    assert "team/r/_coord/acks/ghost/old.md" in t.store


def test_cli_inbox_ack_hides_immediately_pre_reconcile(capsys):
    import json as _j
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "Quick"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    cli.main(["inbox", "r", "-a", "amy", "--ack",
              _dslug("Quick", assignee="amy")], transport=t)
    capsys.readouterr()
    # NO reconcile between ack and read — live self-hide must apply
    cli.main(["inbox", "r", "-a", "amy", "--json"], transport=t)
    assert _j.loads(capsys.readouterr().out) == []


# --- Task 2.5: live-freshness overlay (the PR348 between-reconciles false-clear) ---

def test_cli_inbox_overlay_surfaces_fresh_directive_before_reconcile(capsys):
    """The exact repro: a directive delivered BETWEEN reconciles (task doc present,
    summaries index stale) is surfaced by inbox immediately via the overlay — no
    heartbeat rebuild required."""
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "Old news", "--from", "boss"], transport=t)
    cli.main(["reconcile", "r"], transport=t)            # summaries now has old-news
    # NEW directive between reconciles: task doc written, index NOT rebuilt
    cli.main(["tell", "r", "amy", "Fresh work", "--from", "boss"], transport=t)
    fresh = _dslug("Fresh work", assignee="amy")
    capsys.readouterr()
    cli.main(["inbox", "r", "-a", "amy", "--json"], transport=t)
    names = {r["name"] for r in json.loads(capsys.readouterr().out)}
    assert fresh in names                                # overlay surfaced it, no reconcile


def test_cli_inbox_overlay_no_duplicate_for_indexed_doc(capsys):
    """A doc present in BOTH the index and the task dir yields exactly one row —
    the index row wins, the overlay never re-reads it."""
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "Do it", "--from", "boss"], transport=t)
    cli.main(["reconcile", "r"], transport=t)            # now in index AND task dir
    capsys.readouterr()
    cli.main(["inbox", "r", "-a", "amy", "--json"], transport=t)
    names = [r["name"] for r in json.loads(capsys.readouterr().out)]
    assert names.count(_dslug("Do it", assignee="amy")) == 1


def test_cli_inbox_overlay_skips_unparseable_doc(capsys):
    """A fresh doc that won't parse as a Task is skipped-not-fatal; other fresh docs
    are still served."""
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "Anchor", "--from", "boss"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    cli.main(["tell", "r", "amy", "Fresh good", "--from", "boss"], transport=t)
    good = _dslug("Fresh good", assignee="amy")
    t.put("team/r/task/broken.md", "no frontmatter fence here — unparseable")
    capsys.readouterr()
    assert cli.main(["inbox", "r", "-a", "amy", "--json"], transport=t) == 0  # no crash
    names = {r["name"] for r in json.loads(capsys.readouterr().out)}
    assert good in names and "broken" not in names


def test_load_rows_overlay_listing_failure_degrades_not_silent():
    """Overlay task-dir listing raises while the summaries read succeeds: the index
    rows are STILL served and ``ok`` flips False so the caller surfaces the
    degradation (never a silent empty) — attributed to the OVERLAY, not the index."""
    from coord_engine.transport import TransportError
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "Indexed", "--from", "boss"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    indexed = _dslug("Indexed", assignee="amy")
    orig_list = t.list_dir
    def boom_on_task(prefix):
        if prefix == "team/r/task/":
            raise TransportError("overlay boom")
        return orig_list(prefix)
    t.list_dir = boom_on_task
    rows, ok, reason = cli._load_rows_status(t, "r")
    assert ok is False                                   # degraded, not silent
    assert "task-dir overlay" in reason                  # NOT "summaries index unreadable"
    assert {r["name"] for r in rows} == {indexed}        # index rows still served


def test_load_rows_no_summaries_is_unchanged_no_overlay():
    """A fresh team (no summaries yet) keeps the existing full-listing fallback: the
    overlay only runs when the index is present — absent index stays empty+readable,
    the reconcile-first contract unchanged."""
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "No index yet", "--from", "boss"], transport=t)
    rows, ok, reason = cli._load_rows_status(t, "r")     # NO reconcile: index absent
    assert rows == [] and ok is True and reason == ""    # unchanged: absence != failure


def test_load_rows_overlay_listed_doc_read_failure_degrades_not_silent():
    """MUST 1: a doc the overlay's OWN listing just proved exists but that reads as
    None must NOT vanish silently (the false-clear class, at the overlay's read
    step) — ``ok`` flips False with overlay attribution; the index rows AND the
    other readable fresh docs are still served. Parse-garbage stays a sanctioned
    silent skip (separate test)."""
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "Indexed", "--from", "boss"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    indexed = _dslug("Indexed", assignee="amy")
    cli.main(["tell", "r", "amy", "Fresh readable", "--from", "boss"], transport=t)
    readable = _dslug("Fresh readable", assignee="amy")
    cli.main(["tell", "r", "amy", "Fresh unreadable", "--from", "boss"], transport=t)
    ghost = _dslug("Fresh unreadable", assignee="amy")
    ghost_path = f"team/r/task/{ghost}.md"
    orig_read = t.read
    t.read = lambda p: None if p == ghost_path else orig_read(p)  # listed, unreadable
    rows, ok, reason = cli._load_rows_status(t, "r")
    assert ok is False                                   # degraded, never a silent vanish
    assert "task-dir overlay" in reason and f"{ghost}.md" in reason
    names = {r["name"] for r in rows}
    assert indexed in names and readable in names        # everything readable still served
    assert ghost not in names


def test_load_rows_overlay_unparseable_stays_silent_skip():
    """Sanctioned silent skip preserved: a fresh doc that READS fine but is
    parse-garbage / not a Task does not degrade (mirrors reconcile's tolerance)."""
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "Anchor", "--from", "boss"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    t.put("team/r/task/broken.md", "no frontmatter fence here — unparseable")
    t.put("team/r/task/note.md", "---\ntype: Reference\n---\nnot a task")
    rows, ok, reason = cli._load_rows_status(t, "r")
    assert ok is True and reason == ""                   # skip, not an outage
    assert "broken" not in {r["name"] for r in rows}


def _put_fresh_docs(t, n):
    """n fresh (post-reconcile, absent-from-index) directive docs for amy."""
    for i in range(n):
        t.put(f"team/r/task/fresh-{i:02d}.md",
              f"---\ntype: Task\nid: fresh-{i:02d}\ntitle: F{i}\nstatus: proposed\n"
              f"priority: P2\nowner: boss\nassignee: amy\n---\nbody")


def test_overlay_cap_truncates_deterministically_and_degrades():
    """MUST 2: the overlay read-cost is bounded when reconcile is down. 20 fresh
    docs + default cap 16 -> exactly the first 16 BY SORTED NAME are served (every
    agent converges on the same subset) and the fold degrades visibly with the
    {served, absent_total} counts — capped-but-visible, never silent truncation."""
    t = FakeTransport()
    cli.main(["reconcile", "r"], transport=t)            # index present (empty)
    _put_fresh_docs(t, 20)
    rows, ok, reason = cli._load_rows_status(t, "r")
    assert ok is False                                   # truncation is degraded, not silent
    assert "truncated" in reason and "16 of 20" in reason
    served = {r["name"] for r in rows}
    assert served == {f"fresh-{i:02d}" for i in range(16)}   # sorted-name determinism


def test_overlay_read_failure_reason_survives_cap_truncation():
    """Read failure and cap truncation must both remain visible."""
    t = FakeTransport()
    cli.main(["reconcile", "r"], transport=t)
    _put_fresh_docs(t, 20)
    orig_read = t.read
    t.read = lambda p: None if p == "team/r/task/fresh-03.md" else orig_read(p)

    rows, ok, reason = cli._load_rows_status(t, "r")

    assert ok is False
    assert "fresh-03.md unreadable" in reason
    assert "truncated" in reason and "16 of 20" in reason
    assert {r["name"] for r in rows} == (
        {f"fresh-{i:02d}" for i in range(16)} - {"fresh-03"}
    )


def test_overlay_cap_env_override_honored(monkeypatch):
    t = FakeTransport()
    cli.main(["reconcile", "r"], transport=t)
    _put_fresh_docs(t, 8)
    monkeypatch.setenv("COORD_OVERLAY_CAP", "5")
    rows, ok, reason = cli._load_rows_status(t, "r")
    assert ok is False and "5 of 8" in reason
    assert {r["name"] for r in rows} == {f"fresh-{i:02d}" for i in range(5)}
    # bad env values never disable the bound: fall back to the default
    monkeypatch.setenv("COORD_OVERLAY_CAP", "bananas")
    assert cli._overlay_cap() == cli.DEFAULT_OVERLAY_CAP
    monkeypatch.setenv("COORD_OVERLAY_CAP", "0")
    assert cli._overlay_cap() == cli.DEFAULT_OVERLAY_CAP


def test_overlay_under_cap_unchanged_no_flag():
    """At-or-under the cap nothing changes: all fresh docs served, no degradation.
    (Also the healthy-under-budget guard: runs under the default 10s
    COORD_OVERLAY_BUDGET — fast reads never trip it.)"""
    t = FakeTransport()
    cli.main(["reconcile", "r"], transport=t)
    _put_fresh_docs(t, 3)
    rows, ok, reason = cli._load_rows_status(t, "r")
    assert ok is True and reason == ""
    assert {r["name"] for r in rows} == {"fresh-00", "fresh-01", "fresh-02"}


def test_overlay_budget_stops_slow_reads_partial_served(monkeypatch):
    """MUST (whole-branch review): the cap bounds read COUNT, not TIME — slow
    per-doc reads (each running toward the transport's subprocess timeout) must not
    starve every canonical surface read. A tiny budget + reads that sleep past it →
    the overlay stops after the first slow read (after-op discipline), serves what
    it got plus the index rows, and degrades with the budget reason."""
    import time as _time
    t = FakeTransport()
    cli.main(["reconcile", "r"], transport=t)            # index present (empty)
    _put_fresh_docs(t, 3)
    orig_read = t.read
    def slow_read(path):
        if "/task/fresh-" in path:
            _time.sleep(0.05)                            # each read blows the budget
        return orig_read(path)
    t.read = slow_read
    monkeypatch.setenv("COORD_OVERLAY_BUDGET", "0.01")
    t0 = _time.monotonic()
    rows, ok, reason = cli._load_rows_status(t, "r")
    elapsed = _time.monotonic() - t0
    assert elapsed < 1.0                                 # order-of-magnitude of the budget,
    assert ok is False                                   # not 3 serial timeouts
    assert "budget exhausted" in reason and "1 of 3" in reason
    assert {r["name"] for r in rows} == {"fresh-00"}     # everything read so far served


def test_overlay_budget_fast_none_keeps_unreadable_degrade(monkeypatch):
    """A FAST None (doc deleted between list and read — returns quickly) keeps the
    continue-and-degrade behavior under a small budget: all readables served, and
    the degrade reason is the unreadable one, not a budget breach."""
    t = FakeTransport()
    cli.main(["reconcile", "r"], transport=t)
    _put_fresh_docs(t, 3)
    orig_read = t.read
    t.read = lambda p: None if p == "team/r/task/fresh-01.md" else orig_read(p)
    monkeypatch.setenv("COORD_OVERLAY_BUDGET", "5")      # small but ample for fast reads
    rows, ok, reason = cli._load_rows_status(t, "r")
    assert ok is False
    assert "fresh-01.md unreadable" in reason and "budget" not in reason
    assert {r["name"] for r in rows} == {"fresh-00", "fresh-02"}


def test_overlay_budget_env_fallback():
    """Bad COORD_OVERLAY_BUDGET values never disable the bound."""
    import os as _os
    for bad in ("bananas", "0", "-3", "inf", "nan"):
        _os.environ["COORD_OVERLAY_BUDGET"] = bad
        try:
            assert cli._overlay_budget() == cli.DEFAULT_OVERLAY_BUDGET, bad
        finally:
            del _os.environ["COORD_OVERLAY_BUDGET"]
    assert cli._overlay_budget() == cli.DEFAULT_OVERLAY_BUDGET


def test_parse_when_date_only_gates_until_end_of_day():
    from coord_engine import directives
    assert directives.parse_when("2026-07-02", now="2026-07-02T12:00:00Z") == "2026-07-02T23:59:59Z"


def _old_done_task(title):
    return (f"---\ntype: Task\ntitle: {title}\nid: {title.lower()}\nstatus: done\n"
            f"timestamp: 2020-01-15T00:00:00Z\n---\nold body")


def test_retention_archives_old_terminal_and_moves_shards(capsys):
    import json as _j
    t = FakeTransport()
    t.put("team/r/task/olddone.md", _old_done_task("Olddone"))
    t.put("team/r/task/fresh.md", "---\ntype: Task\ntitle: F\nstatus: active\n---\n")
    t.put("team/r/_coord/acks/olddone/amy-abc123.md",
          "---\ntype: Ack\nagent: amy\ntimestamp: 2020-01-16T00:00:00Z\n---\n")
    import os
    assert cli.main(["reconcile", "r", "--retention-days", "30"], transport=t) == 0
    # task doc moved to archive/<YYYY-MM>/, original gone
    assert "team/r/task/olddone.md" not in t.store
    assert "team/r/task/archive/2020-01/olddone.md" in t.store
    # shards moved with it
    assert "team/r/_coord/archive/acks/olddone/amy-abc123.md" in t.store
    assert "team/r/_coord/acks/olddone/amy-abc123.md" not in t.store
    # index/aggregate exclude it
    agg = _j.loads(t.store["team/r/_coord/summaries.json"])
    assert {r["name"] for r in agg["rows"]} == {"fresh"}
    # log says Archived (with a live archive link), never "removed" w/ a dead link
    log = t.store.get("team/r/task/log.md", "")
    assert "**Archived**" in log and "archive/2020-01/olddone.md" in log
    assert "olddone.md) removed" not in log


def test_retention_keeps_malformed_timestamp_hot(capsys):
    t = FakeTransport()
    t.put("team/r/task/weird.md",
          "---\ntype: Task\ntitle: W\nstatus: done\ntimestamp: not-a-date\n---\n")
    cli.main(["reconcile", "r", "--retention-days", "30"], transport=t)
    assert "team/r/task/weird.md" in t.store            # kept hot, no garbage bucket
    assert not any("task/archive/not-a-d" in p for p in t.store)


def test_retention_off_by_default_and_daily_throttle(capsys):
    t = FakeTransport()
    t.put("team/r/task/olddone.md", _old_done_task("Olddone"))
    cli.main(["reconcile", "r"], transport=t)                      # no flag/env -> no archival
    assert "team/r/task/olddone.md" in t.store
    cli.main(["reconcile", "r", "--retention-days", "30"], transport=t)
    assert "team/r/task/olddone.md" not in t.store
    # marker written; a same-day second pass is a no-op (throttle)
    t.put("team/r/task/olddone2.md", _old_done_task("Olddone2"))
    cli.main(["reconcile", "r", "--retention-days", "30"], transport=t)
    assert "team/r/task/olddone2.md" in t.store                    # throttled today


def test_retention_throttles_zero_archive_days_after_prior_marker(capsys):
    import json as _j
    t = FakeTransport()
    t.put("team/r/_coord/retention/last-run.json",
          _j.dumps({"last_run": "2026-07-01", "archived": 9}))
    t.put("team/r/task/fresh.md", "---\ntype: Task\ntitle: F\nstatus: active\n---\n")

    cli.main(["reconcile", "r", "--retention-days", "30"], transport=t)
    marker = _j.loads(t.store["team/r/_coord/retention/last-run.json"])
    assert marker["last_run"] != "2026-07-01"
    assert marker["archived"] == 0


def test_retention_ignores_unparseable_timestamps(capsys):
    t = FakeTransport()
    t.put("team/r/task/badtime.md",
          "---\ntype: Task\ntitle: Badtime\nstatus: done\ntimestamp: not-a-time\n---\n")

    cli.main(["reconcile", "r", "--retention-days", "30"], transport=t)
    assert "team/r/task/badtime.md" in t.store
    assert not any(p.startswith("team/r/task/archive/not-a-") for p in t.store)


def test_retention_keeps_hot_task_when_shard_move_fails(capsys):
    class FailShardArchiveTransport(FakeTransport):
        def write(self, path, content):
            if path.startswith("team/r/_coord/archive/acks/"):
                return False
            return super().write(path, content)

    t = FailShardArchiveTransport()
    t.put("team/r/task/olddone.md", _old_done_task("Olddone"))
    t.put("team/r/_coord/acks/olddone/amy-abc123.md",
          "---\ntype: Ack\nagent: amy\ntimestamp: 2020-01-16T00:00:00Z\n---\n")

    cli.main(["reconcile", "r", "--retention-days", "30"], transport=t)
    assert "team/r/task/olddone.md" in t.store
    assert "team/r/task/archive/2020-01/olddone.md" not in t.store
    assert "team/r/_coord/acks/olddone/amy-abc123.md" in t.store


def _old_task(title, status):
    return (f"---\ntype: Task\ntitle: {title}\nid: {title.lower()}\nstatus: {status}\n"
            "timestamp: 2020-01-15T00:00:00Z\n---\nold body")


def _old_verdict(reviewer, verdict="approve"):
    return ("---\ntype: Verdict\n"
            f"reviewer: {reviewer}\nverdict: {verdict}\n"
            "timestamp: 2020-01-16T00:00:00Z\n---\n")


def test_retention_archives_old_proposed_but_never_active_or_waiting(capsys):
    t = FakeTransport()
    t.put("team/r/task/old-proposed.md", _old_task("Old-proposed", "proposed"),
          mtime="2020-01-15 12:00PM UTC")
    t.put("team/r/task/recent-proposed.md", _old_task("Recent-proposed", "proposed"))
    t.put("team/r/task/old-active.md", _old_task("Old-active", "active"))
    t.put("team/r/task/old-waiting.md", _old_task("Old-waiting", "waiting"))

    assert cli.main(["reconcile", "r", "--retention-days", "30"], transport=t) == 0

    assert "team/r/task/old-proposed.md" not in t.store
    assert "team/r/task/archive/2020-01/old-proposed.md" in t.store
    assert "team/r/task/recent-proposed.md" in t.store
    assert "team/r/task/old-active.md" in t.store
    assert "team/r/task/old-waiting.md" in t.store


def test_retention_archives_only_single_codex_reviewer_orphan(capsys):
    t = FakeTransport()
    # Eligible: no review doc, exactly one old codex-reviewer verdict.
    t.put("team/r/review/settled/verdicts/codex-reviewer.md",
          _old_verdict("codex-reviewer"), mtime="2020-01-16 12:00PM UTC")
    t.put("team/r/review/recent/verdicts/codex-reviewer.md",
          _old_verdict("codex-reviewer"))
    # Multi-reviewer and non-codex singletons are never settled-single reviews.
    t.put("team/r/review/multi/verdicts/codex-reviewer.md",
          _old_verdict("codex-reviewer"))
    t.put("team/r/review/multi/verdicts/coord-maintainer.md",
          _old_verdict("coord-maintainer"))
    t.put("team/r/review/other/verdicts/coord-maintainer.md",
          _old_verdict("coord-maintainer"))
    # A live review doc excludes the directory even when its verdict shape matches.
    t.put("team/r/review/live.md", "---\ntype: Review\nrequired: codex-reviewer\n---\n")
    t.put("team/r/review/live/verdicts/codex-reviewer.md",
          _old_verdict("codex-reviewer"))

    assert cli.main(["reconcile", "r", "--retention-days", "30"], transport=t) == 0

    src = "team/r/review/settled/verdicts/codex-reviewer.md"
    dst = ("team/r/_coord/archive/reviews/2020-01/settled/verdicts/"
           "codex-reviewer.md")
    assert src not in t.store and dst in t.store
    assert "team/r/review/recent/verdicts/codex-reviewer.md" in t.store
    assert "team/r/review/multi/verdicts/codex-reviewer.md" in t.store
    assert "team/r/review/multi/verdicts/coord-maintainer.md" in t.store
    assert "team/r/review/other/verdicts/coord-maintainer.md" in t.store
    assert "team/r/review/live/verdicts/codex-reviewer.md" in t.store


def test_retention_orphan_verdict_listing_raise_is_loud_and_non_destructive(capsys):
    class FailVerdictListingTransport(FakeTransport):
        def list_dir(self, prefix):
            if prefix == "team/r/review/unknown/verdicts/":
                from coord_engine.transport import TransportError
                raise TransportError("verdict listing unavailable")
            return super().list_dir(prefix)

    t = FailVerdictListingTransport()
    src = "team/r/review/unknown/verdicts/codex-reviewer.md"
    t.put(src, _old_verdict("codex-reviewer"), mtime="2020-01-16 12:00PM UTC")

    assert cli.main(["reconcile", "r", "--retention-days", "30"], transport=t) == 0
    cap = capsys.readouterr()
    assert src in t.store
    assert not any("_coord/archive/reviews/" in p for p in t.store)
    assert "unknown" in cap.err and "listing" in cap.err


def test_review_restore_moves_archived_orphan_verdict_back(capsys):
    t = FakeTransport()
    hot = "team/r/review/settled/verdicts/codex-reviewer.md"
    cold = ("team/r/_coord/archive/reviews/2020-01/settled/verdicts/"
            "codex-reviewer.md")
    t.put(hot, _old_verdict("codex-reviewer"), mtime="2020-01-16 12:00PM UTC")
    cli.main(["reconcile", "r", "--retention-days", "30"], transport=t)
    assert hot not in t.store and cold in t.store

    assert cli.main(["review", "restore", "r", "settled"], transport=t) == 0
    assert hot in t.store and cold not in t.store
    assert "restored review settled" in capsys.readouterr().out


def test_task_restore_and_search_archived(capsys):
    import json as _j
    t = FakeTransport()
    t.put("team/r/task/olddone.md", _old_done_task("Olddone"))
    cli.main(["reconcile", "r", "--retention-days", "30"], transport=t)
    capsys.readouterr()
    # archived doc findable only with --archived
    cli.main(["search", "r", "olddone", "--json"], transport=t)
    assert _j.loads(capsys.readouterr().out) == []
    cli.main(["search", "r", "olddone", "--archived", "--json"], transport=t)
    got = _j.loads(capsys.readouterr().out)
    assert len(got) == 1 and got[0]["archived"] == "2020-01"
    # restore brings it back
    assert cli.main(["task", "restore", "r", "olddone"], transport=t) == 0
    assert "team/r/task/olddone.md" in t.store
    assert "team/r/task/archive/2020-01/olddone.md" not in t.store
    capsys.readouterr()
    assert cli.main(["task", "restore", "r", "nope"], transport=t) == 1


def test_reconcile_writes_health_shard_and_health_folds(capsys):
    import json as _j
    t = FakeTransport()
    t.put("team/r/task/a.md", "---\ntype: Task\ntitle: A\nstatus: active\n---\n")
    cli.main(["reconcile", "r"], transport=t)
    shards = [p for p in t.store if p.startswith("team/r/_coord/health/")]
    assert len(shards) == 1
    sh = _j.loads(t.store[shards[0]])
    assert sh["schema"] == "coord.teams.health.v1" and sh["tasks"] == 1
    capsys.readouterr()
    assert cli.main(["health", "r", "--json"], transport=t) == 0
    view = _j.loads(capsys.readouterr().out)
    assert view["healthy"] is True and view["fresh"] == 1
    assert view["hosts"][0]["stale"] is False


def test_health_reports_stale_host_and_gc_prunes_ancient(capsys):
    import json as _j
    t = FakeTransport()
    t.put("team/r/task/a.md", "---\ntype: Task\ntitle: A\nstatus: active\n---\n")
    t.put("team/r/_coord/health/dead-host.json",
          _j.dumps({"schema": "coord.teams.health.v1", "host": "dead", "at": "2020-01-01T00:00:00Z"}))
    cli.main(["reconcile", "r"], transport=t)                     # writes fresh + GCs ancient
    assert "team/r/_coord/health/dead-host.json" not in t.store    # >30d -> pruned
    capsys.readouterr()
    t2 = FakeTransport()
    t2.put("team/r/_coord/health/slow.json",
           _j.dumps({"host": "slow", "at": "2026-07-01T00:00:00Z"}))  # ~1.5d old vs test now
    assert cli.main(["health", "r"], transport=t2) in (0, 1)      # renders without crash


def test_doctor_reports_and_exit_code(capsys):
    t = FakeTransport()
    assert cli.main(["doctor", "r"], transport=t) == 0            # fake store reachable
    out = capsys.readouterr().out
    assert "File Store reachable" in out and "coord-engine v" in out

    class Broken(FakeTransport):
        def list_dir(self, prefix):
            raise RuntimeError("offline")
    assert cli.main(["doctor", "r"], transport=Broken()) == 1
    assert "unreachable" in capsys.readouterr().err


def test_health_empty_fleet_reads_unhealthy(capsys):
    t = FakeTransport()
    assert cli.main(["health", "r"], transport=t) == 1     # cold-start must not read green
    assert "nobody has ever reconciled" in capsys.readouterr().out


def test_cli_digest_sections_and_store_dedupe(capsys):
    import json as _j
    t = FakeTransport()
    cli.main(["task", "start", "r", "Ask", "--status", "active"], transport=t)
    cli.main(["task", "block", "r", "ask", "--on-user", "please review"], transport=t)
    cli.main(["remind", "r", "amy", "2h", "Soon thing"], transport=t)
    cli.main(["presence", "beat", "r", "-a", "amy", "-s", "working"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    assert cli.main(["digest", "r", "--json"], transport=t) == 0
    d = _j.loads(capsys.readouterr().out)
    assert [r["name"] for r in d["blocked_on_you"]] == ["ask"]      # needs:human
    assert [r["name"] for r in d["upcoming"]] == [
        _dslug("Soon thing", assignee="amy")]                       # not_before in 7d
    assert any(a["agent"] == "amy" for a in d["per_agent"])
    # --store persists once per day+window
    cli.main(["digest", "r", "--store"], transport=t); capsys.readouterr()
    stored = [p for p in t.store if p.startswith("team/r/_coord/digests/")]
    assert len(stored) == 1
    cli.main(["digest", "r", "--store"], transport=t)
    assert "already stored" in capsys.readouterr().err


def test_cli_digest_emit_timeline_once_per_window(capsys, monkeypatch):
    t = FakeTransport()
    cli.main(["presence", "beat", "r", "-a", "amy", "-s", "working"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    emitted = []
    monkeypatch.setattr(cli, "_emit_digest_timeline",
                        lambda **kw: (emitted.append(kw), True)[1])

    assert cli.main(["digest", "r", "--emit-timeline"], transport=t) == 0
    err = capsys.readouterr().err
    # --emit-timeline implies the store-marker write (the bus-copy dedup).
    assert "stored digest" in err and "emitted digest timeline moment" in err
    assert len(emitted) == 1
    assert emitted[0]["window"] in ("morning", "evening")
    assert emitted[0]["window"] in emitted[0]["name"]
    assert emitted[0]["note"]  # the rendered digest body rides in the note
    # A confirmed emit records the .emitted state.
    assert any(p.endswith(".emitted") for p in t.store)

    # Second run in the same window: .emitted suppresses the re-emit.
    assert cli.main(["digest", "r", "--emit-timeline"], transport=t) == 0
    assert "already stored" in capsys.readouterr().err
    assert len(emitted) == 1


def test_cli_digest_emit_failure_retries_next_tick_with_same_id(capsys, monkeypatch):
    # codex P1: a transient emit failure must NOT consume the window. The
    # .emitted state is written only after a confirmed emit, so the next tick
    # retries — with the SAME deterministic record id and the STORED body, so
    # an ambiguously-acked first POST upserts instead of duplicating.
    t = FakeTransport()
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    calls = []
    outcome = {"ok": False}
    monkeypatch.setattr(cli, "_emit_digest_timeline",
                        lambda **kw: (calls.append(kw), outcome["ok"])[1])

    assert cli.main(["digest", "r", "--emit-timeline"], transport=t) == 0
    err = capsys.readouterr().err
    assert "will retry" in err, "a missed window must be loud AND retryable"
    assert any(p.endswith(".md") and "_coord/digests/" in p for p in t.store)
    assert not any(p.endswith(".emitted") for p in t.store), \
        "no emit confirmation may be recorded on failure"

    outcome["ok"] = True
    assert cli.main(["digest", "r", "--emit-timeline"], transport=t) == 0
    assert "emitted digest timeline moment" in capsys.readouterr().err
    assert len(calls) == 2
    assert calls[0]["record_id"] == calls[1]["record_id"], \
        "retry must reuse the deterministic per-window record id"
    assert calls[0]["note"] == calls[1]["note"], \
        "retry must emit the STORED window body, not a re-rendered one"
    assert any(p.endswith(".emitted") for p in t.store)

    # Third tick: confirmed — no further emit calls.
    cli.main(["digest", "r", "--emit-timeline"], transport=t)
    assert len(calls) == 2


def test_cli_digest_concurrent_hosts_converge_on_one_record_id(capsys, monkeypatch):
    # codex P1: the marker read-then-write is NOT atomic — two hosts can both
    # see the window unclaimed and both emit. Correctness lives at the
    # ingestion layer: both emits must carry the SAME deterministic record id,
    # which the typed endpoint upserts into one record (live-verified).
    ta, tb = FakeTransport(), FakeTransport()   # two hosts, isolated stores
    for t in (ta, tb):
        cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    emitted = []
    monkeypatch.setattr(cli, "_emit_digest_timeline",
                        lambda **kw: (emitted.append(kw), True)[1])

    assert cli.main(["digest", "r", "--emit-timeline"], transport=ta) == 0
    assert cli.main(["digest", "r", "--emit-timeline"], transport=tb) == 0
    assert len(emitted) == 2, "both racing hosts emit (no atomic claim exists)"
    assert emitted[0]["record_id"] == emitted[1]["record_id"], \
        "racing emits must converge on one deterministic record id"


def test_cli_digest_store_alone_does_not_emit(capsys, monkeypatch):
    t = FakeTransport()
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    calls = []
    monkeypatch.setattr(cli, "_emit_digest_timeline",
                        lambda **kw: (calls.append(1), True)[1])
    assert cli.main(["digest", "r", "--store"], transport=t) == 0
    assert calls == [], "--store alone must not touch the timeline"


def test_cli_digest_blocked_on_human_uses_token_match(capsys):
    import json as _j
    t = FakeTransport()
    t.put("team/r/task/exact.md",
          "---\ntype: Task\ntitle: Exact\nstatus: blocked\nblocked_on: ash\n---\n")
    t.put("team/r/task/list.md",
          "---\ntype: Task\ntitle: List\nstatus: blocked\nblocked_on: amy, ash\n---\n")
    t.put("team/r/task/substring.md",
          "---\ntype: Task\ntitle: Substring\nstatus: blocked\nblocked_on: trash\n---\n")
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    assert cli.main(["digest", "r", "--human", "ash", "--json"], transport=t) == 0
    d = _j.loads(capsys.readouterr().out)
    assert [r["name"] for r in d["blocked_on_you"]] == ["exact", "list"]


def test_cli_escalate_vacant_role_once_per_day(capsys):
    from coord_engine import okf
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md",
          "---\ntype: Role\npolicy: shared\nsla_hours: 24\nmaintainer: ash\n---\n")
    t.put("team/r/roles/reviewer/leases/ghost.md",
          "---\ntype: Lease\nagent: ghost\ntimestamp: 2020-01-01T00:00:00Z\n---\n")
    assert cli.main(["escalate", "r"], transport=t) == 0
    out = capsys.readouterr().out
    assert "escalated reviewer -> ash" in out
    # marker + P1 directive to maintainer exist
    assert any("escalations/" in p for p in t.store)
    slug = [p for p in t.store if p.startswith("team/r/task/role-vacant-")][0]
    fm = okf.parse_frontmatter(t.store[slug])
    assert fm["assignee"] == "ash" and fm["priority"] == "P1"
    # second sweep same day: marker dedupes
    assert cli.main(["escalate", "r"], transport=t) == 0
    assert "0 escalated" in capsys.readouterr().out


def test_cli_escalate_renotifies_next_day_with_new_directive(capsys):
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md",
          "---\ntype: Role\nsla_hours: 24\nmaintainer: ash\n---\n")
    # day-1 marker from "yesterday" + yesterday's directive already exist
    t.put("team/r/roles/reviewer/escalations/2026-07-01.md", "---\ntype: Escalation\n---\n")
    t.put("team/r/task/role-vacant-2026-07-01-reviewer-unattended-past-24h-sla.md",
          "---\ntype: Task\ntitle: old\nstatus: proposed\n---\n")
    assert cli.main(["escalate", "r"], transport=t) == 0
    out = capsys.readouterr().out
    assert "escalated reviewer -> ash" in out          # a NEW day-scoped directive
    todays = [p for p in t.store if p.startswith("team/r/task/role-vacant-") and "2026-07-01" not in p]
    assert len(todays) == 1


def test_cli_escalate_held_role_no_escalation(capsys):
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md", "---\ntype: Role\nsla_hours: 24\n---\n")
    cli.main(["roles", "claim", "r", "reviewer", "-a", "amy"], transport=t)
    capsys.readouterr()
    cli.main(["escalate", "r"], transport=t)
    assert "0 escalated" in capsys.readouterr().out


def test_cli_continuity_checkpoint_get_set_preserves_role_fields(capsys):
    from coord_engine import okf
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md",
          "---\ntype: Role\npolicy: exclusive\nsla_hours: 12\nmaintainer: ash\n---\nDuties.\n")
    assert cli.main(["continuity", "checkpoint", "r", "--role", "reviewer",
                     "--ref", "team/r/member/amy/continuity/role-reviewer/latest.json"], transport=t) == 0
    fm = okf.parse_frontmatter(t.store["team/r/roles/reviewer.md"])
    assert fm["checkpoint_ref"].endswith("latest.json")
    assert fm["policy"] == "exclusive" and fm["maintainer"] == "ash"   # preserved
    assert "Duties." in t.store["team/r/roles/reviewer.md"]            # body preserved
    capsys.readouterr()
    assert cli.main(["continuity", "checkpoint", "r", "--role", "reviewer"], transport=t) == 0
    assert "checkpoint_ref = " in capsys.readouterr().out
    assert cli.main(["continuity", "checkpoint", "r", "--role", "ghost", "--ref", "x"], transport=t) == 1


def test_cli_park_snapshots_held_roles_only(capsys):
    import json as _j
    from coord_engine import okf
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md", "---\ntype: Role\nsla_hours: 24\n---\n")
    t.put("team/r/roles/oncall.md", "---\ntype: Role\nsla_hours: 24\n---\n")
    cli.main(["roles", "claim", "r", "reviewer", "-a", "amy"], transport=t)  # amy holds reviewer only
    capsys.readouterr()
    assert cli.main(["continuity", "park", "r", "-a", "amy", "--objective", "eod"], transport=t) == 0
    out = capsys.readouterr().out
    assert "parked reviewer" in out and "oncall" not in out
    fm = okf.parse_frontmatter(t.store["team/r/roles/reviewer.md"])
    snap = _j.loads(t.store[fm["checkpoint_ref"]])
    assert snap["objective"] == "eod" and snap["agent"] == "amy"
    # parking with no held roles is a clean no-op
    assert cli.main(["continuity", "park", "r", "-a", "nobody"], transport=t) == 0


def test_cli_briefing_full_and_empty_store(capsys):
    import json as _j
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "Do it", "-p", "P1"], transport=t)
    cli.main(["presence", "beat", "r", "-a", "amy"], transport=t)
    cli.main(["continuity", "snapshot", "r", "amy", "work", "--objective", "finish"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    assert cli.main(["briefing", "r", "-a", "amy", "--json"], transport=t) == 0
    b = _j.loads(capsys.readouterr().out)
    assert b["inbox"] and b["inbox"][0]["name"] == _dslug("Do it", assignee="amy")
    assert b["resume"]["objective"] == "finish"
    assert any(p["agent"] == "amy" for p in b["presence"])
    # empty store: every section degrades gracefully
    assert cli.main(["briefing", "empty-team", "-a", "ghost"], transport=FakeTransport()) == 0


def test_cli_briefing_respects_live_ack_shards(capsys):
    import json as _j
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "Do it", "-p", "P1"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    cli.main(["inbox", "r", "-a", "amy", "--ack",
              _dslug("Do it", assignee="amy")], transport=t)
    capsys.readouterr()
    assert cli.main(["briefing", "r", "-a", "amy", "--json"], transport=t) == 0
    b = _j.loads(capsys.readouterr().out)
    assert b["inbox"] == []


def test_park_respects_per_role_sla(capsys):
    t = FakeTransport()
    t.put("team/r/roles/tight.md", "---\ntype: Role\nsla_hours: 0.001\n---\n")  # ~4s SLA
    t.put("team/r/roles/tight/leases/amy-" + __import__("hashlib").sha1(b"amy").hexdigest()[:6] + ".md",
          "---\ntype: Lease\nagent: amy\ntimestamp: 2020-01-01T00:00:00Z\n---\n")
    cli.main(["continuity", "park", "r", "-a", "amy"], transport=t)
    assert "nothing to park" in capsys.readouterr().out    # stale vs the role's OWN sla


def test_park_refuses_to_claim_nothing_when_role_state_is_unknown(capsys):
    """A blip at session exit must not read as "you hold no roles".

    park runs as a session ENDS, so a silent no-op discards the checkpoint the next
    session resumes from, and nobody is watching to catch it. Before 2026-07-17
    `_held_roles` swallowed a raised roles/ listing into `[]` and park printed
    "nothing to park" and exited 0 — the checkpoint was never written and the
    operator was told it was clean. UNKNOWN is not empty.
    """
    from coord_engine.transport import TransportError

    class ListingDown(FakeTransport):
        def list_dir(self, path):
            if path.endswith("/roles/"):
                raise TransportError("boom")
            return super().list_dir(path)

    t = ListingDown()
    t.put("team/r/roles/reviewer.md", "---\ntype: Role\n---\n")
    rc = cli.main(["continuity", "park", "r", "-a", "amy"], transport=t)
    cap = capsys.readouterr()
    assert rc == 1, f"park must fail loud on unknown role state, got rc={rc}"
    assert "nothing to park" not in cap.out, "must NOT claim nothing to park"
    assert "CHECKPOINT NOT WRITTEN" in cap.err, cap.err
    assert not [p for p in t.store if "/continuity/" in p], "nothing may be written"


def test_park_unreadable_role_doc_is_unknown_not_no_roles(capsys):
    """A role the listing PROVES exists, whose doc will not parse, is UNKNOWN.

    Only a complete, successfully parsed listing is negative membership evidence —
    the same rule the read folds got in #410. This surface (the write path) was the
    fourth one and kept the hole after those three were fixed.
    """
    class DocUnreadable(FakeTransport):
        def read(self, path):
            if path.endswith("/roles/reviewer.md"):
                return "not frontmatter\n"
            return super().read(path)

    t = DocUnreadable()
    t.put("team/r/roles/reviewer.md", "---\ntype: Role\n---\n")
    rc = cli.main(["continuity", "park", "r", "-a", "amy"], transport=t)
    cap = capsys.readouterr()
    assert rc == 1, f"a listed-but-unparseable role doc is UNKNOWN, got rc={rc}"
    assert "nothing to park" not in cap.out


def test_park_genuinely_no_roles_still_exits_zero(capsys):
    """The over-correction guard: holding nothing is a real, knowable answer.

    Passes with AND without the fix, by design — it exists to catch a fix that
    turns every park into UNKNOWN, not to catch the regression.
    """
    t = FakeTransport()
    rc = cli.main(["continuity", "park", "r", "-a", "amy"], transport=t)
    cap = capsys.readouterr()
    assert rc == 0, f"no roles is a knowable answer, not a failure: rc={rc}"
    assert "nothing to park" in cap.out
    assert "CHECKPOINT NOT WRITTEN" not in cap.err


def test_park_failed_snapshot_write_leaves_ref_unchanged(capsys):
    from coord_engine import okf
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md", "---\ntype: Role\nsla_hours: 24\n---\n")
    cli.main(["roles", "claim", "r", "reviewer", "-a", "amy"], transport=t)
    orig_write = t.write
    t.write = lambda p, c: False if "/continuity/" in p else orig_write(p, c)
    capsys.readouterr()
    cli.main(["continuity", "park", "r", "-a", "amy"], transport=t)
    assert "FAILED" in capsys.readouterr().err
    fm = okf.parse_frontmatter(t.store["team/r/roles/reviewer.md"])
    assert "checkpoint_ref" not in fm                      # never points at a ghost snapshot

def test_health_json_uses_monitor_exit_code(capsys):
    import json as _j
    stale = FakeTransport()
    stale.put("team/r/_coord/health/slow.json",
              _j.dumps({"host": "slow", "at": "2020-01-01T00:00:00Z"}))

    assert cli.main(["health", "r", "--json"], transport=stale) == 1
    view = _j.loads(capsys.readouterr().out)
    assert view["healthy"] is False and view["total"] == 1


def test_roles_claim_echoes_shard_filename(capsys):
    from coord_engine.tasks import agent_key
    t = FakeTransport()
    assert cli.main(["roles", "claim", "r", "reviewer", "--agent", "coord-maintainer"], transport=t) == 0
    out = capsys.readouterr().out
    assert f"{agent_key('coord-maintainer')}.md" in out   # agents need their shard name to inspect/delete their exact shard


def test_operator_ask_answer_round_trip(capsys):
    import json as _j
    from coord_engine import okf
    t = FakeTransport()
    # agent hits a wall and asks the operator
    cli.main(["task", "start", "r", "Deploy thing", "--status", "active"], transport=t)
    import os
    os.environ["FULCRA_COORD_HUMAN"] = "ash"
    try:
        cli.main(["task", "block", "r", "deploy-thing", "--on-user",
                  "need prod credentials: use vault A or B?"], transport=t)
        cli.main(["reconcile", "r"], transport=t)
        capsys.readouterr()
        # orchestrator pulls asks
        assert cli.main(["asks", "r", "--human", "ash", "--json"], transport=t) == 0
        got = _j.loads(capsys.readouterr().out)
        assert len(got) == 1 and got[0]["name"] == "deploy-thing"
        assert "vault A or B" in got[0]["blocked_on"]
        assert got[0]["age_hours"] is not None
        # operator answers -> one write: unblocked, handed back, marker stripped
        assert cli.main(["answer", "r", "deploy-thing", "--with", "vault B, creds in 1P"], transport=t) == 0
        fm = okf.parse_frontmatter(t.store["team/r/task/deploy-thing.md"])
        assert fm["status"] == "active"
        assert fm["next_action"].startswith("OPERATOR ANSWER: vault B")
        assert "needs:human" not in (fm.get("tags") or [])
        assert fm["assignee"] == fm["owner"]          # back in the owner's inbox
        cli.main(["reconcile", "r"], transport=t); capsys.readouterr()
        cli.main(["asks", "r", "--human", "ash", "--json"], transport=t)
        assert _j.loads(capsys.readouterr().out) == []  # ask cleared
    finally:
        os.environ.pop("FULCRA_COORD_HUMAN", None)


def test_answer_rejects_non_ask_and_empty(capsys):
    t = FakeTransport()
    cli.main(["task", "start", "r", "Normal", "--status", "active"], transport=t)
    capsys.readouterr()
    assert cli.main(["answer", "r", "normal", "--with", "hi"], transport=t) == 1
    assert "not a waiting-for-operator ask" in capsys.readouterr().err


def test_answer_rejects_blocked_non_human_dependency(capsys):
    from coord_engine import okf
    t = FakeTransport()
    t.put(
        "team/r/task/ci-block.md",
        "---\n"
        "type: Task\n"
        "title: CI Block\n"
        "status: blocked\n"
        "owner: build-agent\n"
        "assignee: ci-agent\n"
        "blocked_on: CI pipeline is red\n"
        "timestamp: 2026-07-01T00:00:00Z\n"
        "---\n",
    )
    assert cli.main(["answer", "r", "ci-block", "--with", "ship it"], transport=t) == 1
    assert "not a waiting-for-operator ask" in capsys.readouterr().err
    fm = okf.parse_frontmatter(t.store["team/r/task/ci-block.md"])
    assert fm["status"] == "blocked"
    assert fm["assignee"] == "ci-agent"
    assert fm["blocked_on"] == "CI pipeline is red"


def test_answer_accepts_configured_human_without_tag(capsys, monkeypatch):
    from coord_engine import okf
    monkeypatch.setenv("FULCRA_COORD_HUMAN", "ash")
    t = FakeTransport()
    for slug, assignee, blocked_on in (
        ("assignee-ask", "ash", "choose a branch"),
        ("blocked-on-ask", "build-agent", "ash"),
    ):
        t.put(
            f"team/r/task/{slug}.md",
            "---\n"
            "type: Task\n"
            f"title: {slug}\n"
            "status: blocked\n"
            "owner: build-agent\n"
            f"assignee: {assignee}\n"
            f"blocked_on: {blocked_on}\n"
            "timestamp: 2026-07-01T00:00:00Z\n"
            "---\n",
        )

    assert cli.main(["answer", "r", "assignee-ask", "--with", "use branch A"], transport=t) == 0
    assert cli.main(["answer", "r", "blocked-on-ask", "--with", "approved"], transport=t) == 0

    for slug in ("assignee-ask", "blocked-on-ask"):
        fm = okf.parse_frontmatter(t.store[f"team/r/task/{slug}.md"])
        assert fm["status"] == "active"
        assert fm["assignee"] == "build-agent"
        assert fm["blocked_on"] == ""
        assert fm["next_action"].startswith("OPERATOR ANSWER:")


def test_asks_oldest_first_ordering(capsys):
    import json as _j
    t = FakeTransport()
    t.put("team/r/task/old-ask.md",
          "---\ntype: Task\ntitle: Old\nstatus: blocked\nowner: a\ntags: [needs:human]\n"
          "blocked_on: pick one\ntimestamp: 2026-07-01T00:00:00Z\n---\n")
    t.put("team/r/task/new-ask.md",
          "---\ntype: Task\ntitle: New\nstatus: blocked\nowner: b\ntags: [needs:human]\n"
          "blocked_on: choose\ntimestamp: 2026-07-04T00:00:00Z\n---\n")
    cli.main(["reconcile", "r"], transport=t); capsys.readouterr()
    cli.main(["asks", "r", "--json"], transport=t)
    got = _j.loads(capsys.readouterr().out)
    assert [g["name"] for g in got] == ["old-ask", "new-ask"]   # oldest first


def test_asks_word_human_in_nonblocked_text_not_matched(capsys):
    import json as _j
    t = FakeTransport()
    t.put("team/r/task/notask.md",
          "---\ntype: Task\ntitle: N\nstatus: active\nowner: a\nassignee: alice\n"
          "blocked_on: waiting on human review board\ntimestamp: 2026-07-01T00:00:00Z\n---\n")
    cli.main(["reconcile", "r"], transport=t); capsys.readouterr()
    cli.main(["asks", "r", "--human", "human", "--json"], transport=t)
    assert _j.loads(capsys.readouterr().out) == []   # word 'human' in free text != an ask


def _claim(t, agent="coord-maintainer"):
    return cli.main(["roles", "claim", "r", "reviewer", "--agent", agent], transport=t)


def test_roles_claim_writes_nonce_and_no_warning_on_own_refresh(capsys, tmp_path, monkeypatch):
    from coord_engine import okf
    from coord_engine.tasks import agent_key
    monkeypatch.setenv("COORD_ENGINE_STATE_DIR", str(tmp_path))
    t = FakeTransport()
    assert _claim(t) == 0
    shard = t.read(f"team/r/roles/reviewer/leases/{agent_key('coord-maintainer')}.md")
    fm = okf.parse_frontmatter(shard)
    assert fm.get("nonce")                      # lease carries a session nonce
    assert _claim(t) == 0                       # own refresh: stored nonce matches shard
    assert "nonce mismatch" not in capsys.readouterr().err


def test_roles_claim_warns_on_foreign_nonce(capsys, tmp_path, monkeypatch):
    from coord_engine import okf
    from coord_engine.tasks import agent_key
    monkeypatch.setenv("COORD_ENGINE_STATE_DIR", str(tmp_path))
    t = FakeTransport()
    assert _claim(t) == 0
    capsys.readouterr()
    # simulate a second session under the SAME id: rewrite the shard with a different nonce
    path = f"team/r/roles/reviewer/leases/{agent_key('coord-maintainer')}.md"
    fm = okf.parse_frontmatter(t.read(path))
    fm["nonce"] = "f" * 16
    t.put(path, okf.render_frontmatter(fm) + "\nHolding reviewer.\n")
    assert _claim(t) == 0                       # still claims (never-raise), but loudly
    assert "nonce mismatch" in capsys.readouterr().err


def test_roles_release_clears_nonce_state(tmp_path, monkeypatch):
    monkeypatch.setenv("COORD_ENGINE_STATE_DIR", str(tmp_path))
    t = FakeTransport()
    assert _claim(t) == 0
    assert list(tmp_path.iterdir())             # state file exists
    assert cli.main(["roles", "release", "r", "reviewer", "--agent", "coord-maintainer"], transport=t) == 0
    assert not list(tmp_path.iterdir())         # state cleaned up


# --- pending-required review surfacing (needs-me / briefing), role-aware ---

def _seed_review(t, slug, required, verdicts=()):
    t.put(f"team/r/review/{slug}.md", f"---\ntype: Review\nrequired: {required}\n---\n")
    for who, v in verdicts:
        t.put(f"team/r/review/{slug}/verdicts/{who}.md",
              f"---\ntype: Verdict\nreviewer: {who}\nverdict: {v}\n---\n")


def test_needs_me_surfaces_pending_required_review(capsys):
    import json as _j
    t = FakeTransport()
    _seed_review(t, "pr-9", "rev1")
    cli.main(["reconcile", "r"], transport=t); capsys.readouterr()
    assert cli.main(["needs-me", "r", "--agent", "rev1", "--json"], transport=t) == 0
    got = _j.loads(capsys.readouterr().out)
    revs = [g for g in got if g.get("type") == "review-pending"]
    assert [r["name"] for r in revs] == ["pr-9"]


def test_needs_me_review_role_aware(capsys):
    import json as _j
    from coord_engine.tasks import agent_key
    t = FakeTransport()
    _seed_review(t, "pr-7", "codex-reviewer")
    # workbook holds a FRESH lease on the codex-reviewer role
    t.put("team/r/roles/codex-reviewer.md", "---\ntype: Role\npolicy: shared\n---\n")
    t.put(f"team/r/roles/codex-reviewer/leases/{agent_key('workbook')}.md",
          f"---\ntype: Lease\nagent: workbook\ntimestamp: {_now_iso()}\n---\n")
    cli.main(["reconcile", "r"], transport=t); capsys.readouterr()
    for who in ("codex-reviewer", "workbook"):        # role id itself AND lease holder
        cli.main(["needs-me", "r", "--agent", who, "--json"], transport=t)
        got = _j.loads(capsys.readouterr().out)
        assert any(g.get("name") == "pr-7" for g in got if g.get("type") == "review-pending"), who
    cli.main(["needs-me", "r", "--agent", "bystander", "--json"], transport=t)
    got = _j.loads(capsys.readouterr().out)
    assert not [g for g in got if g.get("type") == "review-pending"]


def test_needs_me_settled_reviews_not_surfaced(capsys):
    import json as _j
    t = FakeTransport()
    _seed_review(t, "ok", "rev1", verdicts=[("rev1", "approve")])
    _seed_review(t, "rejected", "rev2", verdicts=[("rev2", "changes")])
    cli.main(["reconcile", "r"], transport=t); capsys.readouterr()
    for who in ("rev1", "rev2"):
        cli.main(["needs-me", "r", "--agent", who, "--json"], transport=t)
        got = _j.loads(capsys.readouterr().out)
        assert not [g for g in got if g.get("type") == "review-pending"], who


def test_briefing_includes_pending_reviews(capsys):
    import json as _j
    t = FakeTransport()
    _seed_review(t, "pr-5", "me")
    cli.main(["reconcile", "r"], transport=t); capsys.readouterr()
    assert cli.main(["briefing", "r", "--agent", "me", "--json"], transport=t) == 0
    out = _j.loads(capsys.readouterr().out)
    assert [r["name"] for r in out.get("pending_reviews", [])] == ["pr-5"]


def test_briefing_text_includes_pending_reviews(capsys):
    t = FakeTransport()
    _seed_review(t, "pr-5", "me")
    cli.main(["reconcile", "r"], transport=t); capsys.readouterr()
    assert cli.main(["briefing", "r", "--agent", "me"], transport=t) == 0
    out = capsys.readouterr().out
    assert "pending reviews: 1 item(s)" in out
    assert "pr-5" in out


def test_needs_me_review_stale_lease_holder_not_surfaced(capsys):
    import json as _j
    from coord_engine.tasks import agent_key
    t = FakeTransport()
    _seed_review(t, "pr-8", "codex-reviewer")
    t.put("team/r/roles/codex-reviewer.md", "---\ntype: Role\npolicy: shared\n---\n")
    t.put(f"team/r/roles/codex-reviewer/leases/{agent_key('sleeper')}.md",
          "---\ntype: Lease\nagent: sleeper\ntimestamp: 2020-01-01T00:00:00Z\n---\n")
    cli.main(["reconcile", "r"], transport=t); capsys.readouterr()
    cli.main(["needs-me", "r", "--agent", "sleeper", "--json"], transport=t)
    got = _j.loads(capsys.readouterr().out)
    assert not [g for g in got if g.get("type") == "review-pending"]  # stale lease != holder


def test_needs_me_review_honors_role_doc_sla(capsys):
    import json as _j
    from datetime import datetime, timedelta, timezone
    from coord_engine.tasks import agent_key
    t = FakeTransport()
    _seed_review(t, "pr-slow", "patient-role")
    # role doc grants 72h SLA; lease is 30h old — stale by DEFAULT (24h), fresh per doc
    t.put("team/r/roles/patient-role.md", "---\ntype: Role\npolicy: shared\nsla_hours: 72\n---\n")
    ts = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat().replace("+00:00", "Z")
    t.put(f"team/r/roles/patient-role/leases/{agent_key('tortoise')}.md",
          f"---\ntype: Lease\nagent: tortoise\ntimestamp: {ts}\n---\n")
    cli.main(["reconcile", "r"], transport=t); capsys.readouterr()
    cli.main(["needs-me", "r", "--agent", "tortoise", "--json"], transport=t)
    got = _j.loads(capsys.readouterr().out)
    assert any(g.get("name") == "pr-slow" for g in got if g.get("type") == "review-pending")


def test_roles_claim_warns_on_unregistered_role(capsys):
    t = FakeTransport()
    assert cli.main(["roles", "claim", "r", "ghost-role", "--agent", "a"], transport=t) == 0
    err = capsys.readouterr().err
    assert "no registered role doc" in err
    t.put("team/r/roles/real-role.md", "---\ntype: Role\npolicy: shared\n---\n")
    assert cli.main(["roles", "claim", "r", "real-role", "--agent", "a"], transport=t) == 0
    assert "no registered role doc" not in capsys.readouterr().err


def test_answer_human_flag_matches_asks(capsys):
    # env-skew footgun: asks --human ash listed it; answer must accept with the same flag
    t = FakeTransport()
    cli.main(["task", "start", "r", "Pick window", "--status", "active"], transport=t)
    cli.main(["task", "update", "r", "pick-window", "--status", "blocked",
              "--blocked-on", "ash", "--assignee", "ash"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    import json as _j
    cli.main(["asks", "r", "--human", "ash", "--json"], transport=t)   # asks lists it...
    assert any(g["name"] == "pick-window" for g in _j.loads(capsys.readouterr().out))
    assert cli.main(["answer", "r", "pick-window", "--with", "window B",
                     "--human", "ash"], transport=t) == 0              # ...and answer accepts it


def test_answer_rejects_terminal_task_with_stale_needs_human(capsys):
    t = FakeTransport()
    t.put("team/r/task/oldie.md",
          "---\ntype: Task\ntitle: O\nstatus: done\nowner: a\ntags: [needs:human]\n---\n")
    assert cli.main(["answer", "r", "oldie", "--with", "hi"], transport=t) == 1
    assert "not a waiting-for-operator ask" in capsys.readouterr().err


# --- forge-feedback section budget (the briefing/needs-me forge hang) ---------
#
# `_forge_responsible`/`_forge_feedback_for` did unbounded, team-global per-PR
# transport reads with no budget: under a degraded transport `briefing` hung in
# the FORGE-FEEDBACK section. These cover the shared briefing deadline
# (COORD_BRIEFING_BUDGET) that now bounds the forge section, mirroring the
# review-fold budget: breach/failure -> a `forge-degraded` row (json passthrough
# + one text line), never a hang or a silent drop.

import time as _time  # noqa: E402
from coord_engine import forge as _forge  # noqa: E402
from coord_engine.transport import TransportError as _TransportError  # noqa: E402

_PR_URLS = ["https://github.com/o/r/pull/1",
            "https://github.com/o/r/pull/2",
            "https://github.com/o/r/pull/3"]


def _seed_forge(t, agent="bob", urls=_PR_URLS):
    """Watch each PR for `agent` and drop one unacked feedback shard per PR, so
    the agent is responsible for len(urls) PRs each carrying one feedback item."""
    for u in urls:
        slug = _forge.pr_slug(u)
        t.put(f"team/r/_coord/forge/watch/{slug}.md",
              f"---\ntype: Watch\nurl: {u}\nagent: {agent}\nts: 2026-07-08T12:00:00Z\n---\n")
        t.put(f"team/r/_coord/forge/feedback/{slug}/review-PRR_1.md",
              "---\ntype: ForgeFeedback\nauthor: rev\nbody: fix it\n---\n")


class _SlowFeedbackTransport(FakeTransport):
    """Sleeps only on forge-feedback prefix ops — responsibility building stays
    fast (so `total` is deterministic) while the per-PR feedback scan is slow."""

    def __init__(self, delay=0.03):
        super().__init__()
        self.delay = delay

    def _slow(self, path):
        if "/forge/feedback/" in path:
            _time.sleep(self.delay)

    def read(self, path):
        self._slow(path)
        return super().read(path)

    def list_dir(self, prefix):
        self._slow(prefix)
        return super().list_dir(prefix)


def test_forge_feedback_budget_emits_degraded_marker(capsys):
    t = _SlowFeedbackTransport(delay=0.03)
    _seed_forge(t, agent="bob")
    capsys.readouterr()
    start = _time.monotonic()
    out = cli._forge_feedback_for(t, "r", "bob", deadline=_time.monotonic() + 0.05)
    elapsed = _time.monotonic() - start
    deg = [r for r in out if r.get("type") == "forge-degraded"]
    assert len(deg) == 1, f"budget breach must append exactly one degraded marker: {out}"
    assert deg[0]["total"] == 3, deg[0]
    assert 0 <= deg[0]["scanned"] <= 3, deg[0]
    assert elapsed < 1.0, "the fold must not read every PR unbounded"


def test_forge_feedback_healthy_no_degraded_row(capsys):
    # Healthy transport: no budget breach, real feedback rows, NO degraded marker.
    t = FakeTransport()
    _seed_forge(t, agent="bob")
    out = cli._forge_feedback_for(t, "r", "bob", deadline=_time.monotonic() + 60)
    assert [r for r in out if r.get("type") == "forge-degraded"] == []
    fb = [r for r in out if r.get("type") == "forge-feedback"]
    assert len(fb) == 3 and all(r["count"] == 1 for r in fb)
    # deadline=None (the un-budgeted path) is byte-identical
    assert cli._forge_feedback_for(t, "r", "bob") == out


def test_forge_feedback_listing_raises_degraded_not_crash(capsys):
    # The forge feedback listing for a responsible PR raises: surface a degraded
    # row (skipped), never crash, never silently drop.
    class _FeedbackListFails(FakeTransport):
        def list_dir(self, prefix):
            if "/forge/feedback/" in prefix:
                raise _TransportError("boom")
            return super().list_dir(prefix)

    t = _FeedbackListFails()
    _seed_forge(t, agent="bob")
    out = cli._forge_feedback_for(t, "r", "bob")
    deg = [r for r in out if r.get("type") == "forge-degraded"]
    assert len(deg) == 1 and deg[0]["skipped"] == 3, out
    assert [r for r in out if r.get("type") == "forge-feedback"] == []


def test_briefing_forge_degraded_exits_zero_other_sections_intact(capsys, monkeypatch):
    monkeypatch.setenv("COORD_BRIEFING_BUDGET", "0.01")
    t = _SlowFeedbackTransport(delay=0.03)
    _seed_forge(t, agent="bob")
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()

    assert cli.main(["briefing", "r", "--agent", "bob"], transport=t) == 0
    out = capsys.readouterr().out
    assert "forge fold degraded" in out
    assert "board:" in out and "needs-me:" in out  # other sections still rendered

    assert cli.main(["briefing", "r", "--agent", "bob", "--json"], transport=t) == 0
    doc = json.loads(capsys.readouterr().out)
    assert any(r.get("type") == "forge-degraded" for r in doc["forge_feedback"]), \
        "json path must surface the forge-degraded marker as-is"
    assert "board" in doc and "needs_me" in doc and "presence" in doc


def test_needs_me_forge_degraded_exits_zero(capsys, monkeypatch):
    monkeypatch.setenv("COORD_BRIEFING_BUDGET", "0.01")
    t = _SlowFeedbackTransport(delay=0.03)
    _seed_forge(t, agent="bob")
    capsys.readouterr()
    assert cli.main(["needs-me", "r", "--agent", "bob"], transport=t) == 0
    assert "forge fold degraded" in capsys.readouterr().out
    assert cli.main(["needs-me", "r", "--agent", "bob", "--json"], transport=t) == 0
    got = json.loads(capsys.readouterr().out)
    assert any(r.get("type") == "forge-degraded" for r in got)


# --- presence-shard budget (P1: codex-reviewer) ------------------------------
# `cmd_briefing -> presence.roster(_presence_shards(...))` did unbounded, team-
# global per-shard reads BEFORE the shared briefing deadline even opened, so a
# degraded transport hung the whole briefing in the PRESENCE section (needed a
# SIGINT). These pin the fix: the shared `COORD_BRIEFING_BUDGET` now opens at the
# top of cmd_briefing and bounds the presence fan-out too, mirroring forge/review:
# breach/failure -> a `presence-degraded` row `{scanned, total[, skipped]}` (json
# passthrough + one text line), a PARTIAL roster served, never a hang or a silent
# drop. Healthy path stays byte-identical.


class _SlowPresenceTransport(FakeTransport):
    """Sleeps only on presence-prefix ops — everything else (summaries load,
    reconcile, forge) stays fast, so `total` and the other sections are
    deterministic while the per-shard presence reads are slow."""

    def __init__(self, delay=0.03):
        super().__init__()
        self.delay = delay

    def _slow(self, path):
        if "/presence/" in path:
            _time.sleep(self.delay)

    def read(self, path):
        self._slow(path)
        return super().read(path)

    def list_dir(self, prefix):
        self._slow(prefix)
        return super().list_dir(prefix)


def _seed_presence(t, agents=("amy", "bob", "cid", "dee")):
    for a in agents:
        cli.main(["presence", "beat", "r", "-a", a], transport=t)


def test_presence_shards_bounded_healthy_byte_identical():
    # Healthy transport, generous/absent deadline: the bounded reader yields the
    # SAME shards as the legacy `_presence_shards` (folds to an identical roster)
    # and NO degraded marker — the healthy path must not change.
    t = FakeTransport()
    _seed_presence(t)
    legacy = cli._presence_shards(t, "r")
    shards, marker = cli._presence_shards_bounded(t, "r", deadline=_time.monotonic() + 60)
    assert marker is None
    now = "2026-07-01T12:00:00Z"
    from coord_engine import presence as _presence
    assert _presence.roster(shards, now=now) == _presence.roster(legacy, now=now)
    # deadline=None (legacy/unbounded) is likewise clean
    shards2, marker2 = cli._presence_shards_bounded(t, "r", deadline=None)
    assert marker2 is None
    assert _presence.roster(shards2, now=now) == _presence.roster(legacy, now=now)


def test_presence_shards_bounded_budget_emits_degraded_marker():
    # Slow per-shard reads + a tiny deadline: the fan-out must stop early, return a
    # PARTIAL roster plus exactly one degraded marker, and not read every shard.
    t = _SlowPresenceTransport(delay=0.03)
    _seed_presence(t)
    start = _time.monotonic()
    shards, marker = cli._presence_shards_bounded(t, "r", deadline=_time.monotonic() + 0.05)
    elapsed = _time.monotonic() - start
    assert marker is not None and marker["type"] == "presence-degraded"
    assert marker["total"] == 4
    assert 0 <= marker["scanned"] <= 4
    assert len(shards) <= 4          # partial roster served
    assert elapsed < 1.0, "the fold must not read every shard unbounded"


def test_presence_shards_listing_raises_degraded_not_crash():
    # The presence listing itself raises: roster is UNKNOWN (scanned=0), surfaced
    # as a degraded marker — never crash, never a silent empty roster.
    class _PresenceListFails(FakeTransport):
        def list_dir(self, prefix):
            if "/presence/" in prefix:
                raise _TransportError("boom")
            return super().list_dir(prefix)

    t = _PresenceListFails()
    _seed_presence(t)
    shards, marker = cli._presence_shards_bounded(t, "r", deadline=_time.monotonic() + 60)
    assert shards == []
    assert marker is not None and marker["type"] == "presence-degraded" and marker["scanned"] == 0


def test_presence_shard_unreadable_counts_skipped_not_crash():
    # A listed shard whose read returns None is UNKNOWN (transport problem, not a
    # vanish): counted skipped, the rest still scanned, degraded surfaced.
    bob_shard = f"/presence/{tasks.agent_key('bob')}.md"

    class _OneShardUnreadable(FakeTransport):
        def read(self, path):
            if path.endswith(bob_shard):
                return None
            return super().read(path)

    t = _OneShardUnreadable()
    _seed_presence(t, agents=("amy", "bob"))
    shards, marker = cli._presence_shards_bounded(t, "r", deadline=_time.monotonic() + 60)
    assert marker is not None and marker.get("skipped") == 1
    assert {s.get("agent") for s in shards} == {"amy"}   # partial roster: readable shard only


def test_presence_shards_deadline_spent_before_listing_skips_call():
    # Codex P1 (round 2): an already-spent deadline must SKIP the listing entirely
    # (never pay one more transport timeout of stall) and return the degraded
    # marker, not a falsely-clean empty roster.
    class _ListingMustNotRun(FakeTransport):
        def list_dir(self, prefix):
            if "/presence/" in prefix:
                raise AssertionError("listing must not run once the deadline is spent")
            return super().list_dir(prefix)

    t = _ListingMustNotRun()
    _seed_presence(t)
    shards, marker = cli._presence_shards_bounded(t, "r", deadline=_time.monotonic() - 1)
    assert shards == []
    assert marker == {"type": "presence-degraded", "scanned": 0, "total": 0}


def test_presence_shards_slow_listing_overrun_visible_even_when_empty():
    # Codex P1 (round 2): the deadline passing DURING the listing itself must be
    # detected AFTER the blocking op — even when the listing returns [] (no shard
    # reads happen, so the per-shard loop can't catch it): `([], None)` here would
    # be a falsely-clean empty roster despite blowing the budget.
    class _SlowListingOnly(FakeTransport):
        def list_dir(self, prefix):
            if "/presence/" in prefix:
                _time.sleep(0.05)
            return super().list_dir(prefix)

    t = _SlowListingOnly()  # NO shards seeded: listing returns [] after the stall
    shards, marker = cli._presence_shards_bounded(t, "r", deadline=_time.monotonic() + 0.01)
    assert shards == []
    assert marker is not None and marker["type"] == "presence-degraded"
    assert marker["scanned"] == 0 and marker["total"] == 0

    # And with shards present, the post-listing overrun stops before any read.
    t2 = _SlowListingOnly()
    _seed_presence(t2)
    reads = []
    orig_read = t2.read
    t2.read = lambda p: (reads.append(p), orig_read(p))[1]
    shards2, marker2 = cli._presence_shards_bounded(t2, "r", deadline=_time.monotonic() + 0.01)
    assert marker2 is not None and marker2["type"] == "presence-degraded"
    assert marker2["scanned"] == 0 and marker2["total"] == 4
    assert [p for p in reads if "/presence/" in p] == [], \
        "no shard reads once the listing spent the budget"


def test_briefing_presence_degraded_exits_zero_other_sections_intact(capsys, monkeypatch):
    monkeypatch.setenv("COORD_BRIEFING_BUDGET", "0.01")
    t = _SlowPresenceTransport(delay=0.03)
    _seed_presence(t)
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()

    start = _time.monotonic()
    assert cli.main(["briefing", "r", "--agent", "amy"], transport=t) == 0
    assert _time.monotonic() - start < 2.0, "briefing must return within budget order-of-magnitude"
    out = capsys.readouterr().out
    assert "presence fold degraded" in out
    assert "board:" in out and "needs-me:" in out   # other sections still rendered

    assert cli.main(["briefing", "r", "--agent", "amy", "--json"], transport=t) == 0
    doc = json.loads(capsys.readouterr().out)
    assert any(r.get("type") == "presence-degraded" for r in doc["presence"]), \
        "json path must surface the presence-degraded marker as-is"
    assert "board" in doc and "needs_me" in doc and "inbox" in doc


def test_briefing_presence_healthy_no_degraded_row(capsys):
    # Healthy transport: real roster, NO presence-degraded marker in text or json.
    t = FakeTransport()
    _seed_presence(t, agents=("amy",))
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    assert cli.main(["briefing", "r", "--agent", "amy", "--json"], transport=t) == 0
    doc = json.loads(capsys.readouterr().out)
    assert any(p.get("agent") == "amy" for p in doc["presence"])
    assert not any(r.get("type") == "presence-degraded" for r in doc["presence"])


def test_version_coherent_and_doctor_reports_it(capsys):
    import re
    from coord_engine import __version__ as _v
    assert re.fullmatch(r"\d+\.\d+\.\d+", _v), f"__version__ must be semver: {_v!r}"
    t = FakeTransport()
    assert cli.main(["doctor", "r"], transport=t) == 0
    assert f"coord-engine v{_v}" in capsys.readouterr().out


# --- Task 2: fail-closed roles/vacancy fold ----------------------------------

def test_roles_status_lease_listing_unknown_rc1_no_vacancy(capsys):
    # ADDED SCOPE (fail-closed): a lease LISTING that raises must read as UNKNOWN,
    # never VACANT — a degraded transport must not assert vacancy and fire a false
    # SLA escalation. rc 1, same "unknown, retry" register as review status.
    from coord_engine.transport import TransportError

    class LeaseListFails(FakeTransport):
        def list_dir(self, prefix):
            if prefix.endswith("/leases/"):
                raise TransportError("boom")
            return super().list_dir(prefix)

    t = LeaseListFails()
    t.put("team/r/roles/reviewer.md", "---\ntype: Role\nsla_hours: 24\n---\n")
    rc = cli.main(["roles", "status", "r", "reviewer", "--json"], transport=t)
    assert rc == 1
    cap = capsys.readouterr()
    res = json.loads(cap.out)
    assert res["status"] == "UNKNOWN"
    assert res["escalation_due"] is not True  # unknown never escalates
    assert "unknown" in cap.err.lower()


def test_roles_status_lease_listing_unknown_text_mode_rc1(capsys):
    from coord_engine.transport import TransportError

    class LeaseListFails(FakeTransport):
        def list_dir(self, prefix):
            if prefix.endswith("/leases/"):
                raise TransportError("boom")
            return super().list_dir(prefix)

    t = LeaseListFails()
    t.put("team/r/roles/reviewer.md", "---\ntype: Role\nsla_hours: 24\n---\n")
    assert cli.main(["roles", "status", "r", "reviewer"], transport=t) == 1
    err = capsys.readouterr().err
    assert "unknown" in err.lower() and "retry" in err.lower()


def test_escalate_does_not_escalate_on_unknown_lease(capsys):
    # The vacancy sweep must not escalate when the lease state is UNKNOWN (listing
    # raised) — only a proven VACANT past SLA escalates.
    from coord_engine.transport import TransportError

    class LeaseListFails(FakeTransport):
        def list_dir(self, prefix):
            if prefix.endswith("/leases/"):
                raise TransportError("boom")
            return super().list_dir(prefix)

    t = LeaseListFails()
    t.put("team/r/roles/reviewer.md",
          "---\ntype: Role\nsla_hours: 24\nmaintainer: ash\n---\n")
    assert cli.main(["escalate", "r"], transport=t) == 0
    out = capsys.readouterr().out
    assert "0 escalated" in out
    assert not any(p.startswith("team/r/task/role-vacant-") for p in t.store)
    assert not any("escalations/" in p for p in t.store)


def test_escalate_skips_role_on_transient_doc_read_failure(capsys):
    # Review fix (HIGH): the role doc was JUST LISTED by the parent roles/ scan, so
    # a None doc read is knowably transient-or-deleted — NOT a role with default
    # SLA. Falling through with DEFAULT_SLA_HOURS=24 would collapse a >24h-SLA
    # role's window and fire a false VACANT escalation (the incident vector, on the
    # acting path). Fix: doc-None after listing -> UNKNOWN -> skip.
    class RoleDocReadFails(FakeTransport):
        def read(self, path):
            if path == "team/r/roles/patient.md":
                return None  # transient read failure on a just-listed doc
            return super().read(path)

    t = RoleDocReadFails()
    # 72h-SLA role; lease 30h old: fresh per its doc, stale per the 24h default
    t.put("team/r/roles/patient.md",
          "---\ntype: Role\nsla_hours: 72\nmaintainer: ash\n---\n")
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat().replace("+00:00", "Z")
    t.put("team/r/roles/patient/leases/amy.md",
          f"---\ntype: Lease\nagent: amy\ntimestamp: {ts}\n---\n")
    assert cli.main(["escalate", "r"], transport=t) == 0
    out = capsys.readouterr().out
    assert "0 escalated" in out
    assert not any(p.startswith("team/r/task/role-vacant-") for p in t.store), \
        "a transient role-doc read failure must never fire a VACANT escalation"
    assert not any("escalations/" in p for p in t.store)


def test_roles_status_doc_none_but_listed_unknown_rc1(capsys):
    # Codex P1 follow-through: roles status can now disambiguate a None role-doc
    # read with the parent roles/ listing — doc listed but unreadable = transport
    # failure = UNKNOWN rc 1 (never a default-SLA VACANT on the reporting path).
    class RoleDocReadFails(FakeTransport):
        def read(self, path):
            if path == "team/r/roles/patient.md":
                return None
            return super().read(path)

    t = RoleDocReadFails()
    t.put("team/r/roles/patient.md", "---\ntype: Role\nsla_hours: 72\n---\n")
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat().replace("+00:00", "Z")
    t.put("team/r/roles/patient/leases/amy.md",
          f"---\ntype: Lease\nagent: amy\ntimestamp: {ts}\n---\n")
    assert cli.main(["roles", "status", "r", "patient", "--json"], transport=t) == 1
    cap = capsys.readouterr()
    assert "unknown" in cap.err.lower() and "retry" in cap.err.lower()
    assert "VACANT" not in cap.out


def test_roles_status_unregistered_role_still_works(capsys):
    # Doc genuinely ABSENT (not in the roles/ listing): the unregistered-role flow
    # keeps its default-SLA behavior — claim-without-doc still reads HELD.
    t = FakeTransport()
    assert cli.main(["roles", "claim", "r", "adhoc", "--agent", "amy"], transport=t) == 0
    capsys.readouterr()
    assert cli.main(["roles", "status", "r", "adhoc", "--json"], transport=t) == 0
    import json as _j
    assert _j.loads(capsys.readouterr().out)["status"] == "HELD"


def test_roles_status_listed_lease_shard_unreadable_unknown_rc1(capsys):
    # A listed lease shard whose read returns None must be UNKNOWN (rc 1), never
    # parsed as {} -> timestamp lost -> silently stale -> false VACANT.
    class LeaseReadFails(FakeTransport):
        def read(self, path):
            if "/leases/" in path:
                return None
            return super().read(path)

    t = LeaseReadFails()
    t.put("team/r/roles/reviewer.md", "---\ntype: Role\nsla_hours: 24\n---\n")
    t.put("team/r/roles/reviewer/leases/amy.md",
          f"---\ntype: Lease\nagent: amy\ntimestamp: {_now_iso()}\n---\n")
    assert cli.main(["roles", "status", "r", "reviewer", "--json"], transport=t) == 1
    cap = capsys.readouterr()
    assert json.loads(cap.out)["status"] == "UNKNOWN"
    assert "unknown" in cap.err.lower()


def test_escalate_skips_role_on_lease_shard_read_failure(capsys):
    # Same class on the ACTING path: a listed lease shard read-None must not fold
    # to {} -> stale -> false VACANT escalation. Skip as unknown.
    class LeaseReadFails(FakeTransport):
        def read(self, path):
            if "/leases/" in path:
                return None
            return super().read(path)

    t = LeaseReadFails()
    t.put("team/r/roles/reviewer.md",
          "---\ntype: Role\nsla_hours: 24\nmaintainer: ash\n---\n")
    t.put("team/r/roles/reviewer/leases/amy.md",
          f"---\ntype: Lease\nagent: amy\ntimestamp: {_now_iso()}\n---\n")
    assert cli.main(["escalate", "r"], transport=t) == 0
    assert "0 escalated" in capsys.readouterr().out
    assert not any(p.startswith("team/r/task/role-vacant-") for p in t.store)


# --- role dormancy (deliberately-parked roles) ---

def _future_iso(days=30):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _past_iso(days=30):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def test_escalate_dormant_future_suppresses_vacancy(capsys):
    # A deliberately-parked role (future dormant_until) is vacant past SLA but must
    # NOT fire a vacancy escalation on any heartbeat host — the live incident.
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md",
          f"---\ntype: Role\nsla_hours: 24\nmaintainer: ash\n"
          f"dormant_until: {_future_iso()}\n---\n")
    t.put("team/r/roles/reviewer/leases/ghost.md",
          "---\ntype: Lease\nagent: ghost\ntimestamp: 2020-01-01T00:00:00Z\n---\n")
    assert cli.main(["escalate", "r"], transport=t) == 0
    assert "0 escalated" in capsys.readouterr().out
    assert not any(p.startswith("team/r/task/role-vacant-") for p in t.store)
    assert not any("escalations/" in p for p in t.store)


def test_escalate_dormant_past_escalates_as_normal(capsys):
    # A park whose date has passed reverts to current behavior: it escalates.
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md",
          f"---\ntype: Role\nsla_hours: 24\nmaintainer: ash\n"
          f"dormant_until: {_past_iso()}\n---\n")
    t.put("team/r/roles/reviewer/leases/ghost.md",
          "---\ntype: Lease\nagent: ghost\ntimestamp: 2020-01-01T00:00:00Z\n---\n")
    assert cli.main(["escalate", "r"], transport=t) == 0
    assert "escalated reviewer -> ash" in capsys.readouterr().out


def test_escalate_dormant_garbage_notes_stderr_and_escalates(capsys):
    # Unparseable dormant_until -> treat as ABSENT, note it on stderr, and escalate.
    # Fail OPEN: a typo must never silently suppress an escalation.
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md",
          "---\ntype: Role\nsla_hours: 24\nmaintainer: ash\n"
          "dormant_until: whenever\n---\n")
    t.put("team/r/roles/reviewer/leases/ghost.md",
          "---\ntype: Lease\nagent: ghost\ntimestamp: 2020-01-01T00:00:00Z\n---\n")
    assert cli.main(["escalate", "r"], transport=t) == 0
    cap = capsys.readouterr()
    assert "escalated reviewer -> ash" in cap.out
    assert "dormant_until" in cap.err


def test_roles_status_dormant_when_vacant(capsys):
    ts = _future_iso()
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md",
          f"---\ntype: Role\nsla_hours: 24\ndormant_until: {ts}\n---\n")
    t.put("team/r/roles/reviewer/leases/ghost.md",
          "---\ntype: Lease\nagent: ghost\ntimestamp: 2020-01-01T00:00:00Z\n---\n")
    assert cli.main(["roles", "status", "r", "reviewer", "--json"], transport=t) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["status"] == "DORMANT"
    assert res["escalation_due"] is False
    assert res["dormant_until"] == ts


def test_roles_status_dormant_text_shows_until(capsys):
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md",
          f"---\ntype: Role\nsla_hours: 24\ndormant_until: {_future_iso()}\n---\n")
    t.put("team/r/roles/reviewer/leases/ghost.md",
          "---\ntype: Lease\nagent: ghost\ntimestamp: 2020-01-01T00:00:00Z\n---\n")
    assert cli.main(["roles", "status", "r", "reviewer"], transport=t) == 0
    out = capsys.readouterr().out
    assert "DORMANT" in out and "until" in out


def test_roles_status_held_outranks_dormant(capsys):
    # A live lease outranks the dormancy display: HELD, not DORMANT.
    t = FakeTransport()
    t.put("team/r/roles/reviewer.md",
          f"---\ntype: Role\nsla_hours: 24\ndormant_until: {_future_iso()}\n---\n")
    t.put("team/r/roles/reviewer/leases/ash.md",
          f"---\ntype: Lease\nagent: ash\ntimestamp: {_now_iso()}\n---\n")
    assert cli.main(["roles", "status", "r", "reviewer", "--json"], transport=t) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["status"] == "HELD"


# --- ENG-1: the public-read failure contract (codex CRIT family) -----------
#
# Contract (see cli._read_degraded_row): every aggregate-backed public read
# surfaces the shared degraded marker when the summaries index/listing is UNKNOWN
# (`_load_rows_status(...).ok is False`) — NEVER a clean-empty result that a
# watcher can't distinguish from "nothing to do". codex live-reproduced the
# inbox clean-`[]` exit-0 suppressing a live unacked directive.

from coord_engine.transport import TransportError as _TE


def _degrade_summaries(t, team="r"):
    """Make the summaries index read fail (transport down) so `_load_rows_status`
    reports UNKNOWN (ok=False) rather than a confirmed-empty index."""
    path = f"team/{team}/_coord/summaries.json"
    orig = t.read

    def read(p):
        if p == path:
            raise _TE("summaries index down")
        return orig(p)

    t.read = read


def _seed_indexed_directive(t, team="r", agent="amy"):
    cli.main(["tell", team, agent, "Live P1 charter", "--from", "boss"], transport=t)
    cli.main(["reconcile", team], transport=t)


def test_inbox_degraded_transport_marker_not_clean_empty(capsys):
    t = FakeTransport()
    _seed_indexed_directive(t)
    _degrade_summaries(t)
    capsys.readouterr()
    rc = cli.main(["inbox", "r", "-a", "amy", "--json"], transport=t)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out != [], "degraded inbox must NOT be a clean empty list"
    assert any(r.get("type") == "inbox-degraded" for r in out), \
        f"degraded inbox must carry the inbox-degraded marker: {out}"


def test_inbox_degraded_transport_text_stderr_notice(capsys):
    t = FakeTransport()
    _seed_indexed_directive(t)
    _degrade_summaries(t)
    capsys.readouterr()
    cli.main(["inbox", "r", "-a", "amy"], transport=t)
    err = capsys.readouterr().err
    assert "inbox-degraded" in err


def test_inbox_absent_index_is_not_degraded(capsys):
    # A genuinely-absent index (no reconcile yet) is a real readable empty, NOT a
    # degradation — the marker must NOT appear (absence != failure).
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "hi", "--from", "boss"], transport=t)  # no reconcile
    capsys.readouterr()
    cli.main(["inbox", "r", "-a", "amy", "--json"], transport=t)
    out = json.loads(capsys.readouterr().out)
    assert not any(r.get("type") == "inbox-degraded" for r in out)


def test_status_degraded_transport_marker(capsys):
    t = FakeTransport()
    _seed_indexed_directive(t)
    _degrade_summaries(t)
    capsys.readouterr()
    cli.main(["status", "r", "--json"], transport=t)
    counts = json.loads(capsys.readouterr().out)
    assert "read-degraded" in counts, f"degraded status must carry the marker: {counts}"


def test_board_degraded_transport_marker(capsys):
    t = FakeTransport()
    _seed_indexed_directive(t)
    _degrade_summaries(t)
    capsys.readouterr()
    cli.main(["board", "r", "--json"], transport=t)
    groups = json.loads(capsys.readouterr().out)
    assert "read-degraded" in groups


def test_needs_me_degraded_transport_marker(capsys):
    t = FakeTransport()
    _seed_indexed_directive(t)
    _degrade_summaries(t)
    capsys.readouterr()
    cli.main(["needs-me", "r", "--agent", "amy", "--json"], transport=t)
    got = json.loads(capsys.readouterr().out)
    assert any(r.get("type") == "read-degraded" for r in got), \
        f"degraded needs-me must announce the core fold before add-ons: {got}"


def test_search_degraded_transport_marker(capsys):
    t = FakeTransport()
    _seed_indexed_directive(t)
    _degrade_summaries(t)
    capsys.readouterr()
    cli.main(["search", "r", "charter", "--json"], transport=t)
    got = json.loads(capsys.readouterr().out)
    assert any(r.get("type") == "read-degraded" for r in got)


def test_briefing_degraded_transport_marker(capsys):
    t = FakeTransport()
    _seed_indexed_directive(t)
    _degrade_summaries(t)
    capsys.readouterr()
    cli.main(["briefing", "r", "--agent", "amy", "--json"], transport=t)
    doc = json.loads(capsys.readouterr().out)
    assert "read_degraded" in doc and doc["read_degraded"]["type"] == "read-degraded"


def test_public_reads_healthy_no_degraded_marker(capsys):
    # Positive control: a HEALTHY read must NOT emit the marker (no over-alarm).
    t = FakeTransport()
    _seed_indexed_directive(t)
    capsys.readouterr()
    cli.main(["status", "r", "--json"], transport=t)
    assert "read-degraded" not in json.loads(capsys.readouterr().out)
    cli.main(["inbox", "r", "-a", "amy", "--json"], transport=t)
    assert not any(r.get("type") == "inbox-degraded"
                   for r in json.loads(capsys.readouterr().out))
    cli.main(["needs-me", "r", "--agent", "amy", "--json"], transport=t)
    assert not any(r.get("type") == "read-degraded"
                   for r in json.loads(capsys.readouterr().out))


# --- ENG-1-5: listen daemon per-tick guard ---------------------------------

def test_listen_daemon_survives_tick_exception(monkeypatch, capsys):
    """A:25 — the load-bearing `listen` daemon (`while True: tick()`) must survive
    an UNMODELED tick exception: it degrades that tick and continues, never lets
    the fault kill the watcher."""
    import coord_engine.cli as _cli
    t = FakeTransport()
    _cli.main(["reconcile", "r"], transport=t)
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("unmodeled tick fault")

    monkeypatch.setattr(_cli, "_run_listen_tick", boom)

    def stop_after_first_sleep(_):
        raise KeyboardInterrupt  # break out of the daemon loop cleanly

    monkeypatch.setattr(_cli.time, "sleep", stop_after_first_sleep)
    capsys.readouterr()
    rc = _cli.main(["listen", "r", "--agent", "amy", "--interval", "1"], transport=t)
    assert rc == 0, "daemon must exit cleanly, not propagate the tick RuntimeError"
    assert calls["n"] == 1
    assert "LISTEN DEGRADED" in capsys.readouterr().err


# --- ENG-1-6: registered top-level error envelope --------------------------

def test_toplevel_unexpected_error_is_registered(monkeypatch, capsys):
    """A:26 — an unexpected exception surfaces as a REGISTERED, machine-parseable
    envelope (an `error:` token + command + type), distinct from the retryable
    degrade voice, not an off-register prose line."""
    import coord_engine.cli as _cli
    t = FakeTransport()

    def boom(_args, _transport):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(_cli, "cmd_status", boom)
    capsys.readouterr()
    rc = _cli.main(["status", "r"], transport=t)
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err and "command=status" in err and "RuntimeError" in err
