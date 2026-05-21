"""DayOneEntry is the reader-agnostic entry model."""
from __future__ import annotations

from datetime import datetime, timezone

from fulcra_dayone.entry import DayOneEntry


def test_dayone_entry_holds_all_fields():
    e = DayOneEntry(
        uuid="ABC123",
        creation_date=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        text="Today I learned.",
        tags=("learning",),
        starred=True,
        journal="Personal",
        location="Seattle",
        photo_count=2,
        word_count=3,
    )
    assert e.uuid == "ABC123"
    assert e.tags == ("learning",)
    assert e.starred is True
    assert e.location == "Seattle"


def test_dayone_entry_is_frozen():
    import dataclasses
    e = DayOneEntry(
        uuid="X", creation_date=datetime(2026, 5, 21, tzinfo=timezone.utc),
        text="t", tags=(), starred=False, journal="J",
        location=None, photo_count=0, word_count=1,
    )
    try:
        e.uuid = "Y"  # type: ignore[misc]
        raised = False
    except dataclasses.FrozenInstanceError:
        raised = True
    assert raised
