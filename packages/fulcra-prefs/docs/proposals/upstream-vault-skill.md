# Proposal: a new official `fulcra-vault` skill (locked notes + hot-context injection)

**Status:** draft (reeval epic phase-4, item 2) · **Author:** fulcra-prefs-maintainer
**Target venue:** `fulcradynamics/community-skills` (operator ruling 2026-07-16:
`agent-skills` is core-only; community proposals go to community-skills. Filed by
coord-maintainer, who has fulcradynamics access — this session cannot reach that repo.)
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

## Writing safely to LWW files: best-effort collision detection (NOT compare-and-set)

This matters to the **whole fleet**, not just vault — but state its limits
honestly. Fulcra Files are **last-write-wins with no conditional writes** (verified
live 2026-07-04): there is no ETag/`If-Match`/CAS and no atomic lock service. That
means **there is no client-only way to guarantee no lost updates.** Any recipe
below is *best-effort collision detection plus cooperative serialization*, not a
compare-and-set — it narrows the race window, it does not close it.

The convention:

1. **Read** the target note and remember its current version identity (the file's
   resolved version id from `resolve_filepath`, or a content hash).
2. Do the edit locally (owned-section replace / log append).
3. **Re-check** the version identity immediately before writing; if it changed
   since step 1, **abort and retry** (re-read, re-apply) rather than blindly
   overwrite. This catches the *common* case (a write that fully completed before
   yours starts).
4. Back it with a short-lived **advisory lock** (`vault/.locks/<note>.lock`,
   holder-id + timestamp, self-reaped after ~120 s) so cooperating writers usually
   serialize and fail fast with a retry hint instead of racing.

**The residual race is real and must be documented, not hidden.** Because the final
upload is unconditional, writer B can upload *after* A's step-3 re-check but
*before* A's step-4 upload, and A then silently overwrites B. The advisory lock does
not remove this: lock acquisition is itself a read followed by an unconditional
LWW upload, so two contenders can both observe "no lock" and both proceed. Locks are
advisory (humans and mirrors ignore them) and reduce collisions; they do not make
the write atomic.

**If a caller needs a genuine no-lost-update guarantee, it requires an actually
atomic primitive that the platform does not yet expose** — server-side
CAS/conditional write, an atomic lock service, or a single-writer coordinator. Until
one ships, this convention is the honest best-effort floor: safe for the low-contention,
mostly-owned-sections case vault targets; not a substitute for transactions. This is
the generally-useful export precisely *because* it names its own limits — document it
once so any Files writer adopts the pattern **and** its caveats.

## Proposed skill contents (SKILL.md, prose over `fulcra-api file`)

- **Layout:** notes under `vault/`, Obsidian-style `[[wikilinks]]`, flat
  Dataview-friendly frontmatter, append-only `## Log` sections, and per-agent
  **owned sections** (`<!-- owned:<agent> -->` … ) so an agent edit never disturbs
  a human's or another agent's block.
- **Read:** `fulcra-api file download` the note (or `MAP.md`) and parse.
- **Write:** the best-effort collision-detection recipe above, expressed as steps over
  `fulcra-api file` (resolve → download → edit → re-resolve/verify → upload, abort
  on version mismatch).
- **Index:** `MAP.md` (all notes + links) and `HOT.md` (the hot set) are
  deterministically rendered from the notes; a reindex re-derives them.
- **Rename/delete:** move the note and rewrite inbound wikilinks, taking the
  best-effort advisory lock on every touched note and aborting if any changed since
  it was read. This is the multi-file application of the same *best-effort*
  discipline — it is **not** a transaction: with no cross-file atomic write, a
  concurrent writer can still interleave, so a rename can land partially (some
  inbound links rewritten, some not) and must be safe to re-run to converge. Design
  the operation to be idempotent/re-runnable rather than assuming all-or-nothing.

## HOT.md session-start injection — a convention, not a hook installer

The reference package ships a hook installer that injects `HOT.md` at session
start. To stay CLI-free and match the official pattern (cf.
`fulcra-situational-awareness`), the skill documents injection as a **session-start
convention**: at session start, download and prepend `vault/HOT.md`. Hosts that
want automation wire it themselves; the skill only states the convention.

## Change detection: `data-updates` as an optional optimization, not a dependency

For sync / "what changed in the vault since last visit," `fulcra data-updates
<range>` (incremental record+file change feed) is attractive versus list+stat
polling. **But it is an unpublished endpoint**, so the *official, portable* skill
must not depend on it: an unpublished API cannot be part of the contract, and this
repo's tests cannot make it one.

The convention therefore is: **list+stat polling is the supported baseline**;
`data-updates` is an **optional fast path gated on documented availability** — a
skill/host may use it when the endpoint is published (or known-available in its
environment) and MUST fall back to polling otherwise. The reference CLI may adopt
the fast path behind that same gate and pin its behavior in the package's own
tests; the upstream skill text ships polling as the contract and mentions
`data-updates` only as an optimization to enable once published.

## What stays package-side

The `fulcra-vault` CLI (`init` / `write-section` / `append-log` / `backlinks` /
`reindex` / `map` / `rename` / `delete` / `install-hooks`) remains the reference
implementation in `fulcra-tools` — it is the ergonomic power-user surface and the
place the locking/rename logic is tested. The official skill is the portable,
dependency-free prose expression of the same conventions.

## Open questions for upstream maintainers

1. Venue settled: `community-skills` (per the operator's core-only ruling for
   `agent-skills`). Open: full skill vs. a standalone doc that only *references* the
   best-effort collision-detection convention?
2. Should the **best-effort-write-over-LWW** recipe (with its documented residual race) be its own small,
   general skill (usable by any Files writer) that the vault skill references,
   rather than living inside the vault skill? (We lean toward extracting it — it is
   the most reusable piece.)
3. Naming: `fulcra-vault` vs a more generic `fulcra-notes`.
