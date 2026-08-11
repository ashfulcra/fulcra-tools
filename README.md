# Ash's Fulcra Tools

Vibe-coded by Fulcra's lawyer on Fulcra's own primitives — unofficial,
unsupported, and a genuinely useful thing to point your agents at.

## Why you'd want this

[Fulcra](https://fulcradynamics.com) is the user-owned context backend for AI
agents: one place where every agent you use — any vendor, any harness — can
know you, know what's happening in your world, work with your other agents,
and get more helpful over time, because the context they build outlives every
session, container, and model switch. Context, not models, makes the agent.
Rent the reasoning; own the context.

This repo is what that looks like in practice. **A dozen agents across five
vendors run on it daily**: they hand each other work, review each other's
changes, survive container resets, and record what happened — coordinating
through one Fulcra account and nothing else. No broker, no queue, no
coordination server.

Two layers do the work, and they're designed as a pair:

- **coord** is how agents work *with each other*: a shared bus of typed
  events and versioned documents — tasks, roles, reviews, presence — so any
  agent can see what the fleet owes and is owed.
- **continuity** is how any one agent's work survives *itself*: park a
  session into a checkpoint — objective, decisions, open questions, next
  actions — and resume it later in a different session, on a different
  machine, on a different model or vendor entirely. Parking never closes out
  the agent's obligations on the bus; resuming re-reads them. So the
  checkpoint carries the agent's own thread, coord carries what it owes
  others, and nothing falls between the two.

Together they're why the fleet doesn't start over: an agent that stops
mid-task parks; whoever picks it up — the same agent tomorrow, a different
one on another platform, after a container reset — resumes from the
checkpoint and the bus, not from zero. Everything in this repo is
inspectable, and that loop is the thing to inspect first.

## The demo: point two agents at this repo

The acceptance test for everything here: point two agents at this repo and
get them coordinating. There is no human setup step — the agents run the
installs themselves. Your part is two things at most: **approve** an install
if your agent's harness doesn't have the permission level to run it
unprompted (or run the command for it if it has no shell access at all), and
**sign in** when auth opens the browser — that step is yours because the
account it creates is yours.

1. **Both agents:** clone this repo and read [`AGENTS.md`](AGENTS.md) — it is
   written for them, not you.
2. **Install & auth (agent-run):** each agent installs the client and starts
   sign-in itself — `uv tool install fulcra-api && fulcra auth login`. The
   login lands in your browser (headless agents hand you a URL instead);
   you sign in once, and your Fulcra account is created on that first
   login. Agents authenticated to the same account share a bus.
3. **Join the bus:** [`docs/coord/GET-ON-THE-BUS.md`](docs/coord/GET-ON-THE-BUS.md)
   takes an agent from zero to a named member of a team, verified cold from a
   sandboxed container. There is no infrastructure to run.
4. **Coordinate:** events move as typed records on your Fulcra timeline
   ([bus v3](docs/coord/BUS-V3.md)) — agent A writes a directive record, agent
   B reads its queue at its next wake with one command (`coord-engine queue <team> --agent <you>`),
   does the work, and answers with a response record. Documents (tasks, reports, review
   verdicts) ride the File Store, versioned. Durable tasks, roles, and the
   review handshake are [`coord-engine`](packages/coord-engine)'s job.

Two kinds of thing move, and that's the whole architecture: **events** (typed
records: who it's for, what kind, how urgent, where the document is if there
is one) and **documents** (files). Reading your queue is one bounded range
query, readable ~20 seconds after write. Cloud containers were reset seven
times in one day and the fleet resumed each time, because the state lives in
the account — and a plain chat session with a connector, no shell at all, has
joined and done work. Everything else is convention, and the conventions are
what this repo ships.

## No Fulcra account yet?

Most of this repo works before you authenticate anything: the docs are
public, the skills install into any agent, and the coord engine runs offline.
The line is simple: **everything in this repo is free to read and run;
everything in a Fulcra store needs the token of the person who owns it.**
There is no sample tier — the store *is* the product. Two audiences,
honestly separated:

**For you (the human):**

- [`docs/how-do-i-get-my-data.md`](docs/how-do-i-get-my-data.md) — worked
  examples of getting real sources flowing into Fulcra. Examples, not a
  catalog: you and your agents can put *anything* in via the primitives —
  records with schemas, versioned files, time series, event logs.
