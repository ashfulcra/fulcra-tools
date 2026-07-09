import json

from coord_engine import cli, migrate, okf
from coord_engine_test_helpers import FakeTransport

NOW = "2026-07-02T16:00:00Z"


def _incumbent(id, title, status="active", **over):
    t = {"id": id, "title": title, "status": status, "priority": "P1",
         "workstream": "fulcra-coord", "kind": "bug", "owner_agent": "claude-code:mb:x",
         "assignee": "codex:h:r", "current_summary": "the summary",
         "next_action": "do next", "blocked_on": None, "not_before": None,
         "due": "2026-08-01T00:00:00Z", "updated_at": "2026-06-30T10:00:00Z",
         "tags": ["agent:claude-code:mb:x", "status:active", "extra:label"]}
    t.update(over)
    return t


def test_map_task_field_fidelity():
    slug, fm, body = migrate.map_task(_incumbent("TASK-X-1", "Fix the widget"), now=NOW)
    assert slug == "fix-the-widget"
    assert fm["status"] == "active" and fm["priority"] == "P1"
    assert fm["owner"] == "claude-code:mb:x" and fm["assignee"] == "codex:h:r"
    assert fm["description"] == "the summary" and fm["next_action"] == "do next"
    assert fm["timestamp"] == "2026-06-30T10:00:00Z" and fm["due"] == "2026-08-01T00:00:00Z"
    assert fm["migrated_from"] == "TASK-X-1"
    assert "workstream:fulcra-coord" in fm["tags"] and "kind:bug" in fm["tags"]
    assert "extra:label" in fm["tags"]                       # real labels kept
    assert not any(t.startswith(("agent:", "status:")) for t in fm["tags"])  # dupes dropped
    assert "Migrated from fulcra-coord task `TASK-X-1`" in body


def test_map_task_sanitizes_bad_enums_and_sentinels():
    slug, fm, _ = migrate.map_task(_incumbent("T2", "Odd", status="weird", priority="P9",
                                              assignee="*"), now=NOW)
    assert fm["status"] == "proposed" and fm["priority"] == "P2"
    assert fm["assignee"] == "*"                             # broadcast sentinel preserved


def test_migrate_end_to_end_idempotent_and_marked():
    t = FakeTransport()
    t.put("/coordination/tasks/TASK-X-1.json", json.dumps(_incumbent("TASK-X-1", "Fix the widget")))
    t.put("/coordination/tasks/TASK-X-2.json", json.dumps(_incumbent("TASK-X-2", "Done thing", status="done")))
    res = migrate.migrate(t, "fulcra", now=NOW)
    assert res["migrated"] == 1 and res["errors"] == []       # terminal excluded
    doc = t.store["team/fulcra/task/fix-the-widget.md"]
    assert okf.parse_frontmatter(doc)["migrated_from"] == "TASK-X-1"
    # incumbent gets a TERMINAL transition (the one-active-system mechanism), not just a tag
    marked = json.loads(t.store["/coordination/tasks/TASK-X-1.json"])
    assert migrate.MIGRATED_TAG in marked["tags"]
    assert marked["status"] == "abandoned"
    # DATA-COMPAT: new runs WRITE the new marker bytes.
    assert migrate.MIGRATED_TAG == "migrated:coord"
    assert any(e.get("by") == "coord-migrate" for e in marked.get("events", []))
    # second run: skip via the tag (and via migrated_from even if tag missing)
    res2 = migrate.migrate(t, "fulcra", now=NOW)
    assert res2["migrated"] == 0 and res2["skipped"] >= 1


def test_migrate_dry_run_writes_nothing():
    t = FakeTransport()
    t.put("/coordination/tasks/TASK-X-1.json", json.dumps(_incumbent("TASK-X-1", "Fix it")))
    before = dict(t.store)
    res = migrate.migrate(t, "fulcra", now=NOW, dry_run=True)
    assert len(res["planned"]) == 1 and t.store == before


def test_migrate_slug_collision_disambiguates():
    t = FakeTransport()
    t.put("team/fulcra/task/fix-it.md", "---\ntype: Task\ntitle: Fix it\nstatus: active\n---\n")
    t.put("/coordination/tasks/TASK-X-9.json", json.dumps(_incumbent("TASK-X-9", "Fix it")))
    res = migrate.migrate(t, "fulcra", now=NOW)
    assert res["migrated"] == 1
    news = [p for p in t.store if p.startswith("team/fulcra/task/fix-it-")]
    assert len(news) == 1                                     # suffixed, original untouched


