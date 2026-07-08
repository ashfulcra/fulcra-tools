---
name: fulcra-agent-atc
description: "Model & cap routing (ATC) for a fulcra-agent-teams space: a tier rubric for matching work to the cheapest sufficient model, a cross-subscription cap ledger with a deterministic headroom fold, and an edge-routing procedure any agent follows before dispatching — with an optional step-up to resident per-platform dispatchers."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🛫" } }
---

# Fulcra Agent ATC

Enhances [`fulcra-agent-teams`](https://github.com/fulcradynamics/agent-skills). A subscription fleet
(Claude Max, OpenAI/Codex plans, several harnesses on one coord2 bus) has two failure modes that waste
money and stall work: it **burns frontier budget on mechanical work** that a cheap model would finish,
and it **stalls on one capped account** — hitting a window cap on one subscription while another sits
idle. ATC (air-traffic control) is the routing layer that fixes both. It is three parts:

- **Policy** — this prose: a tier rubric and an edge-routing procedure any agent follows *before* it
  dispatches work.
- **Ledger** — `coord-engine usage log` / `coord-engine headroom`: usage shards written after spend,
  folded deterministically into cross-subscription headroom (the presence-fold pattern). The genuinely
  novel piece is that headroom is *shared fleet state* — every agent sees which account has room.
- **Dispatch** — native, per harness. No new execution machinery: the agent that has the work is
  already awake, so it consults policy + headroom and dispatches through its own harness. Cross-harness
  work posts to the target's inbox, where the already-deployed listeners/heartbeats wake it.

## Where to start — the re-entrancy probes

Probe what this team already has before doing anything. Enter at the **first probe that fails** (per the
repo's skill-quality pattern, `docs/skill-quality-pattern.md`):

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Accounts declared? | `coord-engine headroom <team>` | account rows print (not `no accounts declared`) | §Setup |
| Ledger being fed? | `coord-engine headroom <team> --json` | `used` > 0 on accounts you've dispatched to | §Edge routing (step 6 — log every spend) |
| Near a cap? | `coord-engine digest <team>` | no `headroom LOW:` line | §Edge routing (step 6 fallback — degrade or defer) |

## Setup

ATC needs one operator-declared file: `team/<team>/atc/accounts.json`. Subscriptions don't expose their
caps, so you declare them. Caps are **operator estimates** — they don't have to be exact, because
throttle events calibrate them (a real rate-limit hit zeroes that window regardless of the declared
number; see §Throttle events). The file is strict JSON (parsed with `json.loads`), so it takes **no
comments** — keep the caveat in your head, not in the file.

Shape (this is the canonical example the fold is tested against):

```json
{
  "accounts": [
    {"id": "anthropic-max", "provider": "anthropic", "plan": "max",
     "harnesses": ["claude-code", "cowork"],
     "windows": [{"hours": 5, "cap": 800}, {"hours": 168, "cap": 12000}]},
    {"id": "openai-codex", "provider": "openai", "plan": "pro",
     "harnesses": ["codex"],
     "windows": [{"hours": 5, "cap": 600}]}
  ],
  "tiers": {"frontier": ["fable-5"], "standard": ["opus-4.8", "sonnet-5"],
            "cheap": ["haiku-4.5"]}
}
```

Each account carries an `id`, the `harnesses[]` that can spend on it, and one or more rolling `windows`
(`hours` + `cap`). The `tiers` map names which model ids count as `frontier`, `standard`, and `cheap`.
Upload it with the File Store CLI:

```bash
uv tool run fulcra-api file upload ./accounts.json team/<team>/atc/accounts.json
```

Then `coord-engine headroom <team>` should print a row per account × window. An account entry with no
non-empty string `id` is dropped and reported, but the fold survives — one bad entry never breaks the
rest.

## The tier rubric

Classify the work, not the worker. Pick the **cheapest tier that can do the job right**:

- **frontier** — ambiguous porting where the target shape is unclear; architecture and system design;
  adversarial review of subtle code; cross-system debugging where the fault could be anywhere.
- **standard** — well-specified implementation against a clear interface; integration work with defined
  contracts; documentation that requires judgment about what matters.
- **cheap** — transcription of a complete spec into code; formatting and mechanical reshaping; bulk
  sweeps (rename, lint-fix) across many files; a single-file fix with an obvious diff.

**Caveat:** turn count beats token price — a cheap model taking 3× the turns costs more than the
standard tier doing it once. If a tier is thrashing (re-asking, backtracking, failing its own checks),
step it up rather than paying for the retries.

## Edge routing (the v1 procedure)

Any agent runs this **before dispatching** work it can't or shouldn't do itself:

1. **Classify** the work into a tier with the rubric above.
2. **Read shared headroom:** `coord-engine headroom <team> --json` — one row per account × window with
   `account`, `window_hours`, `cap`, `used`, `headroom`, `pct`, `throttled`, `calibrate`.
3. **Filter to capable accounts:** keep accounts whose `harnesses[]` include a harness that can run one
   of the tier's model ids (cross-reference the account's `harnesses` against the `tiers` map). An
   account that can't run the tier is not a candidate, however much headroom it has.
4. **Pick the target:** lowest sufficient tier first, then among the capable accounts the one with the
   highest `pct` (most headroom in its tightest relevant window).
5. **Dispatch natively:**
   - *Same harness as yours* → spawn a model-pinned subagent locally (see the table).
   - *Different harness* → `coord-engine tell <team> <role> "<title>"` to post the work to that
     platform's dispatcher inbox, with the tier named in the task (tag it `route:`); the deployed
     listeners/heartbeats wake the target.

   | Harness | Pinned-model spawn | Wake surface for a resident dispatcher |
   |---|---|---|
   | Claude Code CLI | `Agent` tool `model:` param; `claude -p --model <m>` | launchd listener (consent-gated wake), ScheduleWakeup |
   | Claude desktop / Cowork | same CC core (Agent tool, hooks) | scheduled-tasks/routines opening a duty-cycle session |
   | Codex app | `codex exec -m <m>`; per-thread model | app automations (proven: coord2-watch) |
   | OpenClaw | per-agent model config | HEARTBEAT.md managed block |
   | Hermes (Daytona/Vercel sandboxes) | spawn env/config | AGENTS.md loop + provision adapter |

6. **Log the spend:** `coord-engine usage log <team> --account <id> --tier <tier> --units <est>` — units
   are coarse and self-reported (a rough token estimate or request count; honesty over false precision).
   This is what feeds the headroom fold for the next agent.

   **Fallback — everyone's near a cap:** if every capable account is under ~10% headroom, either
   **degrade one tier** (run it on the next tier down and note that in the task), or
   `coord-engine later <team> "<title>"` to defer the work past the window boundary so it lands when a
   cap has rolled over.

## Throttle events

The declared caps are estimates; a real rate-limit or cap error is ground truth and overrides them. When
a dispatch hits a throttle, log it **immediately** with the same account:

```bash
coord-engine usage log <team> --account <id> --tier <tier> --throttled
```

A throttled shard **zeroes that account's headroom for the rest of the window** (`headroom` → 0,
`pct` → 0.0) and flags the account `calibrate: true` — `headroom` renders it as
`THROTTLED(calibrate caps)`, and `digest` surfaces a `headroom LOW:` line. The flag expires when the
throttled shard ages out of the window. When things are calm, correct the window `cap` in
`accounts.json` so the estimate matches what you actually observed.

