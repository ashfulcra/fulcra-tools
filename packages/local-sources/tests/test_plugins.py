from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fulcra_local_sources import collect_plugins


class Context:
    def __init__(self, plugin_id: str, config: dict):
        self.plugin_id = plugin_id
        self.config = config
        self.log = Mock()
        self._state = {}
        self.progress_events = []

    def kv_get(self, key, default=None):
        return self._state.get(key, default)

    def kv_set(self, key, value):
        self._state[key] = value

    def progress(self, **fields):
        self.progress_events.append(fields)


class Writer:
    def __init__(self):
        self.uploads = []
        self.text = []

    def upload_file(self, source, remote):
        self.uploads.append((Path(source).name, remote))

    def write_text(self, remote, content):
        self.text.append((remote, content))


def test_plugins_are_scheduled_local_sources_with_folder_picker_and_preview():
    for plugin in (collect_plugins.LOCAL_FOLDERS_PLUGIN, collect_plugins.MEETING_TRANSCRIPTS_PLUGIN):
        assert plugin.kind == "scheduled"
        assert plugin.collect_mode == "live_polled"
        assert plugin.requires_network is True
        assert not plugin.required_credentials
        settings = {setting.key: setting for setting in plugin.required_settings}
        assert settings["source_path"].kind == "path"
        assert settings["collection_name"].kind == "text"
        assert settings["dry_run"].kind == "toggle"
        assert any(step.kind == "folder_picker" for step in plugin.setup_steps)
        assert any(step.kind == "test_connection" for step in plugin.setup_steps)
        assert plugin.health_check is not None


def test_health_preview_returns_only_relative_names(tmp_path: Path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "brief.md").write_text("synthetic", encoding="utf-8")
    ctx = SimpleNamespace(config={"source_path": str(tmp_path), "collection_name": "Project A"})

    result = collect_plugins.local_folders_health(ctx)

    assert result.ok is True
    assert result.preview[0]["title"] == "notes/brief.md"
    assert str(tmp_path) not in repr(result.preview)
    assert "1 eligible" in result.summary


def test_local_folders_run_uploads_to_a_sanitized_collection(tmp_path: Path):
    (tmp_path / "brief.md").write_text("synthetic", encoding="utf-8")
    ctx = Context(
        "local-folders",
        {"source_path": str(tmp_path), "collection_name": "Client / Project", "dry_run": False},
    )
    writer = Writer()

    result = collect_plugins.run_local_folders(ctx, writer=writer)

    assert result.uploaded == 1
    assert writer.uploads == [
        ("brief.md", "/vault/imports/local-folders/Client-Project/brief.md")
    ]
    assert ctx.progress_events[-1]["stage"] == "done"


def test_transcript_run_normalizes_and_deduplicates(tmp_path: Path):
    (tmp_path / "call.vtt").write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nSpeaker A: Synthetic update\n",
        encoding="utf-8",
    )
    ctx = Context(
        "meeting-transcripts",
        {"source_path": str(tmp_path), "collection_name": "Team", "dry_run": False},
    )
    writer = Writer()

    first = collect_plugins.run_meeting_transcripts(ctx, writer=writer)
    second = collect_plugins.run_meeting_transcripts(ctx, writer=writer)

    assert first.uploaded == 1
    assert second.unchanged == 1
    assert writer.text[0][0] == "/vault/meetings/imported/Team/call.md"
    assert "Speaker A: Synthetic update" in writer.text[0][1]


def test_transcript_read_failure_does_not_expose_absolute_path(tmp_path: Path):
    source = tmp_path / "bad.txt"
    source.write_bytes(b"\xff\xfe")
    ctx = Context(
        "meeting-transcripts",
        {"source_path": str(tmp_path), "collection_name": "Team", "dry_run": False},
    )
    with pytest.raises(ValueError) as caught:
        collect_plugins.run_meeting_transcripts(ctx, writer=Writer())
    assert str(tmp_path) not in str(caught.value)
    assert "bad.txt" in str(caught.value)


@pytest.mark.parametrize("name", ["", "///", "..", "."])
def test_collection_name_must_have_a_safe_component(name: str):
    with pytest.raises(ValueError, match="collection name"):
        collect_plugins.collection_slug(name)
