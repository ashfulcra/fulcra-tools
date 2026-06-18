import json
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


def test_map_truncates_hot_to_max_hot_words_instead_of_failing():
    # --max-hot-words must bound HOT by TRUNCATING it to fit, not by failing the
    # command. cmd_map enforced the budget via check_budget but never passed it
    # to render_hot (which self-truncates at a hardcoded 500), so a budget below
    # the natural HOT size made `map` error instead of truncating.
    store = _scaffolded_store()
    for i in range(6):
        store.write_text(
            f"/vault/Note {i}.md",
            f"---\nstatus: active\ntitle: Note {i}\n"
            f"updated_at: 2026-06-1{i}T00:00:00+00:00\n---\n# Note {i}\n\n"
            "This is a reasonably wordy summary line for the hot list entry.\n",
        )
    err = StringIO()

    rc = run(["map", "--check", "--max-hot-words", "50", "--max-map-words", "100000"],
             store=store, now=NOW, stdout=StringIO(), stderr=err)

    assert rc == 0, err.getvalue()
    assert "MAP/HOT render check passed" in err.getvalue()


def test_map_truncated_hot_still_fits_budget_after_marker():
    store = _scaffolded_store()
    for i in range(6):
        store.write_text(
            f"/vault/Note {i}.md",
            f"---\nstatus: active\ntitle: Note {i}\n"
            f"updated_at: 2026-06-1{i}T00:00:00+00:00\n---\n# Note {i}\n\n"
            "This is a reasonably wordy summary line for the hot list entry.\n",
        )
    err = StringIO()

    rc = run(["map", "--check", "--max-hot-words", "26", "--max-map-words", "100000"],
             store=store, now=NOW, stdout=StringIO(), stderr=err)

    assert rc == 0, err.getvalue()
    assert "MAP/HOT render check passed" in err.getvalue()


def test_init_scaffolds_empty_vault():
    store = FakeStore()
    err = StringIO()

    rc = run(["init"], store=store, now=NOW, stdout=StringIO(), stderr=err)

    assert rc == 0, err.getvalue()
    for path in ("/vault/meta.json", "/vault/MAP.md", "/vault/HOT.md", "/vault/LOG.md"):
        assert path in store.files, path
    spec = json.loads(store.files["/vault/meta.json"])["spec"]
    slugs = [s["slug"] for s in spec["sections"]]
    assert slugs == ["projects", "people", "decisions", "domain"]
    # at least one seed note was written
    assert any(p.endswith(".md") and p not in
               {"/vault/MAP.md", "/vault/HOT.md", "/vault/LOG.md"}
               for p in store.files)


def test_init_refuses_existing_vault_without_force():
    store = _scaffolded_store()
    before = store.files["/vault/meta.json"]
    err = StringIO()

    rc = run(["init"], store=store, now=NOW, stdout=StringIO(), stderr=err)

    assert rc == 0
    assert store.files["/vault/meta.json"] == before  # not overwritten
    assert "already initialized" in err.getvalue()


def test_init_force_rescaffolds_with_default_spec():
    store = _scaffolded_store()  # seeded with a single "projects" section
    err = StringIO()

    rc = run(["init", "--force"], store=store, now=NOW, stdout=StringIO(), stderr=err)

    assert rc == 0, err.getvalue()
    spec = json.loads(store.files["/vault/meta.json"])["spec"]
    assert [s["slug"] for s in spec["sections"]] == \
        ["projects", "people", "decisions", "domain"]


def test_init_with_spec_file(tmp_path):
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps({
        "sections": [{"slug": "lab", "title": "Lab", "seed_notes": ["Lab/Intro"]}],
    }))
    store = FakeStore()

    rc = run(["init", "--spec", str(spec_file)], store=store, now=NOW,
             stdout=StringIO(), stderr=StringIO())

    assert rc == 0
    spec = json.loads(store.files["/vault/meta.json"])["spec"]
    assert [s["slug"] for s in spec["sections"]] == ["lab"]


def test_init_malformed_spec_returns_2(tmp_path):
    spec_file = tmp_path / "bad.json"
    spec_file.write_text("{not json")
    err = StringIO()

    rc = run(["init", "--spec", str(spec_file)], store=FakeStore(), now=NOW,
             stdout=StringIO(), stderr=err)

    assert rc == 2
    assert "/vault/meta.json" not in FakeStore().files


def test_rename_moves_note_and_rewrites_inbound_links():
    store = _scaffolded_store()
    store.files["/vault/Project Beta.md"] = "# Beta\n\nSee [[Project Alpha]].\n"
    source_body = store.files["/vault/Project Alpha.md"]
    err = StringIO()

    rc = run(["rename", "Project Alpha", "Project Gamma", "--agent", "agent-a",
              "--force"], store=store, now=NOW, stdout=StringIO(), stderr=err)

    assert rc == 0, err.getvalue()
    assert "/vault/Project Alpha.md" not in store.files          # source gone
    assert store.files["/vault/Project Gamma.md"] == source_body  # content moved
    assert "[[Project Gamma]]" in store.files["/vault/Project Beta.md"]  # link rewritten
    assert "rename Project Alpha.md -> Project Gamma.md" in store.files["/vault/LOG.md"]


