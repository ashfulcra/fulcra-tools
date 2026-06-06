# Ash's Fulcra Tools

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
| **Fulcra Attention** | A relayless browser extension (Chrome MV3, [`attention/chrome`](attention/chrome)) that captures what you read — foreground tab/idle attention — and posts it **directly to the Fulcra API** via an Auth0 device-flow sign-in, with **no daemon**. Broken out to its own top-level project; Collect only surfaces it as an "install the extension" pointer plugin ([`attention/fulcra_attention`](attention/fulcra_attention)). | [`attention/README.md`](attention/README.md) |
| **Hermes Daytona demo** | Operator tooling for the Fulcra "press play" demo — spawn per-guest ephemeral [Hermes](https://hermes-agent.nousresearch.com) agents on [Daytona](https://www.daytona.io) that onboard each person into their own Fulcra account. | [`packages/hermes-daytona/README.md`](packages/hermes-daytona/README.md) |
| **Fulcra Coord** | Shared agent-coordination layer — independent agents (Claude Code, Codex, OpenClaw, ChatGPT, CI) coordinate durable tasks over Fulcra Files as a bus, with no shared memory or direct calls. Lifecycle hooks, cross-agent inbox + broadcast directives, a `fulcra-coord agents` status digest, and a durable per-agent listener.|

> **More coming.** Other Fulcra projects will be added here as the team
> consolidates them into this repo — each as its own row above, linking to
> its own overview.

## For agents

This repo is worked on by multiple autonomous agents (Claude Code, Codex, OpenClaw, Cursor, CI). If you're one of them, before you touch anything:

- **Read [`AGENTS.md`](AGENTS.md) first.** It documents the non-obvious environment: the required `uv` extras, the launchd daemon, and the PATH/keychain gotchas.
- **Set up once:** `bash scripts/setup.sh`. Diagnose later with `uv run fulcra-collect doctor`.
- **Coordinate on the bus.** Agents coordinate durable work over Fulcra Files via [`fulcra-coord`](packages/fulcra-coord/README.md) — check your inbox (`fulcra-coord inbox`), announce presence, and post directives instead of working blind. **Gotcha:** the bus needs a *file-capable* Fulcra CLI. The public PyPI `fulcra-api` build lacks the `file` command group, so every bus write **fails silently**. Run `fulcra-coord doctor`; if it reports `File commands: FAIL`, install a file-capable build and set `FULCRA_CLI_COMMAND` — see [`packages/fulcra-coord/docs/fulcra-cli-branch.md`](packages/fulcra-coord/docs/fulcra-cli-branch.md).
- **Land changes via PR, not direct pushes to `main`.** The team rule is **PR + reviewer-review + author-merge**: open a PR, get a review from a *different* agent, then the author merges. Use `fulcra-coord request-review <pr>` to route to a live reviewer.
- **macOS CI is path-filtered and bills at 10× on this private repo** — only the menubar + macOS-specific `fulcra-coord` modules trigger it (see [`.github/workflows/macos.yml`](.github/workflows/macos.yml)). Everything else is gated by the local suite + review.

## Repo notes

- **One git repo, no submodules.** Most projects live under [`packages/`](packages);
  the **Fulcra Attention** project is broken out to the top-level
  [`attention/`](attention) directory, since the browser extension is
  self-contained and separately installable (it's relayless — it authenticates
  and ingests on its own, no daemon). Each project keeps its own README, build,
  tests, and toolchain (Python and TypeScript both appear here).
- **History.** Several of Collect's pieces were their own repositories until
  2026-05-21, then merged here with `git subtree` so their full commit history
  is preserved (`git log packages/<name>` shows it). The original repos
  (`ashfulcra/fulcra-attention`, `ashfulcra/FulcraMediaHelpers`,
  `ashfulcra/fulcra-csv-importer`) are archived read-only. More on this in the
  Collect overview's [History](docs/collect.md#history) section.
