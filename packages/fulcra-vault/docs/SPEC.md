# fulcra-vault — validated design spec (2026-06-11)

Status: design locked with Ash in-session 2026-06-11 (write model: owned
sections + LWW; retrieval: map-note + link-following; human surface: real
Obsidian via local mirror; structure: interview-driven, fully generic).
Pre-implementation review artifact for Ashs-MBP-Work:Codex-Review-Workbook.

## What this is

An **agent-maintained, Obsidian-like shared knowledge vault** on the Fulcra
file library: plain-markdown notes with `[[wikilinks]]`, readable and writable
by every agent the user runs (Claude Code, Codex, OpenClaw, Hermes, ChatGPT
via recipes) and by the user in **actual Obsidian** through a local mirror.
It holds the prose knowledge that typed preference signals can't: people,
projects, decisions, standing corrections, domain notes — one cross-agent,
cross-machine memory with edges, instead of fragmented per-machine memory
files that drift.

**Generalization is a hard constraint.** The package ships ZERO taxonomy.
Vault structure comes from a first-run interview (the skill's opening move);
nothing in code or skill assumes a developer fleet, a coordination bus, or
any particular life. A novelist, a founder, and a household must all get
vaults shaped like their answers.

Companion to `fulcra-prefs` (same store patterns, same tier model, same
onboarding family: fulcra-onboarding → fulcra-prefs → fulcra-vault).

## Architecture

### Storage: plain markdown in the file library

- Vault root: `vault/` in the user's Fulcra file library (file API verified
  in FULCRA-PRIMITIVES.md; absolute paths at the API boundary per the
  fulcra-prefs lesson).
- Every note is a real `.md` file — valid Obsidian markdown, YAML
  frontmatter, `[[wikilinks]]` (vault-relative note names, Obsidian's
  shortest-path convention).
- `vault/MAP.md` — the map note (see Retrieval). `vault/meta.json` — schema
  version, the structure spec from the interview, created/updated stamps.
- History/undo = the file library's native versioning. No separate index
  files as source of truth; the link index is derived, never authoritative.

### Write model: owned sections + last-writer-wins

Notes are mutable files; safety is conventions enforced by the CLI:

- A note body is divided into **sections**. A section is either:
  - **owned** — fenced by `<!-- section:<slug> owner:<agent-id> -->` …
    `<!-- /section:<slug> -->`. Only the owner rewrites it; the CLI's
    `write-section` refuses (without `--force`) to modify a section owned by
    a different agent id.
  - **shared log** — every note ends with `## Log`, append-only; any agent
    appends one-line dated entries via `append-log`. Appends never rewrite
    existing content.
- Whole-file writes are last-writer-wins; the losing version survives in
  file history. Frontmatter `updated-by` + `updated-at` make races visible.
- Human edits via the mirror are exempt from ownership rules (the user owns
  everything); the sync layer stamps them `updated-by: human`.

### Retrieval: map note + link-following

- `vault/MAP.md` is the single injected entry point — the user's taxonomy
  rendered as curated wikilink lists ("hot" items first), small enough to
  inject whole (target < 2 KB; `doctor` warns beyond it).
- Session start (hook or skill step): inject MAP.md. Working on something
  mapped? `fulcra-vault read <note> [--with-backlinks]` pulls the note and,
  optionally, one hop of backlinks (titles + first lines, not full bodies).
- The **link index** (`vault/.index/links.json`) is derived by `reindex`:
  canonical JSON, deterministic ordering (fulcra-prefs determinism rules) —
  rebuildable from the notes at any time, never hand-edited, safe to lose.
- Map curation is part of the maintenance contract (skill): when an agent
  creates a note that matters beyond one session, it adds a map link in the
  section the structure spec designates; the consolidation pass prunes.

### Human surface: real Obsidian via local mirror

- `fulcra-vault sync` — one-shot bidirectional mirror between `vault/` and a
  local directory (default `~/Fulcra/vault`), which the user opens as an
  Obsidian vault (editor, graph view, mobile via Obsidian's own sync if they
  choose, plugins — all free).
- Change detection: content hash comparison against a sync-state file
  (`.fulcra-vault-sync.json` in the local mirror, hash per path for both
  sides at last sync). Three-way: local-only change → upload; remote-only →
  download; both changed → **LWW by updated-at, loser preserved** (remote
  loser survives in file-library history; local loser copied to
  `.conflict/<note>.<stamp>.md` in the mirror before overwrite — nothing is
  silently destroyed).
- `install-sync` — launchd agent (macOS first) running `sync` on an interval
  (default 5 min) with debounce; `sync --watch` documented for foreground.
