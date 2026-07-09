# Packaging decision — how coord's layers ship

**Question:** should coord's layers be Python **packages**, pure-prose **skills**, or a **hybrid**?

## Options

### A — Packages (pip monorepo)
Each layer is an installable package (`coord-reconcile`, `coord-tasks`, …) with a CLI/library.
- **+** deterministic, testable, versioned, strong separation, reusable as libraries.
- **−** heavyweight distribution: pip install + **version-sync across a fleet** — the exact pain that has
  dominated the coord maintainer workstream (stale hosts, version skew, the summaries-orphan leak needing
  a deploy to fix, wake-auth drift). Not agent-native. Doesn't compose with the agent-skills ecosystem.
  Not upstreamable to `fulcradynamics/agent-skills`.

### B — Pure-prose skills
Each layer is a `SKILL.md` of instructions over `fulcra-api file` + OKF, like `fulcra-agent-teams` itself.
- **+** agent-native (invocable, discoverable), zero-install, composes with teams + the whole skills
  ecosystem, upstreamable, portable across runtimes (Claude Code, OpenClaw, Codex).
- **−** **prose conventions drift.** Asking an agent to hand-maintain `index.md`, eyeball lease
  timestamps for an SLA fold, or keep the aggregate consistent is *the exact failure coord exists to
  eliminate* (teams' `index.md` upkeep → coord's reconcile; the 1142-orphan leak came from a *union* bug
  even with code — prose would be worse). No determinism, no tests, no self-healing.

### C — Skills that bundle one shared, tested engine  ← RECOMMENDED
Each capability is a **skill** (agent-facing interface + genuinely-conventional prose), and the
consistency-critical logic lives in **one shared stdlib-only engine** the skills invoke.
- The **skill** is distribution + discovery + the conventional parts (*how* to write a task doc, *when*
  to claim a role, the inbox lifecycle) — capturing B's adoption/composition/upstream wins.
- The **engine** is the deterministic parts (reconcile/heal, the role HELD/VACANT/CONTESTED fold, SLA
  vacancy timing, OKF parse/render, the aggregate) — capturing A's determinism/testability/self-healing.
- **One engine, many thin skills** — not N duplicated tools. The engine ships *with* the skills (pulled
  together, versioned together), so there's no separate fleet pip-sync.

**Decision rule:** *prose for what an agent does reliably by hand; code for what must be deterministic
(state folds, healing, timing, parsing).*

## Why C over B for the stateful layers
The failure coord is built around is **drift of derived/aggregate state**. Any layer that computes a
*fold over multiple files* (reconcile's index, roles' lease-status, review's verdict tally, retention's
archival window) must be deterministic or it silently rots. Those folds are exactly what a tool does well
and prose does badly. Conversely, *single-file, single-writer* actions (write a task doc, drop an inbox
message, refresh your own lease) are reliable as prose — so those stay in the SKILL.md.

## Concrete shape
```
coord2/
  engine/            # one stdlib-only package: OKF parse/render, transport, reconcile,
                     # role-fold, task-lifecycle, ... exposed as `coord <verb>` subcommands. Tested.
  skills/
    fulcra-agent-reconcile/   SKILL.md + references + (invokes engine `reconcile`/`board`/…)
    fulcra-agent-roles/       SKILL.md + references + (invokes engine `roles status`/`roles escalate`)
    fulcra-agent-tasks/       SKILL.md + references + (invokes engine `task start/update/done`)
    fulcra-agent-review/      SKILL.md (mostly convention; engine tallies verdicts)
    fulcra-agent-continuity/  SKILL.md + schema (engine writes/reads structured snapshots)
    fulcra-agent-automation/  SKILL.md + install scripts (heartbeat/wake)
  docs/…
```
(Open question for review: one `engine/` package wrapped by all skills, vs. each skill bundling the
engine under its own `scripts/`. Shared-engine is DRY but couples the skills' release; per-skill bundling
is independently installable but duplicates. Leaning shared-engine with each skill pinning an engine
version.)

## What this changes vs. the current tree
L1 `coord-reconcile` (already built + tested + live-verified) becomes the seed of `engine/`, wrapped by
the `fulcra-agent-reconcile` skill. The first-draft `fulcra-agent-roles` SKILL.md must gain a bundled
`roles status`/`roles escalate` engine command for the fold + SLA (it is currently too prose-only).

## Recommendation
**Approach C.** Skills for interface + adoption + upstream; one shared tested engine for every stateful
fold.

---

## Resolution (validated 2026-07-01, after review)

Two reviews (independent opus + Codex on the bus) + a probe of the real `fulcradynamics/agent-skills`
distribution model resolved the open questions:

- **Q2 (how the engine ships) — RESOLVED: a standalone published tool, `coord-engine`, invoked via
  `uv tool run coord-engine <verb>`.** The probe found the upstream skills repo has **zero
  pyproject/wheels/pip** — skills that ship code bundle *loose scripts* run by relative path, and they
  invoke shared tools (`fulcra-api`) as an **external command** (`uv tool run fulcra-api`). So neither a
  per-skill wheel dep (opus's first instinct) nor a bundled-under-one-skill package (the initial tree)
  matches upstream. The fit is the **`fulcra-api` pattern**: one standalone versioned tool, skills stay
  **pure prose + references** that call it — fully upstreamable (identical shape to `fulcra-agent-teams`).
  → engine extracted to top-level `engine/` as `coord-engine`; skills carry no code.
- **Q3 (prose↔tool line) — SHARPENED.** The discriminator isn't only "fold vs single-file" but **"does
  correctness require agreement between writers/readers on a derived representation?"** The role
  HELD/VACANT/CONTESTED + SLA fold is single-reader yet must be deterministic (two agents must agree a
  role is vacant before one escalates) → it is now a `coord-engine roles status` command, not prose.
  Single-file, single-writer actions (write a task doc, refresh a lease, drop an inbox message, write the
  escalation marker) stay prose. Engine *decides* escalation (`escalation_due`); the skill *acts*.
- **Q4 (engine-owned index in a hand-edited space) — GUARDRAILED.** `coord-engine` now writes an in-band
  `<!-- ENGINE-OWNED … -->` banner into generated `index.md`/`log.md`, so a hand-editing agent sees the
  boundary in the file, not only in a SKILL.md. Task docs remain hand-editable; only the derived index is
  engine-owned (interop with vanilla teams preserved for the data).
- **Q5 (can a skill run a tool in-runtime) — the real foundation, now answered by Q2's resolution.** By
  shipping the engine as an external `uv tool run` tool (not a bundled wheel), the skill's runtime
  contract is exactly `fulcra-agent-teams`' — it already assumes `uv tool run fulcra-api`. No new
  assumption.

**Build order (revised):** seed `engine/` from reconcile ✅ → add the roles fold as a `coord-engine`
command ✅ → thin prose skills (`fulcra-agent-reconcile`, `fulcra-agent-roles`) invoking `uv tool run
coord-engine` ✅ → tasks → review → continuity → automation, each PR reviewed on the bus (Codex) + an
independent pass.
