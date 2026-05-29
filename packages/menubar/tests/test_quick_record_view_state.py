"""The popover is a curated surface: it shows ONLY pinned tracks. The body's
three states are decided by a pure helper so they're testable without AppKit."""
from fulcra_menubar.popover.quick_record import quick_record_view_state


def _d(id_, pinned, atype="moment"):
    return {"id": id_, "name": id_, "annotation_type": atype, "pinned": pinned}


def test_no_definitions_on_account():
    defs, state = quick_record_view_state([])
    assert state == "no_defs"
    assert defs == []


def test_definitions_exist_but_none_pinned():
    defs, state = quick_record_view_state([_d("a", False), _d("b", False)])
    assert state == "none_pinned"
    assert defs == []


def test_some_pinned_returns_only_pinned():
    defs, state = quick_record_view_state(
        [_d("a", True), _d("b", False), _d("c", True)]
    )
    assert state == "list"
    assert [d["id"] for d in defs] == ["a", "c"]