def test_migrate_mark_failure_reports_but_keeps_coord_doc():
    t = FakeTransport()
    t.put("/coordination/tasks/TASK-X-1.json", json.dumps(_incumbent("TASK-X-1", "Fix it")))
    orig = t.write
    t.write = lambda p, c: False if p.startswith("/coordination/") else orig(p, c)
    res = migrate.migrate(t, "fulcra", now=NOW)
    assert res["migrated"] == 1 and any("transition FAILED" in e for e in res["errors"])
    assert "team/fulcra/task/fix-it.md" in t.store


def test_migrate_readback_mismatch_leaves_incumbent_open():
    t = FakeTransport()
    t.put("/coordination/tasks/TASK-X-1.json", json.dumps(_incumbent("TASK-X-1", "Fix it")))
    orig = t.read

    def corrupt_coord_read(path):
        if path == "team/fulcra/task/fix-it.md":
            return "corrupted"
        return orig(path)

    t.read = corrupt_coord_read
    res = migrate.migrate(t, "fulcra", now=NOW)
    assert res["migrated"] == 0 and any("not readable back" in e for e in res["errors"])
    inc = json.loads(t.store["/coordination/tasks/TASK-X-1.json"])
    assert inc["status"] == "active"


def test_cli_migrate_dry_run(capsys):
    t = FakeTransport()
    t.put("/coordination/tasks/TASK-X-1.json", json.dumps(_incumbent("TASK-X-1", "Fix it")))
    assert cli.main(["migrate", "fulcra", "--dry-run"], transport=t) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "TASK-X-1 -> task/fix-it.md" in out


def test_migrate_skips_open_review_loops():
    t = FakeTransport()
    t.put("/coordination/tasks/TASK-R.json",
          json.dumps(_incumbent("TASK-R", "Review PR 9", pr="https://github.com/o/r/pull/9")))
    res = migrate.migrate(t, "fulcra", now=NOW)
    assert res["migrated"] == 0 and res["skipped_review"] == 1
    inc = json.loads(t.store["/coordination/tasks/TASK-R.json"])
    assert inc["status"] == "active"                          # untouched


def test_migrate_skips_review_workstream_dispatches():
    t = FakeTransport()
    t.put("/coordination/tasks/TASK-R.json",
          json.dumps(_incumbent("TASK-R", "Review migration plan",
                                workstream="review", kind="dispatch",
                                tags=["kind:dispatch", "workstream:review"])))
    res = migrate.migrate(t, "fulcra", now=NOW)
    assert res["migrated"] == 0 and res["skipped_review"] == 1
    inc = json.loads(t.store["/coordination/tasks/TASK-R.json"])
    assert inc["status"] == "active"


def test_migrate_repair_pass_finishes_incumbent_transition():
    t = FakeTransport()
    # coord twin exists (migrated_from), incumbent still open (mark failed previously)
    t.put("team/fulcra/task/fix-it.md",
          "---\ntype: Task\ntitle: Fix it\nstatus: active\nmigrated_from: TASK-X-1\n---\n")
    t.put("/coordination/tasks/TASK-X-1.json", json.dumps(_incumbent("TASK-X-1", "Fix it")))
    res = migrate.migrate(t, "fulcra", now=NOW)
    assert res["repaired"] == 1 and res["migrated"] == 0      # no duplicate doc
    inc = json.loads(t.store["/coordination/tasks/TASK-X-1.json"])
    assert inc["status"] == "abandoned"                       # dual-listing healed


def test_repair_pass_never_clobbers_operator_reopen():
    t = FakeTransport()
    t.put("team/fulcra/task/fix-it.md",
          "---\ntype: Task\ntitle: Fix it\nstatus: active\nmigrated_from: TASK-X-1\n---\n")
    # DATA-COMPAT: the reopen marker was written by an OLD build (`coord2-migrate`);
    # recognition must still hold after the rename or we'd re-terminalize an operator reopen.
    reopened = _incumbent("TASK-X-1", "Fix it", status="active",
                          events=[{"at": "x", "type": "abandoned", "by": "coord2-migrate",
                                   "summary": "migrated"}])
    t.put("/coordination/tasks/TASK-X-1.json", json.dumps(reopened))
    res = migrate.migrate(t, "fulcra", now=NOW)
    assert res["repaired"] == 0
    assert any("reopened by operator" in e for e in res["errors"])
    assert json.loads(t.store["/coordination/tasks/TASK-X-1.json"])["status"] == "active"


