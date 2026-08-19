"""Review rows must be TYPE-DISPATCHED in `briefing` / `needs-me` text output.

Latent pre-existing defect exposed once the head-of-line fix let `briefing`
actually surface a caller's pending-review row: both text renderers used to hand
some review row types to the generic task-row renderer (`_line`), which reads
`priority` / `status` / `title` off a shape that has none — printing garbage like
`[ ?] ? None`. A review row must NEVER reach the generic task line: every type
`briefing` / `needs-me` can receive is dispatched, and the two verbs emit the
IDENTICAL line for the identical row type (a shared helper enforces that).

Two families:
  - Actionable pending items (`review-pending`, `review-orphan`): counted.
  - Degraded / UNKNOWN markers (`review-fold-degraded` — expected tail truncation;
    `review-head-degraded` — the caller's OWN queue could not complete, incident-
    grade): ALWAYS shown, NEVER counted as a pending item, and the head marker's
    line is loud and DISTINCT from the tail marker's.

Second concern, added 2026-08-17: a `review-pending` line must also say WHERE the
artifact is. PR 634 put `of` + `head` on the row dict for `--json`, but the text
render dropped them, so a reviewer who cannot search SHAs had no path from the
line to the thing under review (codex-coder sat blocked 20h on exactly that).
`head` rides along because review is exact-head — the verdict filename is
`<head>--<reviewer>.md`, so the slug alone does not say which head to file
against.
"""

from coord_engine import cli
from coord_engine_test_helpers import FakeTransport


def _head_degraded_row(scanned=0, total=3, skipped=0):
    """The marker `budget.degraded_row('review-head-degraded', ...)` builds: a
    `{type, scanned, total[, skipped]}` shape with no priority/status/title."""
    row = {"type": "review-head-degraded", "scanned": scanned, "total": total}
    if skipped:
        row["skipped"] = skipped
    return row


def test_briefing_renders_review_pending_not_line_garbage(capsys):
    # A real pending review requiring `alice`, surfaced through briefing's text
    # renderer. It must dispatch to the [REVIEW] line, not the generic task line.
    t = FakeTransport()
    cli.main(["review", "request", "r", "pr-x", "--of", "url",
              "--reviewer", "alice"], transport=t)
    capsys.readouterr()
    rc = cli.main(["briefing", "r", "--agent", "alice"], transport=t)
    assert rc == 0
    out = capsys.readouterr().out
    assert "[REVIEW] pending verdict: pr-x" in out, out
    # The generic-task-line tells of the defect must be gone for this row.
    assert "[ ?]" not in out, out
    assert "None" not in out, out


def test_briefing_head_degraded_not_counted_and_loud(capsys, monkeypatch):
    # A review-head-degraded marker must be split out with the degraded rows: NOT
    # counted in "pending reviews: N item(s)", and rendered with its distinct loud
    # line — never conflated with a pending item and never the tail-degraded line.
    monkeypatch.setattr(cli, "_pending_reviews_for",
                        lambda *a, **k: [_head_degraded_row(scanned=0, total=3)])
    t = FakeTransport()
    capsys.readouterr()
    rc = cli.main(["briefing", "r", "--agent", "alice"], transport=t)
    assert rc == 0
    out = capsys.readouterr().out
    assert "pending reviews: 0 item(s)" in out, out
    assert "review HEAD degraded" in out, out
    assert "UNKNOWN" in out, out
    # Distinct from the expected tail truncation phrasing.
    assert "review fold degraded" not in out, out
    # Never the generic task-line garbage.
    assert "[ ?]" not in out, out


def test_needs_me_head_degraded_renders_loud_not_line(capsys, monkeypatch):
    # needs-me must render the same head-degraded row through the same dispatch —
    # the loud UNKNOWN line, not `_line` garbage.
    monkeypatch.setattr(cli, "_pending_reviews_for",
                        lambda *a, **k: [_head_degraded_row(scanned=1, total=4)])
    t = FakeTransport()
    capsys.readouterr()
    rc = cli.main(["needs-me", "r", "--agent", "alice"], transport=t)
    assert rc == 3  # contract 2: head-degraded is DEGRADED health (OC3/E4)
    out = capsys.readouterr().out
    assert "review HEAD degraded" in out, out
    assert "UNKNOWN" in out, out
    assert "[ ?]" not in out, out
    assert "None" not in out, out


