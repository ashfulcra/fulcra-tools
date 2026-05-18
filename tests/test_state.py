"""Tests for state.py."""
from __future__ import annotations

import json
from pathlib import Path

from fulcra_attention.state import State, load, save


def test_load_returns_empty_state_when_file_missing(tmp_path: Path):
    p = tmp_path / "state.json"
    assert load(p) == State()


def test_save_then_load_roundtrips(tmp_path: Path):
    p = tmp_path / "state.json"
    s = State(
        attention_definition_id="def-123",
        tag_ids={"attention": "tag-att", "web": "tag-web"},
        watermarks={"chrome": "2026-05-18T12:00:00Z"},
    )
    save(s, p)
    assert load(p) == s


def test_save_writes_mode_aware_parent_dir(tmp_path: Path):
    p = tmp_path / "nested" / "deep" / "state.json"
    save(State(attention_definition_id="x"), p)
    assert p.exists()


def test_load_tolerates_missing_optional_fields(tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"attention_definition_id": "def-x"}))
    s = load(p)
    assert s.attention_definition_id == "def-x"
    assert s.tag_ids == {}
    assert s.watermarks == {}
