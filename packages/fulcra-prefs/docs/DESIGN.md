# fulcra-prefs — design (v0 draft, 2026-06-10)

> Historical draft. The reviewed design lives in
> [`SPEC.md`](SPEC.md); use that document for implementation work.

User-owned preference layer on Fulcra: **typed preference signals with decay, a
deterministic solver for group decisions, consent-gated execution, callable over
MCP by any agent** — per-platform overrides, auto-capture, auto-reconcile,
bootstrap-injected at session start. General-user package; pairs with the
`fulcra-onboarding` skill (fulcradynamics/agent-skills) as a new-user onboarding
path. CLI (`fulcra auth login` device flow) is the onboarding/auth surface.

## What the platform gives us today (explored 2026-06-10)

- **API** (`api.fulcradynamics.com/openapi.json`, public):
  - Annotation **definitions** already have full CRUD: `POST/GET/PUT/DELETE
    /user/v1alpha1/annotation[/{id}]` + `cancel_deletion` (soft-delete +
    restore). Tags API for grouping. JSON-schema discovery endpoints.
  - **Records** are written via `POST /ingest/v1/record[/batch]` (what the
    Attention extension uses). Record-level delete/replace is the part landing
    soon; CLI annotation commands also landing soon — **we prefer the CLI and
    will adopt it as commands ship; direct HTTP ingest is the interim
    workaround.**
  - Existing `GET/POST /user/v1alpha1/preferences` is flat **UI state** for the
    portal (timezone, pinned metrics). Not our store; leave it alone.
- **CLI / library** (`fulcradynamics/fulcra-api-python` main @ `7e470a4`):
  - `fulcra auth login` = Auth0 **device flow** → `~/.config/fulcra/credentials.json`
    with auto-refresh. This is the new-user onboarding path.
  - `fulcra file list/stat/download/upload/delete` on main — and **files are
    versioned** (`stat` shows version history). `FulcraAPI` is importable
    (files, annotation defs, record reads, tags, user info).
  - No record-write methods in the library yet (matches "soon in CLI").
- **Skills** (`fulcradynamics/agent-skills` @ `f96eda4`): working 7-step
  `fulcra-onboarding` skill (uv + fulcra-api CLI, device-flow auth, creates
  annotations, records first data, dashboard demo, handoff). SKILL.md
  conventions: YAML frontmatter, `user-invocable: true`, `references/` per
  phase, openclaw emoji metadata. fulcra-prefs ships a companion skill in this
  format and slots into the onboarding handoff.

## Architecture: event-sourced, two layers

**Signals (events) → compiled preference document (projection).** Same shape
fulcra-coord converged on (per-task files + views; events + parity), and it
maps 1:1 onto Fulcra primitives:

1. **Signal stream = annotation records.** Every captured preference/fact/
   consent change is a typed, timestamped record ingested to a `Preference
   Signals` annotation definition (ingest now; CLI later; record
   delete/replace when it lands = revocation/supersede).
2. **Compiled store = Fulcra Files** (versioned, atomic, fast single fetch):
   - `prefs/compiled.json` — reconciled current state (global)
   - `prefs/platforms/<platform>.json` — per-platform overrides
   - `prefs/consent.json` — consent grants/scopes (the Privacy-Ledger surface)
   - Compile applies decay, dedup, supersedes, conflict resolution; bootstrap
     injection reads ONE file, never replays the stream.

## Signal schema (v0)

```json
{
  "id": "uuid",
  "kind": "preference | fact | consent",
  "key": "dining.cuisine.thai",          // namespaced
  "scope": "global | platform:<name>",   // per-platform overrides
  "value": {},                            // typed payload
  "strength": 0.8,                        // signed weight (aversion < 0)
  "confidence": 0.9,
  "half_life_days": 90,                   // null = no decay (durable fact)
  "observed_at": "iso8601",
  "source": {"platform": "claude-code", "agent": "...", "session": "..."},
  "supersedes": "uuid | null"
}
```

