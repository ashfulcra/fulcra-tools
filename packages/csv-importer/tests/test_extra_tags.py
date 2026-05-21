"""GenericEvent.extra_tags -> multiple tag ids in the built record."""
from __future__ import annotations

from datetime import datetime, timezone

from fulcra_csv.events import INSTANT, GenericEvent
from fulcra_csv.fulcra import FulcraClient


def _event(**kw) -> GenericEvent:
    base = dict(
        start_time=datetime(2026, 5, 21, tzinfo=timezone.utc),
        note="hi", title="hi", source_id="s1", annotation_type=INSTANT,
    )
    base.update(kw)
    return GenericEvent(**base)


def test_build_record_includes_tag_and_extra_tags_in_order():
    rec = FulcraClient()._build_record(
        _event(tag="primary", extra_tags=("alpha", "beta")),
        definition_id="def-1",
        tag_id_for={"primary": "t-p", "alpha": "t-a", "beta": "t-b"},
        data_type=None,
    )
    assert rec["metadata"]["tags"] == ["t-p", "t-a", "t-b"]


def test_build_record_dedupes_repeated_tag_ids():
    rec = FulcraClient()._build_record(
        _event(tag="x", extra_tags=("x", "y")),
        definition_id="def-1",
        tag_id_for={"x": "t-x", "y": "t-y"},
        data_type=None,
    )
    assert rec["metadata"]["tags"] == ["t-x", "t-y"]


def test_build_record_extra_tags_default_empty():
    rec = FulcraClient()._build_record(
        _event(),
        definition_id=None, tag_id_for={}, data_type=None,
    )
    assert rec["metadata"]["tags"] == []
