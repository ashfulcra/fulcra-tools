# fulcra-tools

WARNING: Everything in this repo was vibe coded by Fulcra's lawyer, 
using Fulcra's file and data stream primitives. 

Nothing in this repo is official Fulcra software, and it may or may
not be supported in the future. 

This repo contains various Fulcra helper projects and tools built on top of Fulcra. 
Each project keeps its own overview, build,
and tests; this README is the index that points you to them.

> **Working in this repo with an AI agent (Claude, Codex, Cursor, …)?**
> Read [`AGENTS.md`](AGENTS.md) first. It documents the non-obvious
> environmental requirements — the required `uv` extras, the launchd daemon,
> and the PATH/keychain gotchas — that otherwise cost time to rediscover on
> first run.

> **First-time setup, one command:** `bash scripts/setup.sh` — installs the
> right Python + `uv` extras, the `fulcra` CLI, and runs the test suite to
> verify. Then `uv run fulcra-collect daemon` (foreground) or install as a
> launchd agent per [`docs/TESTING.md`](docs/TESTING.md). Diagnose later
> with `uv run fulcra-collect doctor`.

## What's in here

| Project | What it is | Start here |
|---|---|---|
| **Fulcra Collect** | The main project — a local-ingest daemon + plugins that import your personal-data streams into [Fulcra](https://fulcradynamics.com). Spans the daemon ([`packages/collect`](packages/collect)), its web wizard, the macOS menu-bar companion, the shared API client, and the data-source plugins. | [`docs/collect.md`](docs/collect.md) |
| **Hermes Daytona demo** | Operator tooling for the Fulcra "press play" demo — spawn per-guest ephemeral [Hermes](https://hermes-agent.nousresearch.com) agents on [Daytona](https://www.daytona.io) that onboard each person into their own Fulcra account. | [`packages/hermes-daytona/README.md`](packages/hermes-daytona/README.md) |
| **fulcra-coord** | Shared agent-coordination layer — independent agents (Claude Code, Codex, OpenClaw, ChatGPT, CI) coordinate durable tasks over Fulcra Files as a bus, with no shared memory or direct calls. Lifecycle hooks, cross-agent inbox + broadcast directives, a `fulcra-coord agents` status digest, and a durable per-agent listener. Canonical home (migrated from `arc-claw-bot/fulcra-coord`). | [`packages/fulcra-coord/README.md`](packages/fulcra-coord/README.md) |

> **More coming.** Other Fulcra projects will be added here as the team
> consolidates them into this repo — each as its own row above, linking to
> its own overview.

## Repo notes

- **One git repo, no submodules.** Everything lives under [`packages/`](packages);
  each package keeps its own README, build, tests, and toolchain (Python and
  TypeScript both appear here).
- **History.** Several of Collect's pieces were their own repositories until
  2026-05-21, then merged here with `git subtree` so their full commit history
  is preserved (`git log packages/<name>` shows it). The original repos
  (`ashfulcra/fulcra-attention`, `ashfulcra/FulcraMediaHelpers`,
  `ashfulcra/fulcra-csv-importer`) are archived read-only. More on this in the
  Collect overview's [History](docs/collect.md#history) section.
