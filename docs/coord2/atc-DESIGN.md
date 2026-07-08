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
2. **Real cap numbers:** accounts.json ships with placeholder windows for your Anthropic + OpenAI subscriptions; correct the caps when awake (throttle events will calibrate regardless).

## v2 — smart dispatch (2026-07-08)

v1 shipped a tier rubric (frontier/standard/cheap) the operator applied by hand and a cap ledger. v2 keeps the ledger and makes the *model choice itself* a fold: the operator declares needs, the engine ranks models, and outcomes feed back. The skill is rewritten **standalone-first** — it now onboards and routes on a single account with no other `fulcra-agent-*` skill installed; the coord-team story moves to a bottom upgrade section.

**Model map.** `packages/coord-engine/coord_engine/default_models.json` (`map_version 2026-07-08`) is a packaged, capability-tagged catalog: each model carries `tags` from a **frozen taxonomy** (`code`, `architecture`, `writing`, `long-context`, `vision`, `fast`, `tool-use`), a `cost_rank` (1 = scarcest cap-weight … 9 = locally free), `provider`, `harnesses[]`, and `context`. A top-level `"models"` object in `accounts.json` overlays it by the same merge the router uses (add/retag/reprice a model) — absent by default, so v1 accounts.json routes on defaults only. The tier rubric survives in prose as the **ambiguous-case fallback** (does this task need `architecture`, or is the plain tag enough?), plus the turn-count-beats-token-price caveat.

**Router.** `coord-engine route <team> --needs a,b [--json]` ranks the models covering *every* requested need, **cheapest-capable-first**, filtered to accounts with live headroom. Output is `N. <model> — (<account>) — <pct>% — <tags>`. Sort key (frozen): non-demoted before demoted, then `cost_rank` (cheap first), then headroom-% desc, then model id. A need outside the taxonomy exits 2 with the valid set. **Uncapped accounts** (no `windows`) are legal and route at 100% headroom — the sanctioned way to declare a local ollama or any pay-nothing lane (conservative gap: nothing to zero, so they can't be throttle-excluded).

**Outcomes → demotion.** `usage log` gains `--model`, `--task-class` (taxonomy-validated), and `--outcome {clean,rework,escalated}`, written only when supplied so v1 shards stay v1 and both folds tolerate their absence. The demotion fold (frozen policy): a (model, task_class) pair demotes when it has **≥3** outcome-bearing shards **and ≥3 of its trailing 5** are `rework`/`escalated` (strict insufficient-evidence rule — 2-of-2 bad never demotes; a recovered pair drops out on later clean shards). `route` marks demoted candidates `[demoted: <need>]` and sinks them below all healthy ones. The skill **mandates outcome logging at task completion** (outcome is only knowable once the work lands), and `headroom --json` changed shape to carry it: **`{"windows": [...], "demotions": [...]}`**, an object, not v1's bare array.

**Onboarding.** `coord-engine atc init [team] [--yes] [--account id=provider:plan …] [--harness …]` seeds `team/<team>/atc/accounts.json` in one command (team defaults `solo`): plausible per-provider starter windows (anthropic 5h/1000 + 168h/15000; openai 5h/600; else 5h/500), `harnesses[]` folded from the map's per-provider union, `--harness` override, unknown-provider warning. Idempotent (merge new accounts by id, preserve `tiers`/`models` siblings), `--account` implies `--yes`, refuses a zero-account run (exit 2), prints paste-ready next commands. The skill's Install section is three commands: `uv tool install fulcra-api`, the coord-engine git-install, `atc init`.

**Proof surfaces.** `coord-engine atc report <team> [--days N] [--json]` — trailing-window dispatch report (tier mix, by-model, throttle events, windows exhausted, calibration/demotion lines) with a required "all figures are estimates" disclaimer; empty ledger → `no dispatches in window`. `coord-engine dash <team> [--port N]` — localhost-only gauge dashboard (binds `127.0.0.1`, default 8787) for headroom + active demotions.

**Watch-items (map maintenance).** Tracked in `default_models.json` `_watch_items`, revisit as the market moves:
- **gpt-5.6** (Sol/Terra/Luna preview) — API+partner only as of 2026-07-08, ids unpublished, **not consumer-dispatchable**; add on ChatGPT GA.
- **claude-mythos-5** — invitation-only, no self-serve; **excluded**.
- **glm-5.2** — leads the open-weights index but is **hosted/frontier-priced**; add if a cheap endpoint appears.
- **kimi-k2.6** — ollama `:cloud` only, competes on hosted price not free; revisit.