Decay at compile: `weight = strength * 2^(-(now - observed_at)/half_life)`.
Facts with `half_life=null` don't decay but carry a staleness flag.
Precedence: `platform:<x>` beats `global`; newer beats older at equal scope;
explicit `supersedes` always wins.

## Solver (deterministic group decisions)

Pure function: N participants' compiled+consented prefs → ranked options with
an explanation trace. Canonical input ordering, no randomness, no LLM in the
loop → same inputs, same answer, auditable "why". (This is the group-dinner /
scheduling-negotiation engine; pairs with the multiplayer demo work.)

## Consent gate

Consent grants are themselves signals (`kind: consent`) compiled into
`prefs/consent.json`. Any cross-party read (solver, MCP tool, another agent)
resolves through scopes; every disclosure is logged → the **Privacy Ledger**
from the multiplayer demo falls out of this for free.

## Distribution & access model (capability tiers)

**The skill is the installable unit** (SKILL.md + references per
fulcradynamics/agent-skills conventions) — installable by any agent. It routes
each agent down the deepest path its capabilities allow:

- **Tier 1 — CLI-capable** (Claude Code, Codex, OpenClaw, Hermes w/ shell):
  `fulcra auth login` (device flow) + `uv tool install fulcra-prefs` — the tiny
  helper tool holding ALL deterministic logic (capture/compile/get/solve/
  consent/inject). CLI preferred over MCP per project direction.
- **Tier 2 — HTTP-capable, no shell** (custom GPT Actions, agents with raw
  fetch): **go around the CLI, direct to the API.** The Auth0 device flow is
  plain HTTP — `POST fulcra.us.auth0.com/oauth/device/code` (public client_id
  `48p3VbMnr5kMuJAUe9gJ9vjmdWLdnqZt`, audience `https://api.fulcradynamics.com/`)
  → show user `verification_uri_complete` → poll `/oauth/token`. With that
  token the agent reads/writes files (`/input/v1/file_upload`), ingests signal
  records (`POST /ingest/v1/record`, `DataRecordV1` shape; batch = JSONL), and
  manages annotation definitions (`/user/v1alpha1/annotation`). The skill
  ships exact request recipes. Determinism caveat: tier-2 agents capture
  signals and read compiled state; they do NOT compile/solve in-session (no
  code execution) — compile runs wherever the helper tool lives (any tier-1
  session or cron).
- **Tier 3 — MCP-only** (claude.ai/ChatGPT connectors without HTTP): read via
  `mcp.fulcradynamics.com` tools (data reads today; tool list unpublished).
  MCP tokens are MCP-scoped (own authorization server) and do NOT work
  against the REST API — so tier 3 has no write path until Fulcra's MCP grows
  file/annotation tools. Gap filed with the platform team.

## Surfaces

- `fulcra_prefs` Python lib + `fulcra-prefs` CLI (tier 1): `capture`,
  `compile`, `get`, `solve`, `consent`, `inject --platform <p>`, `onboard`
- HTTP recipes in skill references (tier 2): device-flow auth, capture via
  ingest, read compiled via file download
- Bootstrap injection per platform: Claude Code SessionStart hook → injected
  context block (first adapter); Codex AGENTS.md gen; ChatGPT custom
  instructions block (tier 2) later.

## Build plan (vertical slice first)

1. `store.py` (files read/write via `FulcraAPI`), `schema.py`, `decay.py`
2. `capture` + `compile` + `get` CLI
3. Claude Code adapter: SessionStart hook → inject compiled prefs
4. MCP read tool
5. Solver + consent gate
6. Companion SKILL.md (agent-skills conventions) + onboarding handoff

Isolation: only `packages/fulcra-prefs/**` on branch `claude-code/fulcra-prefs`;
PR + adversarial review per global rule. No overlap with `packages/fulcra-coord`.
