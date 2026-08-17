"""review-pending rows must show the reviewer WHERE the artifact is.

Origin (2026-08-17): PR 634 made every review-pending emit path carry `of` and
`head` on the row dict, so `needs-me --json` served them — but the human text
render printed only `pending verdict: <slug> (required: ...)`. A reviewer who
cannot search SHAs had no path from that line to the thing under review;
codex-coder sat blocked 20h on reviews whose URLs were in the register docs the
whole time.
"""
from coord_engine import cli


def _row(**kw):
    base = {"type": "review-pending", "name": "pr-500-round3",
            "pending_required": ["coord-opus-worker"]}
    base.update(kw)
    return base


def test_of_and_head_both_render():
    """THE REGRESSION: the row carries both; the line must show both."""
    line = cli._review_row_line(_row(of="https://github.com/o/r/pull/500",
                                     head="9508bd1c2f3a4b5c6d7e8f90"))
    assert "pending verdict: pr-500-round3" in line
    assert "of: https://github.com/o/r/pull/500" in line
    assert "@ 9508bd1c2f3a" in line


def test_head_is_abbreviated_to_twelve():
    """Full 40-char shas crowd the line; 12 is the fleet's usual short form."""
    line = cli._review_row_line(_row(head="a" * 40))
    assert "@ " + "a" * 12 in line
    assert "a" * 13 not in line


def test_of_without_head():
    line = cli._review_row_line(_row(of="https://example.test/pr/1"))
    assert "of: https://example.test/pr/1" in line
    assert "@" not in line


def test_head_without_of():
    """No URL recorded, but the head still tells the reviewer what to file
    against — worth showing on its own."""
    line = cli._review_row_line(_row(head="deadbeefcafe0000"))
    assert "@ deadbeefcafe" in line
    assert "of:" not in line


def test_legacy_row_renders_exactly_as_before():
    """Rows predating 634 carry neither field and must not grow dangling
    separators."""
    line = cli._review_row_line(_row())
    assert line == "  [REVIEW] pending verdict: pr-500-round3 (required: coord-opus-worker)"


def test_none_and_blank_are_treated_as_absent():
    """`of: None` on screen would be worse than no field at all."""
    line = cli._review_row_line(_row(of=None, head="   "))
    assert line == "  [REVIEW] pending verdict: pr-500-round3 (required: coord-opus-worker)"
    assert "None" not in line


def test_prose_of_is_clipped_not_unbounded():
    """`of` is free text in the register (review_gc.head_from_prose parses it),
    so a long prose value must not blow the line width."""
    line = cli._review_row_line(_row(of="x" * 400))
    assert "…" in line
    assert len(line) < 400


def test_multiple_required_reviewers_still_listed():
    line = cli._review_row_line(_row(pending_required=["a", "b"],
                                     of="https://example.test/pr/2"))
    assert "(required: a, b)" in line
    assert "of: https://example.test/pr/2" in line


def test_other_review_row_types_untouched():
    """The dispatch must keep returning the other shapes unchanged."""
    orphan = cli._review_row_line({"type": "review-orphan", "name": "pr-9"})
    assert "orphan review dir" in orphan
    assert cli._review_row_line({"type": "task"}) is None
