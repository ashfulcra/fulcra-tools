# fulcra-vault

`fulcra-vault` is a shared markdown knowledge vault stored in Fulcra Files.
It gives humans and agents one durable place for prose memory: projects,
people, decisions, corrections, domain notes, and links between them.

The vault uses ordinary markdown files under `vault/`. Notes are compatible
with Obsidian-style `[[wikilinks]]`, flat Dataview-friendly frontmatter, owned
sections for agent edits, and append-only logs.

## What Is Implemented

The package now includes:

- Structure validation for first-run vault specs.
- Vault path normalization to Fulcra absolute paths.
- Owned-section parsing and safe section replacement.
- Flat frontmatter parsing and stable mutation.
- Wikilink extraction, backlink indexes, and rename planning.
- Deterministic `MAP.md` and `HOT.md` rendering.
- Deterministic scaffold and additive restructure planning.
- Fulcra Files text store wrapper.
- Advisory per-note locks for agent writes.
- Applied `rename` (moves the note and rewrites inbound wikilinks, never
  overwriting the destination) and `delete`, both `--force`-gated, locking
  every touched note and aborting if a note changed since it was read.
- Platform hook installer (`install-hooks`) that injects `HOT.md` at session
  start for `claude-code` and `codex`; the managed-config merge is surgical,
  idempotent, and reversible (`--uninstall`, `--dry-run`).
- Packaged agent skill (`skill/SKILL.md` plus write and raw-HTTP references)
  that routes agents by capability: CLI, raw HTTP, or MCP read-only.
- CLI commands:
  - `init`
  - `read`
  - `write-section`
  - `append-log`
  - `backlinks`
  - `reindex`
  - `map`
  - `rename`
  - `delete`
  - `install-hooks`

Sync (vault sync / local mirror) is still planned work.

## Install For Local Development

From the repository root:

```bash
uv pip install -e packages/fulcra-vault
```

Run the package tests:

```bash
python3 -m compileall -q packages/fulcra-vault/fulcra_vault
uv run pytest packages/fulcra-vault -q
```

Show the CLI surface:

```bash
uv run fulcra-vault --help
```

## Vault Layout

A vault lives under `/vault` in Fulcra Files:

```text
vault/
  meta.json
  MAP.md
  HOT.md
  LOG.md
  .index/
    links.json
  .locks/
    <note>.md.lock
  Project Alpha.md
  People/Ash.md
```

`meta.json` stores the structure spec and exclusions. `MAP.md` is the
structured index. `HOT.md` is a compact session-start summary. `LOG.md` is the
vault-level audit trail.

## Notes

Each note uses flat frontmatter, markdown body text, owned sections, and a
per-note log:

```markdown
---
section: projects
status: seed
title: Project Alpha
updated_at: 2026-06-12T12:00:00+00:00
---
# Project Alpha

<!-- section:projects owner:fulcra-vault -->
Seed note. Replace this with durable context.
<!-- /section:projects -->

## Log
- 2026-06-12T12:00:00+00:00 fulcra-vault: created seed note
```

Owned sections let agents update their own region without rewriting unrelated
bytes. The shared `## Log` section is append-only.

## CLI Examples

Read a note:

```bash
uv run fulcra-vault read "Project Alpha"
```

Read a note with backlinks:

```bash
uv run fulcra-vault read "Project Alpha" --with-backlinks
```

Rewrite an owned section:

```bash
printf 'New durable context.\n' |
  uv run fulcra-vault write-section "Project Alpha" \
    --section projects \
    --agent codex-prefs \
    --force
```

Append to a note log:

```bash
uv run fulcra-vault append-log "Project Alpha" \
  --entry "Captured the current implementation state." \
  --agent codex-prefs
```

Rebuild the link index:

```bash
uv run fulcra-vault reindex --agent codex-prefs
```

Render `MAP.md` and `HOT.md`:

```bash
uv run fulcra-vault map --agent codex-prefs
```

Check rendered map output without writing:

```bash
uv run fulcra-vault map --check
```

## Safety Model

`fulcra-vault` uses inspectable rules:

- The markdown vault is the source of truth.
- Derived files are rebuildable.
- CLI writes validate frontmatter before and after mutation.
- Agent writes take advisory locks.
- A write aborts if the note changes between read and pre-write stat.
- Every CLI mutation appends one line to `vault/LOG.md`.
- Excluded paths from `meta.json` refuse writes.
- Deletes and applied renames are explicit commands, never write side effects.

Locks coordinate agent writes. They do not restrict direct human edits through
the future local mirror.

## Data classification

Despite the name, `fulcra-vault` is **not** an encrypted store. It is a
**plaintext markdown store** persisted through the Fulcra Files API. There is no
encryption at the application layer — notes are written and read as ordinary
markdown bytes, and the CLI performs zero cryptographic operations.

Concretely:

- **Do NOT store secrets or credentials** here — passwords, API keys, tokens,
  private keys, recovery codes, or anything whose disclosure is harmful. Use a
  real secrets manager for those.
- The vault provides **integrity and safe-mutation** guarantees (frontmatter
  validation, owned sections, advisory locks, path-traversal defense — note
  paths are normalized to Fulcra absolute paths and excluded/`meta.json` paths
  refuse writes), but it does **not** provide **confidentiality**. Path
  traversal is defended; secrecy of contents is not.
- Treat vault contents at the confidentiality level of your Fulcra account: any
  agent or session authenticated to the same account can read every note.

Classify what you put here as durable *context and memory*, not sensitive data.

## Development Notes

Most modules are pure and dependency-injected:

- `schema.py`: structure contracts and path helpers.
- `sections.py`: owned-section mutation and note logs.
- `frontmatter.py`: flat frontmatter subset.
- `links.py`: wikilinks, backlinks, rename planning.
- `map.py`: deterministic MAP/HOT rendering.
- `vault.py`: scaffold and restructure planning.
- `store.py`: Fulcra Files text transport.
- `locks.py`: advisory lock records.
- `cli.py`: command composition.

The implementation plan remains in [`docs/PLAN.md`](docs/PLAN.md). The design
contract remains in [`docs/SPEC.md`](docs/SPEC.md).
