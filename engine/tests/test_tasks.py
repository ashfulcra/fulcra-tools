import pytest

from coord_engine import model, okf, tasks

NOW = "2026-07-01T18:00:00Z"


def test_slugify():
    assert tasks.slugify("Fix the Widget!") == "fix-the-widget"
    assert tasks.slugify("  ") == "task"


def test_status_transitions():
    assert model.is_valid_transition("proposed", "active")
    assert model.is_valid_transition("active", "done")
    assert model.is_valid_transition("active", "active")   # no-op ok
    assert not model.is_valid_transition("done", "active")  # terminal
    assert not model.is_valid_transition("waiting", "done")  # must go active first


def test_render_frontmatter_roundtrips():
    fm = {"type": "Task", "title": "T", "status": "active", "tags": ["a:b", "c:d"],
          "priority": "P1", "assignee": None}
    text = okf.render_frontmatter(fm) + "\nbody"
    parsed = okf.parse_frontmatter(text)
    assert parsed["type"] == "Task"
    assert parsed["title"] == "T"
    assert parsed["status"] == "active"
    assert parsed["tags"] == ["a:b", "c:d"]
    assert "assignee" not in parsed  # None omitted


def test_render_frontmatter_roundtrips_multiline_scalars_and_comma_lists():
    fm = {
        "type": "Task",
        "description": "first line\nsecond line",
        "next_action": "do one\ndo two",
        "tags": ["workstream:web,api", "kind:bug"],
    }
    assert okf.parse_frontmatter(okf.render_frontmatter(fm)) == fm


def test_new_task_doc():
    slug, content = tasks.new_task_doc(
        "Wire up L2", now=NOW, workstream="coord2", priority="P1",
        status="active", summary="typed lifecycle", assignee="ash", kind="feature")
    assert slug == "wire-up-l2"
    fm = okf.parse_frontmatter(content)
    assert fm["type"] == "Task" and fm["status"] == "active" and fm["priority"] == "P1"
    assert fm["assignee"] == "ash"
    assert "workstream:coord2" in fm["tags"] and "kind:feature" in fm["tags"]


def test_new_task_doc_rejects_bad_enum():
    with pytest.raises(tasks.TaskError):
        tasks.new_task_doc("x", now=NOW, status="bogus")
    with pytest.raises(tasks.TaskError):
        tasks.new_task_doc("x", now=NOW, priority="P9")


def test_apply_update_legal_transition_and_note():
    doc = tasks.new_task_doc("T", now="2026-07-01T00:00:00Z", status="active")[1]
    out = tasks.apply_update(doc, now=NOW, status="waiting", next_action="hold")
    fm = okf.parse_frontmatter(out)
    assert fm["status"] == "waiting"
    assert fm["next_action"] == "hold"
    assert fm["timestamp"] == NOW
    assert "active → waiting" in out  # body note appended


def test_apply_update_rejects_illegal_transition():
    doc = tasks.new_task_doc("T", now=NOW, status="done")  # can't build done? proposed->done ok
    # build an active task then try done->active
    done_doc = tasks.apply_update(tasks.new_task_doc("T", now=NOW, status="active")[1],
                                  now=NOW, status="done", evidence="e")
    with pytest.raises(tasks.TaskError):
        tasks.apply_update(done_doc, now=NOW, status="active")


def test_apply_update_missing_doc():
    with pytest.raises(tasks.TaskError):
        tasks.apply_update("garbage no frontmatter", now=NOW, status="active")


def test_mark_done_requires_evidence():
    doc = tasks.new_task_doc("T", now=NOW, status="active")[1]
    with pytest.raises(tasks.TaskError):
        tasks.mark_done(doc, now=NOW, evidence="")
    out = tasks.mark_done(doc, now=NOW, evidence="PR #9 merged")
    fm = okf.parse_frontmatter(out)
    assert fm["status"] == "done"
    assert "evidence: PR #9 merged" in out
