from coord_engine import model


def test_is_task():
    assert model.is_task({"type": "Task"})
    assert not model.is_task({"type": "Playbook"})
    assert not model.is_task(None)
    assert not model.is_task({})


def test_row_backfills_defaults():
    row = model.row_from_frontmatter({"type": "Task"}, name="foo", path="task/foo.md")
    assert row["id"] == "foo"          # id backfilled from name
    assert row["title"] == "foo"       # title backfilled from name
    assert row["status"] == "proposed"  # default
    assert row["priority"] == "P2"      # default
    assert row["tags"] == []
    assert row["assignee"] is None


def test_row_carries_explicit_fields():
    fm = {
        "type": "Task", "id": "TASK-1", "title": "T", "description": "d",
        "status": "active", "priority": "P1", "owner": "o", "assignee": "a",
        "tags": ["k:bug"], "timestamp": "2026-07-01T00:00:00Z",
        "blocked_on": "x", "due": "2026-07-02", "not_before": "2026-07-01",
        "next_action": "go",
    }
    row = model.row_from_frontmatter(fm, name="foo", path="task/foo.md", mtime="2026-07-01 04:12PM UTC")
    assert row["id"] == "TASK-1"
    assert row["status"] == "active"
    assert row["priority"] == "P1"
    assert row["assignee"] == "a"
    assert row["tags"] == ["k:bug"]
    assert row["mtime"] == "2026-07-01 04:12PM UTC"


def test_row_scalar_tag_normalized_to_list():
    row = model.row_from_frontmatter({"type": "Task", "tags": "solo"}, name="f", path="p")
    assert row["tags"] == ["solo"]


def test_priority_key_order():
    assert model.priority_key({"priority": "P0"}) < model.priority_key({"priority": "P3"})
    # unknown priority sorts last
    assert model.priority_key({"priority": "P9"}) == len(model.VALID_PRIORITIES)


def test_sort_rows_priority_then_recency():
    rows = [
        {"id": "a", "priority": "P2", "timestamp": "2026-01-01T00:00:00Z"},
        {"id": "b", "priority": "P0", "timestamp": "2026-01-01T00:00:00Z"},
        {"id": "c", "priority": "P2", "timestamp": "2026-06-01T00:00:00Z"},  # newer
    ]
    ordered = [r["id"] for r in model.sort_rows(rows)]
    assert ordered[0] == "b"          # P0 first
    assert ordered.index("c") < ordered.index("a")  # newer P2 before older P2


def test_sort_rows_missing_timestamp_last():
    rows = [
        {"id": "none", "priority": "P2", "timestamp": None},
        {"id": "dated", "priority": "P2", "timestamp": "2026-01-01T00:00:00Z"},
    ]
    ordered = [r["id"] for r in model.sort_rows(rows)]
    assert ordered == ["dated", "none"]


def test_terminal_and_open_sets():
    assert model.TERMINAL_STATUSES == {"done", "abandoned"}
    assert "active" in model.OPEN_STATUSES
    assert "done" not in model.OPEN_STATUSES


# --- summaries-row text cap (COORD_SUMMARY_TEXT_CAP) ---------------------------
# The summaries index is a SUMMARY: a fleet whose directives carry multi-KB
# payloads in title/description must not inflate _coord/summaries.json past what
# a remote transport can read inside the fold budgets (the 954KiB incident,
# 2026-07-15: every remote briefing degraded to "summaries index unreadable").


def test_row_caps_long_title_and_description():
    long = "x" * 5000
    row = model.row_from_frontmatter(
        {"type": "Task", "title": long, "description": long},
        name="foo", path="task/foo.md")
    assert len(row["title"]) == model.DEFAULT_SUMMARY_TEXT_CAP
    assert len(row["description"]) == model.DEFAULT_SUMMARY_TEXT_CAP
    assert row["title"].endswith("…")
    assert row["description"].endswith("…")
    assert row["title"].startswith("x" * 10)


def test_row_short_text_untouched():
    row = model.row_from_frontmatter(
        {"type": "Task", "title": "short", "description": "also short"},
        name="foo", path="task/foo.md")
    assert row["title"] == "short"
    assert row["description"] == "also short"


def test_cap_boundary_exact_length_untouched():
    exact = "y" * model.DEFAULT_SUMMARY_TEXT_CAP
    assert model.cap_summary_text(exact) == exact
    over = exact + "z"
    capped = model.cap_summary_text(over)
    assert len(capped) == model.DEFAULT_SUMMARY_TEXT_CAP
    assert capped.endswith("…")


def test_cap_env_override(monkeypatch):
    monkeypatch.setenv("COORD_SUMMARY_TEXT_CAP", "40")
    row = model.row_from_frontmatter(
        {"type": "Task", "title": "t" * 100}, name="foo", path="task/foo.md")
    assert len(row["title"]) == 40
    assert row["title"].endswith("…")


def test_cap_bad_env_falls_back_to_default(monkeypatch):
    # config policy: unparseable / non-positive / non-finite -> default, never unbounded
    for bad in ("nan", "-5", "0", "banana", "inf"):
        monkeypatch.setenv("COORD_SUMMARY_TEXT_CAP", bad)
        row = model.row_from_frontmatter(
            {"type": "Task", "title": "t" * 5000}, name="foo", path="task/foo.md")
        assert len(row["title"]) == model.DEFAULT_SUMMARY_TEXT_CAP, bad
