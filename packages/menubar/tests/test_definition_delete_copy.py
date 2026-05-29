"""The soft-delete confirmation must name the track and state, honestly,
that the track itself may not be recoverable (events already written stay).
Pinned to a constant so the menubar and web copy can't silently diverge."""
from fulcra_menubar import _definition_delete as dd


def test_delete_body_states_unrecoverable_and_both_surfaces():
    body = dd.DELETE_TRACK_BODY
    assert "may not be recoverable" in body
    assert "menubar" in body and "web" in body
    assert "timeline" in body


def test_delete_title_names_the_track():
    assert dd.delete_track_title("Journal") == 'Delete the entire "Journal" track?'
