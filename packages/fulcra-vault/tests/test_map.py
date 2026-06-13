from datetime import datetime, timezone

import pytest

from fulcra_vault.links import build_index
from fulcra_vault.map import (
    BudgetError,
    HotItem,
    check_budget,
    render_hot,
    render_map,
    select_hot_items,
    truncate_markdown,
)
from fulcra_vault.schema import StructureSpec


SPEC = StructureSpec.from_dict({
    "sections": [
        {
            "slug": "projects",
            "title": "Projects",
            "description": "Active work",
            "seed_notes": ["Project Alpha", "Project Beta"],
        },
        {
            "slug": "people",
            "title": "People",
            "description": "Durable people notes",
            "seed_notes": ["People/Ash"],
        },
    ],
    "map_highlights": ["Project Alpha"],
})


NOTES = {
    "Project Alpha.md": """---
title: Project Alpha
status: active
updated_at: 2026-06-12T12:00:00+00:00
tags:
- standing-correction
---

# Project Alpha

Alpha first line.
See [[People/Ash]].

## Log
- 2026-06-12T12:00:00+00:00 codex: decided the alpha shape
""",
    "Project Beta.md": """---
title: Project Beta
status: parked
updated_at: 2026-06-10T12:00:00+00:00
---

# Project Beta

Beta first line.
""",
    "People/Ash.md": """---
title: Ash
updated_at: 2026-06-11T12:00:00+00:00
---

# Ash

Ash first line.
See [[Project Alpha]].
""",
}


def test_render_map_is_deterministic_and_preserves_section_order():
    links = build_index({
        "Project Alpha.md": "[[People/Ash]]",
        "People/Ash.md": "[[Project Alpha]]",
    })

    rendered = render_map(SPEC, NOTES, links)

    assert rendered == render_map(SPEC, dict(reversed(NOTES.items())), links)
    assert rendered.startswith("# Vault Map\n\n")
    assert rendered.index("## Projects") < rendered.index("## People")
    assert "- [[Project Alpha|Project Alpha]] — Alpha first line. (hot, 1 link, 1 backlink)" in rendered
    assert "- [[Project Beta|Project Beta]] — Beta first line." in rendered
    assert "- [[People/Ash|Ash]] — Ash first line. (1 link, 1 backlink)" in rendered


def test_select_hot_items_prioritizes_active_corrections_and_recent_decisions():
    links = build_index({
        "Project Alpha.md": "[[People/Ash]]",
        "People/Ash.md": "[[Project Alpha]]",
    })

    items = select_hot_items(NOTES, links, datetime(2026, 6, 13, tzinfo=timezone.utc))

    assert [item.path for item in items] == [
        "Project Alpha.md",
        "People/Ash.md",
        "Project Beta.md",
    ]
    assert items[0].reasons == ("active", "standing-correction", "recent-decision")
    assert items[0].backlink_count == 1


def test_select_hot_items_honors_max_items():
    items = select_hot_items(NOTES, build_index(NOTES), datetime(2026, 6, 13, tzinfo=timezone.utc), max_items=1)

    assert [item.path for item in items] == ["Project Alpha.md"]


def test_select_hot_items_ranks_undated_notes_after_dated_notes():
    notes = {
        "Old.md": "---\ntitle: Old\nupdated_at: 2020-01-01T00:00:00+00:00\n---\n# Old\n",
        "Undated.md": "---\ntitle: Undated\n---\n# Undated\n",
        "New.md": "---\ntitle: New\nupdated_at: 2026-06-13T00:00:00+00:00\n---\n# New\n",
        "NonAscii.md": "---\ntitle: NonAscii\nupdated_at: 2026-06-13T00:00:00+00:00\U0001f642\n---\n# NonAscii\n",
    }

    items = select_hot_items(notes, build_index(notes), datetime(2026, 6, 13, tzinfo=timezone.utc))

    assert [item.path for item in items] == [
        "New.md",
        "Old.md",
        "NonAscii.md",
        "Undated.md",
    ]


def test_recent_decision_accepts_extra_space_in_log_heading():
    notes = {
        "Decision.md": """---
title: Decision
---
# Decision

##  Log
- 2026-06-12T12:00:00+00:00 codex: decided the thing
""",
    }

    items = select_hot_items(notes, build_index(notes), datetime(2026, 6, 13, tzinfo=timezone.utc))

    assert items[0].reasons == ("recent-decision",)


def test_render_hot_is_budgeted_at_section_boundaries():
    items = [
        HotItem(path="A.md", title="A", summary="one two", reasons=("active",), updated_at="2026-06-12", backlink_count=0),
        HotItem(path="B.md", title="B", summary="three four", reasons=("recent",), updated_at="2026-06-11", backlink_count=0),
    ]

    rendered = render_hot(items, max_words=10)

    assert rendered == (
        "# Hot\n\n"
        "## [[A|A]]\n"
        "- Reasons: active\n"
        "- Updated: 2026-06-12\n"
        "- Summary: one two\n\n"
        "(truncated - run fulcra-vault map)\n"
    )


def test_render_hot_empty_state_is_deterministic():
    assert render_hot([]) == "# Hot\n\nNo hot items.\n"


def test_budget_check_reports_without_mutating():
    markdown = "# Title\n\n## A\none two three\n"

    with pytest.raises(BudgetError, match="3 words over budget"):
        check_budget(markdown, max_words=2, label="MAP")

    assert check_budget(markdown, max_words=5, label="MAP") == markdown


def test_truncate_markdown_never_cuts_mid_section():
    markdown = "# Hot\n\n## A\none two three\n\n## B\nfour five six\n"

    assert truncate_markdown(markdown, max_words=6) == "# Hot\n\n## A\none two three\n\n(truncated - run fulcra-vault map)\n"
