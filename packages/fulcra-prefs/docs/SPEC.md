# fulcra-prefs — validated design spec (2026-06-10)

Status: design approved by Ash in-session 2026-06-10 (storage approach "A"
locked); sent for adversarial review to Ashs-MBP-Work:Codex-Review-Workbook.
Supersedes the v0 sketch in `packages/fulcra-prefs/docs/DESIGN.md` where they
differ.

## What this is

A user-owned preference layer on Fulcra: **typed preference signals with
decay, a deterministic solver for group decisions, consent-gated execution** —
auto-captured and consumable across all of the user's agentic platforms
(Claude, Claude Code, Codex, ChatGPT, OpenClaw, Hermes), with per-platform
overrides and session-bootstrap injection. General-user package in
ashfulcra/fulcra-tools (`packages/fulcra-prefs`), paired with the
[fulcra-onboarding skill](https://github.com/fulcradynamics/agent-skills/blob/main/skills/fulcra-onboarding/SKILL.md)
as a new-user onboarding path. Platform-surface facts referenced throughout
are documented in [`FULCRA-PRIMITIVES.md`](../../../FULCRA-PRIMITIVES.md)
(verified live 2026-06-10).

## Architecture: event-sourced, two layers (approach "A", locked)

- **Signal stream = annotation records.** Every captured preference / fact /
  consent event is one record ingested to a "Preference Signals" annotation
  definition via `POST /ingest/v1/record` (the platform's record-write path
  today; switches to CLI annotation commands when they land — record-level
  delete/replace then becomes native revocation).
- **Compiled projections = versioned files** under `prefs/` in the Fulcra
  file library. Session bootstrap reads ONE file; nothing replays the stream
  at session start.
- **All computation is code** in the `fulcra_prefs` package (helper tool) —
  never re-derived by an LLM in-session. This is what makes "deterministic"
  a real guarantee rather than a vibe.

Rationale highlights: capture is a single atomic POST (no read-modify-write
races across concurrently-capturing platforms — decisive for tier-2 agents);
signals appear on the user's Fulcra timeline next to their other data;
bootstrap is one GET; revocation arrives free with platform record CRUD.

## Capability tiers (distribution = the skill)

The skill (SKILL.md + references, agent-skills conventions) is the installable
unit for every agent; it routes by capability:

- **Tier 1 — CLI-capable** (Claude Code, Codex, OpenClaw, Hermes w/ shell):
  `fulcra auth login` (device flow) + `uv tool install fulcra-prefs`. Full
  surface: capture/compile/get/solve/consent/inject.
- **Tier 2 — HTTP-capable, no shell** (e.g. GPT Actions): goes around the CLI
  direct to the API. Device flow = 3 plain HTTP calls (public client_id);
  capture = `POST /ingest/v1/record`; read = download `prefs/compiled.json`.
  Tier 2 never computes — compile/solve always run where code runs.
- **Tier 3 — MCP-only**: read-side only today; MCP tokens are MCP-scoped and
  do not work against the REST API. Write-tools gap filed with the platform
  team.

## Data model

**Annotation definition:** one moment-type definition "Preference Signals",
created at onboard, tagged `fulcra-prefs`; UUID cached in `prefs/meta.json`
(tier 2 reads it from there, no catalog discovery needed).

**Signal payload** (record `data`, canonical JSON, schema-versioned):

```json
{"v": 1,
 "kind": "preference | fact | consent",
 "key": "dining.cuisine.thai",
 "scope": "global | platform:<name>",
 "value": {},
 "strength": 0.8,
 "confidence": 0.9,
 "half_life_days": 90,
 "source": {"platform": "claude-code", "agent": "<bus id>", "session": "<id>"},
 "supersedes": "<signal-id> | null"}
```

- `strength` is signed: aversions are negative.
- `half_life_days: null` = durable fact — no decay; compile flags staleness
  by age instead.
- Signal id = Fulcra record id once persisted; before persistence, local
  outbox entries use a deterministic `metadata.source` id as their temporary
  id. `supersedes` may reference either form and is resolved after upload.
- Record envelope: `recorded_at` = observation time;
  `source` = deterministic id first, then
  `["com.fulcra-prefs.capture.<platform>"]`.

**Files:**

| Path | Contents |
|---|---|
| `prefs/meta.json` | definition UUID, schema version, last-compile watermark |
| `prefs/compiled.json` | global compiled doc |
| `prefs/platforms/<p>.json` | per-platform merged views |
| `prefs/consent.json` | grants |

History/audit = the file library's native versioning (`file stat`).

## Compile (pure function, full recompute in v1)

`compile(signals, now) -> docs`, with `now` an explicit argument.

1. Effective weight per signal: `strength * 2^(-(now - observed_at) / half_life)`.
2. Drop superseded signals (follow `supersedes` chains).
3. Group by `key` + `scope`; conflicts resolve to highest |effective weight|,
   ties to newer `observed_at`.
4. Emit per key: `{value, weight, confidence, observed_at, n_signals, sources}`.
5. Platform docs = global overlaid with `platform:<p>`-scoped entries
   (platform beats global).

Determinism contract: canonical JSON output (sorted keys, fixed float
precision, stable signal-id sort before conflict resolution) — identical
`(signals, now)` produce **byte-identical** files. Runs at every tier-1
session start; optional cron (documented, not installed by v1).

## Solver (deterministic group decisions)

`solve(options, participant_docs, policy) -> ranked options + trace`.

- Pure function; canonical input ordering; explicit tie-breaker
  (lexicographic option id); no LLM in the loop.
- v1 policies: `weighted-sum` (default) and `hard-veto` (any participant's
  effective weight for an option below threshold removes it).
- The trace explains every ranking step in human-readable terms (the "why").
- v1 takes participant docs as local inputs; cross-user sharing is post-v1
  and rides the consent layer.

## Consent = the Privacy Ledger

`prefs/consent.json` grants: `{key_glob, audience, level: read|solve,
granted_at, expires}`. Enforcement at the export boundary: `get --for
<audience>` filters the compiled doc through grants. **Every export is itself
logged as a `kind: consent` disclosure signal** — disclosures land on the
user's timeline. This is the Privacy Ledger surface from the multiplayer-demo
work, derived rather than built.

## Surfaces

- **CLI** (`fulcra-prefs`): `onboard`, `capture`, `compile`, `get [--for
  <audience>] [--platform <p>]`, `solve`, `consent grant|revoke|list`,
  `inject --platform <p>`.
- **Claude Code adapter (first):** SessionStart hook runs `fulcra-prefs
  inject --platform claude-code` → compiled block as session context.
- **Codex:** managed block in AGENTS.md. **ChatGPT (tier 2):** skill recipe —
  download compiled doc into custom instructions; capture via ingest POST.
- **Skill:** SKILL.md + references: tier routing, the tier-2 HTTP recipes,
  and capture heuristics (capture on explicit "remember…", corrections, and
  user-confirmed repeated patterns — not silent bulk inference).

## Errors & edges

- Ingest failure: tier 1 spools to a local outbox, retries next invocation;
  tier-2 recipe = retry once then tell the user.
- No `compiled.json` yet: injector exits silently (never breaks a session
  start).
- Concurrent compiles: safe — idempotent recompute; last-writer-wins on a
  deterministic artifact.
- Expired auth: re-run device flow; tokens never logged or stored in
  repo/skill artifacts.
- Clock skew: trust ingest-side `recorded_at`.

## Testing

TDD throughout (superpowers flow). Pure units — decay, supersedes,
precedence, conflict resolution, solver policies — against golden fixtures;
an explicit **determinism test** asserting byte-identical compile output for
fixed inputs; store layer against a fake FulcraAPI; one live-API smoke test
gated behind an env var; adversarial solver fixtures (ties, vetoes, empty
docs, single participant).

## v1 cut-line

**In:** everything above. **Out:** MCP write path (platform gap, filed),
cross-user doc sharing, ChatGPT auto-injection, incremental compile, cron
installer (documented only). Isolation: all work in `packages/fulcra-prefs/**`
on branch `claude-code/fulcra-prefs`; PR + adversarial review per the global
rule (reviewer: Ashs-MBP-Work:Codex-Review-Workbook).
