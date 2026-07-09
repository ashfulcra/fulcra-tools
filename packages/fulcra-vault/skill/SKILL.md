---
name: fulcra-vault
description: "Read and write the user's shared markdown knowledge vault in Fulcra — durable prose memory for projects, people, decisions, and domain notes, linked with [[wikilinks]]. Routes by agent capability: CLI (preferred), raw HTTP, or MCP read-only."
homepage: "https://github.com/ashfulcra/fulcra-tools/tree/main/packages/fulcra-vault"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "📓" } }
---

# fulcra-vault

The vault is the user's durable prose memory, stored as ordinary markdown in
their Fulcra account: projects, people, decisions, corrections, domain notes,
and the links between them. Your job: LOAD what's hot at session start, READ the
relevant notes, and WRITE durable context back as you learn it.

`HOT.md` is the compact session-start summary; `MAP.md` is the full index;
each note carries flat frontmatter, agent-owned sections, and an append-only
`## Log`.

## Where to start — the re-entrancy probes

Before loading anything, probe how far this user already got. Enter at the
**first row whose probe fails**:

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Authed? | `fulcra user-info` | exits 0 and prints valid JSON | [Pick your path](#pick-your-path) — install, then `fulcra auth login` |
| Vault initialized? | `fulcra-vault map --check` | exits 0 (`MAP/HOT render check passed`); exit 2 naming `/vault/meta.json` means no vault yet | [Onboarding a new user](#onboarding-a-new-user) — `fulcra-vault init` |
| Content present? | `fulcra-vault read HOT` | non-empty stdout (HOT.md renders notes) | [Pick your path](#pick-your-path) tier-1 write — vault is empty; start writing durable context |
| Hooks installed? | `grep -q fulcra-vault-hooks ~/.claude/settings.json` (or `~/.codex/hooks.json`; custom `--target-dir` installs won't match this grep) | exits 0 — SessionStart injects `HOT.md` | [Onboarding a new user](#onboarding-a-new-user) — `fulcra-vault install-hooks --platform <claude-code\|codex>` |

First failure wins. Hooks are optional: without them you just `read HOT`
yourself; everything else works the same. All four pass → `HOT.md` is auto-injected
at session start; read the relevant notes and write durable context back as you learn it.

## Pick your path

1. **You can run shell commands** → use the CLI. Setup once:
   `uv tool install "git+https://github.com/ashfulcra/fulcra-tools.git#subdirectory=packages/fulcra-vault"`
   (and `fulcra auth login` if not authed).
   - Load: if the SessionStart hook is installed, `HOT.md` is already in your
     context. Otherwise `fulcra-vault read HOT` and read it. Empty output = no
     vault yet; see onboarding.
   - Read a note: `fulcra-vault read "<note>"` (add `--with-backlinks` to see
     what links to it).
   - Find related notes: `fulcra-vault backlinks "<note>"`, or
     `fulcra-vault map --check` to render the index without writing.
   - Write durable context: `fulcra-vault write-section "<note>" --section
     <slug> --agent <you>` (body on stdin). This replaces only your owned
     section, never the rest of the note. See references/fulcra-vault-write.md.
   - Log a decision/correction: `fulcra-vault append-log "<note>" --entry
     "<text>" --agent <you>` — append-only, never rewrites history.
   - After writes: `fulcra-vault reindex` then `fulcra-vault map` to refresh the
     link index and MAP/HOT.
2. **You can make HTTP requests but not run commands** → follow
   references/fulcra-vault-tier2-http.md (device-flow auth + the Fulcra Files
   API).
3. **You only have the Fulcra MCP** → you can read user data the MCP exposes,
   but the vault's read/write surface is not available via MCP today. Ask the
   user to run onboarding from a CLI-capable agent.

## Onboarding a new user

If `read HOT` reports not onboarded: run `fulcra-vault init` (requires
`fulcra auth login` first). That scaffolds `meta.json`, `MAP.md`, `HOT.md`,
`LOG.md`, and seed notes from a default structure (projects, people, decisions,
domain). Pass `--spec <file>` to supply your own `StructureSpec`.

To get `HOT.md` injected at the top of every session automatically:
`fulcra-vault install-hooks --platform <claude-code|codex>`.

## Rules

- The markdown is the source of truth; `MAP.md`/`HOT.md`/`.index/` are derived
  and rebuildable with `reindex`/`map`.
- Edit only your owned section of a note; the shared `## Log` is append-only.
- Link notes with `[[wikilinks]]` so they show up in backlinks and the map.
- Excluded paths in `meta.json` refuse writes — respect them.
- NEVER print or store the user's access token.