- Obsidian-specific artifacts (`.obsidian/`, plugins, workspace state) are
  mirror-local and never uploaded (sync ignore list).

### First-run interview → structure spec (the generalization mechanism)

- The **skill** opens with an interview (agent-led, conversational, in the
  fulcra-onboarding voice): what do you do; which assistants/agents do you
  use and for what; what knowledge should outlive a session; what's
  off-limits or restricted; what belongs at the top of your map.
- The interview produces a **structure spec** (JSON): ordered sections, each
  `{slug, title, description, seed_notes[]}`, plus `exclusions[]` (paths/
  topics agents must not write) and `map_highlights[]`.
- Code makes it real: `fulcra-vault onboard --from-spec spec.json` validates
  the spec (schema-versioned), scaffolds folders + seed notes + MAP.md in
  the user's taxonomy, writes `vault/meta.json`. Deterministic: same spec →
  byte-identical scaffold (modulo timestamps passed explicitly).
- `restructure` re-runs interview → produces a NEW spec → v1 applies
  **additive-only** migrations (new sections/seeds; never moves or deletes).
- The skill ships **archetype references** (solo builder, exec/team,
  researcher, household) as adaptation material for the interviewing agent —
  explicitly never applied verbatim.

### Maintenance contract (the "auto agent-maintained" part, in the skill)

- Write facts on the entity's note, in your owned section or the log —
  check `read --with-backlinks` first; update, don't duplicate.
- Respect `exclusions` from the structure spec absolutely.
- End of session: append one log line to notes you materially used; add map
  links for new durable notes.
- Periodic consolidation pass (user-triggered or scheduled): merge
  duplicates, fix dangling links (`doctor` lists them), prune the map,
  retire stale claims — the cross-agent consolidate-memory.

### Tier model (same as fulcra-prefs)

- Tier 1 (CLI): full surface.
- Tier 2 (HTTP, no shell): read MAP.md + notes via file download; append-log
  via... **not in v1** — tier-2 is read-only for the vault (file uploads
  need the two-step signed-URL dance; recipe documented as read-only, write
  path noted as the same platform gap as prefs').
- Tier 3 (MCP-only): read-side when/if MCP file tools land; out of scope.

## CLI surface (v1)

`fulcra-vault onboard --from-spec <json> | read <note> [--with-backlinks] |
write-section <note> --section <slug> --agent <id> (stdin body) |
append-log <note> --entry <text> --agent <id> | map [--check] |
backlinks <note> | reindex | doctor | sync [--push-only|--pull-only] |
install-sync [--interval-min N] | restructure --from-spec <json>`

Conventions carried from fulcra-prefs: dependency-injected `run()` for tests;
status → stderr, data → stdout; canonical JSON for all derived artifacts;
explicit `now`; store module is the only Fulcra I/O; transport-only exception
spooling where applicable (sync retries, never crashes the daemon).

## Error handling & edges

- Sync conflict: LWW + dual preservation (above). Sync crash mid-run: state
  file written atomically last; next run reconverges from hashes.
- Broken wikilinks: `doctor` reports; never auto-deleted.
- Map > size budget: `doctor` warns; injection truncates at section
  boundaries with a "(map truncated — run fulcra-vault map)" marker.
- A note matching an exclusion: `write-section`/`append-log` refuse with an
  actionable error.
- Missing vault (not onboarded): every read command exits 0 with empty
  output + stderr hint (never breaks a session start — prefs' inject rule).

## Testing

TDD throughout. Pure units: section parser/writer (ownership enforcement,
marker round-trip), link extractor + index determinism (byte-identical,
shuffle-invariant), map renderer, structure-spec validator + scaffold
determinism, sync three-way merge logic against a fake store + temp dirs
(the conflict matrix: local/remote/both/neither). One env-gated live smoke
(scaffold a throwaway spec under `vault-smoke/`, write/read/sync round-trip).
Fake FulcraAPI reused from fulcra-prefs' conftest patterns — with the real
shapes already verified there.

## v1 cut-line

**In:** everything above. **Out (explicit):** tier-2 writes; MCP; search-first
retrieval (revisit when a real vault outgrows the map); destructive
restructure migrations; prefs-note rendering + consent-graph edges into the
vault (fast-follow once both packages are stable); non-macOS sync installers
(sync itself is portable; launchd installer is macOS-only in v1); Obsidian
plugin (the mirror makes it unnecessary for v1).

Isolation: `packages/fulcra-vault/**` on branch `claude-code/fulcra-vault`,
worktree `fulcra-tools-vault`, PR + adversarial review per the global rule.
