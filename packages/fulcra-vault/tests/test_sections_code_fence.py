"""Owned-section markers inside fenced code blocks are documentation, not sections.

parse_sections matched the markers line-by-line with no fence awareness, so a
note that documents the section syntax inside a ``` block had those example
markers treated as live sections — and a fenced example reusing a real slug
raised DuplicateSectionError, breaking every section op on the note.
"""
from fulcra_vault.sections import parse_sections


def test_parse_sections_ignores_markers_in_code_fence():
    md = (
        "```\n"
        "<!-- section:demo owner:x -->\n"
        "body\n"
        "<!-- /section:demo -->\n"
        "```\n"
        "<!-- section:real owner:y -->\n"
        "r\n"
        "<!-- /section:real -->\n"
    )
    assert [s.slug for s in parse_sections(md)] == ["real"]


def test_fenced_example_reusing_real_slug_does_not_raise():
    md = (
        "<!-- section:notes owner:y -->\n"
        "real body\n"
        "<!-- /section:notes -->\n"
        "## How to\n"
        "```\n"
        "<!-- section:notes owner:x -->\n"
        "example\n"
        "<!-- /section:notes -->\n"
        "```\n"
    )
    assert [s.slug for s in parse_sections(md)] == ["notes"]
