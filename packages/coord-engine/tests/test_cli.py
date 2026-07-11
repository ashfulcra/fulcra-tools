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