## Step up to B: resident dispatchers

Edge routing needs no standing process — the agent with the work does the routing. When one platform's
work is heavy enough to want a dedicated router, an agent steps up to a **resident dispatcher** without
any new engine surface:

1. **Claim the role:** `coord-engine roles claim <team> dispatcher-<platform>` (e.g.
   `dispatcher-codex`) — a durable lease other agents can see.
2. **Arm your native tick:** wire the platform's own wake surface (the right column of the harness
   table) — Codex app automation, Cowork scheduled task, CC launchd listener, OpenClaw HEARTBEAT.md,
   Hermes loop.
3. **Watch the queue:** each tick, `coord-engine inbox --agent <id>` and pick up tasks tagged `route:`.
4. **Spawn model-pinned subagents locally** for each, per the tier named in the task.
5. **Log every spend** with `coord-engine usage log …` so headroom stays honest fleet-wide.

**Tick on the cheapest tier.** The dispatcher is polling, not doing the work — it must not eat the
budget it exists to guard.

## Relationship to the lifecycle contract

ATC governs **which model runs the work and against which account** — a decision made once, at dispatch
time. It does not replace continuity: every session ATC spawns is still bound by the
[fulcra-agent-continuity lifecycle contract](../fulcra-agent-continuity/SKILL.md#the-lifecycle-contract-applies-on-every-harness)
— resume on wake, snapshot on change, park before context loss. Route at dispatch; the contract runs for
the life of the session that routing created.
