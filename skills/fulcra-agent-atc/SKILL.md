---
name: fulcra-agent-atc
description: "Model & cap routing (ATC) for a subscription fleet: declare your accounts once, then a capability-ranked router (`coord-engine route --needs …`) picks the cheapest model that can do the job across every subscription, a cross-account cap ledger keeps traffic inside its windows, and outcome logging demotes a model that keeps failing a task class. Standalone on one account; scales to a coord team of resident dispatchers."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🛫" } }
---

# Fulcra Agent ATC

A subscription fleet — Claude Max, OpenAI/Codex plans, maybe a local ollama and a
hosted overflow lane, across one or several harnesses — has two failure modes that
waste money and stall work: it **burns frontier budget on mechanical work** a cheap
model would finish, and it **stalls on one capped account** — hitting a window cap on
one subscription while another sits idle. ATC (air-traffic control) is the routing
layer that fixes both, and it runs on **one account** the same way it runs on a fleet.

Three parts:

- **The map** — a packaged default model map (`map_version 2026-07-08`) tagging every
  model by capability (`code`, `architecture`, `writing`, `long-context`, `vision`,
  `fast`, `tool-use`) and a cost rank (1 = scarcest cap-weight, 9 = locally free). You
  declare accounts; the map says which models each can run and what they cost.
- **The router** — `coord-engine route <team> --needs …`: ranks the models that cover
  your declared needs **cheapest-capable-first**, filtered to the accounts that have
  headroom right now. `coord-engine headroom` is the shared cap ledger it reads.
- **The feedback loop** — `coord-engine usage log … --model --task-class --outcome`:
  every dispatch logs what it cost and how it turned out. Repeated bad outcomes on a
  (model, task_class) pair **demote** it — the router marks it and ranks it below
  everything that hasn't been failing.

Dispatch itself stays native to your harness — ATC decides *which model against which
account*; your harness runs it. Nothing here depends on the other `fulcra-agent-*`
skills; the last section shows the fleet upgrade path if you have a coord team.

## Install

Three commands to a routable ledger. The engine is stdlib-only and installs on its own:

```bash
uv tool install fulcra-api   # the `fulcra` CLI: auth + the Fulcra File Store (the bus)
uv tool install "git+https://github.com/ashfulcra/fulcra-tools@coord-engine-v1.6.12#subdirectory=packages/coord-engine"
fulcra auth login            # browser sign-in; an account is created on first login
```

Then seed your accounts. `atc init` writes `team/<team>/atc/accounts.json` (team
defaults to `solo`) and is idempotent — re-running merges new accounts in by id and
leaves the rest untouched:

```bash
coord-engine atc init                 # interactive: pick providers from the default map
# — or non-interactively (each --account implies --yes):
coord-engine atc init solo --account anthropic-max=anthropic:max --account openai-codex=openai:pro
```

`--account id=provider:plan` (the `:plan` is optional). init seeds plausible starter
windows per provider (anthropic 5h/1000 + 168h/15000, openai 5h/600, else 5h/500) and
fills each account's `harnesses[]` from the map's per-provider union; `--harness`
overrides that. A provider absent from the map is seeded with no harnesses and a
warning — pass `--harness` or edit the file to make it routable. It prints the exact
next commands to paste.

### The accounts file

`team/<team>/atc/accounts.json` is the one operator-declared file. Subscriptions don't
publish their caps, so you declare them; the numbers are **estimates** and don't have to
be exact — throttle events calibrate them (a real rate-limit hit zeroes that window
regardless of the declared number; see §Throttle events). It is strict JSON (parsed with
`json.loads`), so it takes **no comments**.

```json
{
  "accounts": [
    {"id": "anthropic-max", "provider": "anthropic", "plan": "max",
     "harnesses": ["claude-code", "cowork"],
     "windows": [{"hours": 5, "cap": 1000}, {"hours": 168, "cap": 15000}]},
    {"id": "openai-codex", "provider": "openai", "plan": "pro",
     "harnesses": ["codex"],
     "windows": [{"hours": 5, "cap": 600}]},
    {"id": "local-ollama", "provider": "oss", "harnesses": ["ollama"]}
  ]
}
```

