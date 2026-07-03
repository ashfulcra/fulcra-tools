"""Regression: catalog-shape drift minted duplicate timeline definitions daily."""
import json
from unittest import mock

from fulcra_coord import annotations


NEW_SHAPE = {"id": "MomentAnnotation/aaa-111", "name": "Test Track",
             "column_name": "moment", "deprecated": False}
NEW_SHAPE_2 = {"id": "MomentAnnotation/bbb-222", "name": "Test Track",
               "column_name": "moment", "deprecated": False}
OLD_SHAPE = {"name": "Test Track",
             "metadata": {"annotation_type": "moment", "id": "ccc-333"}}
WRONG_NAME = {"id": "MomentAnnotation/zzz", "name": "Test Track SMOKE",
              "column_name": "moment"}


def _resolve(entries):
    with mock.patch.object(annotations, "_fulcra_cli_json_lines", return_value=entries), \
         mock.patch.object(annotations, "_fulcra_cli_json") as create:
        create.return_value = {"id": "NEW-MINTED"}
        got = annotations._resolve_def_via_cli("Test Track", "d", [])
        return got, create.called


def test_current_catalog_shape_matches_no_create():
    got, created = _resolve([NEW_SHAPE, WRONG_NAME])
    assert got == "aaa-111" and not created   # top-level id, prefix stripped


def test_duplicates_converge_on_deterministic_oldest():
    got, created = _resolve([NEW_SHAPE_2, NEW_SHAPE])
    assert got == "aaa-111" and not created   # min() -> same pick on every host


def test_legacy_metadata_shape_still_matches():
    got, created = _resolve([OLD_SHAPE])
    assert got == "ccc-333" and not created


def test_creates_only_when_no_exact_match():
    got, created = _resolve([WRONG_NAME])
    assert created and got == "NEW-MINTED"


def test_pinned_canonical_wins_over_catalog(monkeypatch):
    # archived dupes still list in the catalog; the pin must short-circuit
    with mock.patch.object(annotations, "_fulcra_cli_json_lines") as cat, \
         mock.patch.object(annotations, "_fulcra_cli_json") as create:
        got = annotations._resolve_def_via_cli("Agent Tasks", "d", [])
        assert got == "56405cba-02a5-4e75-b93e-37a0e5964a86"
        assert not cat.called and not create.called


def test_unpinned_name_still_resolves_via_catalog():
    with mock.patch.object(annotations, "_fulcra_cli_json_lines",
                           return_value=[{"id": "MomentAnnotation/x-1", "name": "Other Track",
                                          "column_name": "moment"}]), \
         mock.patch.object(annotations, "_fulcra_cli_json") as create:
        assert annotations._resolve_def_via_cli("Other Track", "d", []) == "x-1"
        assert not create.called
