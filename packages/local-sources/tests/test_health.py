"""Setup must stop reading a folder after its saved selection is revoked."""
from types import SimpleNamespace
from contextlib import contextmanager

import pytest

from fulcra_collect import config
from fulcra_local_sources import collect_plugins, scan


@pytest.fixture(autouse=True)
def private_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "private-config"))


@pytest.fixture(params=["local-folders", "meeting-transcripts"])
def selection(request, tmp_path):
    source = tmp_path / "selected"
    source.mkdir()
    (source / "a.md").write_text("synthetic first file")
    settings = {"source_path": str(source), "collection_name": "Synthetic", "dry_run": True}
    saved = config.load()
    saved.update_plugin_settings(request.param, settings)
    config.save(saved)
    ctx = SimpleNamespace(plugin_id=request.param, config=settings, config_epoch="")
    health = (collect_plugins.local_folders_health if request.param == "local-folders"
              else collect_plugins.meeting_transcripts_health)
    return ctx, health, source


def revoke(ctx, change):
    saved = config.load()
    if change == "epoch":
        saved.rotate_plugin_epoch(ctx.plugin_id)
    elif change == "transition":
        try:
            with config.fulcra_account_transition():
                raise RuntimeError("synthetic interrupted sign-in")
        except RuntimeError:
            pass
        return
    else:
        saved.update_plugin_settings(ctx.plugin_id, {change: False if change == "dry_run" else "changed"})
    config.save(saved)


def test_setup_health_works_before_enablement_without_worker_epoch(selection):
    ctx, health, _ = selection
    assert ctx.plugin_id not in config.load().enabled
    assert health(ctx).ok is True


@pytest.mark.parametrize("change", ["source_path", "collection_name", "dry_run", "transition"])
def test_stale_setup_selection_does_not_read_any_content(selection, monkeypatch, change):
    ctx, health, _ = selection
    revoke(ctx, change)
    reads = []
    original = scan.hash_file
    def hashed(*args, **kwargs):
        reads.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(scan, "hash_file", hashed)
    result = health(ctx)
    assert result.ok is False and not result.preview
    assert not reads


@pytest.mark.parametrize("change", ["source_path", "collection_name", "dry_run", "epoch", "transition"])
@pytest.mark.parametrize("second_file", [False, True])
def test_setup_revocation_during_scan_stops_reads_and_discards_preview(
        selection, monkeypatch, change, second_file):
    ctx, health, source = selection
    if second_file:
        (source / "b.md").write_text("synthetic second file")
    reads = []
    original = scan.hash_file
    def hashed(*args, **kwargs):
        reads.append(True)
        result = original(*args, **kwargs)
        revoke(ctx, change)
        return result
    monkeypatch.setattr(scan, "hash_file", hashed)
    result = health(ctx)
    assert result.ok is False and not result.preview
    assert len(reads) == 1


def test_setup_revocation_stops_the_next_chunk_of_a_large_file(selection, monkeypatch):
    ctx, health, source = selection
    (source / "a.md").write_bytes(b"a" * (2 * 1024 * 1024))
    original = scan.open_regular
    reads = []
    class Reader:
        def __init__(self, handle):
            self.handle = handle
        def fileno(self):
            return self.handle.fileno()
        def read(self, size):
            reads.append(size)
            chunk = self.handle.read(size)
            revoke(ctx, "epoch")
            return chunk
    @contextmanager
    def opened(*args, **kwargs):
        with original(*args, **kwargs) as handle:
            yield Reader(handle)
    monkeypatch.setattr(scan, "open_regular", opened)
    result = health(ctx)
    assert result.ok is False and not result.preview
    assert len(reads) == 1
