"""normalize_note_path should accept any-case .md and normalize it.

The suffix check was case-sensitive ("Report.MD" raised "must be markdown").
"""
import pytest

from fulcra_vault.schema import normalize_note_path, SchemaError


def test_uppercase_md_accepted_and_normalized():
    assert normalize_note_path("Report.MD") == "Report.md"


def test_mixed_case_md_normalized():
    assert normalize_note_path("People/Jane.Md") == "People/Jane.md"


def test_no_suffix_gets_md():
    assert normalize_note_path("Ash") == "Ash.md"


def test_non_md_suffix_still_rejected():
    with pytest.raises(SchemaError):
        normalize_note_path("notes.txt")