Each account carries an `id`, the `harnesses[]` that can spend on it, and zero or more
rolling `windows` (`hours` + `cap`). **An account with no `windows` is uncapped** — it
routes at 100% headroom and can never be throttle-excluded. That is how you declare a
local ollama or any pay-nothing lane: omit the windows. An account entry with no
non-empty string `id` is dropped and reported, but the fold survives — one bad entry
never breaks the rest.

**Overriding the map (optional).** A top-level `"models"` object in the same file
overlays the packaged default map — add a model, retag one, or change a `cost_rank` — by
the same merge the router uses. Absent (the init default), routing runs on defaults only.

Edit `atc init`'s starter windows to your real caps when you know them; until then the
throttle path keeps them honest.

## Declaring the work's needs

Route by **what the task exercises**, not by a tier guess. Pick the capability tags from
the frozen taxonomy that the work actually demands, and pass them to `route`:

| Tag | The work needs… |
|---|---|
| `code` | writing/editing/reasoning over source |
| `architecture` | system design, cross-component reasoning, non-obvious refactors |
| `writing` | prose that carries judgment about what matters |
| `long-context` | ≥400k usable context in one shot |
| `vision` | reading images/screenshots/diagrams |
| `fast` | latency-sensitive, cheap, high-throughput turns |
| `tool-use` | reliable multi-step tool/function calling |

A tag outside this set is refused (`route` exits 2 and lists the valid set), so a typo
can't silently route to nothing. Most tasks are one or two tags — `--needs code`,
`--needs code,long-context`, `--needs writing,vision`. The router does the cost/headroom
math; your only judgment call is naming the needs honestly.

**When the need is ambiguous** — you can't tell whether a task is "just" mechanical or
genuinely hard — fall back to the tier rubric and let it tell you whether to add
`architecture` (which pulls in the pricier, more-capable models) or stay on the plain
capability tag:

- **frontier judgment** (add `architecture`) — ambiguous porting where the target shape
  is unclear; architecture and system design; adversarial review of subtle code;
  cross-system debugging where the fault could be anywhere.
- **standard** (the plain tag, e.g. `code` / `writing`) — well-specified implementation
  against a clear interface; integration work with defined contracts; documentation that
  requires judgment about what matters.
- **cheap** (add `fast`) — transcription of a complete spec into code; formatting and
  mechanical reshaping; bulk sweeps (rename, lint-fix) across many files; a single-file
  fix with an obvious diff.

**Caveat:** turn count beats token price — a cheap model taking 3× the turns costs more
than the standard tier doing it once. If a tier is thrashing (re-asking, backtracking,
failing its own checks), step it up rather than paying for the retries.

## Routing — the procedure

Any agent runs this **before dispatching** work it can't or shouldn't do itself:

1. **Declare needs** — the capability tags from the taxonomy above.

2. **Rank the candidates:** `coord-engine route <team> --needs <tags>` — models covering
   every requested need, ranked cheapest-capable-first among the accounts with headroom
   right now. Each line is `N. <model> — (<account>) — <pct>% — <tags>`:

   ```
   route — solo — needs code (map 2026-07-08)
   1. claude-sonnet-5 — (anthropic-max) — 82% — code,architecture,writing,long-context,vision,fast,tool-use
   2. gpt-5.5 — (openai-codex) — 61% — code,architecture,long-context,vision,tool-use
   ```

   Take rank 1 unless you have a reason not to. `pct` is the account's headroom in its
   tightest relevant window; an uncapped account shows `100%`. `route` adds ` [demoted:
   <need>]` to any candidate that recent outcomes have soured for a requested need — a
   demoted candidate sorts **below every non-demoted one** regardless of cost, so it only
   surfaces when nothing healthy covers the need. Prefer a non-demoted candidate; take a
   demoted one only knowingly. `--json` emits the full structure (candidates, dropped
   tags, `reason` when empty).

