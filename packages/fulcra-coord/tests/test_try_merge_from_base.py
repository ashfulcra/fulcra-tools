"""Pure 3-way merge for fold-sourced writes (root cause A, Step 3).

writepipe._try_merge_from_base(base, mine, theirs) reconciles a fold-sourced
write. ``base`` is the fold body at read; ``mine`` is the command's edited body;
``theirs`` is the fresh mutable file body. The KEY insight (vs the 2-way
_try_merge): a field unchanged in ``mine`` relative to ``base`` is STALE READ
STATE, not an assertion — so a newer ``theirs`` value for that field must be
recovered, not clobbered. These tests pin every per-field rule branch on small
pure dicts, before the function is wired into the write path.
"""

import pytest

from fulcra_coord import writepipe


def _base(**over):
    b = {
        "id": "TASK-1",
        "status": "active",
        "current_summary": "s",
        "next_action": "n",
        "events": [],
        "acked_by": [],
        "tags": ["status:active", "workstream:ws", "agent:a",
                 "kind:ops", "priority:P2"],
        "workstream": "ws",
        "owner_agent": "a",
        "priority": "P2",
    }
    b.update(over)
    return b


def test_recovers_newer_file_field_when_mine_unchanged():
    """mine==base for a field, theirs changed it => take theirs (recover file)."""
    base = _base(current_summary="orig", next_action="orig-na")
    mine = _base(current_summary="orig", next_action="MY-EDIT")   # edited next_action only
    theirs = _base(current_summary="FILE-NEW", next_action="orig-na")  # file changed summary
    merged = writepipe._try_merge_from_base(base, mine, theirs)
    assert merged is not None
    # Recovered the newer file field (my unchanged copy was stale read state)...
    assert merged["current_summary"] == "FILE-NEW"
    # ... and kept my real edit.
    assert merged["next_action"] == "MY-EDIT"


def test_takes_mine_when_only_mine_changed():
    base = _base(current_summary="orig")
    mine = _base(current_summary="MY-EDIT")
    theirs = _base(current_summary="orig")
    merged = writepipe._try_merge_from_base(base, mine, theirs)
    assert merged["current_summary"] == "MY-EDIT"


def test_same_value_both_changed():
    base = _base(current_summary="orig")
    mine = _base(current_summary="SAME")
    theirs = _base(current_summary="SAME")
    merged = writepipe._try_merge_from_base(base, mine, theirs)
    assert merged["current_summary"] == "SAME"


def test_both_changed_scalar_differently_is_conflict():
    base = _base(current_summary="orig")
    mine = _base(current_summary="MINE")
    theirs = _base(current_summary="THEIRS")
    merged = writepipe._try_merge_from_base(base, mine, theirs)
    assert merged is None


def test_remote_only_status_change_survives_stale_fold():
    """theirs changed status, mine did not => remote status must NOT be clobbered."""
    base = _base(status="active", current_summary="orig")
    mine = _base(status="active", current_summary="MY-NOTE")  # only edits summary
    theirs = _base(status="done", current_summary="orig")     # file moved to done
    merged = writepipe._try_merge_from_base(base, mine, theirs)
    assert merged is not None
    assert merged["status"] == "done"          # remote transition preserved
    assert merged["current_summary"] == "MY-NOTE"


def test_local_only_status_change_wins():
    base = _base(status="active")
    mine = _base(status="done")
    theirs = _base(status="active")
    merged = writepipe._try_merge_from_base(base, mine, theirs)
    assert merged is not None
    assert merged["status"] == "done"


def test_both_changed_status_differently_is_conflict():
    base = _base(status="active")
    mine = _base(status="done")
    theirs = _base(status="blocked")
    merged = writepipe._try_merge_from_base(base, mine, theirs)
    assert merged is None


def test_acked_by_is_union_never_shrinks():
    base = _base(acked_by=["a"])
    mine = _base(acked_by=["a"])          # fold lacked the file's extra ack
    theirs = _base(acked_by=["a", "b"])   # file has an ack the fold missed
    merged = writepipe._try_merge_from_base(base, mine, theirs)
    assert merged is not None
    assert set(merged["acked_by"]) == {"a", "b"}


def test_events_are_unioned():
    e1 = {"at": "2026-06-08T00:00:01.000000Z", "type": "active", "by": "a"}
    e2 = {"at": "2026-06-08T00:00:02.000000Z", "type": "update", "by": "a"}
    base = _base(events=[e1])
    mine = _base(events=[e1])
    theirs = _base(events=[e1, e2])
    merged = writepipe._try_merge_from_base(base, mine, theirs)
    ats = {e["at"] for e in merged["events"]}
    assert e1["at"] in ats and e2["at"] in ats


def test_tags_repaired_to_merged_status():
    """After a remote status change is recovered, derived status tag follows it."""
    base = _base(status="active", current_summary="orig")
    mine = _base(status="active", current_summary="note")
    theirs = _base(status="done", current_summary="orig",
                   tags=["status:done", "workstream:ws", "agent:a",
                         "kind:ops", "priority:P2"])
    merged = writepipe._try_merge_from_base(base, mine, theirs)
    assert merged["status"] == "done"
    assert "status:done" in merged["tags"]
    assert "status:active" not in merged["tags"]
