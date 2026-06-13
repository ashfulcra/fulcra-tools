import pytest

from fulcra_vault.frontmatter import FrontmatterError, parse_note, update_keys


NOTE = """---
title: Demo
tags:
- alpha
- beta
active: true
priority: 2
---

# Demo

<!-- section:summary owner:agent-a -->
body
<!-- /section:summary -->
"""


def test_parse_note_reads_flat_scalars_and_lists():
    fm, body = parse_note(NOTE)

    assert fm == {
        "title": "Demo",
        "tags": ["alpha", "beta"],
        "active": True,
        "priority": 2,
    }
    assert body.startswith("\n# Demo")


def test_update_keys_serializes_stable_sorted_frontmatter():
    changed = update_keys(NOTE, {"updated_by": "human", "priority": 3})

    assert changed.startswith(
        "---\n"
        "active: true\n"
        "priority: 3\n"
        "tags:\n"
        "- alpha\n"
        "- beta\n"
        "title: Demo\n"
        "updated_by: human\n"
        "---\n"
    )
    assert "<!-- section:summary owner:agent-a -->\nbody" in changed


def test_note_without_frontmatter_can_be_initialized():
    changed = update_keys("# Title\n", {"title": "Title", "tags": ["seed"]})

    assert changed == "---\ntags:\n- seed\ntitle: Title\n---\n# Title\n"


@pytest.mark.parametrize(
    "markdown",
    [
        "---\ntitle Demo\n---\nbody",
        "---\nparent:\n  child: bad\n---\nbody",
        "---\nitems: [1,\n---\nbody",
        "---\nmissing: true\nbody",
    ],
)
def test_parse_note_rejects_invalid_frontmatter(markdown):
    with pytest.raises(FrontmatterError):
        parse_note(markdown)


@pytest.mark.parametrize(
    "changes",
    [
        {"bad.key": "x"},
        {"nested": {"x": 1}},
        {"items": [{"x": 1}]},
        {"none": None},
    ],
)
def test_update_keys_rejects_unsupported_keys_and_values(changes):
    with pytest.raises(FrontmatterError):
        update_keys(NOTE, changes)


def test_quoted_and_special_strings_round_trip():
    changed = update_keys("# Body\n", {"title": "A: B", "empty": ""})

    fm, body = parse_note(changed)
    assert fm == {"title": "A: B", "empty": ""}
    assert body == "# Body\n"


def test_type_ambiguous_strings_round_trip_as_strings():
    values = {
        "zip": "02134",
        "count": "123",
        "rating": "7.5",
        "enabled": "true",
        "disabled": "false",
        "nothing": "null",
    }

    changed = update_keys("# Body\n", values)

    fm, body = parse_note(changed)
    assert fm == values
    assert body == "# Body\n"


def test_quote_prefixed_strings_round_trip_as_strings():
    values = {
        "single": "'tis the season",
        "double": '"quoted start',
    }

    changed = update_keys("# Body\n", values)

    fm, body = parse_note(changed)
    assert fm == values
    assert body == "# Body\n"


def test_single_quoted_scalars_parse_like_yaml_strings():
    fm, body = parse_note("---\ntitle: 'Ash''s note'\n---\nBody\n")

    assert fm == {"title": "Ash's note"}
    assert body == "Body\n"
