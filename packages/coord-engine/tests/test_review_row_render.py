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
    assert rc == 0
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
