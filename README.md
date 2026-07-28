# Ash's Fulcra Tools

Vibe-coded by Fulcra's lawyer on Fulcra's own primitives — unofficial,
unsupported, and a genuinely useful thing to point your agents at.

**This repo is a working demo of Fulcra as agent infrastructure.** A dozen
agents across five vendors run on it daily: they hand each other work, review
each other's changes, survive container resets, and record what happened —
coordinating through one Fulcra account and nothing else. No broker, no
queue, no coordination server.

## The demo: point two agents at this repo

The acceptance test for everything here: point two agents at this repo and
get them coordinating.

1. **One-time, human:** `uv tool install fulcra-api && fulcra auth login`
   (browser sign-in; your account is created on first login).
2. **Both agents:** clone this repo and read [`AGENTS.md`](AGENTS.md) — it is
   written for them, not you.
3. **Join the bus:** [`docs/coord/GET-ON-THE-BUS.md`](docs/coord/GET-ON-THE-BUS.md)
   takes an agent from zero to a named member of a team, verified cold from a
   sandboxed container. There is no infrastructure to run.
4. **Coordinate:** events move as typed records on your Fulcra timeline
   ([bus v3](docs/coord/BUS-V3.md)) — agent A writes a directive record, agent
   B reads its queue at its next wake with one command (`coord-engine queue`),
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

## No Fulcra account? Start here

Your agent can get real value out of this repo before you authenticate
anything. Reading is free; live data is the only thing gated behind a Fulcra
token, and this file won't pretend otherwise.

**No account needed — read and understand:**

- [`FULCRA-PRIMITIVES.md`](FULCRA-PRIMITIVES.md) maps the whole platform surface
  (auth, files, annotations, queries, MCP) by agent capability tier — CLI, raw
  HTTP, or MCP-only.
- [`docs/how-do-i-get-my-data.md`](docs/how-do-i-get-my-data.md) is a lookup of
  every data source Fulcra can pull from today and the pathway for each.
- The [`skills/`](skills) directory — thirteen `fulcra-agent-*` skills (of 15
  total) — is the prose an agent reads to learn the coordination layer;
  [`docs/coord/BUS-V3.md`](docs/coord/BUS-V3.md) and
  [`docs/coord-DESIGN.md`](docs/coord-DESIGN.md) explain the bus without
  touching it. ([`docs/README.md`](docs/README.md) indexes which docs are
  written for a cold reader.)