3. **Dispatch natively.** Pin the chosen model on your own harness:

   | Harness | Pinned-model spawn | Wake surface for a resident dispatcher |
   |---|---|---|
   | Claude Code CLI | `Agent` tool `model:` param; `claude -p --model <m>` | launchd listener (consent-gated wake), ScheduleWakeup |
   | Claude desktop / Cowork | same CC core (Agent tool, hooks) | scheduled-tasks/routines opening a duty-cycle session |
   | Codex app | `codex exec -m <m>`; per-thread model | app automations (proven: coord-watch) |
   | OpenClaw | per-agent model config | HEARTBEAT.md managed block |
   | Hermes (Daytona/Vercel sandboxes) | spawn env/config | AGENTS.md loop + provision adapter |

   If the chosen model runs on a harness other than yours and you have a coord team, post
   the work to that platform's dispatcher inbox (see the last section). Standalone, you
   dispatch on your own harness or pick the best candidate you can run locally.

4. **Log the spend AND the outcome — at task completion.** When the dispatched work
   finishes, log it with the full attribution. The outcome is known only once the work
   is done, so this is a completion-time step, not a dispatch-time one:

   ```bash
   coord-engine usage log <team> --account <id> --tier <tier> --units <est> \
     --model <model-id> --task-class <tag> --outcome clean|rework|escalated
   ```

   - `--units` is coarse and self-reported (a rough token estimate or request count;
     honesty over false precision) — it feeds the headroom fold.
   - `--model` + `--task-class` + `--outcome` are what feed the demotion fold. `--model`
     is the id you actually ran; `--task-class` is the single taxonomy tag the work most
     exercised (validated — an unknown tag exits 2); `--outcome` is `clean` (landed as
     dispatched), `rework` (needed another pass), or `escalated` (had to bump to a bigger
     model). **Log all three or the outcome loop can't learn** — a shard missing any of
     them still counts toward headroom but is invisible to demotion.

   The demotion rule (frozen): a (model, task_class) pair demotes when it has **≥3**
   outcome-bearing shards **and ≥3 of its trailing 5** are `rework`/`escalated`. Below
   that it's insufficient evidence and the pair stays healthy; later clean shards pull it
   back out of demotion on their own.

5. **Throttle events override the estimate** — log immediately, see §Throttle events.

6. **Fallback — everyone's near a cap:** if `route` returns `no candidates` (or every
   candidate is near zero), either **degrade the needs** (drop `architecture`, run the
   next tier down, and note it), or defer the work past the window boundary so it lands
   when a cap has rolled over. Declaring an uncapped local lane (§accounts file) gives
   `route` something to fall to instead of nothing.

## Throttle events

The declared caps are estimates; a real rate-limit or cap error is ground truth and
overrides them. When a dispatch hits a throttle, log it **immediately** against that
account:

```bash
coord-engine usage log <team> --account <id> --tier <tier> --throttled
```

A throttled shard **zeroes that account's headroom for the rest of the window**
(`headroom` → 0, `pct` → 0.0) and flags the account `calibrate: true` — `headroom`
renders it `THROTTLED(calibrate caps)`, `route` drops it from candidacy, and `digest`
surfaces a `headroom LOW:` line. The flag expires when the throttled shard ages out of
the window. (An uncapped account has no window to zero, so it can't be throttle-excluded
— a known conservative gap.) When things are calm, correct the window `cap` in
`accounts.json` to match what you actually observed.

## Proof surfaces

Two read-only folds show ATC is working, from the same accounts.json + usage shards:

- **`coord-engine atc report <team> [--days N] [--json]`** — the trailing-window
  dispatch report: tier mix, by-model breakdown, throttle events, windows exhausted, and
  the calibration (demotion) lines. Every figure is labelled an estimate from
  self-reported units and operator-declared caps. `by model: (no model attribution)`
  means dispatches aren't logging `--model` — fix that at step 4. Empty ledger collapses
  to `no dispatches in window`, never a crash.

- **`coord-engine dash <team> [--port N]`** — a localhost gauge dashboard (binds
  `127.0.0.1` only, default port 8787) showing per-account headroom and the active
  demotions live. For eyeballing the fleet; the ledger is the source of truth.

- **`coord-engine headroom <team> [--json]`** — the raw ledger. `--json` returns
  `{"windows": [...], "demotions": [...]}` — an **object**, not the bare array v1 emitted
  (the top level had to gain the demotions sibling). Each window row carries `account`,
  `window_hours`, `cap`, `used`, `headroom`, `pct`, `throttled`, `calibrate`; each
  demotion carries `model`, `task_class`, `bad`, `of`.

