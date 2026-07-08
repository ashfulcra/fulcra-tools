# Ash's Fulcra Tools

WARNING: Everything in this repo was vibe coded by Fulcra's lawyer,
using Fulcra's file and datastream primitives.

Nothing in this repo is official Fulcra software, and it may or may
not be supported in the future.

This is a uv-workspace monorepo of helper projects built on
[Fulcra](https://fulcradynamics.com) — the personal data platform: your
health, location, calendar, media, attention, and any custom data streams you
define, in one store you own, with an API your agents can use. Code lives
under [`packages/`](packages) (Python and TypeScript both appear here), agent
skills live under [`skills/`](skills), and each package keeps its own README,
build, and tests. This file is the front door; the package READMEs carry the
detail.

> **Note:** Using the coord2 skills, Continuity and Prefs effectively turns
> any agent (including Claude Code and Codex) into a looping multithreaded
> agent with persistent memory across sessions, much like Openclaw or Hermes
> Agent.

## The packages

| Project | What it is | Start here |
|---|---|---|
| **coord2** | The agent-coordination layer, second generation: judgment stays in prose (skills), bookkeeping is deterministic code (a stdlib-only CLI, [`packages/coord-engine`](packages/coord-engine)). Independent agents — Claude Code, Codex, OpenClaw, Hermes, CI — coordinate durable tasks over Fulcra Files as a bus: a cross-agent inbox with directives and broadcasts, role-based identity with leases, a review handshake where the obligation persists until the verdict file exists (no ack can clear it), continuity checkpoints, and `briefing`/`needs-me` as the single entry fold for a waking agent. The twelve `fulcra-agent-*` skills under [`skills/`](skills) are how an agent actually uses it; per-platform watch/wake installers (Codex app automations, Claude Code hooks, OpenClaw heartbeat blocks) live in [`fulcra-agent-automation`](skills/fulcra-agent-automation/SKILL.md). Design and pitch docs: [`docs/coord2/`](docs/coord2). | [`README.md`](packages/coord-engine/README.md) · [`skills/`](skills) (agents) |
| **ATC** | Air-traffic control for a fleet running on subscription caps. You declare your accounts and rolling cap windows once (`team/<team>/atc/accounts.json`); every agent logs coarse usage after a dispatch (`coord-engine usage log`) and consults the shared headroom fold (`coord-engine headroom`) before the next one — so frontier-model caps go to work that needs frontier judgment, and a throttle event on one account routes traffic to the ones with room. A tier rubric and routing procedure in the skill; the ledger math in the engine. | [`SKILL.md`](skills/fulcra-agent-atc/SKILL.md) · [design](docs/coord2/atc-DESIGN.md) |
| **Fulcra FDE** | A forward-deployed engineer as a skill: bring a business plan, deck, or idea; it interviews you to surface goals and assumptions, maps the product onto Fulcra primitives (with an honest gap register), builds a verification prototype — including a deployment rehearsal — and only then the real thing. Engagement state lives in your own Fulcra file store; judgment is prose ([`skills/fulcra-fde`](skills/fulcra-fde/SKILL.md)), bookkeeping is a stdlib-only engine ([`packages/fde-engine`](packages/fde-engine)). | [`SKILL.md`](skills/fulcra-fde/SKILL.md) · [`README.md`](packages/fde-engine/README.md) |
| **Fulcra Continuity** | Turns a long-running agent task into a structured checkpoint (objective, decisions, artifacts, open questions, next actions) that another session or agent can resume from without guessing. A standalone library + CLI (`checkpoint` / `resume`) that pairs with coord2 without depending on it: `coord-engine continuity resume/snapshot/park` read and write the same shape, and the [continuity skill](skills/fulcra-agent-continuity/SKILL.md) carries the cross-harness lifecycle contract (resume on wake, snapshot on change, park before context loss) with installers for each harness. | [`README.md`](packages/fulcra-continuity/README.md) |
| **Fulcra Prefs** *(alpha)* | A user-owned preference layer: typed preference signals with half-life decay, captured by any of your agents, deterministically compiled into per-platform preference docs, plus a group-decision solver and consent-gated export where every disclosure is logged (the Privacy Ledger). Ships an agent skill with raw-HTTP recipes for shell-less agents, and a session hook that boots Claude Code with your preferences loaded. | [`README.md`](packages/fulcra-prefs/README.md) · [`SKILL.md`](packages/fulcra-prefs/skill/SKILL.md) (agents) |
| **Fulcra Vault** *(alpha)* | A shared markdown knowledge vault in Fulcra Files — one durable place for humans and agents to keep prose memory: projects, people, decisions, corrections, and domain notes, linked with Obsidian-style `[[wikilinks]]`. Flat Dataview-friendly frontmatter, owned sections agents can edit safely, append-only logs, backlink indexes, and deterministic `MAP.md`/`HOT.md` rendering. | [`README.md`](packages/fulcra-vault/README.md) |
| **Fulcra Collect** | A local daemon that imports your personal-data streams into Fulcra. The daemon ([`packages/collect`](packages/collect/README.md)) hosts every importer plugin, runs them on schedule in worker subprocesses, stores secrets in the OS keychain, and serves the onboarding wizard + dashboard at `127.0.0.1:9292` ([`packages/web-ui`](packages/web-ui/README.md)). [`packages/menubar`](packages/menubar/README.md) is its macOS menu-bar companion; [`packages/fulcra-common`](packages/fulcra-common/README.md) is the shared API client + ingest pipeline every importer builds against; and [`packages/dayone`](packages/dayone/README.md), [`packages/csv-importer`](packages/csv-importer/README.md), and [`packages/media-helpers`](packages/media-helpers/README.md) are data-source importers (Day One journals, arbitrary CSVs, and watched/listened/read history from ~13 services). | [`docs/collect.md`](docs/collect.md) |
| **Fulcra Attention** | A Chrome (MV3) extension that captures what you read while browsing — foreground-tab attention, with title and time-on-page — and posts it directly to the Fulcra API after a browser sign-in. No daemon involved: the Python half of the package is just the Collect pointer plugin that tells you to install the extension. Three privacy tiers (param-strip, categorize, ignore) are built in. | [`README.md`](packages/attention/README.md) |

**Legacy:** [`packages/fulcra-coord`](packages/fulcra-coord/README.md) and
[`packages/fulcra-coord-files`](packages/fulcra-coord-files/README.md) are the
first-generation coordination layer, superseded by coord2. They're kept for
the annotations helper and provenance; don't build anything new on them.

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
uv tool install "git+https://github.com/ashfulcra/fulcra-tools@coord-engine-v1.3.0#subdirectory=packages/coord-engine"
```

and `coord-engine doctor` checks the bus setup end to end. Continuity and
Prefs install independently — see their READMEs.

## For agents

[`AGENTS.md`](AGENTS.md) is your entry point. It documents the non-obvious
environment — the required `uv` extras, the launchd daemon, the PATH/keychain
gotchas — plus the coordination and backlog conventions. Coordinate durable
work on the bus via the [coord2 skills](skills): on wake, `coord-engine
briefing <team> --agent <you>` is the one command that surfaces your inbox,
your roles' inboxes, and every review you owe — start there, not with a
narrower check.
[`FULCRA-PRIMITIVES.md`](FULCRA-PRIMITIVES.md) maps the whole platform surface
(auth, files, annotations, queries, MCP) by agent capability tier — CLI, raw
HTTP, or MCP-only. If you only need to **read** Fulcra data, the official MCP
server is the fastest path (`uvx fulcra-context-mcp@latest`, or hosted at
mcp.fulcradynamics.com) — it is read-only; **Collect is the write/ingest
side**, and MCP tokens are not API tokens (see the primitives doc's MCP
section for both caveats).

## Review conventions

Nothing lands without an independent review by a *different agent identity*
than the author. Changes go through a PR where a forge exists — never direct
pushes to `main` — and the review handshake rides the bus, not the forge:
`coord-engine review request <team> <slug> --of <artifact> --reviewer <role>`
creates a review doc that sits in the reviewer's `needs-me` until their
verdict file exists at `team/<team>/review/<slug>/verdicts/<reviewer>.md`;
`coord-engine review status <team> <slug>` gates the merge (a GitHub-only
comment doesn't count, and neither does an ack). The artifact ref is opaque —
PR#, branch, commit, URL — so the handshake works with any forge or none.
Full rule: [`AGENTS.md`](AGENTS.md). One per-clone setup step:
`git config core.hooksPath .githooks` enables the shared pre-push hook that
runs the legacy fulcra-coord suite when that package changes (the macOS CI
job is path-filtered and bills at 10×, so the local gate is the real one);
`coord-engine` changes are gated by its own pytest suite — run it before
pushing.
