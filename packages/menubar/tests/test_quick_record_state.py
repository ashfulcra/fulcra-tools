"""Pure-function tests for the quick-record popover's in-memory
state helpers (recent-list cap, section header label fallback).

The AppKit rendering layer is exercised manually — these tests cover
the headless logic that the popover delegates to.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

# Importing the popover module pulls in AppKit. That's available on macOS
# (where these tests run) but skip cleanly if not — keeps the suite
# portable to CI environments without PyObjC.
pytest.importorskip("AppKit")

from fulcra_menubar.popover.quick_record import (  # noqa: E402
    RECENT_MAX, _append_recent, _group_label,
)


def _entry(name: str, source_id: str | None = None) -> dict:
    return {
        "source_id": source_id or f"src-{name}",
        "name": name,
        "ts": datetime.now(timezone.utc),
        "undone": False,
    }


def test_recent_appends_under_cap():
    recent: list[dict] = []
    _append_recent(recent, _entry("Coffee"))
    _append_recent(recent, _entry("Run"))
    assert [e["name"] for e in recent] == ["Coffee", "Run"]


def test_recent_caps_at_max_and_rolls_off_oldest():
    """Once RECENT_MAX entries are present, each new append drops the
    oldest (FIFO) so the list stays bounded — important because the
    popover is space-constrained."""
    recent: list[dict] = []
    for i in range(RECENT_MAX):
        _append_recent(recent, _entry(f"entry-{i}"))
    assert len(recent) == RECENT_MAX
    _append_recent(recent, _entry("new"))
    assert len(recent) == RECENT_MAX
    # Oldest ("entry-0") rolled off; "new" is at the tail.
    names = [e["name"] for e in recent]
    assert "entry-0" not in names
    assert names[-1] == "new"


def test_recent_caps_when_many_appended_at_once():
    """A burst of appends still bounds the list to RECENT_MAX, not
    RECENT_MAX+N."""
    recent: list[dict] = []
    for i in range(RECENT_MAX * 3):
        _append_recent(recent, _entry(f"e-{i}"))
    assert len(recent) == RECENT_MAX


def test_group_label_known_types():
    assert _group_label("moment") == "Moments"
    assert _group_label("duration") == "Durations"


def test_group_label_unknown_type_falls_back_to_titlecase():
    """A future Fulcra annotation_type shouldn't render as a blank
    header — the fallback titlecases the raw string so the user sees
    something readable."""
    assert _group_label("custom_type") == "Custom_Type"
    assert _group_label("") == "Other"
