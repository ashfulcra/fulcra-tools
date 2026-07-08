# ATC — model & cap routing for agent fleets

**Status:** approved direction (Ash, 2026-07-07 night); v1 in build.
**Name:** working name **ATC** (air-traffic control) — skill `fulcra-agent-atc`. Alternatives Ash floated: Dispatcher, Traffic Cop. "ATC" wins on metaphor fit (assign the right runway/tier, keep traffic inside capacity) and doesn't collide with directives' tell/dispatch vocabulary. One-word rename possible at any time before upstream pitch.

## Problem

Subscription power users (Claude Max, OpenAI/Codex plans, multi-harness fleets) burn scarce capped capacity on work that doesn't need it, and stall when one subscription hits its window cap while another sits idle. Enterprises will eventually want the same thing as governance ("routing layers" per Nikesh Arora's framing), but the in-market user today is the fleet operator dodging caps. Sequence: **A (task-level routing) → C (capability brokerage over the mesh) → B (org governance)**.

## Users

v1: agent-fleet operators on subscriptions — concretely, this fleet (Claude Code CLI + Cowork + Codex app + OpenClaw + Hermes sandboxes on one coord2 bus). Presentable to: ClawHub/OpenClaw community (the existing Fulcra-adjacent install base), then the agent-skills upstream audience once proven here.

## Architecture — three layers (topology 3: hybrid)

1. **Policy (prose, the skill):** `skills/fulcra-agent-atc/SKILL.md` — how any agent classifies a task into a tier and picks a target. Judgment stays prose per coord2 doctrine.
2. **Ledger (code, the engine):** stdlib-only cap ledger in `coord-engine` — usage shards written after spend, folded deterministically into per-account **headroom** (the presence-fold pattern). The genuinely novel piece: cross-subscription headroom as shared fleet state.
3. **Dispatch (native, per harness):** no new execution machinery in v1. The agent that has work is already awake ("edge routing"); it consults policy + headroom, then dispatches via its own harness's native mechanism. Cross-harness work posts to the target's inbox; the already-deployed listeners/automations/heartbeats wake it.

**Step-up to B (shipped as prose in the same skill):** a `dispatcher-<platform>` role any agent can claim — arm your platform's native tick (Codex automation / Cowork scheduled task / CC listener wake / OpenClaw heartbeat / Hermes loop), watch the shared queue, spawn model-pinned subagents locally. B emerges from A; no new engine surface required.

## Native dispatch + wake inventory (verified per the cross-harness continuity work)

| Harness | Pinned-model spawn | Wake surface for a resident dispatcher |
|---|---|---|
| Claude Code CLI | `Agent` tool `model:` param; `claude -p --model <m>` | launchd listener (consent-gated wake), ScheduleWakeup |
| Claude desktop / Cowork | same CC core (Agent tool, hooks) | scheduled-tasks/routines opening a duty-cycle session |
| Codex app | `codex exec -m <m>`; per-thread model | app automations (proven: coord2-watch) |
| OpenClaw | per-agent model config | HEARTBEAT.md managed block |
| Hermes (Daytona/Vercel sandboxes) | spawn env/config | AGENTS.md loop + provision adapter (fulcra-hermes-vercel #1) |

## v1 scope

### Engine additions (stdlib only)

- **Accounts doc** `team/<team>/atc/accounts.json` — operator-declared (subscriptions don't expose caps), strict JSON (the stdlib mini-frontmatter parser doesn't do nested structures): accounts `{id, provider, plan, harnesses[], windows[{hours, cap, metric}]}` and a `tiers` map (`frontier|standard|cheap` → model ids). Caps are estimates; corrections come from throttle events.
- **`coord-engine usage log <team> --account <id> --tier <tier> [--units N] [--throttled]`** — writes one usage shard (agent, ts, account, tier, units, throttled?). Units are coarse and self-reported in v1 (a dispatch = its rough token estimate or request count); honesty over false precision.
- **`coord-engine headroom <team> [--json]`** — pure fold: per account × window, `cap − Σ(shards in window)` → headroom + percent; a `--throttled` shard **zeroes that account's headroom for the remainder of its window** (conservative ground truth beats the estimate) and flags the account `calibrate`. Unknown accounts/tiers in shards are reported, never crash the fold (health-surface discipline).
- **`digest`/`briefing` hook-in:** one headroom line when any account is <15% (surfaces in the operator loop like everything else).

### Skill `fulcra-agent-atc` (prose)

- **Tier rubric** — the productized version of the fleet's proven subagent-model-policy: *frontier* = ambiguous porting, architecture, adversarial review of subtle code; *standard* = well-specified implementation, docs, integration; *cheap* = mechanical transcription, formatting, bulk sweeps. Includes the turn-count-beats-token-price caveat (cheapest tier taking 3× turns costs more).
- **Edge-routing procedure** (rules an agent follows before any dispatch): 1) classify tier; 2) `coord-engine headroom` → candidate accounts whose harness can run that tier; 3) pick lowest sufficient tier, then highest headroom-%; 4) same-harness → native spawn (table above); cross-harness → `tell` the target role's inbox with the tier pinned in the task; 5) `usage log` what you spent; 6) if every capable account is <10%: degrade one tier with a note, or `later` the task past the window boundary.
- **Step-up §B** — dispatcher role claim + per-platform tick wiring (table above), queue = tasks tagged `route:` in the team inbox.
- Installation (engine git-install), probes ("headroom prints?", "accounts doc parses?", "am I logging usage?"), verb-contract-clean commands.

### Tests

Fold: window math, multi-window accounts, throttle-zeroing + expiry, unknown account/tier resilience, empty ledger. CLI: usage-log shard shape, headroom text + `--json`. Probe/verb-contract additions. All in `packages/coord-engine/tests/`.

## Out of scope (v1)

- Automatic token capture from harness transcripts (later; shards accept better numbers whenever available).
- Resident dispatcher *implementation* beyond the prose step-up (it's just an agent following the skill).
- Enterprise policy/governance (B-layer pricing, data-routing rules) — sequenced last per A→C→B.
- Cost-in-dollars; v1 optimizes cap headroom, not spend.

## Open questions for Ash (non-blocking, defaulted)

1. **Name:** shipping as ATC/`fulcra-agent-atc` unless renamed.
2. **Real cap numbers:** accounts.md ships with placeholder windows for your Anthropic + OpenAI subscriptions; correct the caps when awake (throttle events will calibrate regardless).
