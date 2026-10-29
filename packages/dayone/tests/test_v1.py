"""Day One identity and enrichment must survive the flat v1 boundary."""
from datetime import datetime, timezone
from hashlib import sha256

import pytest

from fulcra_common.v1 import record_id_for
from fulcra_dayone.entry import DayOneEntry
from fulcra_dayone.v1 import JOURNAL_SPEC, build_journal_record

DESTINATION = "Event/11111111-2222-4333-8444-555555555555"
TAG = "aaaaaaaa-bbbb-4ccc-8ddd-123456789abc"


def entry(**changes):
    values = dict(uuid="ABC123", creation_date=datetime(2024, 2, 3, tzinfo=timezone.utc),
        text="## Café 日本語\n![](dayone-moment://photo)", tags=("first", "second"),
        starred=False, journal="Synthetic journal", location=None, photo_count=1, word_count=3)
    return DayOneEntry(**(values | changes))


def test_flat_moment_keeps_exact_legacy_identity_and_normalized_content():
    record = build_journal_record(entry(), DESTINATION, tags=[TAG])
    source = "com.fulcra.dayone." + sha256(b"ABC123").hexdigest()[:16]
    assert record == {"id": record_id_for(DESTINATION, source), "sources": [source], "tags": [TAG],
        "start_time": "2024-02-03T00:00:00Z", "end_time": "2024-02-03T00:00:00Z",
        "note": "## Café 日本語\n[photo]", "title": "Café 日本語",
        "dayone_tag_names": ["first", "second"],
        "external_ids": {"dayone_uuid": "ABC123", "journal": "Synthetic journal",
            "starred": False, "word_count": 3, "photo_count": 1}}
    assert JOURNAL_SPEC.base_type == "Event" and JOURNAL_SPEC.key == "dayone.journal.v1"
    assert set(record) - {"id", "sources", "tags", "start_time", "end_time"} == set(JOURNAL_SPEC.fields)


def test_uuid_spelling_is_not_canonicalized_and_location_is_preserved():
    first = build_journal_record(entry(uuid="ABC123", location="Synthetic place"), DESTINATION)
    second = build_journal_record(entry(uuid="abc123"), DESTINATION)
    assert first["id"] != second["id"]
    assert first["external_ids"]["location"] == "Synthetic place"


@pytest.mark.parametrize("changes", [{"creation_date": datetime(2024, 1, 1)}, {"starred": 0},
    {"word_count": True}, {"photo_count": -1}, {"journal": None}, {"tags": ("ok", "")},
    {"tags": ("bad\x00tag",)}, {"uuid": 123}, {"location": {"bad": True}},
    {"location": False}, {"location": 0}, {"location": {}}, {"tags": "tag"}])
def test_invalid_source_values_are_rejected_in_pure_preflight(changes):
    with pytest.raises((ValueError, TypeError)):
        build_journal_record(entry(**changes), DESTINATION)
