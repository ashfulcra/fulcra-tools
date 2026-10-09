from pathlib import Path

from fulcra_local_sources.scan import scan_tree
from fulcra_local_sources.sync import sync_files


class MemoryState:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class RecordingWriter:
    def __init__(self):
        self.uploads = []
        self.text = []

    def upload_file(self, source, remote):
        self.uploads.append((Path(source).name, remote))

    def write_text(self, remote, content):
        self.text.append((remote, content))


def test_raw_folder_sync_is_additive_and_idempotent(tmp_path: Path):
    (tmp_path / "brief.md").write_text("synthetic brief", encoding="utf-8")
    files = scan_tree(tmp_path, allowed_extensions={".md"}).files
    state = MemoryState()
    writer = RecordingWriter()

    first = sync_files(
        files,
        writer=writer,
        state_get=state.get,
        state_set=state.set,
        destination_root="/vault/imports/local-folders/client-a",
    )
    second = sync_files(
        files,
        writer=writer,
        state_get=state.get,
        state_set=state.set,
        destination_root="/vault/imports/local-folders/client-a",
    )

    assert first.uploaded == 1
    assert second.unchanged == 1
    assert writer.uploads == [
        ("brief.md", "/vault/imports/local-folders/client-a/brief.md")
    ]
    assert all(str(tmp_path) not in repr(value) for value in state.values.values())


def test_transcript_sync_renders_markdown_and_does_not_mutate_source(tmp_path: Path):
    source = tmp_path / "call.txt"
    source.write_text("Speaker A: Synthetic update", encoding="utf-8")
    before = source.read_bytes()
    files = scan_tree(tmp_path, allowed_extensions={".txt"}).files
    state = MemoryState()
    writer = RecordingWriter()

    result = sync_files(
        files,
        writer=writer,
        state_get=state.get,
        state_set=state.set,
        destination_root="/vault/meetings/imported/team",
        render_text=lambda item: (
            "---\nsource: meeting-transcript\n---\n\n"
            + item.path.read_text(encoding="utf-8")
        ),
        force_markdown=True,
    )

    assert result.uploaded == 1
    assert writer.text[0][0] == "/vault/meetings/imported/team/call.md"
    assert "Synthetic update" in writer.text[0][1]
    assert source.read_bytes() == before


def test_dry_run_writes_no_remote_or_state(tmp_path: Path):
    (tmp_path / "brief.md").write_text("synthetic brief", encoding="utf-8")
    files = scan_tree(tmp_path, allowed_extensions={".md"}).files
    state = MemoryState()
    writer = RecordingWriter()

    result = sync_files(
        files,
        writer=writer,
        state_get=state.get,
        state_set=state.set,
        destination_root="/vault/imports/local-folders/preview",
        dry_run=True,
    )

    assert result.previewed == 1
    assert not writer.uploads
    assert not writer.text
    assert not state.values


def test_two_transcript_extensions_cannot_overwrite_the_same_remote_path(tmp_path: Path):
    (tmp_path / "call.txt").write_text("synthetic", encoding="utf-8")
    (tmp_path / "call.vtt").write_text("WEBVTT\n\nsynthetic", encoding="utf-8")
    files = scan_tree(tmp_path, allowed_extensions={".txt", ".vtt"}).files
    state = MemoryState()
    writer = RecordingWriter()

    import pytest

    with pytest.raises(ValueError, match="same destination"):
        sync_files(
            files,
            writer=writer,
            state_get=state.get,
            state_set=state.set,
            destination_root="/vault/meetings/imported/team",
            render_text=lambda item: "synthetic",
            force_markdown=True,
        )
    assert not writer.text
    assert not state.values
