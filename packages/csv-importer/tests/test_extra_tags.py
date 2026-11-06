"""GenericEvent.extra_tags -> multiple tag ids in the built record."""

from __future__ import annotations

from datetime import datetime, timezone

from fulcra_csv.events import INSTANT, GenericEvent
from fulcra_csv.v1 import build_csv_record


def _event(**kw) -> GenericEvent:
    base = dict(
        start_time=datetime(2026, 5, 21, tzinfo=timezone.utc),
        note="hi",
        title="hi",
        source_id="s1",
        annotation_type=INSTANT,
    )
    base.update(kw)
    return GenericEvent(**base)


DEST = "Event/11111111-2222-4333-8444-555555555555"


def test_build_record_includes_tag_and_extra_tags_in_order():
    event = _event(tag="primary", extra_tags=("alpha", "beta"))
    rec = build_csv_record(event, DEST, tags=["t-p", "t-a", "t-b"])
    assert rec["tags"] == ["t-p", "t-a", "t-b"]
    assert rec["csv_tag"] == "primary" and rec["csv_extra_tags"] == ["alpha", "beta"]


def test_build_record_dedupes_repeated_tag_ids():
    rec = build_csv_record(_event(tag="x", extra_tags=("x", "y")), DEST, tags=["t-x", "t-x", "t-y"])
    assert rec["tags"] == ["t-x", "t-y"]
    assert rec["csv_extra_tags"] == ["x", "y"]


def test_build_record_extra_tags_default_empty():
    rec = build_csv_record(_event(), DEST)
    assert rec["tags"] == [] and rec["csv_extra_tags"] == []
