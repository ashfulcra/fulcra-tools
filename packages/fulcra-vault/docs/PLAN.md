# fulcra-vault v1 implementation plan

Status: pre-build plan for adversarial review. Source of truth is
[`SPEC.md`](SPEC.md), merged in PR #183. This plan decomposes that spec into
bite-sized TDD tasks and fixes the intended file structure before
implementation starts.

The build must follow the same discipline that shipped `fulcra-prefs`: small
test-first tasks, one focused module boundary per task where possible, no
shared mutable state outside the store/sync boundaries, and a final
whole-implementation review plus live Fulcra Files smoke before merge.

## Non-negotiable invariants

- The vault is the durable store. There is no hidden recall database or
  authoritative derived index.
- Remote file paths are absolute at the Fulcra API boundary.
- Generated JSON is canonical and deterministic.
- Same structure spec plus same explicit timestamps produces byte-identical
  scaffold output.
- Sync never treats read failure as absence on a destructive path.
- Sync never propagates deletions implicitly. Destruction requires the explicit
  `delete` command.
- Human/mirror conflicts preserve the losing version before overwrite.
- Section edits are targeted mutations, not whole-file rewrites.
- Agent read-modify-write commands verify the note version/stat immediately
  before write; if sync or a human mirror edit changed the note after the
  command's read, the command aborts with a retry hint instead of overwriting
  the newer content.
- Every CLI mutation appends one line to `vault/LOG.md` with who/what/when.
- Frontmatter must parse before and after every mutation.
- Missing/not-onboarded vault must not break session-start reads.
- CLI data goes to stdout; status/errors go to stderr.

## File structure

Create the package around these modules:

```text
packages/fulcra-vault/
  README.md
  docs/
    PLAN.md
    SPEC.md
  fulcra_vault/
    __init__.py
    cli.py
    config.py
    frontmatter.py
    links.py
    locks.py
    installers.py
    map.py
    schema.py
    sections.py
    store.py
    sync.py
    vault.py
  skill/
    SKILL.md
    references/
      archetype-exec-team.md
      archetype-household.md
      archetype-researcher.md
      archetype-solo-builder.md
      fulcra-vault-maintenance.md
      fulcra-vault-tier2-readonly.md
  tests/
    conftest.py
    test_cli.py
    test_frontmatter.py
    test_links.py
    test_locks.py
    test_installers.py
    test_map.py
    test_schema.py
    test_sections.py
    test_store.py
    test_sync.py
    test_vault.py
    test_live_smoke.py
```

`store.py` is the only module that shells out to Fulcra or wraps the file API.
`sync.py` is the only module that touches the local mirror filesystem beyond
test fixtures. Everything else should be pure or dependency-injected.

## Task 1: schema and path model

Goal: define the stable data contracts before any behavior leans on loose
dictionaries.

Build:

- `schema.StructureSpec`, `SectionSpec`, and `VaultMeta`.
- Schema version constants.
- Validation for section slugs, seed note names, exclusions, and
  `map_highlights`.
- Path helpers that normalize user note names to vault-relative markdown paths
  and Fulcra absolute paths.

Tests:

- Valid minimal and multi-section structure specs parse.
- Invalid slugs, duplicate slugs, path traversal, absolute local paths, and
  non-markdown seed notes fail with actionable errors.
- Same accepted spec emits canonical JSON bytes in deterministic key order.
- Vault path helpers reject attempts to escape `vault/`.

## Task 2: note section parser and writer

Goal: enforce owned-section edits without fragile whole-document rewrites.

Build:

- `sections.parse_sections(markdown)`.
- `sections.replace_owned_section(markdown, slug, owner, body, force=False)`.
- `sections.append_log(markdown, entry, now, agent)`.
- Clear error types for missing section, duplicate markers, owner mismatch,
  malformed close marker, and missing log section.

Tests:

- Marker round-trip preserves unrelated bytes.
- Replace touches only the target section.
- Owner mismatch refuses without `force`.
- Duplicate or nested target markers refuse.
- Append-log adds one dated line and never rewrites prior log entries.
- Empty files and missing vault files produce user-facing hints, not tracebacks.

## Task 3: frontmatter validation and mutation

Goal: make YAML/frontmatter corruption impossible through the CLI path.

Build:

