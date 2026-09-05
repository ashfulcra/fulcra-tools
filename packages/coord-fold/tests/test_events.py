import json
import pytest
from coord_fold import events


def _rec(note, rid="r1", at="2026-09-04T13:45:00Z"):
    return {"id": rid, "recorded_at": at, "note": json.dumps(note)}


def test_build_produces_exactly_the_eight_fields():
    p = events.build_payload(at="t", sender="boss", to="me", kind="open", slug="s", pri="P1", ptr="x.md")
    assert set(p) == {"v", "at", "from", "to", "kind", "slug", "pri", "ptr"} and p["v"] == 1


def test_open_and_close_without_ptr_are_refused():
    for k in ("open", "close"):
        with pytest.raises(ValueError):
            events.build_payload(at="t", sender="a", to="b", kind=k, slug="s", pri="P1", ptr=None)


def test_ptr_must_be_a_single_file_path():
    for bad in ("team/r/task/", "team/r/task/*.md", ""):
        with pytest.raises(ValueError):
            events.build_payload(at="t", sender="a", to="b", kind="open", slug="s", pri="P1", ptr=bad)


def test_unknown_kind_and_priority_are_refused():
    with pytest.raises(ValueError):
        events.build_payload(at="t", sender="a", to="b", kind="directive", slug="s", pri="P1", ptr="x.md")
    with pytest.raises(ValueError):
        events.build_payload(at="t", sender="a", to="b", kind="note", slug="s", pri="P9", ptr=None)


def test_parse_accepts_v1_and_carries_record_id_and_recorded_at():
    p = events.build_payload(at="t", sender="a", to="b", kind="open", slug="s", pri="P1", ptr="x.md")
    ev = events.parse_event(_rec(p, rid="abc", at="T2"))
    assert ev and ev["record_id"] == "abc" and ev["recorded_at"] == "T2"


def test_parse_skips_free_text_and_foreign_payloads_silently():
    assert events.parse_event({"id": "x", "note": "hello"}) is None
    assert events.parse_event(_rec({"kind": "directive", "v": 1})) is None
    assert events.parse_event(_rec({"v": 2, "kind": "open", "slug": "s"})) is None
