"""DayOneEntry -> fulcra_csv GenericEvent conversion."""
from __future__ import annotations

from datetime import datetime, timezone

from fulcra_csv.events import INSTANT

from fulcra_dayone.convert import to_event
from fulcra_dayone.entry import DayOneEntry


def _entry(**kw) -> DayOneEntry:
    base = dict(
        uuid="ABC123",
        creation_date=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        text="My title line\n\nbody text here",
        tags=("alpha", "beta"),
        starred=False,
        journal="Personal",
        location="Seattle",
        photo_count=1,
        word_count=6,
    )
    base.update(kw)
    return DayOneEntry(**base)


def test_event_is_instant_at_creation_date():
    ev = to_event(_entry())
    assert ev.annotation_type == INSTANT
    assert ev.end_time is None
    assert ev.start_time == datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc)


def test_title_is_first_non_empty_line_without_markdown_hashes():
    ev = to_event(_entry(text="## My title line\n\nbody"))
    assert ev.title == "My title line"


def test_title_caps_at_120_chars():
    ev = to_event(_entry(text="x" * 200))
    assert ev.title is not None and len(ev.title) == 120


def test_note_replaces_dayone_media_placeholders():
    ev = to_event(_entry(text="before ![](dayone-moment://ABC) after"))
    assert ev.note == "before [photo] after"


def test_tags_become_extra_tags():
    ev = to_event(_entry(tags=("alpha", "beta")))
    assert ev.tag is None
    assert ev.extra_tags == ("alpha", "beta")


def test_source_id_is_stable_and_uuid_derived():
    a = to_event(_entry()).source_id
    b = to_event(_entry()).source_id
    assert a == b
    assert a.startswith("com.fulcra.dayone.")


def test_external_ids_carry_metadata():
    ev = to_event(_entry())
    assert ev.external_ids["dayone_uuid"] == "ABC123"
    assert ev.external_ids["journal"] == "Personal"
    assert ev.external_ids["word_count"] == 6
    assert ev.external_ids["photo_count"] == 1
    assert ev.external_ids["location"] == "Seattle"


def test_location_omitted_from_external_ids_when_absent():
    ev = to_event(_entry(location=None))
    assert "location" not in ev.external_ids
