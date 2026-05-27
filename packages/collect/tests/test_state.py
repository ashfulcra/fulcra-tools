"""Per-plugin persisted state. Phase 1 of refactor #1 moved the backing
store from one JSON file per plugin to a SQLite db; the ``state.load``
/ ``state.save`` API is unchanged, and these tests pin that contract."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fulcra_collect import state


def test_load_returns_fresh_state_when_no_row(collect_home: Path):
    st = state.load("lastfm")
    assert st.plugin_id == "lastfm"
    assert st.last_run is None
    assert st.consecutive_failures == 0
    assert st.watermark is None


def test_record_success_resets_failures_and_sets_outcome(collect_home: Path):
    st = state.load("lastfm")
    st.consecutive_failures = 3
    when = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    st.record_finish(outcome="done", when=when)
    assert st.last_outcome == "done"
    assert st.last_run == when
    assert st.last_error is None
    assert st.consecutive_failures == 0


def test_record_error_increments_failures(collect_home: Path):
    st = state.load("lastfm")
    when = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    st.record_finish(outcome="error", when=when, error="boom")
    st.record_finish(outcome="error", when=when, error="boom again")
    assert st.consecutive_failures == 2
    assert st.last_error == "boom again"


def test_state_round_trips_through_db(collect_home: Path):
    st = state.load("lastfm")
    when = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    st.record_finish(outcome="done", when=when)
    st.watermark = "2026-05-22T11:59:00Z"
    state.save(st)
    reloaded = state.load("lastfm")
    assert reloaded.last_outcome == "done"
    assert reloaded.last_run == when
    assert reloaded.watermark == "2026-05-22T11:59:00Z"


def test_load_falls_back_on_unparseable_timestamp(collect_home: Path):
    """A row with a malformed last_run shouldn't crash the daemon
    loop — load() returns last_run=None (mirrors the corrupt-file
    fallback the old JSON loader provided)."""
    from fulcra_collect import db
    conn = db.open()
    conn.execute(
        "INSERT INTO plugin_state ("
        "  plugin_id, last_run, consecutive_failures, updated_at"
        ") VALUES (?, ?, ?, ?)",
        ("lastfm", "not-a-date", 0, "2026-05-26T00:00:00+00:00"),
    )
    st = state.load("lastfm")
    assert st.plugin_id == "lastfm"
    assert st.last_run is None


def test_definition_id_round_trips(collect_home: Path):
    st = state.load("attention")
    assert st.definition_id is None  # default
    st.definition_id = "fulcra-uuid-123"
    state.save(st)
    again = state.load("attention")
    assert again.definition_id == "fulcra-uuid-123"


def test_override_definition_name_round_trips(collect_home: Path):
    """``override_definition_name`` was the most recent field added (today,
    2026-05-26). Confirm round-trip — caught a missing column in db.py
    earlier."""
    st = state.load("attention")
    st.override_definition_name = "My Custom Name"
    state.save(st)
    reloaded = state.load("attention")
    assert reloaded.override_definition_name == "My Custom Name"


def test_save_then_load_in_a_separate_thread_sees_the_write(
        collect_home: Path):
    """Each thread opens its own SQLite connection (per the
    db.open() thread-local cache). With WAL + autocommit, a write
    committed in thread A is visible to a fresh SELECT in thread B."""
    import threading

    st = state.load("lastfm")
    st.watermark = "2026-05-26T10:00:00Z"
    state.save(st)

    seen: dict[str, str | None] = {}
    def reader():
        # Different thread → different cached connection → exercises the
        # "writes are visible across connections" property of WAL.
        seen["watermark"] = state.load("lastfm").watermark
    t = threading.Thread(target=reader)
    t.start()
    t.join()
    assert seen["watermark"] == "2026-05-26T10:00:00Z"


def test_concurrent_writes_from_two_threads_do_not_lose_data(
        collect_home: Path):
    """Two threads writing different plugins' state concurrently both
    land. The old atomic-write-via-tempfile path made a single-writer
    assumption that we are now explicitly relaxing under WAL."""
    import threading

    def write(pid: str, watermark: str) -> None:
        st = state.load(pid)
        st.watermark = watermark
        state.save(st)

    t1 = threading.Thread(target=write, args=("plugin-a", "wm-a"))
    t2 = threading.Thread(target=write, args=("plugin-b", "wm-b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert state.load("plugin-a").watermark == "wm-a"
    assert state.load("plugin-b").watermark == "wm-b"
