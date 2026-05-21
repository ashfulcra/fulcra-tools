"""Entry selection filters."""
from __future__ import annotations

from datetime import datetime, timezone

from fulcra_dayone.entry import DayOneEntry
from fulcra_dayone.filter import select


def _entry(uuid, *, tags=(), journal="Personal", starred=False, day=15) -> DayOneEntry:
    return DayOneEntry(
        uuid=uuid,
        creation_date=datetime(2026, 5, day, tzinfo=timezone.utc),
        text="t", tags=tuple(tags), starred=starred, journal=journal,
        location=None, photo_count=0, word_count=1,
    )


ENTRIES = [
    _entry("a", tags=("work",), journal="Personal", starred=True, day=10),
    _entry("b", tags=("travel",), journal="Travel", starred=False, day=20),
    _entry("c", tags=("work", "travel"), journal="Travel", starred=True, day=15),
]


def test_no_filters_returns_everything():
    assert {e.uuid for e in select(ENTRIES)} == {"a", "b", "c"}


def test_tag_filter_matches_any_given_tag():
    got = select(ENTRIES, tags=frozenset({"work"}))
    assert {e.uuid for e in got} == {"a", "c"}


def test_journal_filter():
    got = select(ENTRIES, journals=frozenset({"Travel"}))
    assert {e.uuid for e in got} == {"b", "c"}


def test_starred_filter():
    got = select(ENTRIES, starred_only=True)
    assert {e.uuid for e in got} == {"a", "c"}


def test_date_range_is_inclusive():
    got = select(
        ENTRIES,
        since=datetime(2026, 5, 15, tzinfo=timezone.utc),
        until=datetime(2026, 5, 20, 23, 59, 59, tzinfo=timezone.utc),
    )
    assert {e.uuid for e in got} == {"b", "c"}


def test_filters_are_anded_together():
    got = select(
        ENTRIES,
        tags=frozenset({"travel"}),
        journals=frozenset({"Travel"}),
        starred_only=True,
    )
    assert {e.uuid for e in got} == {"c"}
