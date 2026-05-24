"""Tests for the in-memory RecentActivity ring buffer (activity.py)."""
from __future__ import annotations


def test_add_and_recent_returns_newest_first():
    from fulcra_collect.activity import RecentActivity
    buf = RecentActivity()
    buf.add(plugin_id="lastfm", summary="Song A")
    buf.add(plugin_id="trakt", summary="Movie B")
    entries = buf.recent()
    assert len(entries) == 2
    assert entries[0].summary == "Movie B"   # newest first
    assert entries[1].summary == "Song A"


def test_recent_caps_at_limit():
    from fulcra_collect.activity import RecentActivity
    buf = RecentActivity()
    for i in range(20):
        buf.add(plugin_id="x", summary=f"item {i}")
    assert len(buf.recent(limit=10)) == 10


def test_buffer_rolls_after_max_entries():
    from fulcra_collect.activity import RecentActivity
    buf = RecentActivity(max_entries=5)
    for i in range(10):
        buf.add(plugin_id="x", summary=f"item {i}")
    entries = buf.recent()
    assert len(entries) == 5
    # Oldest dropped; newest 5 (5-9) remain
    assert entries[0].summary == "item 9"
    assert entries[-1].summary == "item 5"


def test_thread_safe_concurrent_adds():
    import threading
    from fulcra_collect.activity import RecentActivity
    buf = RecentActivity()

    def writer(prefix):
        for i in range(100):
            buf.add(plugin_id="x", summary=f"{prefix}-{i}")

    threads = [threading.Thread(target=writer, args=(p,)) for p in ("A", "B", "C")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # All 300 writes attempted; default buffer holds 200 max
    assert len(buf.recent(limit=400)) == 200


def test_failed_writes_carry_ok_false():
    from fulcra_collect.activity import RecentActivity
    buf = RecentActivity()
    buf.add(plugin_id="lastfm", summary="auth failed", ok=False)
    entry = buf.recent()[0]
    assert entry.ok is False


def test_timestamp_field_has_z_suffix():
    from fulcra_collect.activity import RecentActivity
    buf = RecentActivity()
    buf.add(plugin_id="x", summary="test")
    ts = buf.recent()[0].timestamp
    assert ts.endswith("Z"), f"timestamp {ts!r} should end with Z"


def test_clear_empties_buffer():
    from fulcra_collect.activity import RecentActivity
    buf = RecentActivity()
    buf.add(plugin_id="x", summary="a")
    buf.add(plugin_id="x", summary="b")
    buf.clear()
    assert buf.recent() == []


def test_make_singleton_returns_fresh_instance():
    from fulcra_collect.activity import make_singleton, RecentActivity
    a = make_singleton()
    b = make_singleton()
    assert isinstance(a, RecentActivity)
    assert a is not b  # each call returns a distinct instance
