# Proposal: a new official `fulcra-vault` skill (locked notes + hot-context injection)

**Status:** draft (reeval epic phase-4, item 2) · **Author:** fulcra-prefs-maintainer
**Target venue:** `fulcradynamics/agent-skills` (issue → PR from an `ashfulcra` fork)
**Source of record:** phase-3 realignment verdict (`artifact/2026-07-04-prefs-vault-realignment-verdict.md`, APPROVED)

> Staged in-repo for codex-prefs review before any external post. This proposes a
> new official skill (prose over `fulcra-api file`); no package code ships from it.
> The `fulcra-tools/packages/fulcra-vault` CLI remains the power-user reference
> implementation.

## Why a new skill (the gap)

Nothing in the official `agent-skills` repo covers a **shared, durable knowledge
vault**: markdown notes agents and humans co-edit, wikilinks + backlinks, owned
sections for safe agent edits, a rendered hot-context file, or the concurrency
discipline needed to write to it safely. The official surface is prose over the
`fulcra-api` CLI with no package dependency; a vault skill fits that shape exactly
— it is entirely `fulcra-api file` operations plus conventions.

## The one broadly-useful primitive: optimistic concurrency over LWW files

This matters to the **whole fleet**, not just vault. Fulcra Files are
**last-write-wins with no conditional writes** (verified live 2026-07-04): there is
no ETag/`If-Match`/CAS. Any two agents writing the same file can silently clobber
each other. The vault's answer — worth standardizing as a documented convention —
is a **client-built compare-and-set**:

1. **Read** the target note and remember its current version identity (the file's
   resolved version id from `resolve_filepath`, or a content hash).
2. Do the edit locally (owned-section replace / log append).
3. **Re-verify** the version identity immediately before writing; if it changed
   since step 1, **abort and retry** (re-read, re-apply) rather than overwrite.
4. Back it with a short-lived **advisory lock** (`vault/.locks/<note>.lock`,
   holder-id + timestamp, self-reaped after ~120 s) so concurrent writers fail fast
   with a retry hint instead of racing.

Locks are advisory (humans and mirrors ignore them); the abort-if-changed check is
the real safety. This pattern is the generally-useful export — document it once in
the skill so any agent writing to Files (not only vault) can adopt it.

## Proposed skill contents (SKILL.md, prose over `fulcra-api file`)

- **Layout:** notes under `vault/`, Obsidian-style `[[wikilinks]]`, flat
  Dataview-friendly frontmatter, append-only `## Log` sections, and per-agent
  **owned sections** (`<!-- owned:<agent> -->` … ) so an agent edit never disturbs
  a human's or another agent's block.
- **Read:** `fulcra-api file download` the note (or `MAP.md`) and parse.
- **Write:** the optimistic-concurrency recipe above, expressed as steps over
  `fulcra-api file` (resolve → download → edit → re-resolve/verify → upload, abort
  on version mismatch).
- **Index:** `MAP.md` (all notes + links) and `HOT.md` (the hot set) are
  deterministically rendered from the notes; a reindex re-derives them.
- **Rename/delete:** move the note and rewrite inbound wikilinks, locking every
  touched note and aborting if any changed — the multi-file version of the same
  discipline.

## HOT.md session-start injection — a convention, not a hook installer

The reference package ships a hook installer that injects `HOT.md` at session
start. To stay CLI-free and match the official pattern (cf.
`fulcra-situational-awareness`), the skill documents injection as a **session-start
convention**: at session start, download and prepend `vault/HOT.md`. Hosts that
want automation wire it themselves; the skill only states the convention.

## Change detection: adopt `data-updates`

For sync / "what changed in the vault since last visit," adopt
`fulcra data-updates <range>` (incremental record+file change feed) in place of
list+stat polling. **Caveat:** it is an unpublished endpoint — pin its behavior in
tests before relying on it. (Same adoption applies to the reference CLI's planned
sync path.)

## What stays package-side

The `fulcra-vault` CLI (`init` / `write-section` / `append-log` / `backlinks` /
`reindex` / `map` / `rename` / `delete` / `install-hooks`) remains the reference
implementation in `fulcra-tools` — it is the ergonomic power-user surface and the
place the locking/rename logic is tested. The official skill is the portable,
dependency-free prose expression of the same conventions.

## Open questions for upstream maintainers

1. Is a vault skill in scope for `agent-skills`, or preferred as a standalone doc
   that only *references* the optimistic-concurrency convention?
2. Should the **optimistic-concurrency-over-LWW** recipe be its own small,
   general skill (usable by any Files writer) that the vault skill references,
   rather than living inside the vault skill? (We lean toward extracting it — it is
   the most reusable piece.)
3. Naming: `fulcra-vault` vs a more generic `fulcra-notes`.