def test_rename_without_force_refuses():
    store = _scaffolded_store()
    err = StringIO()

    rc = run(["rename", "Project Alpha", "Project Gamma", "--agent", "agent-a"],
             store=store, now=NOW, stdout=StringIO(), stderr=err)

    assert rc == 2
    assert "/vault/Project Alpha.md" in store.files
    assert "--force" in err.getvalue()


def test_rename_missing_source_returns_2():
    store = _scaffolded_store()
    err = StringIO()

    rc = run(["rename", "Nope", "Project Gamma", "--agent", "agent-a", "--force"],
             store=store, now=NOW, stdout=StringIO(), stderr=err)

    assert rc == 2
    assert "does not exist" in err.getvalue()


def test_rename_refuses_when_destination_exists():
    store = _scaffolded_store()
    store.files["/vault/Project Beta.md"] = "# Beta\n"
    err = StringIO()

    rc = run(["rename", "Project Alpha", "Project Beta", "--agent", "agent-a",
              "--force"], store=store, now=NOW, stdout=StringIO(), stderr=err)

    assert rc == 2
    assert "already exists" in err.getvalue()
    assert "/vault/Project Alpha.md" in store.files          # untouched


def test_rename_refuses_active_lock_before_mutation():
    store = _scaffolded_store()
    store.files["/vault/Project Beta.md"] = "# Beta\n\nSee [[Project Alpha]].\n"
    source_body = store.files["/vault/Project Alpha.md"]
    beta_body = store.files["/vault/Project Beta.md"]
    store.files["/vault/.locks/Project Alpha.md.lock"] = canonical_json({
        "acquired_at": NOW.isoformat(),
        "holder": "other-agent",
        "note": "Project Alpha.md",
        "ttl_seconds": 120,
    }) + "\n"
    err = StringIO()

    rc = run(["rename", "Project Alpha", "Project Gamma", "--agent", "agent-a",
              "--force"], store=store, now=NOW, stdout=StringIO(), stderr=err)

    assert rc == 2
    assert "held by other-agent" in err.getvalue()
    assert store.files["/vault/Project Alpha.md"] == source_body
    assert store.files["/vault/Project Beta.md"] == beta_body
    assert "/vault/Project Gamma.md" not in store.files


def test_rename_aborts_if_touched_note_changes_before_mutation():
    store = RacingStore("/vault/Project Beta.md")
    _load_scaffold(store)
    store.files["/vault/Project Beta.md"] = "# Beta\n\nSee [[Project Alpha]].\n"
    source_body = store.files["/vault/Project Alpha.md"]
    beta_body = store.files["/vault/Project Beta.md"]
    err = StringIO()

    rc = run(["rename", "Project Alpha", "Project Gamma", "--agent", "agent-a",
              "--force"], store=store, now=NOW, stdout=StringIO(), stderr=err)

    assert rc == 2
    assert "changed since read; retry" in err.getvalue()
    assert store.files["/vault/Project Alpha.md"] == source_body
    assert store.files["/vault/Project Beta.md"] == beta_body
    assert "/vault/Project Gamma.md" not in store.files


def test_delete_removes_note_and_logs():
    store = _scaffolded_store()
    assert "/vault/Project Alpha.md" in store.files
    err = StringIO()

    rc = run(["delete", "Project Alpha", "--agent", "agent-a", "--force"],
             store=store, now=NOW, stdout=StringIO(), stderr=err)

    assert rc == 0, err.getvalue()
    assert "/vault/Project Alpha.md" not in store.files
    assert "delete Project Alpha.md" in store.files["/vault/LOG.md"]


def test_delete_without_force_refuses():
    store = _scaffolded_store()
    err = StringIO()

    rc = run(["delete", "Project Alpha", "--agent", "agent-a"],
             store=store, now=NOW, stdout=StringIO(), stderr=err)

    assert rc == 2
    assert "/vault/Project Alpha.md" in store.files          # untouched
    assert "--force" in err.getvalue()


def test_delete_missing_note_returns_2():
    store = _scaffolded_store()
    err = StringIO()

    rc = run(["delete", "Nope", "--agent", "agent-a", "--force"],
             store=store, now=NOW, stdout=StringIO(), stderr=err)

    assert rc == 2
    assert "not found" in err.getvalue() or "missing" in err.getvalue()


def test_delete_refuses_excluded_path():
    store = _scaffolded_store()
    store.files["/vault/private/Secret.md"] = "# secret\n"
    err = StringIO()

    rc = run(["delete", "private/Secret", "--agent", "agent-a", "--force"],
             store=store, now=NOW, stdout=StringIO(), stderr=err)

    assert rc == 2
    assert "/vault/private/Secret.md" in store.files          # untouched
    assert "excluded" in err.getvalue()


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
