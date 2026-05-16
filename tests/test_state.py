from pathlib import Path

from fulcra_media.state import State, load, save


def test_load_returns_default_when_file_missing(tmp_path: Path):
    state = load(tmp_path / "does-not-exist.json")
    assert state == State()
    assert state.watched_definition_id is None
    assert state.tag_ids == {}
    assert state.watermarks == {}


def test_save_then_load_round_trips(tmp_path: Path):
    state = State(
        watched_definition_id="def-watched-uuid",
        listened_definition_id="def-listened-uuid",
        tag_ids={"netflix": "tag-uuid-1", "media": "tag-uuid-2"},
        watermarks={"netflix-slim": "2026-05-12"},
    )
    path = tmp_path / "nested" / "state.json"
    save(state, path)
    assert path.exists()
    loaded = load(path)
    assert loaded == state


def test_save_creates_parent_directories(tmp_path: Path):
    state = State()
    path = tmp_path / "a" / "b" / "c" / "state.json"
    save(state, path)
    assert path.exists()
