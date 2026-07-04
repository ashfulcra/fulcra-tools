from coord_engine import okf


# --- split_frontmatter ---

def test_split_valid():
    fm, body = okf.split_frontmatter("---\ntype: Task\n---\nhello\nworld")
    assert fm == "type: Task"
    assert body == "hello\nworld"


def test_split_leading_blank_lines_tolerated():
    assert okf.split_frontmatter("\n\n---\na: 1\n---\nb") == ("a: 1", "b")


def test_split_no_frontmatter():
    assert okf.split_frontmatter("just a body, no dashes") is None


def test_split_no_closing_delim():
    assert okf.split_frontmatter("---\ntype: Task\nnever closes") is None


def test_split_empty_or_none():
    assert okf.split_frontmatter("") is None
    assert okf.split_frontmatter(None) is None


# --- parse_frontmatter ---

def test_parse_scalars_and_types():
    fm = okf.parse_frontmatter(
        "---\n"
        "type: Task\n"
        'title: "Quoted title"\n'
        "status: active\n"
        "assignee: null\n"
        "empty:\n"
        "flag: true\n"
        "---\nbody"
    )
    assert fm["type"] == "Task"
    assert fm["title"] == "Quoted title"
    assert fm["status"] == "active"
    assert fm["assignee"] is None
    assert fm["empty"] is None
    assert fm["flag"] is True


def test_parse_inline_list():
    fm = okf.parse_frontmatter("---\ntype: Task\ntags: [workstream:x, kind:bug]\n---\n")
    assert fm["tags"] == ["workstream:x", "kind:bug"]


def test_parse_block_list():
    fm = okf.parse_frontmatter(
        "---\ntype: Task\ntags:\n  - workstream:x\n  - kind:bug\n---\n"
    )
    assert fm["tags"] == ["workstream:x", "kind:bug"]


def test_parse_comment_lines_ignored():
    fm = okf.parse_frontmatter("---\n# a comment\ntype: Task\n---\n")
    assert fm == {"type": "Task"}


def test_parse_missing_frontmatter_returns_none():
    assert okf.parse_frontmatter("no frontmatter here") is None


def test_parse_value_with_colon_kept():
    fm = okf.parse_frontmatter("---\ntype: Task\nid: TASK-2026-abc:def\n---\n")
    assert fm["id"] == "TASK-2026-abc:def"


# --- render_index (OKF §6) ---

def _row(name, status="active", title=None, desc="", priority="P2", ts=None):
    return {
        "id": name, "name": name, "path": f"task/{name}.md",
        "title": title or name, "description": desc, "status": status,
        "priority": priority, "timestamp": ts, "tags": [],
    }


def test_render_index_sections_and_bullets():
    out = okf.render_index([
        _row("a", "active", "Alpha", "do alpha"),
        _row("d", "done", "Delta", "did delta"),
    ])
    assert "# Tasks" in out
    assert "## Active" in out
    assert "* [Alpha](a.md) - do alpha" in out
    assert "## Recently Done" in out
    assert "* [Delta](d.md) - did delta" in out
    # empty sections omitted
    assert "## Waiting" not in out


def test_render_index_bullet_without_description():
    out = okf.render_index([_row("a", "active", "Alpha", "")])
    assert "* [Alpha](a.md)" in out
    assert " - " not in out.split("* [Alpha]")[1].split("\n")[0]


def test_render_index_unknown_status_goes_to_other():
    out = okf.render_index([_row("x", status="mystery")])
    assert "## Other" in out
    assert "* [x](x.md)" in out


def test_render_index_priority_ordering():
    out = okf.render_index([
        _row("low", "active", "Low", priority="P3", ts="2026-01-01T00:00:00Z"),
        _row("hi", "active", "Hi", priority="P0", ts="2026-01-01T00:00:00Z"),
    ])
    assert out.index("[Hi]") < out.index("[Low]")


# --- log (OKF §7) ---

def test_merge_log_into_empty():
    out = okf.merge_log(None, ["* **Creation**: [a](a.md) created (active)."], date="2026-07-01")
    assert out.startswith(okf.ENGINE_BANNER)  # in-band ownership guardrail
    assert "# Task Update Log" in out
    assert "## 2026-07-01" in out
    assert "Creation" in out


def test_render_index_has_engine_banner():
    out = okf.render_index([_row("a", "active", "Alpha", "x")])
    assert out.startswith(okf.ENGINE_BANNER)
    assert "ENGINE-OWNED" in out


def test_merge_log_prepends_new_date_newest_first():
    existing = "# Task Update Log\n\n## 2026-06-30\n* **Update**: old.\n"
    out = okf.merge_log(existing, ["* **Update**: [a](a.md) active → done."], date="2026-07-01")
    assert out.index("2026-07-01") < out.index("2026-06-30")
    assert "old." in out  # history preserved


def test_merge_log_same_date_inserts_under_existing_heading():
    existing = "# Task Update Log\n\n## 2026-07-01\n* **Update**: first.\n"
    out = okf.merge_log(existing, ["* **Update**: second."], date="2026-07-01")
    assert out.count("## 2026-07-01") == 1
    assert "first." in out and "second." in out


def test_merge_log_no_transitions_returns_existing():
    existing = "# Task Update Log\n\n## 2026-06-30\n* x\n"
    assert okf.merge_log(existing, [], date="2026-07-01") == existing