def test_repair_pass_recognizes_new_marker_reopen():
    # Mirror of the legacy-marker reopen test, proving the NEW `coord-migrate`
    # bytes are read back as a reopen signal too.
    t = FakeTransport()
    t.put("team/fulcra/task/fix-it.md",
          "---\ntype: Task\ntitle: Fix it\nstatus: active\nmigrated_from: TASK-X-1\n---\n")
    reopened = _incumbent("TASK-X-1", "Fix it", status="active",
                          events=[{"at": "x", "type": "abandoned", "by": "coord-migrate",
                                   "summary": "migrated"}])
    t.put("/coordination/tasks/TASK-X-1.json", json.dumps(reopened))
    res = migrate.migrate(t, "fulcra", now=NOW)
    assert res["repaired"] == 0
    assert any("reopened by operator" in e for e in res["errors"])
    assert json.loads(t.store["/coordination/tasks/TASK-X-1.json"])["status"] == "active"


def test_migrate_skips_task_tagged_by_old_build():
    # DATA-COMPAT: a task an OLD build already migrated carries `migrated:coord2`.
    # It must be recognized and skipped (never re-migrated) after the rename.
    t = FakeTransport()
    t.put("/coordination/tasks/TASK-X-1.json",
          json.dumps(_incumbent("TASK-X-1", "Fix it",
                                tags=["agent:x", migrate.LEGACY_MIGRATED_TAG])))
    res = migrate.migrate(t, "fulcra", now=NOW)
    assert res["migrated"] == 0 and res["skipped"] >= 1
    # untouched: not re-terminalized, no coord twin written
    assert "team/fulcra/task/fix-it.md" not in t.store
    assert json.loads(t.store["/coordination/tasks/TASK-X-1.json"])["status"] == "active"


def test_migrate_mixed_store_folds_both_generations():
    # A store holding one already-migrated (old-tag) task, one already-migrated
    # (new-tag) task, and one fresh task: only the fresh one migrates; both
    # generations of marker are recognized as done.
    t = FakeTransport()
    t.put("/coordination/tasks/OLD.json",
          json.dumps(_incumbent("OLD", "Old done", tags=[migrate.LEGACY_MIGRATED_TAG])))
    t.put("/coordination/tasks/NEW.json",
          json.dumps(_incumbent("NEW", "New done", tags=[migrate.MIGRATED_TAG])))
    t.put("/coordination/tasks/FRESH.json", json.dumps(_incumbent("FRESH", "Fresh work")))
    res = migrate.migrate(t, "fulcra", now=NOW)
    assert res["migrated"] == 1 and res["skipped"] == 2
    fresh = json.loads(t.store["/coordination/tasks/FRESH.json"])
    assert migrate.MIGRATED_TAG in fresh["tags"]                # fresh got the NEW tag
    assert any(e.get("by") == "coord-migrate" for e in fresh.get("events", []))


def test_map_task_preserves_links_and_checklist_in_body():
    t = _incumbent("T-L", "Linky", links={"prs": ["https://github.com/o/r/pull/7"],
                                          "local_ticket": "JIRA-9"},
                   checklist=["step one", "step two"])
    _, _, body = migrate.map_task(t, now=NOW)
    assert "PR: https://github.com/o/r/pull/7" in body
    assert "Ticket: JIRA-9" in body and "- [ ] step one" in body


def test_long_title_slug_capped_and_stable():
    from coord_engine.tasks import MAX_SLUG_LEN
    long_title = "drift alert " + "x" * 700
    a = _incumbent("TASK-LONG-1", long_title)
    slug1, _, _ = migrate.map_task(a, now=NOW)
    assert len(slug1) <= MAX_SLUG_LEN
    # same prefix, different id -> different slug (no collision)
    slug2, _, _ = migrate.map_task(_incumbent("TASK-LONG-2", long_title), now=NOW)
    assert slug1 != slug2
    # deterministic across runs
    assert slug1 == migrate.map_task(a, now=NOW)[0]