def test_briefing_and_needs_me_emit_identical_head_degraded_line(capsys, monkeypatch):
    # The anti-divergence guarantee: identical row type -> identical line in both.
    row = _head_degraded_row(scanned=2, total=5, skipped=1)
    monkeypatch.setattr(cli, "_pending_reviews_for", lambda *a, **k: [row])
    t = FakeTransport()

    capsys.readouterr()
    cli.main(["briefing", "r", "--agent", "alice"], transport=t)
    brief_lines = [ln for ln in capsys.readouterr().out.splitlines()
                   if "review HEAD degraded" in ln]

    cli.main(["needs-me", "r", "--agent", "alice"], transport=t)
    needs_lines = [ln for ln in capsys.readouterr().out.splitlines()
                   if "review HEAD degraded" in ln]

    assert brief_lines and needs_lines, (brief_lines, needs_lines)
    assert brief_lines == needs_lines, (brief_lines, needs_lines)


def test_briefing_degraded_markers_never_counted_as_pending(capsys, monkeypatch):
    # Live 2026-07-21: an orphan-classification marker was tallied as
    # "pending reviews: 1 item(s)" — the section claimed a pending review that
    # does not exist. NO degraded/UNKNOWN marker may count as a pending item;
    # only real review rows (review-pending, review-orphan) are tallied.
    rows = [
        {"type": "review-orphan-degraded", "unclassified": 15},
        {"type": "review-role-degraded", "roles": ["reviewer"]},
        _head_degraded_row(scanned=0, total=3),
    ]
    monkeypatch.setattr(cli, "_pending_reviews_for", lambda *a, **k: rows)
    t = FakeTransport()
    capsys.readouterr()
    rc = cli.main(["briefing", "r", "--agent", "alice"], transport=t)
    assert rc == 0
    out = capsys.readouterr().out
    assert "pending reviews: 0 item(s)" in out, out
    # Every marker still rendered — never hidden by the count fix.
    assert "dir classification degraded" in out, out
    assert "review role resolution degraded" in out, out
    assert "review HEAD degraded" in out, out


# --- `of` + `head` on review-pending rows (2026-08-17) ----------------------

def _pending_row(**kw):
    base = {"type": "review-pending", "name": "pr-500-round3",
            "pending_required": ["coord-opus-worker"]}
    base.update(kw)
    return base


def test_of_and_head_both_render():
    """THE REGRESSION: the row carries both; the line must show both."""
    line = cli._review_row_line(_pending_row(of="https://github.com/o/r/pull/500",
                                             head="9508bd1c2f3a4b5c6d7e8f90"))
    assert "pending verdict: pr-500-round3" in line
    assert "of: https://github.com/o/r/pull/500" in line
    assert "@ 9508bd1c2f3a" in line


def test_head_is_abbreviated_to_twelve():
    """Full 40-char shas crowd the line; 12 is the fleet's usual short form."""
    line = cli._review_row_line(_pending_row(head="a" * 40))
    assert "@ " + "a" * 12 in line
    assert "a" * 13 not in line


def test_of_without_head():
    line = cli._review_row_line(_pending_row(of="https://example.test/pr/1"))
    assert "of: https://example.test/pr/1" in line
    assert "@" not in line


def test_head_without_of():
    """No URL recorded, but the head still tells the reviewer what to file
    against — worth showing on its own."""
    line = cli._review_row_line(_pending_row(head="deadbeefcafe0000"))
    assert "@ deadbeefcafe" in line
    assert "of:" not in line


def test_legacy_row_renders_exactly_as_before():
    """Rows predating 634 carry neither field and must not grow dangling
    separators."""
    line = cli._review_row_line(_pending_row())
    assert line == "  [REVIEW] pending verdict: pr-500-round3 (required: coord-opus-worker)"


def test_none_and_blank_are_treated_as_absent():
    """`of: None` on screen would be worse than no field at all."""
    line = cli._review_row_line(_pending_row(of=None, head="   "))
    assert line == "  [REVIEW] pending verdict: pr-500-round3 (required: coord-opus-worker)"
    assert "None" not in line


def test_prose_of_is_clipped_not_unbounded():
    """`of` is free text in the register (review_gc.head_from_prose parses it),
    so a long prose value must not blow the line width."""
    line = cli._review_row_line(_pending_row(of="x" * 400))
    assert "…" in line
    assert len(line) < 400


def test_multiple_required_reviewers_still_listed():
    line = cli._review_row_line(_pending_row(pending_required=["a", "b"],
                                             of="https://example.test/pr/2"))
    assert "(required: a, b)" in line
    assert "of: https://example.test/pr/2" in line


def test_other_review_row_types_untouched():
    """The dispatch must keep returning the other shapes unchanged."""
    orphan = cli._review_row_line({"type": "review-orphan", "name": "pr-9"})
    assert "orphan review dir" in orphan
    assert cli._review_row_line({"type": "task"}) is None