## Coordinator joins: bindings, harvest, role-aware routing

On a coord team a COORDINATOR dispatches to ROLES, and outcomes are already
recorded on the bus (review verdict rounds) — two joins make ATC learn from that
without self-reporting:

- **`team/<team>/atc/bindings.json`** — the declared agent/role -> account join:
  `{"bindings": [{"agent": "codex-reviewer", "account": "openai-codex",
  "tier": "standard", "model": "gpt-x", "task_class": "code"}]}`. `agent`,
  `account`, `tier` are required; `model`/`task_class` give harvest full
  demotion-fold attribution. Malformed entries are dropped and reported; the
  fold survives.
- **`coord-engine atc harvest <team>`** — derives outcome shards from SETTLED
  review families: a single-round settled family is `clean`; any `-rN`
  re-request round means `rework`. Attribution comes from the review's
  `requested_by` joined through bindings; unbound authors are reported, never
  guessed. Idempotent by construction (one deterministic `harvest-<base>.md`
  shard per family; re-runs skip). Units are 0 — harvest feeds the demotion
  fold, never fakes headroom spend. Run it on a coordinator cadence.
- **`coord-engine route <team> --needs ... --for-role <role>`** — filters
  candidates to the role's bound account and prints the role's lease liveness
  (`HELD by ...` / `VACANT — dispatch will wait` / `UNKNOWN` on a degraded
  fold) so a coordinator never routes into a void silently. No binding for the
  role is a loud exit 2.

## Where to start — the re-entrancy probes

Probe what's already set up before doing anything. Enter at the **first probe that
fails** (per the repo's skill-quality pattern, `docs/skill-quality-pattern.md`):

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Accounts declared? | `coord-engine headroom <team>` | account rows print (not `no accounts declared`) | §Install |
| Does route rank? | `coord-engine route <team> --needs code` | a ranked candidate list prints (not `no candidates`) | §Install / §Declaring the work's needs |
| Ledger being fed? | `coord-engine headroom <team> --json` | `used` > 0 on accounts you've dispatched to | §Routing (step 4 — log every spend) |
| Outcomes being logged? | `coord-engine atc report <team>` | the `by model:` line shows real ids (not `(no model attribution)`) | §Routing (step 4 — log `--model`/`--task-class`/`--outcome`) |
| Near a cap? | `coord-engine digest <team>` | no `headroom LOW:` line | §Routing (step 6 — degrade or defer) |

## Running with a coord team

Everything above works on one account. On a `fulcra-agent-teams` space — several
harnesses on one shared bus — ATC's headroom becomes **shared fleet state**: every agent
reads the same `headroom`, so the cap ledger is honest across subscriptions and one
agent's spend is visible to the next. Two upgrades open up:

**Cross-harness dispatch.** When the best candidate runs on a harness other than yours,
post the work to that platform's dispatcher inbox with the chosen model/tier named in the
task (tag it `route:`); the target's deployed listeners/heartbeats (the right column of
the harness table) wake it. Same-harness work you still spawn locally.

**Step up to a resident dispatcher (§B).** When one platform's routed work is heavy
enough to want a dedicated router, an agent steps up — no new engine surface:

1. **Claim the role:** `coord-engine roles claim <team> dispatcher-<platform>` (e.g.
   `dispatcher-codex`) — a durable lease other agents can see.
2. **Arm your native tick:** wire the platform's own wake surface (the harness table's
   right column) — Codex app automation, Cowork scheduled task, CC launchd listener,
   OpenClaw HEARTBEAT.md, Hermes loop.
3. **Watch the queue:** each tick, `coord-engine inbox <team> --agent <id>` and pick up
   tasks tagged `route:`.
4. **Route and spawn:** `coord-engine route <team> --needs …`, spawn the model-pinned
   subagent locally per the ranked pick.
5. **Log every spend** with `coord-engine usage log … --model --task-class --outcome` so
   headroom and the demotion loop stay honest fleet-wide.

**Tick on the cheapest model.** The dispatcher is polling, not doing the work — it must
not eat the budget it exists to guard.

Routing is a decision made once, at dispatch time — which model, against which account.
Whatever session it spawns still runs its own lifecycle for the rest of its life; ATC
governs the runway assignment, not the flight.