- `frontmatter.parse_note(markdown)` returning frontmatter plus body.
- `frontmatter.update_keys(markdown, changes)` with stable serialization.
- Validation for flat scalar/list Dataview-compatible keys.

Tests:

- Valid frontmatter round-trips with stable key ordering.
- Invalid YAML refuses before body mutation.
- Invalid emitted keys or unsupported nested values refuse.
- Notes without frontmatter can be initialized deterministically.
- Section edits preserve frontmatter bytes unless frontmatter is intentionally
  changed.

## Task 4: wikilinks and derived index

Goal: make links rebuildable, deterministic, and never authoritative.

Build:

- `links.extract_wikilinks(markdown)`.
- `links.build_index(note_map)` producing canonical `vault/.index/links.json`.
- Backlink lookup helpers.
- Rename planning helpers that compute all link rewrites before applying any.

Tests:

- Link extraction handles aliases, headings, duplicates, punctuation, and plain
  markdown links.
- Index output is byte-identical for shuffled input order.
- Backlinks are deterministic.
- Rename rewrite plan updates every referring wikilink and reports dangling
  links without deleting anything.

## Task 5: map and hot renderers

Goal: implement the injection surfaces as deterministic renderers, not
hand-authored string assembly in the CLI.

Build:

- `map.render_map(spec, notes, links)` for `vault/MAP.md`.
- `map.select_hot_items(notes, links, now, max_items=None)` deriving active
  items, standing corrections, and recent decisions from frontmatter/log
  metadata.
- `map.render_hot(items, max_words=500)` for `vault/HOT.md`.
- Budget check helpers that warn at section boundaries and never silently
  truncate.

Tests:

- Same spec and note set render byte-identical MAP output.
- MAP includes configured section ordering and seed notes.
- HOT selection is deterministic and prioritizes active items, standing
  corrections, and recent decisions before older background notes.
- HOT truncation includes a loud marker and never cuts through the middle of a
  markdown section.
- `map --check` reports over-budget MAP/HOT without mutating files.

## Task 6: structure scaffold and onboarding

Goal: turn the first-run interview output into a deterministic vault scaffold.

Build:

- `vault.plan_scaffold(spec, now)` returning write operations.
- `vault.onboard(spec, store, now)` writing `vault/meta.json`, `vault/MAP.md`,
  `vault/HOT.md`, `vault/LOG.md`, and seed notes.
- `vault.plan_restructure(old_meta, new_spec, existing_notes, now)` producing
  additive-only write operations for new sections, seed notes, and map links.
- `vault.apply_restructure(...)` that writes additive migrations under the
  same stat-verify and vault-log rules as write commands.
- Exclusion enforcement hooks used by later write commands.

Tests:

- Same spec plus same `now` yields byte-identical scaffold operations.
- Onboard refuses to overwrite an existing initialized vault unless explicitly
  forced.
- Seed notes contain valid frontmatter, owned section placeholders, and `## Log`.
- Exclusions are stored in meta and enforced by write helpers.
- Restructure never moves, rewrites, or deletes existing notes.
- Restructure adds only new sections/seed notes and updates meta/MAP
  deterministically.
- Restructure refuses schema downgrades, duplicate new slugs, and any migration
  that would require destructive movement.
- Restructure appends exactly one vault LOG.md entry for the migration and
  aborts if any target file changes between planning and write.

## Task 7: Fulcra Files store

Goal: isolate live API shape and preserve the absolute-path contract learned
from `fulcra-prefs`.

Build:

- `store.FulcraVaultStore` with `read_text`, `write_text`, `stat`, `list`,
  `delete_explicit`, and best-effort version/stat helpers.
- Narrow transport exceptions.
- Optional dependency injection of a command runner or client.

Tests:

- Fake store matches live file command shapes used by `fulcra-prefs`.
- Relative paths are normalized to absolute remote paths exactly once.
- Missing files are distinguishable from transport/read failures.
- Writes use canonical text and do not swallow transport failures.
- Explicit delete logs intent and refuses if confirmation stat fails.

## Task 8: advisory locks

Goal: serialize agent writes without pretending locks constrain human mirror
edits.

Build:

- `locks.acquire(note, holder, now, ttl_seconds=120)`.
- `locks.release(note, holder)`.
- Stale lock reap with holder/timestamp validation.
- Context-manager wrapper for CLI write paths.

Tests:

- Acquiring an unlocked note writes a lock record.
- Active lock held by another agent fails fast with retry hint.
- Stale lock reaps and replaces.
- Release by non-holder refuses.
- Transport failure during lock read/write does not proceed to mutate the note.

## Task 9: write commands

Goal: expose safe note mutation primitives before sync exists.

Build:

- `read <note> [--with-backlinks]`.
- `write-section <note> --section <slug> --agent <id>`.
- `append-log <note> --entry <text> --agent <id>`.
- `backlinks <note>`.
- `reindex`.
- `map` refreshes `vault/MAP.md` and auto-curates `vault/HOT.md` from the
  current note graph; `map --check` reports budget/staleness without writing.
- `doctor` checks for broken links, missing MAP/HOT/meta/log, over-budget
  injection files, side-orphans, and lock staleness.
- Shared write helper that records a pre-read stat/version, acquires the note
  lock, re-stats immediately before writing, and aborts if the note changed
  after the command's read.
- Shared vault-log helper that appends one `vault/LOG.md` line for every CLI
  mutation, including `write-section`, `append-log`, `map`, `reindex`, and
  `restructure`.

Tests:

- CLI stdout/stderr split.
- Missing vault read exits 0 with empty stdout and stderr hint.
- Exclusions refuse writes.
- Write-section and append-log take locks and release them.
- Write-section and append-log abort with a retry hint when a fake store
  mutates the note between the command's read and pre-write stat.
- Write-section, append-log, map refresh, reindex, and restructure each append
  exactly one line to `vault/LOG.md`.
- Reindex is deterministic.
- Doctor reports but does not mutate unless an explicit fix command is later
  added.

## Task 10: local mirror sync core

Goal: implement the three-way state machine in pure code before touching real
files or Fulcra.

Build:

- `sync.SyncState` for `.fulcra-vault-sync.json`.
- Hash and timestamp comparison helpers.
- Pure `sync.plan_sync(local_snapshot, remote_snapshot, state, mode)`.
- Conflict operation model: upload, download, preserve-local-conflict,
  preserve-remote-by-version, skip-on-read-failure, report-side-orphan.

Tests:

- Conflict matrix covers neither changed, local-only, remote-only, both
  changed local-wins, both changed remote-wins, both changed equal timestamp,
  missing local, missing remote, and read-failure on either side.
- Read failure never emits delete.
- Deletion never propagates automatically.
- Loser preservation is planned before overwrite.
- State write is last.

## Task 11: sync filesystem/store integration

Goal: wire the pure sync plan to a local mirror and Fulcra store with no silent
loss paths.

Build:

- `sync run [--push-only|--pull-only]`.
- Local ignore rules for `.obsidian/`, `.conflict/`, and sync state.
- Atomic local state write.
- `.conflict/<note>.<stamp>.md` preservation before local overwrite.
- Remote loser preservation relies on Fulcra file versioning and logs the
  version/stat evidence.
- Mirror-originated uploads update Dataview frontmatter with
  `updated-by: human` before writing remote content.

Tests:

- Temp-dir integration for uploads, downloads, conflicts, ignored files, and
  state crash/retry.
- Local loser is copied before overwrite.
- Uploaded mirror edits are stamped `updated-by: human` without corrupting
  existing frontmatter.
- Sync resumes idempotently after simulated crash before state write.
- Push-only and pull-only modes suppress the opposite direction without
  treating skipped work as success.

## Task 12: sync installer

Goal: make scheduled mirror sync installable on macOS without hiding the fact
that non-macOS installers are outside v1.

Build:

- `installers.install_sync_launchd(mirror, interval_min, command, label)`.
- `install-sync [--interval-min N]` CLI command that writes a launchd plist
  for the current user.
- Idempotent overwrite semantics for the same label.
- Clear error on non-macOS platforms.

Tests:

- Plist contains the configured interval, command, and mirror path.
- Re-running install-sync updates the same plist path, not a duplicate.
- Non-macOS path exits nonzero with a documented v1 limitation.
- Installer does not run sync immediately and does not require the vault to be
  initialized at install time.

## Task 13: explicit delete and rename

Goal: keep destructive operations opt-in and auditable.

Build:

- `delete <note>` removes local plus remote when both confirmations are clean,
  takes the note lock, writes LOG.md, and leaves remote history as rollback.
- `rename <note> <new>` computes link repair across referring notes and
  applies all-or-nothing under locks for touched notes.