- The coord engine is stdlib-only, so it installs with no Fulcra account (see
  [Getting started](#getting-started)); `coord-engine --help` then prints the
  full verb surface offline.
- The **wake router** is the optional latency ceiling: agents need no polling
  loops (the queue read rides any wake), but only an always-on process can
  notice a deadline passing or wake an agent that's asleep. `coord-engine
  router run` routes directed work to agents via content-safe adapters.
  Design and status: [`docs/coord/wake-router-SPEC.md`](docs/coord/wake-router-SPEC.md).

**Needs your Fulcra token — live data:** anything that reads or writes your
actual data — `fulcra` CLI queries, `coord-engine doctor`/`briefing`/…, the
read-only MCP server, and Collect's ingest. Auth is a browser sign-in that
creates your account on first login (`fulcra auth login`). There is no
sample-data or offline demo bundled here, so that sign-in is the honest line
between reading about Fulcra and running it on your own life.

## The packages

Ordered from the coordination layer your agents run on, down to the data it
works on top of. **coord** is the killer feature; **Collect** shows the promise
underneath it — your agents operating on your life data without ever logging in
as you:

| Project | What it is | Start here |
|---|---|---|
| **coord** | The agent-coordination layer: judgment stays in prose (skills), bookkeeping is deterministic stdlib-only code ([`packages/coord-engine`](packages/coord-engine)). Independent agents — Claude Code, Codex, OpenCode, OpenClaw, CI — coordinate durable work over one Fulcra account: events on typed records ([bus v3](docs/coord/BUS-V3.md)), documents on Fulcra Files, role-based identity with leases, and a review handshake whose obligation persists until the verdict file exists (no ack can clear it). The thirteen `fulcra-agent-*` skills (of 15 total) under [`skills/`](skills) are how an agent actually uses it. | [quickstart](docs/coord/GET-ON-THE-BUS.md) (from zero) · [bus v3](docs/coord/BUS-V3.md) · [`README.md`](packages/coord-engine/README.md) · [design](docs/coord-DESIGN.md) |
| **coord tracker bridge** *(alpha)* | Mirrors coord work into external trackers without making the tracker authoritative: normalized snapshots, full source-identity state, versioned policy, pure diff planning, `coord-engine --json` and strict read-only teams sources, plus a paginated/retrying Linear adapter with explicit `plan` / `apply-resources` / `sync` phases. | [`README.md`](packages/coord-tracker-bridge/README.md) |
| **ATC** | Air-traffic control for a fleet running on subscription caps — capability-matched model routing. A versioned capability map ships in the engine (current Claude/GPT/Gemini/Grok lineups + the local OSS tier); `coord-engine route <team> --needs code,long-context` ranks the cheapest capable model on the account with headroom, agents log usage and outcomes after each dispatch (`usage log`), and three bad outcomes demote a model for that kind of work. `coord-engine atc init` gets a solo operator from zero to routed dispatch in one command — no team concepts required; `atc report` and `atc dash` (localhost) show the tier mix and estimated frontier-cap days preserved. | [`SKILL.md`](skills/fulcra-agent-atc/SKILL.md) · [design](docs/coord/atc-DESIGN.md) |
| **Fulcra Collect** | A local daemon that imports your personal-data streams into Fulcra. The daemon ([`packages/collect`](packages/collect/README.md)) hosts every importer plugin, runs them on schedule in worker subprocesses, stores secrets in the OS keychain, and serves the onboarding wizard + dashboard at `127.0.0.1:9292` ([`packages/web-ui`](packages/web-ui/README.md)). [`packages/menubar`](packages/menubar/README.md) is its macOS menu-bar companion; [`packages/fulcra-common`](packages/fulcra-common/README.md) is the shared API client + ingest pipeline every importer builds against; and [`packages/dayone`](packages/dayone/README.md), [`packages/csv-importer`](packages/csv-importer/README.md), and [`packages/media-helpers`](packages/media-helpers/README.md) are data-source importers (Day One journals, arbitrary CSVs, and watched/listened/read history from ~13 services). | [`docs/collect.md`](docs/collect.md) |
| **Fulcra Attention** | A Chrome (MV3) extension that captures what you read while browsing — foreground-tab attention, with title and time-on-page — and posts it directly to the Fulcra API after a browser sign-in. No daemon involved: the Python half of the package is just the Collect pointer plugin that tells you to install the extension. Three privacy tiers (param-strip, categorize, ignore) are built in. | [`README.md`](packages/attention/README.md) |
| **Fulcra Continuity** | Turns a long-running agent task into a structured checkpoint (objective, decisions, artifacts, open questions, next actions) that another session or agent can resume from without guessing. A standalone library + CLI (`checkpoint` / `resume`) that pairs with coord without depending on it: `coord-engine continuity resume/snapshot/park` read and write the same shape, and the [continuity skill](skills/fulcra-agent-continuity/SKILL.md) carries the cross-harness lifecycle contract (resume on wake, snapshot on change, park before context loss) with installers for each harness. | [`README.md`](packages/fulcra-continuity/README.md) |
| **Durable agent state** | The pattern that lets agents survive their machines: ephemeral compute (rollback-prone cloud containers, sleepy desktops) plus a durable per-agent stash on the Fulcra File Store — local disk is a cache, the store is the truth. Restore on wake, push on change, and a fail-closed secrets rule: nothing credential-shaped ever enters a shared team path (secrets ride in environment config or the OS keychain instead). The `coord-engine stash` verb (push/pull/list) is the deterministic bookkeeping: a per-file sha256 manifest, loud checksum-drift detection on restore, and the fail-closed secrets guard enforced at push; plain `fulcra-api file` commands remain the no-engine fallback. | [`SKILL.md`](skills/fulcra-agent-durable-state/SKILL.md) |
| **Fulcra Prefs** *(alpha)* | A user-owned preference layer: typed preference signals with half-life decay, captured by any of your agents, deterministically compiled into per-platform preference docs, plus a group-decision solver and consent-gated export where every disclosure is logged (the Privacy Ledger). Ships an agent skill with raw-HTTP recipes for shell-less agents, and a session hook that boots Claude Code with your preferences loaded. | [`README.md`](packages/fulcra-prefs/README.md) · [`SKILL.md`](packages/fulcra-prefs/skill/SKILL.md) (agents) |
| **Fulcra Vault** *(alpha)* | A shared markdown knowledge vault in Fulcra Files — one durable place for humans and agents to keep prose memory: projects, people, decisions, corrections, and domain notes, linked with Obsidian-style `[[wikilinks]]`. Flat Dataview-friendly frontmatter, owned sections agents can edit safely, append-only logs, backlink indexes, and deterministic `MAP.md`/`HOT.md` rendering. | [`README.md`](packages/fulcra-vault/README.md) |
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
uv tool install "git+https://github.com/ashfulcra/fulcra-tools@coord-engine-v1.7.2#subdirectory=packages/coord-engine"
```

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
verdict file exists at `team/<team>/review/<slug>/verdicts/<role>.md` (the
filename stem is the `required` token — the role passed to `--reviewer` — not the
holder's name);
`coord-engine review status <team> <slug>` gates the merge (a GitHub-only
comment doesn't count, and neither does an ack). The artifact ref is opaque —
PR#, branch, commit, URL — so the handshake works with any forge or none.
Full rule: [`AGENTS.md`](AGENTS.md). `coord-engine` changes are gated by its
pytest suite — run it before pushing.
