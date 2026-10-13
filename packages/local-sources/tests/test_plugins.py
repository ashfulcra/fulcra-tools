from pathlib import Path
from unittest.mock import Mock

import pytest

from fulcra_local_sources import collect_plugins
from fulcra_collect import config as collect_config, credentials as collect_credentials


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "private-config"))
    collect_credentials._clear_caches()
    monkeypatch.setattr(collect_credentials, "_keyring_get", lambda service, key: "synthetic-token")


class Context:
    def __init__(self, plugin_id: str, config: dict):
        self.plugin_id = plugin_id
        self.config = config
        self.log = Mock()
        self._state = {}
        self.progress_events = []
        saved = collect_config.load()
        saved.enable(plugin_id)
        saved.update_plugin_settings(plugin_id, config)
        collect_config.save(saved)
        self.config_epoch = saved.plugin_epochs[plugin_id]

    def collect_token(self):
        return "synthetic-token"

    def kv_get(self, key, default=None):
        return self._state.get(key, default)

    def kv_set(self, key, value):
        self._state[key] = value

    def progress(self, **fields):
        self.progress_events.append(fields)


class Writer:
    namespace = "synthetic-account"

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
    ctx = Context("local-folders", {"source_path": str(tmp_path), "collection_name": "Project A"})

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


@pytest.mark.parametrize("change", ["disable", "source_path", "collection_name", "dry_run",
                                    "account_transition", "epoch"])
def test_changed_consent_stops_in_flight_upload(tmp_path, change):
    (tmp_path / "brief.md").write_text("synthetic selected data")
    ctx = Context("local-folders", {"source_path": str(tmp_path),
                                    "collection_name": "chosen", "dry_run": False})
    writer = Writer()

    def progress(**fields):
        if fields.get("stage") != "scanned":
            return
        cfg = collect_config.load()
        if change == "disable":
            cfg.disable("local-folders")
        elif change == "account_transition":
            try:
                with collect_config.fulcra_account_transition():
                    raise RuntimeError("synthetic interrupted sign-in")
            except RuntimeError:
                pass
            return
        elif change == "epoch":
            cfg.rotate_plugin_epoch("local-folders")
        else:
            value = True if change == "dry_run" else "different"
            cfg.update_plugin_settings("local-folders", {change: value})
        collect_config.save(cfg)

    ctx.progress = progress
    with pytest.raises(RuntimeError, match="configuration changed"):
        collect_plugins.run_local_folders(ctx, writer=writer)
    assert not writer.uploads
    assert not ctx._state


def test_preview_requires_an_actual_boolean(tmp_path):
    ctx = Context("local-folders", {"source_path": str(tmp_path),
                                    "collection_name": "chosen", "dry_run": "false"})
    with pytest.raises(ValueError, match="Preview only"):
        collect_plugins.run_local_folders(ctx, writer=Writer())


def test_unannounced_token_replacement_stops_upload(tmp_path, monkeypatch):
    (tmp_path / "brief.md").write_text("synthetic")
    ctx = Context("local-folders", {"source_path": str(tmp_path), "collection_name": "Test"})
    writer = Writer()
    assert collect_credentials.get_user_secret("bearer-token") == "synthetic-token"
    scan = collect_plugins.scan_tree
    def changed(*args, **kwargs):
        result = scan(*args, **kwargs)
        monkeypatch.setattr(collect_credentials, "_keyring_get", lambda service, key: "different-account")
        assert collect_credentials.get_user_secret("bearer-token") == "synthetic-token"
        return result
    monkeypatch.setattr(collect_plugins, "scan_tree", changed)
    with pytest.raises(RuntimeError, match="configuration changed"):
        collect_plugins.run_local_folders(ctx, writer=writer)
    assert not writer.uploads
