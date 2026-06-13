from datetime import datetime, timezone

import pytest

from fulcra_vault.frontmatter import parse_note
from fulcra_vault.schema import StructureSpec, VaultMeta
from fulcra_vault.vault import (
    InitializedVaultError,
    RestructureError,
    WriteOp,
    apply_restructure,
    onboard,
    plan_restructure,
    plan_scaffold,
)


NOW = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)


SPEC = StructureSpec.from_dict({
    "sections": [
        {
            "slug": "projects",
            "title": "Projects",
            "description": "Active work",
            "seed_notes": ["Project Alpha"],
        }
    ],
    "exclusions": ["private"],
    "map_highlights": ["Project Alpha"],
})


class FakeStore:
    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})
        self.writes: list[WriteOp] = []

    def read_text(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write_text(self, path: str, content: str) -> None:
        self.files[path] = content
        self.writes.append(WriteOp(path, content))


class RacingStore(FakeStore):
    def __init__(self, files: dict[str, str], race_path: str, race_content: str):
        super().__init__(files)
        self.race_path = race_path
        self.race_content = race_content
        self.missed_once = False

    def read_text(self, path: str) -> str:
        if path == self.race_path and not self.missed_once:
            self.missed_once = True
            raise FileNotFoundError(path)
        if path == self.race_path:
            self.files[path] = self.race_content
        return super().read_text(path)


def test_plan_scaffold_is_deterministic_for_same_spec_and_time():
    first = plan_scaffold(SPEC, NOW)
    second = plan_scaffold(SPEC, NOW)

    assert first == second
    assert [op.path for op in first] == [
        "/vault/meta.json",
        "/vault/MAP.md",
        "/vault/HOT.md",
        "/vault/LOG.md",
        "/vault/Project Alpha.md",
    ]


def test_seed_notes_have_valid_frontmatter_owned_section_and_log():
    seed = _op(plan_scaffold(SPEC, NOW), "/vault/Project Alpha.md").content

    frontmatter, body = parse_note(seed)

    assert frontmatter == {
        "section": "projects",
        "status": "seed",
        "title": "Project Alpha",
        "updated_at": "2026-06-12T12:00:00+00:00",
    }
    assert "<!-- section:projects owner:fulcra-vault -->" in body
    assert "<!-- /section:projects -->" in body
    assert "## Log\n- 2026-06-12T12:00:00+00:00 fulcra-vault: created seed note" in body


def test_onboard_writes_scaffold_and_refuses_initialized_vault():
    store = FakeStore()

    onboard(SPEC, store, NOW)

    assert "/vault/meta.json" in store.files
    assert "/vault/MAP.md" in store.files
    assert "/vault/Project Alpha.md" in store.files

    with pytest.raises(InitializedVaultError):
        onboard(SPEC, store, NOW)


def test_onboard_force_overwrites_existing_meta():
    store = FakeStore({"/vault/meta.json": "{}"})

    onboard(SPEC, store, NOW, force=True)

    assert "\"schema_version\":1" in store.files["/vault/meta.json"]


def test_plan_restructure_is_additive_only():
    old_spec = SPEC
    new_spec = StructureSpec.from_dict({
        "sections": [
            {
                "slug": "projects",
                "title": "Projects",
                "description": "Active work",
                "seed_notes": ["Project Alpha", "Project Beta"],
            },
            {
                "slug": "people",
                "title": "People",
                "seed_notes": ["People/Ash"],
            },
        ]
    })
    meta = VaultMeta(spec=old_spec, created_at=NOW.isoformat(), updated_at=NOW.isoformat())
    existing = {
        "Project Alpha.md": "# Existing\n",
        "Project Beta.md": "# Human already made beta\n",
    }

    ops = plan_restructure(meta, new_spec, existing, NOW)

    assert [op.path for op in ops] == [
        "/vault/meta.json",
        "/vault/MAP.md",
        "/vault/LOG.md",
        "/vault/People/Ash.md",
    ]
    assert all("Project Beta.md" not in op.path for op in ops)


def test_plan_restructure_refuses_schema_downgrade_and_removed_sections():
    bad_meta = VaultMeta(
        spec=SPEC,
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
        schema_version=2,
    )

    with pytest.raises(RestructureError, match="schema downgrade"):
        plan_restructure(bad_meta, SPEC, {}, NOW)

    removed = StructureSpec.from_dict({
        "sections": [{"slug": "people", "title": "People"}],
    })
    meta = VaultMeta(spec=SPEC, created_at=NOW.isoformat(), updated_at=NOW.isoformat())

    with pytest.raises(RestructureError, match="remove section"):
        plan_restructure(meta, removed, {}, NOW)


def test_apply_restructure_writes_additive_ops_and_refuses_changed_targets():
    new_spec = StructureSpec.from_dict({
        "sections": [
            {
                "slug": "projects",
                "title": "Projects",
                "seed_notes": ["Project Alpha", "Project Beta"],
            }
        ]
    })
    meta = VaultMeta(spec=SPEC, created_at=NOW.isoformat(), updated_at=NOW.isoformat())
    store = FakeStore({"/vault/Project Alpha.md": "# Existing\n"})

    apply_restructure(meta, new_spec, store, NOW)

    assert "/vault/Project Beta.md" in store.files
    assert store.writes[-2].path == "/vault/LOG.md"

    changed = RacingStore(
        {"/vault/Project Alpha.md": "# Existing\n"},
        "/vault/Project Beta.md",
        "# Race\n",
    )
    with pytest.raises(RestructureError, match="changed before write"):
        apply_restructure(meta, new_spec, changed, NOW)


def _op(ops: list[WriteOp], path: str) -> WriteOp:
    for op in ops:
        if op.path == path:
            return op
    raise AssertionError(f"missing op {path}")
