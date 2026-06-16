"""store.list must preserve paths containing spaces.

`file list` emits bare absolute paths, one per line. _normalize_list_entry used
split()[-1], keeping only the token after the last space, so a real note like
"/vault/Project Alpha.md" was truncated to "/vault/Alpha.md" — corrupting
reindex/map/backlinks for any human-named note.
"""
from fulcra_vault.store import _normalize_list_entry


def test_list_entry_preserves_spaces():
    assert _normalize_list_entry("/vault/Project Alpha.md", "/vault") == \
        "/vault/Project Alpha.md"


def test_list_entry_preserves_spaces_nested():
    assert _normalize_list_entry("/vault/People/Jane Doe.md", "/vault") == \
        "/vault/People/Jane Doe.md"


def test_list_entry_plain_unchanged():
    assert _normalize_list_entry("/vault/A.md", "/vault") == "/vault/A.md"
