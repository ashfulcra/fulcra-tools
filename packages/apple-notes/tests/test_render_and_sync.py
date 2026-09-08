"""Rendering and the end-to-end dry-run sync."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path

import pytest

from fulcra_apple_notes import render, sync, vaultio

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
LOG = logging.getLogger("test")


def test_filename_is_stable_against_title_changes_via_uuid_suffix():
    a = render.note_filename("ABCDEF12-3456", "Shopping List")
    b = render.note_filename("ABCDEF12-3456", "Groceries")
    assert a.endswith("-abcdef12.md") and b.endswith("-abcdef12.md")
    assert a.startswith("notes/apple/")


def test_two_notes_with_the_same_title_get_different_files():
    assert (render.note_filename("11111111-a", "Notes")
            != render.note_filename("22222222-b", "Notes"))


def test_untitled_note_still_produces_a_valid_filename():
    assert render.note_filename("DEADBEEF-1", "").endswith("untitled-deadbeef.md")


def test_body_is_wrapped_in_an_owner_fence():
    md = render.render_note(
        uuid="u1", title="T", folder="F", body_markdown="hello",
        modified=NOW, created=NOW, deleted=False, synced_at=NOW,
        attachment_links={})
    assert render.OPEN_FENCE in md and render.CLOSE_FENCE in md
    assert md.index(render.OPEN_FENCE) < md.index("hello") < md.index(render.CLOSE_FENCE)


def test_frontmatter_carries_the_state_two_way_sync_will_need():
    md = render.render_note(
        uuid="u1", title="T", folder="F", body_markdown="hello",
        modified=NOW, created=NOW, deleted=False, synced_at=NOW,
        attachment_links={})
    for key in ("apple-uuid:", "apple-modified:", "apple-hash:", "apple-deleted:"):
        assert key in md


def test_title_with_a_colon_is_quoted_so_frontmatter_stays_parseable():
    md = render.render_note(
        uuid="u1", title="Re: budget", folder="", body_markdown="x",
        modified=None, created=None, deleted=False, synced_at=NOW,
        attachment_links={})
    assert 'title: "Re: budget"' in md


def test_deleted_note_is_marked_and_its_body_is_kept():
    md = render.render_note(
        uuid="u1", title="T", folder="", body_markdown="important",
        modified=NOW, created=NOW, deleted=True, synced_at=NOW,
        attachment_links={})
    assert "apple-deleted: true" in md
    assert "important" in md


def test_image_attachment_is_embedded_and_other_types_are_linked():
    md = render.resolve_attachments(
        "{{attachment:a}} {{attachment:b}}",
        {"a": "notes/apple/_attachments/m1/x.png",
         "b": "notes/apple/_attachments/m2/y.xlsx"})
    assert md.startswith("![x.png](")
    assert "[y.xlsx](" in md and "![y.xlsx]" not in md


def test_attachment_with_no_exported_file_degrades_visibly():
    md = render.resolve_attachments("{{attachment:missing}}", {})
    assert "unsupported attachment" in md


def test_content_hash_changes_with_content():
    assert render.content_hash("a") != render.content_hash("b")


def test_dry_run_sync_decodes_everything_and_writes_nothing(notes_db, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("dry run must not touch the vault")
    monkeypatch.setattr(vaultio, "write_text", explode)
    monkeypatch.setattr(vaultio, "upload_file", explode)
    monkeypatch.setattr(vaultio, "read_text", explode)

    stats = sync.run_sync(container=notes_db, dry_run=True, log=LOG)
    assert stats.notes_seen == 2          # two real notes; the husk is excluded
    assert stats.notes_written == 2
    assert stats.notes_failed == 0
    assert stats.attachments_uploaded == 1
    assert stats.attachments_no_file == 1  # the inline-table attachment


def test_corrupt_state_aborts_rather_than_re_uploading_everything(monkeypatch):
    monkeypatch.setattr(vaultio, "read_text", lambda *a, **k: "{not json")
    with pytest.raises(RuntimeError, match="corrupt"):
        sync.load_state(log=LOG)


def test_unreadable_state_propagates_and_is_not_treated_as_first_run(monkeypatch):
    def boom(*a, **k):
        raise vaultio.VaultIOError("network down")
    monkeypatch.setattr(vaultio, "read_text", boom)
    # Treating a transport failure as an empty state would re-upload the
    # whole vault and look like a successful first run.
    with pytest.raises(vaultio.VaultIOError):
        sync.load_state(log=LOG)


def test_missing_state_is_a_first_run(monkeypatch):
    def missing(*a, **k):
        raise vaultio.MissingFile("/vault/notes/apple/.sync-state.json")
    monkeypatch.setattr(vaultio, "read_text", missing)
    state = sync.load_state(log=LOG)
    assert state["notes"] == {} and state["attachments"] == {}


def test_note_that_fails_to_decode_is_skipped_not_written_empty(notes_db, monkeypatch):
    from fulcra_apple_notes.body import BodyDecodeError
    monkeypatch.setattr(
        sync, "decode",
        lambda blob: (_ for _ in ()).throw(BodyDecodeError("boom")))
    stats = sync.run_sync(container=notes_db, dry_run=True, log=LOG)
    assert stats.notes_failed == 2
    assert stats.notes_written == 0


def test_dry_run_reports_quality_aggregates_not_just_counts(notes_db):
    # A decoder that produced empty bodies would report zero failures.
    stats = sync.run_sync(container=notes_db, dry_run=True, log=LOG)
    assert stats.notes_empty_body == 0
    assert stats.markdown_chars > 0
    assert stats.notes_with_attachments == 1   # only the first note has one
    assert stats.notes_with_structure == 2


class FakeVault:
    """In-memory stand-in for Fulcra Files, recording every write."""

    def __init__(self, existing=None):
        self.files = dict(existing or {})
        self.writes = []
        self.uploads = []

    def install(self, monkeypatch):
        def read_text(remote, **kw):
            if remote not in self.files:
                raise vaultio.MissingFile(remote)
            return self.files[remote]

        def write_text(remote, content, **kw):
            self.files[remote] = content
            self.writes.append(remote)

        def upload_file(local, remote, **kw):
            self.files[remote] = "<binary>"
            self.uploads.append(remote)

        def stat(remote, **kw):
            return "ok" if remote in self.files else None

        monkeypatch.setattr(vaultio, "stat", stat)
        monkeypatch.setattr(vaultio, "read_text", read_text)
        monkeypatch.setattr(vaultio, "write_text", write_text)
        monkeypatch.setattr(vaultio, "upload_file", upload_file)
        return self


def test_real_run_writes_note_attachment_and_state(notes_db, monkeypatch):
    vault = FakeVault().install(monkeypatch)
    stats = sync.run_sync(container=notes_db, dry_run=False, log=LOG)
    assert stats.notes_written == 2
    assert any(p.endswith(".md") and "notes/apple/" in p for p in vault.writes)
    assert vault.uploads and vault.uploads[0].endswith(".png")
    assert sync.STATE_PATH in vault.files


def test_second_run_is_idempotent(notes_db, monkeypatch):
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, dry_run=False, log=LOG)
    vault.writes.clear()
    vault.uploads.clear()
    stats = sync.run_sync(container=notes_db, dry_run=False, log=LOG)
    assert stats.notes_written == 0
    assert stats.notes_unchanged == 2
    assert vault.uploads == []
    # Only the state file may be rewritten.
    assert all(p == sync.STATE_PATH for p in vault.writes), (
        "an unchanged run must not rewrite notes or the index")


def test_deadline_stops_early_and_still_saves_progress(notes_db, monkeypatch):
    vault = FakeVault().install(monkeypatch)
    stats = sync.run_sync(container=notes_db, dry_run=False,
                          deadline_s=-1, log=LOG)
    assert stats.stopped_early is True
    assert stats.notes_remaining >= 1
    # A run killed with nothing saved makes no progress and repeats forever.
    assert sync.STATE_PATH in vault.files


def test_a_note_that_vanished_from_the_store_is_marked_deleted(notes_db, monkeypatch):
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, dry_run=False, log=LOG)
    # Seed a note that the vault knows about but the store no longer has.
    state = json.loads(vault.files[sync.STATE_PATH])
    state["notes"]["gone-uuid"] = {
        "path": "notes/apple/gone.md", "hash": "x", "modified": "",
        "title": "Gone", "deleted": False}
    vault.files[sync.STATE_PATH] = json.dumps(state)
    vault.files["/vault/notes/apple/gone.md"] = (
        "---\napple-uuid: gone-uuid\napple-deleted: false\n---\nbody\n")

    stats = sync.run_sync(container=notes_db, dry_run=False, log=LOG)
    assert stats.deletions_marked == 1
    assert "apple-deleted: true" in vault.files["/vault/notes/apple/gone.md"]
    assert "body" in vault.files["/vault/notes/apple/gone.md"]


def test_a_note_still_in_the_store_is_never_marked_deleted(notes_db, monkeypatch):
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, dry_run=False, log=LOG)
    stats = sync.run_sync(container=notes_db, dry_run=False, log=LOG)
    assert stats.deletions_marked == 0
    assert not any("apple-deleted: true" in v
                   for v in vault.files.values() if isinstance(v, str))


def test_a_note_not_processed_because_of_a_limit_is_not_marked_deleted(
        notes_db, monkeypatch):
    # The dangerous case: a pass that handles only some notes must not
    # conclude the rest were deleted.
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, dry_run=False, log=LOG)
    stats = sync.run_sync(container=notes_db, dry_run=False, limit=1, log=LOG)
    assert stats.deletions_marked == 0
    assert not any("apple-deleted: true" in v
                   for v in vault.files.values() if isinstance(v, str))


def test_live_uuid_set_covers_every_note_in_the_store_not_just_processed_ones(
        notes_db):
    # Guards the refactor that would build the live set from the limit
    # slice: the deletion sweep tests membership against it.
    import tempfile as _tf
    from fulcra_apple_notes import notestore as _ns
    with _tf.TemporaryDirectory() as td:
        snap = _ns.snapshot(notes_db / "NoteStore.sqlite", Path(td))
        with _ns.NoteStore(snap) as store:
            assert len({n.uuid for n in store.notes()}) == 2


def test_checkpoint_saves_state_during_a_long_run(notes_db, monkeypatch):
    vault = FakeVault().install(monkeypatch)
    saves = []
    real_save = sync.save_state
    monkeypatch.setattr(sync, "save_state",
                        lambda state, **kw: saves.append(1) or real_save(state, **kw))
    sync.run_sync(container=notes_db, dry_run=False, checkpoint_every=1, log=LOG)
    # One checkpoint after the single note, plus the final save.
    assert len(saves) >= 2


def test_deleted_note_is_marked_in_place_without_losing_its_body(monkeypatch):
    existing = ("---\napple-uuid: u1\napple-deleted: false\n---\n\n"
                "<!-- section:apple-note owner:x -->\nprecious\n"
                "<!-- /section:apple-note -->\n")
    vault = FakeVault({"/vault/notes/apple/n.md": existing}).install(monkeypatch)
    changed = sync._mark_deleted_in_place(
        "/vault/notes/apple/n.md", now=NOW, log=LOG)
    assert changed is True
    updated = vault.files["/vault/notes/apple/n.md"]
    assert "apple-deleted: true" in updated
    assert "precious" in updated, "the vault copy is now the only copy"


def test_marking_deletion_twice_is_a_no_op(monkeypatch):
    existing = "---\napple-uuid: u1\napple-deleted: true\n---\nbody\n"
    FakeVault({"/vault/notes/apple/n.md": existing}).install(monkeypatch)
    assert sync._mark_deleted_in_place(
        "/vault/notes/apple/n.md", now=NOW, log=LOG) is False


def test_index_note_is_written_on_a_changed_run(notes_db, monkeypatch):
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, dry_run=False, log=LOG)
    assert sync.INDEX_PATH in vault.files
    assert "Apple Notes" in vault.files[sync.INDEX_PATH]


def test_index_note_is_not_rewritten_when_nothing_changed(notes_db, monkeypatch):
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, dry_run=False, log=LOG)
    vault.writes.clear()
    sync.run_sync(container=notes_db, dry_run=False, log=LOG)
    assert sync.INDEX_PATH not in vault.writes


def test_index_is_not_written_on_a_partial_pass(notes_db, monkeypatch):
    # A checkpointed partial run must not advertise a smaller library than
    # the vault actually holds.
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, dry_run=False, deadline_s=-1, log=LOG)
    assert sync.INDEX_PATH not in vault.files


def test_sync_never_writes_outside_its_own_namespace(notes_db, monkeypatch):
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, dry_run=False, log=LOG)
    for path in vault.files:
        assert path.startswith("/vault/notes/apple/"), (
            f"{path} is outside the plugin's namespace")


def test_cli_failure_detail_keeps_the_exception_not_the_traceback_header():
    # The callee is a Python CLI: the real cause is the LAST line.
    trace = "Traceback (most recent call last):\n" + ("  frame\n" * 200) + \
            "ValueError: the actual cause\n"
    detail = vaultio._detail(trace)
    assert "ValueError: the actual cause" in detail
    assert len(detail) < len(trace)


def test_deadline_reached_during_attachments_does_not_write_a_partial_note(
        notes_db, monkeypatch):
    """The fix for a production worker kill: one note can carry dozens of
    attachments, so the deadline must be observed between ATTACHMENTS, and
    a note whose attachment set is incomplete must not be recorded as
    synced -- its remaining attachments would never be referenced again."""
    vault = FakeVault().install(monkeypatch)
    # Clock: start=0, loop-top check=0 (not expired), then jump past it.
    ticks = iter([0, 0] + [1000] * 200)
    monkeypatch.setattr(sync.time, "monotonic", lambda: next(ticks))

    stats = sync.run_sync(container=notes_db, dry_run=False,
                          deadline_s=50, log=LOG)
    assert stats.stopped_early is True
    assert stats.notes_written == 0, "a note with incomplete attachments was written"
    assert not any(p.endswith(".md") for p in vault.writes), \
        "no note file may be written when the deadline hit mid-attachment"


def test_attachment_deadline_is_checked_per_attachment_not_only_per_note(
        notes_db, monkeypatch):
    from fulcra_apple_notes import sync as S
    calls = {"n": 0}

    def expired():
        calls["n"] += 1
        return False

    stats = S.SyncStats()
    with __import__("tempfile").TemporaryDirectory() as td:
        snap = S.notestore.snapshot(notes_db / "NoteStore.sqlite", Path(td))
        with S.notestore.NoteStore(snap) as store:
            atts = store.attachments()
    links, hit = S._sync_attachments(
        atts, notes_db, {"attachments": {}}, stats,
        max_bytes=10**9, dry_run=True, log=LOG, expired=expired)
    assert hit is False
    assert calls["n"] == len(atts), "deadline must be tested for every attachment"


def test_state_updated_at_is_the_save_time_not_the_run_start(notes_db, monkeypatch):
    vault = FakeVault().install(monkeypatch)
    old_start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    sync.save_state({"notes": {}, "attachments": {}}, now=old_start)
    saved = json.loads(vault.files[sync.STATE_PATH])
    assert saved["run_started_at"].startswith("2020-01-01")
    assert not saved["updated_at"].startswith("2020-01-01"), \
        "updated_at must reflect when the state was actually written"


def test_resync_preserves_user_text_and_frontmatter(notes_db, monkeypatch):
    import sqlite3
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, log=LOG)
    path = '/vault/' + render.note_filename('note-uuid-1111', 'Soup')
    vault.files[path] = vault.files[path].replace('source: apple-notes', 'my-tag: keep\nsource: apple-notes') + '\nMy annotation stays.\n'
    with sqlite3.connect(notes_db / 'NoteStore.sqlite') as conn:
        conn.execute('UPDATE ZICCLOUDSYNCINGOBJECT SET ZMODIFICATIONDATE1=ZMODIFICATIONDATE1+1 WHERE Z_PK=2')
    sync.run_sync(container=notes_db, log=LOG)
    assert vault.files[path].endswith('\nMy annotation stays.\n')
    assert 'my-tag: keep\n' in vault.files[path]


def test_resync_refuses_removed_fence(notes_db, monkeypatch):
    import sqlite3
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, log=LOG)
    path = '/vault/' + render.note_filename('note-uuid-1111', 'Soup')
    vault.files[path] = 'My restructured note'
    with sqlite3.connect(notes_db / 'NoteStore.sqlite') as conn:
        conn.execute('UPDATE ZICCLOUDSYNCINGOBJECT SET ZMODIFICATIONDATE1=ZMODIFICATIONDATE1+1 WHERE Z_PK=2')
    stats = sync.run_sync(container=notes_db, log=LOG)
    assert vault.files[path] == 'My restructured note'
    assert stats.notes_failed == 1


def test_unavailable_attachment_retains_previously_exported_link(notes_db, monkeypatch):
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, log=LOG)
    path = '/vault/' + render.note_filename('note-uuid-1111', 'Soup')
    before = vault.files[path]
    for p in notes_db.rglob('photo.png'):
        p.unlink()
    sync.run_sync(container=notes_db, log=LOG)
    assert vault.files[path] == before


def test_same_size_attachment_edit_is_uploaded(notes_db, monkeypatch):
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, log=LOG)
    vault.uploads.clear()
    for p in notes_db.rglob('photo.png'):
        p.write_bytes(b'y' * p.stat().st_size)
    sync.run_sync(container=notes_db, log=LOG)
    assert len(vault.uploads) == 1


def test_attachment_link_resolves_from_note_directory():
    from urllib.parse import urljoin
    md = render.resolve_attachments('{{attachment:a}}', {'a': 'notes/apple/_attachments/m/x.png'})
    target = md.split('](', 1)[1].rstrip(')')
    assert urljoin('https://example.test/vault/notes/apple/soup.md', target) == 'https://example.test/vault/notes/apple/_attachments/m/x.png'


def test_unreadable_attachment_does_not_stop_later_notes(notes_db, monkeypatch):
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, log=LOG)
    photo = next(notes_db.rglob('photo.png'))
    original_open = Path.open

    def deny_body(path, *args, **kwargs):
        if path == photo:
            raise PermissionError('synthetic unreadable attachment')
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', deny_body)
    stats = sync.run_sync(container=notes_db, log=LOG)
    assert stats.attachments_failed == 1
    assert stats.notes_unchanged == 2


@pytest.mark.parametrize("edit_seconds", [15, 75])
@pytest.mark.parametrize("apple_changed, expected", [
    (False, "vault_edited"), (True, "conflict"),
])
def test_reconcile_detects_vault_edits_inside_listing_timestamp_margin(
        notes_db, monkeypatch, edit_seconds, apple_changed, expected):
    from datetime import timedelta
    import sqlite3
    from apple_notes_test_helpers import make_body, make_run
    from fulcra_apple_notes import reconcile

    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, dry_run=False, log=LOG)
    state = json.loads(vault.files[sync.STATE_PATH])
    entry = state["notes"]["note-uuid-3333"]
    entry["synced_at"] = NOW.isoformat()
    vault.files[sync.STATE_PATH] = json.dumps(state)
    path = f"/vault/{entry['path']}"
    _, baseline = reconcile.parse_vault_note(vault.files[path])
    vault.files[path] = vault.files[path].replace(baseline, "Vault edit")
    if apple_changed:
        with sqlite3.connect(notes_db / "NoteStore.sqlite") as db:
            db.execute("UPDATE ZICNOTEDATA SET ZDATA=? WHERE Z_PK=12",
                       (make_body("Apple edit", [make_run(10)]),))

    # A real file listing truncates seconds, so even a later edit can appear
    # in the sync minute or in the following minute. Time passing never
    # changes this stored timestamp; both reconciliation passes must find it.
    edited = NOW + timedelta(seconds=edit_seconds)
    listing_row = (f"1 KiB {edited:%Y-%m-%d %I:%M%p} UTC "
                   f"{entry['path'].rsplit('/', 1)[-1]}")
    monkeypatch.setattr(vaultio, "list_dir", lambda *a, **k: listing_row)
    for _ in range(2):
        result = sync.run_reconcile(container=notes_db, log=LOG)
        target = [c for c in result["changes"] if c["uuid"] == "note-uuid-3333"]
        assert [c["status"] for c in target] == [expected]


def test_reconcile_retains_edits_and_conflicts_beyond_two_hundred(
        notes_db, monkeypatch):
    import sqlite3
    from apple_notes_test_helpers import make_body, make_run
    from fulcra_apple_notes import reconcile

    baseline = make_body("Baseline", [make_run(8)])
    with sqlite3.connect(notes_db / "NoteStore.sqlite") as db:
        for index in range(205):
            key = 1000 + index
            db.execute("INSERT INTO ZICNOTEDATA VALUES (?, ?)", (key, baseline))
            db.execute(
                "INSERT INTO ZICCLOUDSYNCINGOBJECT "
                "(Z_PK, Z_ENT, ZIDENTIFIER, ZTITLE1, ZFOLDER, ZNOTEDATA, "
                "ZMODIFICATIONDATE1, ZCREATIONDATE3, ZMARKEDFORDELETION) "
                "VALUES (?, 12, ?, ?, 1, ?, 700000000.0, 690000000.0, 0)",
                (key, f"bulk-{index:04d}", f"Example {index:04d}", key))
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, dry_run=False, log=LOG)
    state = json.loads(vault.files[sync.STATE_PATH])
    for uuid, entry in state["notes"].items():
        if not uuid.startswith("bulk-"):
            continue
        path = f"/vault/{entry['path']}"
        _, body = reconcile.parse_vault_note(vault.files[path])
        vault.files[path] = vault.files[path].replace(body, "Vault edit")
    with sqlite3.connect(notes_db / "NoteStore.sqlite") as db:
        db.execute("UPDATE ZICNOTEDATA SET ZDATA=? WHERE Z_PK=1204",
                   (make_body("Apple edit", [make_run(10)]),))
    monkeypatch.setattr(vaultio, "list_dir", lambda *a, **k: "")

    result = sync.run_reconcile(container=notes_db, log=LOG, download_all=True)
    assert result["summary"]["vault_edited"] == 204
    assert result["summary"]["conflict"] == 1
    assert len(result["changes"]) == 205
    assert {c["uuid"] for c in result["changes"]} == {
        f"bulk-{index:04d}" for index in range(205)}
    assert result["changes"][-1]["status"] == "conflict"


def test_reconcile_propagates_unreadable_note_instead_of_returning_partial_worklist(
        notes_db, monkeypatch):
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, dry_run=False, log=LOG)
    state = json.loads(vault.files[sync.STATE_PATH])
    target = f"/vault/{state['notes']['note-uuid-3333']['path']}"
    read_text = vaultio.read_text

    def unreadable_target(remote, **kwargs):
        if remote == target:
            raise vaultio.VaultIOError("synthetic transient read failure")
        return read_text(remote, **kwargs)

    monkeypatch.setattr(vaultio, "read_text", unreadable_target)
    with pytest.raises(vaultio.VaultIOError, match="synthetic transient read failure"):
        sync.run_reconcile(container=notes_db, log=LOG)


@pytest.fixture(autouse=True)
def isolated_reconcile_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "DEFAULT_RECONCILE_CHECKPOINT",
                       tmp_path / "private" / "reconcile.json", raising=False)


def test_reconcile_deadline_counts_state_and_snapshot_and_caps_reads(notes_db, monkeypatch):
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, log=LOG)
    clock = [0.0]
    monkeypatch.setattr(sync.time, "monotonic", lambda: clock[0])
    original_read = vaultio.read_text
    original_snapshot = sync.notestore.snapshot
    timeouts = []

    def slow_read(remote, **kwargs):
        timeouts.append((remote, kwargs.get("timeout")))
        clock[0] += 290 if remote == sync.STATE_PATH else 21
        return original_read(remote, **kwargs)

    def slow_snapshot(*args, **kwargs):
        clock[0] += 290
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(vaultio, "read_text", slow_read)
    monkeypatch.setattr(sync.notestore, "snapshot", slow_snapshot)
    result = sync.run_reconcile(container=notes_db, log=LOG)
    assert result["complete"] is False
    assert result["notes_remaining"] == 1
    assert len(timeouts) == 2
    assert 0 < timeouts[-1][1] <= 20


def test_reconcile_limit_resumes_private_hash_checkpoint_and_refreshes_apple(
        notes_db, monkeypatch):
    import sqlite3
    import stat
    from apple_notes_test_helpers import make_body, make_run

    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, log=LOG)
    state = json.loads(vault.files[sync.STATE_PATH])
    first_uuid, first_entry = next(iter(state["notes"].items()))
    first_path = f"/vault/{first_entry['path']}"
    from fulcra_apple_notes import reconcile
    _, body = reconcile.parse_vault_note(vault.files[first_path])
    vault.files[first_path] = vault.files[first_path].replace(body, "Private synthetic vault edit")
    first = sync.run_reconcile(container=notes_db, log=LOG, limit=1)
    assert first["complete"] is False
    assert first["stopped_early"] is True
    assert first["notes_remaining"] == 1
    checkpoint = sync.DEFAULT_RECONCILE_CHECKPOINT
    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600
    saved = checkpoint.read_text()
    assert "Private synthetic vault edit" not in saved
    assert body not in saved
    assert first_entry["title"] not in saved

    # The cached vault observation must be reclassified against today's Apple
    # snapshot, rather than caching yesterday's "vault_edited" decision.
    with sqlite3.connect(notes_db / "NoteStore.sqlite") as db:
        db.execute("UPDATE ZICNOTEDATA SET ZDATA=? WHERE Z_PK=10",
                   (make_body("New Apple body", [make_run(14)]),))
    second = sync.run_reconcile(container=notes_db, log=LOG, limit=1)
    assert second["complete"] is True
    assert second["stopped_early"] is False
    assert second["notes_remaining"] == 0
    assert second["vault_files_fetched"] == 1
    assert [c["status"] for c in second["changes"] if c["uuid"] == first_uuid] == ["conflict"]
    assert not checkpoint.exists()

    # Completion consumes the checkpoint: the next invocation reads all files.
    third = sync.run_reconcile(container=notes_db, log=LOG)
    assert third["complete"] is True
    assert third["vault_files_fetched"] == 2


def test_reconcile_read_failure_saves_progress_for_retry(notes_db, monkeypatch):
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, log=LOG)
    state = json.loads(vault.files[sync.STATE_PATH])
    target = f"/vault/{list(state['notes'].values())[1]['path']}"
    original_read = vaultio.read_text

    def fail_second(remote, **kwargs):
        if remote == target:
            raise vaultio.VaultIOError("synthetic failure")
        return original_read(remote, **kwargs)

    monkeypatch.setattr(vaultio, "read_text", fail_second)
    with pytest.raises(vaultio.VaultIOError):
        sync.run_reconcile(container=notes_db, log=LOG)
    monkeypatch.setattr(vaultio, "read_text", original_read)
    resumed = sync.run_reconcile(container=notes_db, log=LOG)
    assert resumed["complete"] is True
    assert resumed["vault_files_fetched"] == 1


@pytest.mark.parametrize("change_scope", ["state", "container"])
def test_reconcile_checkpoint_does_not_cross_changed_sync_scope(notes_db, monkeypatch, change_scope):
    import shutil
    vault = FakeVault().install(monkeypatch)
    sync.run_sync(container=notes_db, log=LOG)
    first = sync.run_reconcile(container=notes_db, log=LOG, limit=1)
    assert first["complete"] is False
    container = notes_db
    if change_scope == "state":
        state = json.loads(vault.files[sync.STATE_PATH])
        next(iter(state["notes"].values()))["hash"] = "new-synthetic-baseline"
        vault.files[sync.STATE_PATH] = json.dumps(state)
    else:
        container = notes_db.parent / "different-container"
        shutil.copytree(notes_db, container)
    result = sync.run_reconcile(container=container, log=LOG)
    assert result["complete"] is True
    assert result["vault_files_fetched"] == 2


def test_reconcile_exhausted_budget_does_not_start_remote_or_snapshot_work(
        notes_db, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("exhausted budget must not start I/O")

    monkeypatch.setattr(vaultio, "read_text", forbidden)
    monkeypatch.setattr(sync.notestore, "snapshot", forbidden)
    result = sync.run_reconcile(container=notes_db, log=LOG, deadline_s=0)
    assert result["complete"] is False
    assert result["notes_remaining"] is None
    assert result["changes"] == []
    assert result["progress"]["phase"] == "state"


def test_reconcile_state_loading_can_exhaust_budget_before_snapshot(notes_db, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(sync.time, "monotonic", lambda: clock[0])

    def slow_state(remote, **kwargs):
        assert remote == sync.STATE_PATH
        assert 0 < kwargs["timeout"] <= 1
        clock[0] = 1.1
        return json.dumps({"notes": {"example": {"path": "notes/apple/example.md"}},
                           "attachments": {}})

    def forbidden(*args, **kwargs):
        raise AssertionError("state read consumed the snapshot budget")

    monkeypatch.setattr(vaultio, "read_text", slow_state)
    monkeypatch.setattr(sync.notestore, "snapshot", forbidden)
    result = sync.run_reconcile(container=notes_db, log=LOG, deadline_s=1)
    assert result["complete"] is False
    assert result["notes_remaining"] == 1
    assert result["progress"]["phase"] == "snapshot"


def test_reconcile_atomic_checkpoint_failure_keeps_previous_private_file(tmp_path, monkeypatch):
    import stat
    from fulcra_apple_notes import reconcile_checkpoint, reconcile

    path = tmp_path / "private" / "checkpoint.json"
    observation = reconcile.VaultObservation(True, True, "0123456789abcdef")
    reconcile_checkpoint.save(path, "example-scope", {"one": observation})
    before = path.read_bytes()

    def fail_replace(*args):
        raise OSError("synthetic interrupted replace")

    monkeypatch.setattr(reconcile_checkpoint.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic interrupted replace"):
        reconcile_checkpoint.save(path, "example-scope", {"two": observation})
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.iterdir()) == [path]


def test_reconcile_rejects_overlapping_checkpoint_scan(tmp_path):
    from fulcra_apple_notes import reconcile_checkpoint

    path = tmp_path / "checkpoint.json"
    with reconcile_checkpoint.locked(path):
        with pytest.raises(RuntimeError, match="already running"):
            with reconcile_checkpoint.locked(path):
                pytest.fail("overlapping scan acquired the same checkpoint")
