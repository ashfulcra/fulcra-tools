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


def test_pinned_cache_entry_never_expires(tmp_path, monkeypatch):
    # operator pin (annotations pin <uuid>) must survive TTL expiry — duplicates
    # are archived but still listed, so resolution alone can pick a wrong one
    monkeypatch.setattr(annotations.cache, "annotations_dir", lambda: tmp_path)
    path = annotations.pin_definition_id("pinned-uuid-1")
    data = json.loads(open(path).read())
    assert data["pinned"] is True
    # even with an ancient written_at the pinned id is honored
    data["written_at"] = "2020-01-01T00:00:00Z"
    open(path, "w").write(json.dumps(data))
    assert annotations._cached_definition_id() == "pinned-uuid-1"


def test_pinned_digest_cache_is_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(annotations.cache, "annotations_dir", lambda: tmp_path)
    annotations.pin_definition_id("digest-uuid-9", digest=True)
    assert annotations._cached_digest_definition_id() == "digest-uuid-9"
    assert annotations._cached_definition_id() is None   # non-digest untouched


def test_unpinned_cache_still_ttl_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(annotations.cache, "annotations_dir", lambda: tmp_path)
    (tmp_path / "definition.json").write_text(json.dumps(
        {"id": "old-uuid", "written_at": "2020-01-01T00:00:00Z"}))   # no pin flag
    assert annotations._cached_definition_id() is None