- [`docs/coord-DESIGN.md`](docs/coord-DESIGN.md) and
  [`docs/coord/BUS-V3.md`](docs/coord/BUS-V3.md) — why the coordination bus
  looks the way it does, readable without touching it.
  ([`docs/README.md`](docs/README.md) indexes which docs are written for a
  cold reader.)

**For your agent (point one here and let it read):**

- [`AGENTS.md`](AGENTS.md) — this repo's conventions, written for agents.
- [`FULCRA-PRIMITIVES.md`](FULCRA-PRIMITIVES.md) — the map of the platform
  surface (auth, files, records, queries, MCP) organized by what an agent
  can reach from where it runs: CLI, raw HTTP, or MCP-only. This is how an
  agent works out what it can do *before* it tries.
- The [`skills/`](skills) directory — the procedures agents follow, covering a
  lot more than the bus: **continuity** (checkpoint work in one session,
  resume it in another — different day, different model, different vendor),
  **durable state** (tooling that survives machine resets), presence and
  liveness, review handshakes, automation, and the coordination layer
  itself.
- The [`coord-engine`](packages/coord-engine) is stdlib-only and installs
  with no account (see [Getting started](#getting-started));
  `coord-engine --help` prints the full verb surface offline. (An optional
  always-on **wake router** was evaluated and retired: measured against a
  fleet on listener cadence alone, it never delivered a wake the cadence
  didn't already cover — [`docs/coord/wake-router-SPEC.md`](docs/coord/wake-router-SPEC.md)
  is retained as the historical spec, and true push wake-up is an upstream
  platform ask, not a component of this repo.)

**Needs your Fulcra token:** any touch of a store — every read and every
write — `fulcra` CLI queries, `coord-engine doctor`/`briefing`/…, the
read-only MCP server, and Collect's ingest. Auth is a browser sign-in that
creates your account on first login (`fulcra auth login`). There is no
sample-data or offline demo bundled here, so that sign-in is the honest line
between reading about Fulcra and running it on your own context.

## The packages

Ordered from the coordination layer your agents run on, down to the data it
works on top of. **coord** is the killer feature; **Collect** shows the promise
underneath it — give agents context, not credentials: your agents work from
the real-world data you stream in, without ever logging in as you:

| Project | What it is | Start here |
|---|---|---|
| **coord** | The agent-coordination layer: judgment stays in prose (skills), bookkeeping is deterministic stdlib-only code ([`packages/coord-engine`](packages/coord-engine)). Independent agents — Claude Code, Codex, OpenCode, OpenClaw, CI — coordinate durable work over one Fulcra account: events on typed records ([bus v3](docs/coord/BUS-V3.md)), documents on Fulcra Files, role-based identity with leases, and a review handshake whose obligation persists until the verdict file exists (no ack can clear it). The `fulcra-agent-*` skills under [`skills/`](skills) are how an agent actually uses it. | [quickstart](docs/coord/GET-ON-THE-BUS.md) (from zero) · [bus v3](docs/coord/BUS-V3.md) · [`README.md`](packages/coord-engine/README.md) · [design](docs/coord-DESIGN.md) |
| **coord tracker bridge** *(alpha)* | Mirrors coord work into external trackers without making the tracker authoritative: normalized snapshots, full source-identity state, versioned policy, pure diff planning, `coord-engine --json` and strict read-only teams sources, plus a paginated/retrying Linear adapter with explicit `plan` / `apply-resources` / `sync` phases. | [`README.md`](packages/coord-tracker-bridge/README.md) |
| **ATC** *(alpha)* | Air-traffic control for a fleet running on subscription caps — capability-matched model routing. A versioned capability map ships in the engine (current Claude/GPT/Gemini/Grok lineups + the local OSS tier); `coord-engine route <team> --needs code,long-context` ranks the cheapest capable model on the account with headroom, agents log usage and outcomes after each dispatch (`usage log`), and three bad outcomes demote a model for that kind of work. `coord-engine atc init` gets a solo operator from zero to routed dispatch in one command — no team concepts required; `atc report` and `atc dash` (localhost) show the tier mix and estimated frontier-cap days preserved. | [`SKILL.md`](skills/fulcra-agent-atc/SKILL.md) · [design](docs/coord/atc-DESIGN.md) |
| **Fulcra Collect** | A local daemon that imports your real-world data streams into Fulcra. The daemon ([`packages/collect`](packages/collect/README.md)) hosts every importer plugin, runs them on schedule in worker subprocesses, stores secrets in the OS keychain, and serves the onboarding wizard + dashboard at `127.0.0.1:9292` ([`packages/web-ui`](packages/web-ui/README.md)). [`packages/menubar`](packages/menubar/README.md) is its macOS menu-bar companion; [`packages/fulcra-common`](packages/fulcra-common/README.md) is the shared API client + ingest pipeline every importer builds against; and [`packages/dayone`](packages/dayone/README.md), [`packages/csv-importer`](packages/csv-importer/README.md), and [`packages/media-helpers`](packages/media-helpers/README.md) are data-source importers (Day One journals, arbitrary CSVs, and watched/listened/read history from ~13 services). | [`docs/collect.md`](docs/collect.md) |
| **Fulcra Attention** | A Chrome (MV3) extension that captures what you read while browsing — foreground-tab attention, with title and time-on-page — and posts it directly to the Fulcra API after a browser sign-in. No daemon involved: the Python half of the package is just the Collect pointer plugin that tells you to install the extension. Three privacy tiers (param-strip, categorize, ignore) are built in. | [`README.md`](packages/attention/README.md) |
| **Fulcra Continuity** | Turns a long-running agent task into a structured checkpoint (objective, decisions, artifacts, open questions, next actions) that another session or agent can resume from without guessing. A standalone library + CLI (`checkpoint` / `resume`) that pairs with coord without depending on it: `coord-engine continuity resume/snapshot/park` read and write the same shape, and the [continuity skill](skills/fulcra-agent-continuity/SKILL.md) carries the cross-harness lifecycle contract (resume on wake, snapshot on change, park before context loss) with installers for each harness. | [`README.md`](packages/fulcra-continuity/README.md) |
| **Durable agent state** | The pattern that lets agents survive their machines: ephemeral compute (rollback-prone cloud containers, sleepy desktops) plus a durable per-agent stash on the Fulcra File Store — local disk is a cache, the store is the truth. Restore on wake, push on change, and a fail-closed secrets rule: nothing credential-shaped ever enters a shared team path (secrets ride in environment config or the OS keychain instead). The `coord-engine stash` verb (push/pull/list) is the deterministic bookkeeping: a per-file sha256 manifest, loud checksum-drift detection on restore, and the fail-closed secrets guard enforced at push; plain `fulcra-api file` commands remain the no-engine fallback. | [`SKILL.md`](skills/fulcra-agent-durable-state/SKILL.md) |
| **Fulcra Prefs** *(alpha)* | A user-owned preference layer: typed preference signals with half-life decay, captured by any of your agents, deterministically compiled into per-platform preference docs, plus a group-decision solver and consent-gated export where every disclosure is logged (the Privacy Ledger). Ships an agent skill with raw-HTTP recipes for shell-less agents, and a session hook that boots Claude Code with your preferences loaded. | [`README.md`](packages/fulcra-prefs/README.md) · [`SKILL.md`](packages/fulcra-prefs/skill/SKILL.md) (agents) |
| **Fulcra Vault** *(alpha)* | A shared markdown knowledge vault in Fulcra Files — one shared place for humans and agents to keep durable context in prose: projects, people, decisions, corrections, and domain notes, linked with Obsidian-style `[[wikilinks]]`. Flat Dataview-friendly frontmatter, owned sections agents can edit safely, append-only logs, backlink indexes, and deterministic `MAP.md`/`HOT.md` rendering. | [`README.md`](packages/fulcra-vault/README.md) |
| **Fulcra FDE** | A forward-deployed engineer as a skill: bring a business plan, deck, or idea; it interviews you to surface goals and assumptions, maps the product onto Fulcra primitives (with an honest gap register), builds a verification prototype — including a deployment rehearsal — and only then the real thing. Engagement state lives in your own Fulcra file store; judgment is prose ([`skills/fulcra-fde`](skills/fulcra-fde/SKILL.md)), bookkeeping is a stdlib-only engine ([`packages/fde-engine`](packages/fde-engine)). | [`SKILL.md`](skills/fulcra-fde/SKILL.md) · [`README.md`](packages/fde-engine/README.md) |

The first-generation `fulcra-coord` and `fulcra-coord-files` packages were
retired after their last live annotations surface moved to
[`fulcra-common`](packages/fulcra-common/README.md). Their implementation and
provenance remain available in git history; all new coordination work uses
coord.

## Getting started

Everything here sits on a Fulcra account and the `fulcra` CLI, which covers
auth, data queries, custom data types, tags, and files:

```bash
uv tool install fulcra-api   # installs the `fulcra` CLI
fulcra auth login            # browser sign-in; an account is created on first login
```

`fulcra user-info` confirms you're in, `fulcra catalog` shows what's
queryable, and `fulcra --help` covers the rest. For a guided setup, give your
agent the
[fulcra-onboarding skill](https://github.com/fulcradynamics/agent-skills/blob/main/skills/fulcra-onboarding/SKILL.md).
Platform docs: [docs.fulcradynamics.com](https://docs.fulcradynamics.com).

For this repo, one command: `bash scripts/setup.sh` — installs the right
Python + `uv` extras and the `fulcra` CLI, then runs the test suite to verify
(macOS-first; the menubar's PyObjC deps are macOS-only). From there,
`uv run fulcra-collect daemon` runs Collect in the foreground, or install it
as a launchd agent per [`docs/TESTING.md`](docs/TESTING.md); diagnose with
`uv run fulcra-collect doctor`. The coord engine installs on its own:

```bash
uv tool install "git+https://github.com/ashfulcra/fulcra-tools@coord-engine-v1.11.0#subdirectory=packages/coord-engine"
```

(The release tag is the **cold-install** path — correct for a first install from
this repo. The **fleet's runtime authority** is the store BOOTSTRAP —
`team/fulcra/_coord/bus-v3/adopt-latest.sh` + `BOOTSTRAP.md`, whose current pin
scheme is `pp-<sha>` — not this doc. Once you are on the bus, adopt from there.)

and `coord-engine doctor` checks the bus setup end to end. The FDE engagement
engine installs the same way (it is not on PyPI yet — use the git source form
until it is):

```bash
uv tool install --from "git+https://github.com/ashfulcra/fulcra-tools#subdirectory=packages/fde-engine" fde-engine
```

and `fde-engine list` shows any engagements already in your store. Continuity
and Prefs install independently — see their READMEs.

## For agents

[`AGENTS.md`](AGENTS.md) is your entry point. It documents the non-obvious
environment — the required `uv` extras, the launchd daemon, the PATH/keychain
gotchas — plus the coordination and backlog conventions. Joining the coord bus
for the first time — especially from a **remote or sandboxed environment**
(Claude Code cloud, CI) — start with the
[get-on-the-bus quickstart](docs/coord/GET-ON-THE-BUS.md), then adopt the
[bus v3 read/send contract](docs/coord/BUS-V3.md): on any wake, read your
record queue first (one query), and let events point you at documents. Don't
build a polling loop for the bus; the read rides every wake you already have.
[`FULCRA-PRIMITIVES.md`](FULCRA-PRIMITIVES.md) maps the whole platform surface
(auth, files, annotations, queries, MCP) by agent capability tier — CLI, raw
HTTP, or MCP-only. If you only need to **read** Fulcra data, the official MCP
server is the fastest path (`uvx fulcra-context-mcp@latest`, or hosted at
mcp.fulcradynamics.com) — it is read-only; **Collect is the write/ingest
side**, and MCP tokens are not API tokens (see the primitives doc's MCP
section for both caveats). And when the task is building a *product* on
Fulcra — a business plan, a deck, an idea that needs the platform as its
backend — start from the [fulcra-fde skill](skills/fulcra-fde/SKILL.md): it
runs the whole engagement (interview → architecture → prototype → build) with
resumable state in the user's own file store, instead of improvising a
one-off build.

## Review conventions

Nothing lands without an independent review by a *different agent identity*
than the author. Changes go through a PR where a forge exists — never direct
pushes to `main` — and the review handshake rides the bus, not the forge:
`coord-engine review request <team> <slug> --of <artifact> --reviewer <role>`
creates a review doc that sits in the reviewer's `needs-me` until their
verdict file exists at `team/<team>/review/<slug>/verdicts/<head>--<role>.md`
(head-keyed PR rounds; legacy/non-code reviews keep the bare
`verdicts/<role>.md`) — the filename token is the `required` one, the role
passed to `--reviewer`, not the holder's name;
`coord-engine review status <team> <slug>` gates the merge (a GitHub-only
comment doesn't count, and neither does an ack). The artifact ref is opaque —
PR#, branch, commit, URL — so the handshake works with any forge or none.
Full rule: [`AGENTS.md`](AGENTS.md). `coord-engine` changes are gated by its
pytest suite — run it before pushing.
