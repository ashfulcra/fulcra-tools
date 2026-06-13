import json

import pytest

from fulcra_vault.schema import (
    SCHEMA_VERSION,
    SchemaError,
    StructureSpec,
    VaultMeta,
    fulcra_absolute_path,
    normalize_note_path,
    vault_relative_path,
)


def test_structure_spec_accepts_minimal_and_canonicalizes_json():
    raw = {
        "sections": [
            {
                "slug": "projects",
                "title": "Projects",
                "description": "Active project memory",
                "seed_notes": ["Alpha", "areas/Beta.md"],
            }
        ],
        "map_highlights": ["Alpha"],
        "exclusions": ["private"],
    }

    spec = StructureSpec.from_dict(raw)

    assert spec.schema_version == SCHEMA_VERSION
    assert spec.sections[0].seed_notes == ("Alpha.md", "areas/Beta.md")
    assert json.loads(spec.canonical_json()) == spec.to_dict()
    assert spec.canonical_json() == StructureSpec.from_dict(raw).canonical_json()


@pytest.mark.parametrize(
    "raw",
    [
        {"sections": []},
        {"sections": [{"slug": "BadSlug", "title": "Bad"}]},
        {"sections": [{"slug": "a", "title": "A"}, {"slug": "a", "title": "A2"}]},
        {"sections": [{"slug": "a", "title": "A", "seed_notes": ["../x"]}]},
        {"sections": [{"slug": "a", "title": "A", "seed_notes": ["/x.md"]}]},
        {"sections": [{"slug": "a", "title": "A", "seed_notes": ["x.txt"]}]},
        {"sections": [{"slug": "a", "title": "A"}], "exclusions": ["../secret"]},
        {"sections": [{"slug": "a", "title": "A"}], "exclusions": "private"},
        {"sections": [{"slug": "a", "title": "A"}], "map_highlights": "x"},
    ],
)
def test_structure_spec_rejects_invalid_inputs(raw):
    with pytest.raises(SchemaError):
        StructureSpec.from_dict(raw)


def test_note_path_helpers_normalize_without_escaping_vault():
    assert normalize_note_path("People/Ash") == "People/Ash.md"
    assert normalize_note_path("vault/People/Ash.md") == "People/Ash.md"
    assert vault_relative_path("People/Ash") == "vault/People/Ash.md"
    assert fulcra_absolute_path("People/Ash") == "/vault/People/Ash.md"


@pytest.mark.parametrize("name", ["", "/abs.md", "../x.md", "x/../y.md", "x.txt", "vault"])
def test_note_path_helpers_reject_escape_and_non_markdown(name):
    with pytest.raises(SchemaError):
        normalize_note_path(name)


def test_vault_meta_extra_cannot_override_canonical_keys():
    spec = StructureSpec.from_dict({"sections": [{"slug": "projects", "title": "Projects"}]})
    meta = VaultMeta(
        spec=spec,
        created_at="2026-06-13T00:00:00+00:00",
        updated_at="2026-06-13T00:00:01+00:00",
        extra={
            "schema_version": 999,
            "created_at": "bad",
            "updated_at": "bad",
            "spec": {"bad": True},
            "owner": "codex-prefs",
        },
    )

    data = meta.to_dict()

    assert data["schema_version"] == SCHEMA_VERSION
    assert data["created_at"] == "2026-06-13T00:00:00+00:00"
    assert data["updated_at"] == "2026-06-13T00:00:01+00:00"
    assert data["spec"] == spec.to_dict()
    assert data["owner"] == "codex-prefs"
