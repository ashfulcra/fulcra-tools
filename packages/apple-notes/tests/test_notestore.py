"""Store reading: the measured edge cases must survive."""
from __future__ import annotations

from pathlib import Path
import tempfile

from fulcra_apple_notes import notestore


def _open(container: Path):
    tmp = tempfile.mkdtemp()
    snap = notestore.snapshot(container / "NoteStore.sqlite", Path(tmp))
    return notestore.NoteStore(snap)


def test_husk_notes_are_excluded_by_body_not_by_mod_date(notes_db):
    with _open(notes_db) as store:
        notes = store.notes()
    uuids = {n.uuid for n in notes}
    assert "note-uuid-1111" in uuids
    # The husk has a NULL body and no mod date; filtering on mod date would
    # also exclude it, but for the wrong reason -- assert it is gone.
    assert "husk-uuid-2222" not in uuids


def test_note_carries_title_folder_and_dates(notes_db):
    with _open(notes_db) as store:
        note = next(n for n in store.notes() if n.uuid == "note-uuid-1111")
    assert note.title == "Soup"
    assert note.folder == "Recipes"
    assert note.modified is not None and note.modified.year == 2023
    assert note.created is not None


def test_attachments_without_a_media_row_are_kept_and_flagged(notes_db):
    with _open(notes_db) as store:
        atts = {a.uuid: a for a in store.attachments()}
    assert set(atts) == {"att-1", "att-2"}
    assert atts["att-1"].has_file is True
    # An INNER join on media would have dropped this row entirely.
    assert atts["att-2"].has_file is False
    assert atts["att-2"].note_uuid == "note-uuid-1111"


def test_media_file_is_found_two_levels_below_the_media_uuid(notes_db):
    found = notestore.media_file(notes_db, "media-uuid-1", "photo.png")
    assert found is not None and found.name == "photo.png"
    assert found.parent.name == "inner"


def test_media_file_returns_none_for_unknown_uuid(notes_db):
    assert notestore.media_file(notes_db, "no-such-uuid", "x.png") is None


def test_missing_store_raises_rather_than_returning_no_notes(tmp_path):
    import pytest
    with pytest.raises(notestore.NoteStoreError):
        notestore.snapshot(tmp_path / "NoteStore.sqlite", tmp_path)


def test_paper_bundle_directory_resolves_to_its_pdf_not_the_directory(notes_db):
    # Production failure: "Invalid value for 'LOCAL_FILE': ... Is a directory".
    found = notestore.media_file(notes_db, "media-bundle", "Sample Scan")
    assert found is not None
    assert found.is_file(), "a directory must never be returned for upload"
    assert found.suffix == ".pdf", "the whole document beats an arbitrary page"


def test_zero_byte_attachment_is_not_returned(notes_db):
    # Production failure: "puremagic.main.PureValueError: Input was empty".
    assert notestore.media_file(notes_db, "media-empty", "Image.jpeg") is None


def test_bundle_without_a_pdf_falls_back_to_the_largest_file(notes_db):
    found = notestore.media_file(notes_db, "media-nopdf", "Scan")
    assert found is not None and found.is_file()
    assert found.name == "big.jpeg"


def test_every_resolved_media_path_is_a_usable_file(notes_db):
    for uuid, name in (("media-uuid-1", "photo.png"),
                       ("media-bundle", "Sample Scan"),
                       ("media-nopdf", "Scan")):
        found = notestore.media_file(notes_db, uuid, name)
        assert found is not None and found.is_file() and found.stat().st_size > 0


def test_note_exposes_its_core_data_primary_key_for_writeback(notes_db):
    with _open(notes_db) as store:
        note = next(n for n in store.notes() if n.uuid == "note-uuid-1111")
    # AppleScript addresses notes as x-coredata://<store>/ICNote/p<pk>.
    assert note.pk == 2
