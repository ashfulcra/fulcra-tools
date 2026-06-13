import json

import pytest

from fulcra_vault.links import (
    backlinks_for,
    build_index,
    extract_wikilinks,
    index_json,
    plan_rename,
)


def test_extract_wikilinks_handles_aliases_headings_and_duplicates():
    markdown = (
        "See [[People/Ash|Ash]], [[Project Alpha#Decision]], "
        "[[People/Ash]], and [plain](Project.md)."
    )

    assert extract_wikilinks(markdown) == ["People/Ash.md", "Project Alpha.md"]


def test_index_is_deterministic_for_shuffled_input():
    a = {
        "B.md": "[[A]] [[C]]",
        "A.md": "[[C]]",
    }
    b = {
        "A.md": "[[C]]",
        "B.md": "[[C]] [[A]]",
    }

    assert index_json(a) == index_json(b)
    parsed = json.loads(index_json(a))
    assert parsed["backlinks"]["C.md"] == ["A.md", "B.md"]


def test_backlinks_for_returns_deterministic_sources():
    index = build_index({
        "z.md": "[[target]]",
        "a.md": "[[target]]",
    })

    assert backlinks_for(index, "target") == ["a.md", "z.md"]


def test_plan_rename_rewrites_referring_wikilinks_and_target_path():
    notes = {
        "Alpha.md": "# Alpha\n",
        "Refs.md": "[[Alpha|old]] and [[Alpha#Heading]] and [[Other]]",
    }

    plan = plan_rename(notes, "Alpha", "Projects/Alpha")

    assert plan.source == "Alpha.md"
    assert plan.destination == "Projects/Alpha.md"
    assert plan.rewrites["Projects/Alpha.md"] == "# Alpha\n"
    assert "[[Projects/Alpha|old]]" in plan.rewrites["Refs.md"]
    assert "[[Projects/Alpha#Heading]]" in plan.rewrites["Refs.md"]
    assert plan.dangling == ("Other.md",)


def test_plan_rename_refuses_missing_source_or_existing_destination():
    with pytest.raises(ValueError, match="source"):
        plan_rename({"A.md": ""}, "Missing", "B")
    with pytest.raises(ValueError, match="destination"):
        plan_rename({"A.md": "", "B.md": ""}, "A", "B")
