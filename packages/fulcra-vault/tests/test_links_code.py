"""Wikilinks inside code (fences or inline spans) are examples, not real links.

extract_wikilinks scanned the whole document, so [[Target]] inside a ``` fence
or an inline `code` span created phantom backlinks, inflated MAP.md badges, and
got rewritten by plan_rename — corrupting documentation that merely shows the
syntax.
"""
from fulcra_vault.links import extract_wikilinks


def test_wikilinks_ignores_code_fence():
    md = "```\n[[InFence]]\n```\n[[Real]]\n"
    assert extract_wikilinks(md) == ["Real.md"]


def test_wikilinks_ignores_shorter_nested_fence_example():
    md = "````\nexample:\n```\n[[InFence]]\n```\n````\n[[Real]]\n"
    assert extract_wikilinks(md) == ["Real.md"]


def test_wikilinks_ignores_inline_code():
    md = "use `[[Inline]]` here\n[[Real]]\n"
    assert extract_wikilinks(md) == ["Real.md"]


def test_wikilinks_outside_code_still_found():
    assert extract_wikilinks("[[A]] and [[B]]\n") == ["A.md", "B.md"]


def test_rename_does_not_rewrite_links_in_code():
    from fulcra_vault.links import plan_rename
    note_map = {
        "Old.md": "# Old\n",
        "Doc.md": "Real [[Old]]\n```\n[[Old]]\n```\ninline `[[Old]]` x\n",
    }
    plan = plan_rename(note_map, "Old", "New")
    updated = next(v for k, v in plan.rewrites.items() if "Doc" in k)
    assert "[[New]]" in updated          # the real link is renamed
    assert updated.count("[[Old]]") == 2  # fenced + inline examples untouched


def test_rename_ignores_shorter_nested_fence_example():
    from fulcra_vault.links import plan_rename
    note_map = {
        "Old.md": "# Old\n",
        "Doc.md": "Real [[Old]]\n````\nexample:\n```\n[[Old]]\n```\n````\n",
    }
    plan = plan_rename(note_map, "Old", "New")
    updated = plan.rewrites["Doc.md"]
    assert "Real [[New]]" in updated
    assert updated.count("[[Old]]") == 1
