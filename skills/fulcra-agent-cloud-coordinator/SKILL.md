---
name: fulcra-agent-cloud-coordinator
description: "Configure a Claude Code cloud session as a persistent, container-reset-proof coordinator that loops on schedules and keeps ALL state on the Fulcra bus — the pattern a live fleet coordinator has run in production."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🛰️" } }
---

# Fulcra Cloud Coordinator

Turn one Claude Code **cloud** session (claude.ai/code) into a persistent
coordinator: an agent with a durable identity that wakes on schedules, reads
the bus, dispatches and reviews work, and **survives its own container being
destroyed** — because the session persists across containers and every scrap
of state lives in the Fulcra store, not on the machine.

This is the OpenClaw/Hermes experience (persistent identity, heartbeat,
durable context) rebuilt on managed cloud infrastructure you don't operate.
The reference deployment ran a full fleet day — 4 merged PRs, 2 live-incident
diagnoses, ~20 dispatches — through **seven container resets**, losing
nothing. The whole trick is one sentence:

> **The machine is disposable, the agent is permanent, and the context lives
> in the store.**

## Where to start — re-entrancy probes

Run in order; enter at the first probe that fails.

| Probe | Command / check | Passes when | If it fails, enter at |
|---|---|---|---|
| Cloud session exists? | You are reading this INSIDE a claude.ai/code cloud session | yes | §1 (create environment + session) |
| Store reachable? | `fulcra-api user-info` | prints your account | §2 (auth; remote-sandbox walls) |
| Engine present? | `coord-engine --help` | verb list prints | §2 (install) |
| Identity established? | `fulcra-api file download team/<team>/_coord/agents/<you>/census.md -` | your census prints | §3 |
| On the bus? | `coord-engine queue <team> --agent <you>` | rc 0 (or events), no VERSION WARNING/INCOMPATIBLE result | §3 |
| Recovery self-heals? | `ls scripts/<you>/bootstrap.sh` in the repo checkout | exists | §4 |
| Wakes armed? | your standing schedules exist (see §5's inventory check) | every duty has a wake | §5 |
| Duties silent-when-healthy? | last several duty runs produced no operator noise | quiet | §6 |

All pass → you are the pattern; keep the doctrine (§7) and maintain your
harness doc (`docs/coord/agents/<you>.md`).

## 1. The environment (one-time, human)

In claude.ai/code, create an **environment** for the repo
(session menu → environments):

- **Network**: *Full*, or *Custom* allowing at minimum `fulcra.us.auth0.com`
  and `api.fulcradynamics.com` (the bus), plus anything your duties call.
- **Secrets via environment config, never via chat or files**: add env vars
  (e.g. `LINEAR_API_KEY`) in the environment's configuration. Duty scripts
  materialize their own 0600 env files from injected vars on each container
  (see §4) — a fresh container needs zero secret handling.
- **Setup script** (optional but recommended): restore duty tooling from the
  store stash before the agent wakes (the same commands as §4's self-heal).

Then start a session on a **working branch** (never the default branch) —
this session IS the agent; you will keep resuming it, not creating new ones.

## 2. Tools + auth (per the quickstart, with cloud walls)

Follow [`docs/coord/GET-ON-THE-BUS.md`](../../docs/coord/GET-ON-THE-BUS.md)
§§2–3 — install `fulcra-api` + `coord-engine` (tag-pinned) and authenticate.
The cloud-specific walls (permission classifier blocking installers, the
device-flow proxy bypass, egress) are documented there with verified
fallbacks; do not re-derive them. Verify end-to-end with
`coord-engine doctor <team>`.
Doctor also prints the Bus-v3 fleet version census. Do not activate a new
cursor schema while it reports mixed/unknown agents: a presence row proves the
version is actively running, while an adoption claim proves only installation.
The shared authority and physically isolated cursor-generation contract are in
[`docs/coord/BUS-V3.md`](../../docs/coord/BUS-V3.md).

## 3. Identity (durable, on the bus)

Pick a canonical agent name — one string, minted once (see AGENTS.md
identity rules). Then make the identity durable:

```bash
coord-engine presence beat <team> --agent <you> -s "born: cloud coordinator"
coord-engine queue <team> --agent <you>          # first read; 7-day lookback
# announce on the record plane (stdin pipe — flag-only fails in non-TTY):
echo '{"note":"{\"v\":1,\"to\":\"all\",\"kind\":\"claim\",\"pri\":\"P3\",\"slug\":\"on-bus-v3-<you>\"}"}' | \
  fulcra-api record "<COORD_TYPE>" --api-version v1alpha1 --source=<you>
```

File two documents (both on the bus, both maintained forever):

- `team/<team>/_coord/agents/<you>/census.md` — wake sources, harness,
  read discipline, deputy arrangement. The fleet must never wonder how the
  coordinator wakes ("the busiest node was the least watched" is a real
  postmortem line — don't repeat it).
- `docs/coord/agents/<you>.md` in the repo — your harness self-description
  (self-service rule; coord-boss review).

If a wake router runs, register your directed-wake route (for a cloud
session: the `managed-agents-message` adapter with your session ref).

## 4. Container-reset survival (the load-bearing section)

A cloud container can be reclaimed at ANY moment — mid-turn, mid-task,
seven times a day. Design so a reset costs seconds, not state:

**4a. State placement rules.** On the bus: records cursor (the engine
already keeps it at `_coord/agents/<you>/records-cursor.json`), duty
scripts (stash), operator grants, standing-duty specs, continuity
checkpoints, reports. In the repo: anything reviewable. In the container:
NOTHING you are not willing to lose this second. The scratchpad is a cache.

**4b. The stash.** Every duty script the agent needs lives in the store:

```bash
fulcra-api file upload scripts/<you>/my-duty.sh \
  "team/<team>/_coord/agents/<you>/stash/my-duty.sh"
```

`scripts/<you>/bootstrap.sh` (committed to the repo) installs the stash into
the scratchpad. Secrets never enter the stash — scripts materialize env
files from environment-config vars at run time (§1).

**4c. The recovery ritual — inline in EVERY standing prompt:**

```bash
cd <repo> || exit 1        # fail-closed: NEVER run recovery from the wrong cwd
if [ ! -f scripts/<you>/bootstrap.sh ]; then
  test "$(git remote get-url origin)" = "<expected-origin>" || exit 1
  git fetch origin <branch> && git reset --hard origin/<branch> && \
    bash scripts/<you>/bootstrap.sh
fi
```

The `cd` is an explicit prerequisite, not the head of a `&&/||` chain — a
one-liner like `cd X && probe || recover` runs the DESTRUCTIVE fallback in
whatever directory the wake started in when the `cd` fails (and a hard reset
in the wrong checkout is exactly the disaster this ritual exists to prevent).
Probe the file the recovery actually depends on (`-f .../bootstrap.sh`), and
pin the expected origin before any hard reset.

The prompt that wakes you must carry its own recovery, because the container
it lands in may be minutes old. Never assume the previous turn's filesystem.

**4d. What NOT to trust across resets:** session cron jobs (die with the
worker), background processes (never run any), local git state (reset it),
installed tools (reinstall or PYTHONPATH from checkout), MCP connections
(they flicker; always have a CLI path).

## 5. Wakes — schedules are not loops

The doctrine (BUS-V3): no resident processes; every duty gets a
harness-native scheduled wake; the queue read rides every wake.

Three wake layers, most durable first:

1. **Server-side Routines** (the claude-code-remote scheduler /
   cloud "scheduled prompts") — survive EVERYTHING including session
   worker restarts. Use for standing duties. One Routine per duty,
   firing the full standing prompt into this session.
2. **Self-chained one-shots** (`send_later`) — for work loops ("check the
   PR in 45m, re-arm"). Re-arm at the end of each firing. The scheduler
   MCP can flicker: on failure, fall back to layer 3 and re-arm durably
   when it returns.
3. **Session cron** — cheap, but session-scoped and dies with worker
   restarts. Fallback only; never the sole wake for anything that matters.

**Standing-prompt design rules** (each duty prompt must be):
- **Self-contained**: full instructions + recovery ritual inline — a
  compacted or fresh context must be able to execute it cold.
- **Silent-when-healthy**: say exactly when to report (nonzero rc, anomaly,
  blocked) and otherwise say nothing. Operator attention is the scarcest
  resource on the bus.
- **Fail-closed**: a degraded read is UNKNOWN, never clear; quiet is not
  clear; never advance state past an unverified window.
- **Only the operator retires it**: mark standing duties as such, or a
  well-meaning cleanup will kill your heartbeat.

Minimum viable duty set for a coordinator: an hourly **watchdog**
(self-heal check + `coord-engine queue` + presence beat), a periodic
**blocked-work sweep** (asks, reviews pending, `threads --for <every
principal with assignments>`, agents lacking wake sources), and whatever
operator-ordered duties accrue. Timers/deferrals are **future-dated
records** (`coord-engine remind`) — never a local scheduler entry: work
state lives on the bus (the session scheduler is layer-2 convenience, not
the record of what is owed).

## 6. Operating doctrine (what makes it a coordinator, not a cron job)

- **Queue first, every wake.** Then act on what surfaced, oldest first.
- **Durable-first dispatch**: obligation doc before delivery record; the
  record plane is best-effort wake hints, documents are the truth.
- **Batch the operator**: operator-gated items accumulate into ONE message
  with everything decision-ready; never N pings.
- **Record grants verbatim** in `_coord/agents/<you>/operator-grants.md`
  the moment they're given, with scope interpretation. Standing orders and
  "re-present until acknowledged" rules live there too.
- **Presence beat on every wake** with a one-line truthful status.
- **Supersede, don't abandon**: re-dispatched work gets
  `task supersede --by <new>`; blocked work names its `--unlock`.
- **Continuity**: park a checkpoint before context loss; on takeover,
  `continuity resume` — the session summary is your bridge across context
  loss; the store is your context.

## 7. Verification (prove the pattern before trusting it)

1. **The reset test**: note your state, ask the operator to restart the
   container (or wait — one will come), confirm the next wake self-heals
   and the cursor covered the gap. This is the acceptance test.
2. **The silence test**: a healthy day produces near-zero operator
   messages. If your duties chat when nothing is wrong, fix the prompts.
3. **The blindness test**: no gap in bus coverage longer than your longest
   wake interval — check `records-cursor.json` advances across a day.

## Relationship to the other skills

This skill is the ASSEMBLY of the others for one harness: identity/liveness
([presence](../fulcra-agent-presence/SKILL.md)), directives/reviews
([directives](../fulcra-agent-directives/SKILL.md),
[review](../fulcra-agent-review/SKILL.md)), context
([continuity](../fulcra-agent-continuity/SKILL.md)), durable tooling
([durable-state](../fulcra-agent-durable-state/SKILL.md)), scheduling
([automation](../fulcra-agent-automation/SKILL.md)), and the bus contract
([BUS-V3](../../docs/coord/BUS-V3.md), [quickstart](../../docs/coord/GET-ON-THE-BUS.md)).
Read those for depth; this one exists so the next cloud coordinator is an
afternoon of configuration, not a week of rediscovery.