- Rename recovery story: because the remote store has no transaction primitive,
  a crash mid-apply is detected by `doctor` as dangling links or partial path
  movement; the operator re-runs `rename` or follows the doctor hint to finish
  the repair.

Tests:

- Delete takes the same note lock as write-section and refuses when the note is
  actively locked by another holder.
- Delete refuses if remote stat/read confirmation fails.
- Delete reports side-orphans instead of guessing.
- Delete appends exactly one vault LOG.md line.
- Rename rewrites all referring wikilinks and the target note path.
- Rename refuses on conflicting destination, dangling source, lock failure, or
  any preflight error before mutation.
- Rename crash-after-N-writes fixture leaves a partial state that `doctor`
  reports with a re-run/repair hint.

## Task 14: CLI composition

Goal: keep the command surface dependency-injected and testable.

Build:

- `cli.run(argv, store=None, local_fs=None, now=None, stdout=None, stderr=None)`.
- Console script entrypoint.
- Argument parsing for the v1 CLI surface in `SPEC.md`.
- Shared error rendering.

Tests:

- Every command has a smoke test through `run()`.
- No command writes data to stderr or status to stdout.
- Common failures exit nonzero with actionable one-line errors.
- Missing vault read/session-start path exits zero as specified.

## Task 15: README and operator docs

Goal: make the package understandable before the skill lands.

Build:

- `README.md` with what it is, install/use, v1 limitations, and safety model.
- Foreground `sync --watch` usage and its relationship to `install-sync`.
- Update `docs/SPEC.md` only if implementation planning reveals a spec
  inconsistency; otherwise leave it as the reviewed contract.

Tests:

- Local doc links resolve.
- README examples match the real CLI names.
- README documents that `sync --watch` is foreground-only in v1.

## Task 16: skill and references

Goal: package the agent behavior that makes the vault useful, not just the CLI.

Build:

- `skill/SKILL.md` with trigger-oriented description.
- Opening interview flow: one anchor question, agent-proposed taxonomy, one
  exclusions question.
- Archetype references for solo builder, exec/team, researcher, household.
- Maintenance reference covering read-before-write, owned sections, log lines,
  map curation, exclusions, and consolidation.
- Tier-2 read-only reference.

Tests:

- Manual checklist review against agent-skills conventions.
- Ensure the skill does not hard-code a developer taxonomy.
- Ensure every write recommendation routes through CLI-capable agents.

## Task 17: live smoke

Goal: verify the actual Fulcra Files contract after unit/integration tests pass.

Build:

- Env-gated `test_live_smoke.py` that creates a throwaway remote root such as
  `vault-smoke/<run-id>/`.
- Onboard a tiny spec, read a seed note, write an owned section, append log,
  reindex, sync to temp mirror, sync a mirror edit back, rename a note, delete
  a note, and run doctor.
- Cleanup is best-effort and never masks the test result.

Tests:

- Skipped by default unless required env vars are present.
- Fails loudly on absolute-path, stat envelope, upload/download, or sync-state
  contract drift.
- Exercises rename and delete against real Fulcra Files, because those are the
  highest-risk no-CAS paths.

## Build sequence

1. Land this plan after adversarial review.
2. Implement tasks 1-5 first; no Fulcra I/O yet.
3. Implement tasks 6-9; CLI can onboard/read/write/reindex/doctor against fake
   store.
4. Implement tasks 10-13; sync/install/delete/rename are reviewed hardest.
5. Implement task 14 and run the full package suite.
6. Implement tasks 15-16.
7. Run a final whole-implementation review focused on spec fidelity, data loss,
   determinism, CLI ergonomics, and non-developer breakage.
8. Run the env-gated live smoke against real Fulcra Files.
9. Route PRs to Arc reviewers first, using
   `claude-code:ArcBot:Arc-Code-Review` while live; if a reviewer pushes
   fixes, require author or second-reviewer sign-off before merge.

## Review focus for this plan

- Does the sync/delete/rename sequencing leave any path to silent edit loss?
- Are owned sections plus advisory locks enough to prevent two-agent
  corruption while still allowing human mirror edits?
- Is the module split too fine or too coarse for the intended subagent-driven
  build?
- Are the tasks small enough for fresh TDD subagents without hidden global
  context?
- Is there any spec promise that has no task and no test gate here?
