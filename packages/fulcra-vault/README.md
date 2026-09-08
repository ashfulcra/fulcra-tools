# fulcra-vault

`fulcra-vault` is a shared markdown knowledge vault stored in Fulcra Files —
one durable place for humans and agents to record what matters: projects,
people, decisions, corrections, domain notes, and links between them. It's
how agents know their user's world beyond the data streams, and the context
belongs to the user rather than any individual agent.

This is a standalone 0.1.0 CLI and agent skill; Collect is not required.
It needs an authenticated Fulcra Files account.

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

## Install and start a vault

Python 3.11+ and `uv`. From the repository root:

```bash
uv tool install ./packages/fulcra-vault
uv tool install fulcra-api
fulcra-api auth login
fulcra-vault init
fulcra-vault read "Projects/Overview"
```

The package itself uses only the Python standard library. Its remote store shells
out to `fulcra-api file ...`, so the separate CLI is a runtime requirement for
Fulcra Files access. `FULCRA_CLI_COMMAND` can select a specific CLI command.

`init` creates a default structure for projects, people, decisions, and domain
notes. For your own structure, pass `--spec structure.json`; the schema is in
[docs/SPEC.md](docs/SPEC.md). An initialized vault refuses another scaffold
unless you supply `--force`.

For session-start context:

```bash
fulcra-vault install-hooks --platform codex
# or: fulcra-vault install-hooks --platform claude-code
```

The [agent skill](skill/SKILL.md) covers ownership, reads, writes, and the raw-HTTP
fallback. A local mirror and sync are still planned; the CLI reads and writes
Fulcra Files directly.

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
  People/Example Person.md
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
fulcra-vault read "Projects/Overview"
```

Read a note with backlinks:

```bash
fulcra-vault read "Projects/Overview" --with-backlinks
```

Rewrite an owned section:

```bash
printf 'New durable context.\n' |
  fulcra-vault write-section "Projects/Overview" \
    --section projects \
    --agent example-agent \
    --force
```

Append to a note log:

```bash
fulcra-vault append-log "Projects/Overview" \
  --entry "Captured the current implementation state." \
  --agent example-agent
```

Rebuild the link index:

```bash
fulcra-vault reindex --agent example-agent
```

Render `MAP.md` and `HOT.md`:

```bash
fulcra-vault map --agent example-agent
```

Check rendered map output without writing:

```bash
fulcra-vault map --check
```

## Safety Model

`fulcra-vault` uses inspectable rules:

- The markdown vault is the source of truth.
- Derived files are rebuildable.
- CLI writes validate frontmatter before and after mutation.
- Note mutations take advisory locks.
- A write aborts if the note changes between read and pre-write stat.
- Note mutations and index/map updates append to `vault/LOG.md`.
- Excluded paths from `meta.json` refuse writes.
- Deletes and applied renames are explicit commands, never write side effects.

Locks coordinate cooperating agents; they cannot prevent a writer that ignores
them. Stat checks detect changes observed before a write, but the multi-file
rename and audit-log updates are not one atomic transaction.

**A missing note is a successful, empty read.** `read` prints a warning on stderr
and returns 0 when a note is missing or the vault is not onboarded. Check the
output as well as the exit status. Likewise, session hooks are intended to leave
a session usable when vault context is unavailable.

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

## Testing

From the repository root:

```bash
uv run --package fulcra-vault --extra dev pytest packages/fulcra-vault/tests -q
```

Tests use in-memory or stubbed stores for CLI mutations, locks, frontmatter,
links, indexing, and hook installation. They do not contact a real vault.
