# Ash's Fulcra Tools

WARNING: Everything in this repo was vibe coded by Fulcra's lawyer,
using Fulcra's file and datastream primitives.

Nothing in this repo is official Fulcra software, and it may or may
not be supported in the future.

This is a uv-workspace monorepo of helper projects built on
[Fulcra](https://fulcradynamics.com) — the personal data platform: your
health, location, calendar, media, attention, and any custom data streams you
define, in one store you own, with an API your agents can use. Everything
lives under [`packages/`](packages) (Python and TypeScript both appear here),
and each package keeps its own README, build, and tests. This file is the
front door; the package READMEs carry the detail.

## NOTE - Using Coord, Continuity and Prefs effectively turns any agent (including 
CLaude Code and Codex) into a looping multithreaded agent with persistent memory a
cross sessions, much like Openclaw or Hermes Agent. 

## The packages

**Fulcra Coord** ([`packages/fulcra-coord`](packages/fulcra-coord/README.md))
— the shared agent-coordination layer. Independent agents (Claude Code, Codex,
OpenClaw, ChatGPT, CI) coordinate durable tasks over Fulcra Files as a bus,
with no shared memory, direct calls, or central broker: lifecycle hooks, a
cross-agent inbox with directives and broadcasts, roles with leases, a
forge-agnostic review handshake (`request-review` / `review-done`), and a
durable per-agent listener that can wake an idle agent when work arrives.
Agents start at [`SKILL.md`](packages/fulcra-coord/SKILL.md). The bus
transport is its own small package,
[`packages/fulcra-coord-files`](packages/fulcra-coord-files/README.md) — a
documented no-CAS object-store contract over the Fulcra Files CLI.

**Fulcra Continuity**
([`packages/fulcra-continuity`](packages/fulcra-continuity/README.md)) — turns
a long-running agent task into a structured checkpoint (objective, decisions,
artifacts, open questions, next actions) that another session or agent can
resume from without guessing. A standalone library + CLI (`checkpoint` /
`resume`) that pairs with coord without depending on it: they share one
checkpoint shape, so coord's `snapshot`, `pause --snapshot`, `handoff`, and
`resume --with-continuity` write and read the same files.

**Fulcra Prefs** ([`packages/fulcra-prefs`](packages/fulcra-prefs/README.md),
*alpha*) — a user-owned preference layer: typed preference signals with
half-life decay, captured by any of your agents, deterministically compiled
into per-platform preference docs, plus a group-decision solver and
consent-gated export where every disclosure is logged (the Privacy Ledger).
Ships an agent skill ([`skill/SKILL.md`](packages/fulcra-prefs/skill/SKILL.md))
with raw-HTTP recipes for shell-less agents, and a session hook that boots
Claude Code with your preferences loaded.

**Fulcra Collect** — a local daemon that imports your personal-data streams
into Fulcra. The daemon ([`packages/collect`](packages/collect/README.md))
hosts every importer plugin, runs them on schedule in worker subprocesses,
stores secrets in the OS keychain, and serves the onboarding wizard +
dashboard at `127.0.0.1:9292`
([`packages/web-ui`](packages/web-ui/README.md)).
[`packages/menubar`](packages/menubar/README.md) is its macOS menu-bar
companion; [`packages/fulcra-common`](packages/fulcra-common/README.md) is the
shared API client + ingest pipeline every importer builds against; and
[`packages/dayone`](packages/dayone/README.md),
[`packages/csv-importer`](packages/csv-importer/README.md), and
[`packages/media-helpers`](packages/media-helpers/README.md) are data-source
importers (Day One journals, arbitrary CSVs, and watched/listened/read history
from ~13 services). Project overview: [`docs/collect.md`](docs/collect.md).

**Fulcra Attention** ([`packages/attention`](packages/attention/README.md)) —
a Chrome (MV3) extension that captures what you read while browsing —
foreground-tab attention, with title and time-on-page — and posts it directly
to the Fulcra API after a browser sign-in. No daemon involved: the Python half
of the package is just the Collect pointer plugin that tells you to install
the extension. Three privacy tiers (param-strip, categorize, ignore) are
built in.

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
`uv run fulcra-collect doctor`. Coord, Continuity, and Prefs install
independently — see their READMEs, and `fulcra-coord doctor` checks the bus
setup end to end.

## For agents

[`AGENTS.md`](AGENTS.md) is your entry point. It documents the non-obvious
environment — the required `uv` extras, the launchd daemon, the PATH/keychain
gotchas — plus the coordination and backlog conventions. Coordinate durable
work on the bus via [`fulcra-coord`](packages/fulcra-coord/SKILL.md): check
your inbox, announce presence, post directives instead of working blind.
[`FULCRA-PRIMITIVES.md`](FULCRA-PRIMITIVES.md) maps the whole platform surface
(auth, files, annotations, queries, MCP) by agent capability tier — CLI, raw
HTTP, or MCP-only.

## Review conventions

Nothing lands without an independent review by a *different agent identity*
than the author. Changes go through a PR where a forge exists — never direct
pushes to `main` — and the review handshake rides the bus, not the forge:
`fulcra-coord request-review <artifact>` routes it to a reviewer, and
`fulcra-coord review-done <artifact> --verdict approve|changes` lands the
verdict in the author's bus inbox (a GitHub-only comment doesn't count). The
artifact ref is opaque — PR#, branch, commit, URL — so the handshake works
with any forge or none. Full rule: [`AGENTS.md`](AGENTS.md). One per-clone
setup step before pushing coord changes: `git config core.hooksPath .githooks`
enables the shared pre-push hook that runs the fulcra-coord suite locally
(the macOS CI job is path-filtered and bills at 10×, so the local gate is the
real one).
