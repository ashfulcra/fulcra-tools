import pytest

from coord_engine import model, okf, tasks

NOW = "2026-07-01T18:00:00Z"

# clock-pin support (see #378):
from datetime import datetime, timezone
from coord_engine import cli
PINNED_NOW = datetime(2026, 7, 1, 18, 30, tzinfo=timezone.utc)


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
        "Wire up L2", now=NOW, workstream="coord", priority="P1",
        status="active", summary="typed lifecycle", assignee="ash", kind="feature")
    assert slug == "wire-up-l2"
    fm = okf.parse_frontmatter(content)
    assert fm["type"] == "Task" and fm["status"] == "active" and fm["priority"] == "P1"
    assert fm["assignee"] == "ash"
    assert "workstream:coord" in fm["tags"] and "kind:feature" in fm["tags"]


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


def test_apply_update_done_without_evidence_rejected():
    # the "done requires evidence" invariant must hold via the update path too,
    # not only mark_done (regression guard for the merge-race that dropped it)
    doc = tasks.new_task_doc("T", now=NOW, status="active")[1]
    with pytest.raises(tasks.TaskError):
        tasks.apply_update(doc, now=NOW, status="done")  # no evidence
    ok = tasks.apply_update(doc, now=NOW, status="done", evidence="done it")
    assert okf.parse_frontmatter(ok)["status"] == "done"
