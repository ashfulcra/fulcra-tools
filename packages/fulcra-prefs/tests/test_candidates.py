import json

import pytest

from fulcra_prefs.candidates import append_candidate, candidate_file, mark_captured


def test_candidate_file_uses_platform_and_session_under_root(tmp_path):
    path = candidate_file("codex", "sess-1", root=tmp_path)

    assert path == tmp_path / "codex" / "sess-1.json"


@pytest.mark.parametrize("value", ["", "../x", "a/b", r"a\b"])
def test_candidate_file_rejects_unsafe_path_parts(tmp_path, value):
    with pytest.raises(ValueError):
        candidate_file(value, "sess", root=tmp_path)
    with pytest.raises(ValueError):
        candidate_file("codex", value, root=tmp_path)


def test_append_candidate_creates_json_array_and_preserves_existing(tmp_path):
    path = candidate_file("codex", "sess", root=tmp_path)

    assert append_candidate(path, {"key": "a", "value": True}) == 1
    assert append_candidate(path, {"key": "b", "value": False}) == 2

    assert json.loads(path.read_text()) == [
        {"key": "a", "value": True},
        {"key": "b", "value": False},
    ]


def test_mark_captured_renames_candidate_file(tmp_path):
    path = candidate_file("codex", "sess", root=tmp_path)
    append_candidate(path, {"key": "a", "value": True})

    captured = mark_captured(path)

    assert not path.exists()
    assert captured.name == "sess.json.captured"
    assert json.loads(captured.read_text()) == [{"key": "a", "value": True}]
