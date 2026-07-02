import json

from coord_engine import cli
from tests.test_reconcile import FakeTransport, _task


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
    assert cli.main(["task", "start", "r", "Build the thing", "-w", "coord2",
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
    assert cli.main(["tell", "r", "amy", "Do the thing", "-p", "P1", "--from", "boss"], transport=t) == 0
    cli.main(["broadcast", "r", "All hands"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    cli.main(["inbox", "r", "-a", "amy", "--json"], transport=t)
    got = {r["name"] for r in _j.loads(capsys.readouterr().out)}
    assert got == {"do-the-thing", "all-hands"}
    # ack the direct one -> disappears for amy, broadcast still there
    cli.main(["inbox", "r", "-a", "amy", "--ack", "do-the-thing"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    cli.main(["inbox", "r", "-a", "amy", "--json"], transport=t)
    assert {r["name"] for r in _j.loads(capsys.readouterr().out)} == {"all-hands"}
    # bob sees only the broadcast (do-the-thing is amy's)
    cli.main(["inbox", "r", "-a", "bob", "--json"], transport=t)
    assert [r["name"] for r in _j.loads(capsys.readouterr().out)] == ["all-hands"]


def test_cli_inbox_ack_hides_before_reconcile(capsys):
    import json as _j
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "Immediate hide"], transport=t)
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()

    cli.main(["inbox", "r", "-a", "amy", "--ack", "immediate-hide"], transport=t)
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
    assert [r["name"] for r in _j.loads(capsys.readouterr().out)] == ["someday-idea"]


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
    assert cli.main(["respond", "r", "question", "-o", "answered", "-a", "amy"], transport=t) == 0
    out = capsys.readouterr().out
    assert "closed" in out
    assert okf.parse_frontmatter(t.store["team/r/task/question.md"])["status"] == "done"
    assert any(p.startswith("team/r/_coord/responses/question/") for p in t.store)


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


def test_reconcile_gcs_orphaned_ack_shards(capsys):
    t = FakeTransport()
    t.put("team/r/task/live.md", "---\ntype: Task\ntitle: L\nstatus: active\nassignee: amy\n---\n")
    t.put("team/r/_coord/acks/live/amy.md", "---\ntype: Ack\nagent: amy\n---\n")
    t.put("team/r/_coord/acks/ghost/amy.md", "---\ntype: Ack\nagent: amy\n---\n")
    cli.main(["reconcile", "r"], transport=t)
    assert "team/r/_coord/acks/ghost/amy.md" not in t.store   # GC'd
    assert "team/r/_coord/acks/live/amy.md" in t.store        # kept
    import json as _j
    agg = _j.loads(t.store["team/r/_coord/summaries.json"])
    row = next(r for r in agg["rows"] if r["name"] == "live")
    assert row["acked_by"] == ["amy"]
