"""Per-plugin persisted state."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fulcra_collect import state


def test_load_returns_fresh_state_when_no_file(collect_home: Path):
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


def test_state_round_trips_through_disk(collect_home: Path):
    st = state.load("lastfm")
    when = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    st.record_finish(outcome="done", when=when)
    st.watermark = "2026-05-22T11:59:00Z"
    state.save(st)
    reloaded = state.load("lastfm")
    assert reloaded.last_outcome == "done"
    assert reloaded.last_run == when
    assert reloaded.watermark == "2026-05-22T11:59:00Z"


def test_load_returns_fresh_state_for_a_corrupt_json_file(collect_home: Path):
    """A torn / corrupt state file must not crash the daemon loop — load
    falls back to a fresh PluginState."""
    path = state._state_dir() / "lastfm.json"
    path.write_text('{"last_run": "2026-05-2', encoding="utf-8")  # truncated
    st = state.load("lastfm")
    assert st.plugin_id == "lastfm"
    assert st.last_run is None
    assert st.consecutive_failures == 0


def test_load_returns_fresh_state_for_an_unparseable_timestamp(collect_home: Path):
    """A syntactically-valid file with a bad datetime also falls back."""
    path = state._state_dir() / "lastfm.json"
    path.write_text('{"last_run": "not-a-date"}', encoding="utf-8")
    st = state.load("lastfm")
    assert st.plugin_id == "lastfm"
    assert st.last_run is None


def test_save_is_atomic_and_leaves_no_torn_file(collect_home: Path):
    """save() writes via a temp file + atomic rename — at no point is the
    final file partially written, and no temp files are left behind."""
    st = state.load("lastfm")
    st.record_finish(outcome="done",
                     when=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc))
    state.save(st)
    d = state._state_dir()
    final = d / "lastfm.json"
    # the only file in the dir is the final one — no temp leftovers
    assert sorted(p.name for p in d.iterdir()) == ["lastfm.json"]
    # the final file is always valid JSON (never torn)
    import json
    json.loads(final.read_text(encoding="utf-8"))
