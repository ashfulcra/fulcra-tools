from datetime import datetime, timezone
from io import StringIO

from fulcra_vault.cli import run
from fulcra_vault.schema import StructureSpec, VaultMeta, canonical_json
from fulcra_vault.vault import plan_scaffold


NOW = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self):
        self.files: dict[str, str] = {}
        self.stats: dict[str, int] = {}

    def read_text(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write_text(self, path: str, content: str) -> None:
        self.files[path] = content
        self.stats[path] = self.stats.get(path, 0) + 1

    def stat(self, path: str):
        if path not in self.files:
            return None
        return {"version": self.stats.get(path, 0)}

    def list(self, prefix: str = "vault") -> list[str]:
        return sorted(path for path in self.files if path.startswith("/vault/"))

    def delete_explicit(self, path: str, expected_stat=None) -> bool:
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]
        return True


class RacingStore(FakeStore):
    def __init__(self, race_path: str):
        super().__init__()
        self.race_path = race_path
        self.raced = False

    def stat(self, path: str):
        stat = super().stat(path)
        if path == self.race_path and not self.raced:
            self.raced = True
            self.stats[path] = self.stats.get(path, 0) + 1
        return stat


def test_read_missing_vault_exits_zero_with_hint():
    out, err = StringIO(), StringIO()

    rc = run(["read", "Missing"], store=FakeStore(), now=NOW, stdout=out, stderr=err)

    assert rc == 0
    assert out.getvalue() == ""
    assert "not onboarded or note missing" in err.getvalue()


def test_write_section_uses_lock_and_appends_vault_log():
    store = _scaffolded_store()
    out, err = StringIO(), StringIO()

    rc = run(
        [
            "write-section", "Project Alpha",
            "--section", "projects",
            "--agent", "agent-a",
            "--force",
        ],
        store=store,
        now=NOW,
        stdin=StringIO("new body\n"),
        stdout=out,
        stderr=err,
    )

    assert rc == 0
    assert "new body\n<!-- /section:projects -->" in store.files["/vault/Project Alpha.md"]
    assert "/vault/.locks/Project Alpha.md.lock" not in store.files
    assert "write-section Project Alpha.md projects" in store.files["/vault/LOG.md"]
    assert err.getvalue() == "updated Project Alpha.md\n"


def test_append_log_uses_lock_and_appends_vault_log():
    store = _scaffolded_store()

    rc = run(
        ["append-log", "Project Alpha", "--entry", "noted", "--agent", "agent-a"],
        store=store,
        now=NOW,
        stdout=StringIO(),
        stderr=StringIO(),
    )

    assert rc == 0
    assert "agent-a: noted" in store.files["/vault/Project Alpha.md"]
    assert "append-log Project Alpha.md" in store.files["/vault/LOG.md"]


def test_write_aborts_when_note_changes_between_read_and_write():
    store = RacingStore("/vault/Project Alpha.md")
    _load_scaffold(store)
    err = StringIO()

    rc = run(
        [
            "write-section", "Project Alpha",
            "--section", "projects",
            "--agent", "agent-a",
            "--force",
        ],
        store=store,
        now=NOW,
        stdin=StringIO("new body\n"),
        stdout=StringIO(),
        stderr=err,
    )

    assert rc == 2
    assert "changed since read; retry" in err.getvalue()


def test_exclusions_refuse_writes():
    store = _scaffolded_store()
    store.files["/vault/private/Secret.md"] = store.files["/vault/Project Alpha.md"]
    err = StringIO()

    rc = run(
        ["append-log", "private/Secret", "--entry", "nope", "--agent", "agent-a"],
        store=store,
        now=NOW,
        stdout=StringIO(),
        stderr=err,
    )

    assert rc == 2
    assert "excluded path" in err.getvalue()


def test_reindex_and_backlinks_are_deterministic():
    store = _scaffolded_store()
    store.files["/vault/Project Beta.md"] = "# Beta\n\nSee [[Project Alpha]].\n"

    rc = run(["reindex", "--agent", "agent-a"], store=store, now=NOW,
             stdout=StringIO(), stderr=StringIO())

    assert rc == 0
    first = store.files["/vault/.index/links.json"]
    rc = run(["reindex", "--agent", "agent-a"], store=store, now=NOW,
             stdout=StringIO(), stderr=StringIO())
    assert rc == 0
    assert store.files["/vault/.index/links.json"] == first

    out = StringIO()
    rc = run(["backlinks", "Project Alpha"], store=store, now=NOW,
             stdout=out, stderr=StringIO())
    assert rc == 0
    assert out.getvalue() == "Project Beta.md\n"


def test_map_check_reports_without_writing():
    store = _scaffolded_store()
    before = store.files["/vault/MAP.md"]
    err = StringIO()

    rc = run(["map", "--check"], store=store, now=NOW, stdout=StringIO(), stderr=err)

    assert rc == 0
    assert store.files["/vault/MAP.md"] == before
    assert "MAP/HOT render check passed" in err.getvalue()


def _scaffolded_store() -> FakeStore:
    store = FakeStore()
    _load_scaffold(store)
    return store


def _load_scaffold(store: FakeStore) -> None:
    spec = StructureSpec.from_dict({
        "sections": [{
            "slug": "projects",
            "title": "Projects",
            "seed_notes": ["Project Alpha"],
        }],
        "exclusions": ["private"],
        "map_highlights": ["Project Alpha"],
    })
    for op in plan_scaffold(spec, NOW):
        store.write_text(op.path, op.content)
    meta = VaultMeta(spec=spec, created_at=NOW.isoformat(), updated_at=NOW.isoformat())
    store.write_text("/vault/meta.json", canonical_json(meta.to_dict()) + "\n")
