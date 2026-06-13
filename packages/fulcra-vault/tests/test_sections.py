from datetime import datetime, timezone

import pytest

from fulcra_vault.sections import (
    DuplicateSectionError,
    MissingSectionError,
    OwnerMismatchError,
    SectionError,
    append_log,
    parse_sections,
    replace_owned_section,
)


NOTE = """---
title: Demo
---

# Demo

before
<!-- section:summary owner:agent-a -->
old summary
<!-- /section:summary -->
middle
<!-- section:notes owner:agent-b -->
old notes
<!-- /section:notes -->

## Log

- existing
"""


def test_parse_sections_returns_boundaries_and_owners():
    sections = parse_sections(NOTE)

    assert [s.slug for s in sections] == ["summary", "notes"]
    assert sections[0].owner == "agent-a"
    assert sections[0].body_start_line == sections[0].open_line + 1
    assert sections[0].close_line > sections[0].body_start_line


def test_replace_owned_section_touches_only_target_body():
    changed = replace_owned_section(NOTE, "summary", "agent-a", "new\nsummary")

    assert "new\nsummary\n<!-- /section:summary -->" in changed
    assert "old summary" not in changed
    assert "old notes" in changed
    assert changed.startswith("---\ntitle: Demo\n---")
    assert changed.endswith("- existing\n")


def test_replace_owned_section_requires_owner_unless_forced():
    with pytest.raises(OwnerMismatchError):
        replace_owned_section(NOTE, "summary", "agent-b", "bad")

    changed = replace_owned_section(NOTE, "summary", "agent-b", "forced", force=True)
    assert "forced\n<!-- /section:summary -->" in changed


def test_replace_owned_section_reports_missing_and_duplicate():
    with pytest.raises(MissingSectionError):
        replace_owned_section(NOTE, "missing", "agent-a", "x")

    dup = NOTE + "\n<!-- section:summary owner:agent-a -->\nagain\n<!-- /section:summary -->\n"
    with pytest.raises(DuplicateSectionError):
        parse_sections(dup)


def test_parse_sections_rejects_nested_and_mismatched_markers():
    nested = "<!-- section:a owner:x -->\n<!-- section:b owner:x -->\n<!-- /section:b -->\n<!-- /section:a -->\n"
    with pytest.raises(SectionError):
        parse_sections(nested)

    mismatched = "<!-- section:a owner:x -->\nbody\n<!-- /section:b -->\n"
    with pytest.raises(SectionError):
        parse_sections(mismatched)


def test_append_log_adds_one_dated_line_after_log_heading():
    now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)

    changed = append_log(NOTE, "updated summary", now, "agent-a")

    assert "- 2026-06-12T12:00:00+00:00 agent-a: updated summary\n- existing" in changed
    assert changed.count("- existing") == 1


def test_append_log_requires_log_section_and_non_empty_entry():
    with pytest.raises(MissingSectionError):
        append_log("# No log\n", "entry", datetime.now(timezone.utc), "agent")
    with pytest.raises(SectionError):
        append_log(NOTE, "   ", datetime.now(timezone.utc), "agent")
