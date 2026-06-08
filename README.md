# Ash's Fulcra Tools

WARNING: Everything in this repo was vibe coded by Fulcra's lawyer, 
using Fulcra's file and data stream primitives. 

Nothing in this repo is official Fulcra software, and it may or may
not be supported in the future. 

This repo contains various helper projects and tools built on top of Fulcra. 
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
| **Fulcra Collect** | A local-ingest daemon + plugins that import your personal-data streams into [Fulcra](https://fulcradynamics.com). Spans the daemon ([`packages/collect`](packages/collect)), its web wizard, the macOS menu-bar companion, the shared API client, and the data-source plugins. | [`docs/collect.md`](docs/collect.md) |
| **Fulcra Attention** | A relayless browser extension (Chrome MV3, [`packages/attention/chrome`](packages/attention/chrome)) that captures what you read — foreground tab/idle attention — and posts it **directly to the Fulcra API** via an Auth0 device-flow sign-in, with **no daemon**. Broken out to its own package, [`packages/attention`](packages/attention); Collect only surfaces it as an "install the extension" pointer plugin ([`packages/attention/fulcra_attention`](packages/attention/fulcra_attention)). | [`packages/attention/README.md`](packages/attention/README.md) |
| **Fulcra Coord** | Shared agent-coordination layer — independent agents (Claude Code, Codex, OpenClaw, ChatGPT, CI) coordinate durable tasks over Fulcra Files as a bus, with no shared memory or direct calls. Lifecycle hooks, cross-agent inbox + broadcast directives, a `fulcra-coord agents` status digest, and a durable per-agent listener. Forge-agnostic: the review/merge handshake rides the bus (`request-review`/`review-done`), so GitHub is optional. | [`packages/fulcra-coord/README.md`](packages/fulcra-coord/README.md) · [`SKILL.md`](packages/fulcra-coord/SKILL.md) (agents) |
| **Fulcra Continuity** | Turns a long-running agent task into a structured **checkpoint** another session or agent can resume from without guessing — objective, decisions, artifacts, open questions, next actions, memory writes — so work survives compaction or a handoff (the "Context Cliff Rescue"). A standalone library + CLI (`checkpoint` / `resume`) that **pairs with Fulcra Coord without depending on it**: they share one checkpoint schema, so coord's `snapshot` / `pause --snapshot` / `resume --with-continuity` write and read the same shape. | [`packages/fulcra-continuity/README.md`](packages/fulcra-continuity/README.md) |

> **Related standalone repos.** The **Hermes "press play" demo** (per-guest
> ephemeral Hermes agents that onboard each person into their own Fulcra
> account) lives in its own repos, not this monorepo:
> [`ashfulcra/fulcra-hermes-vercel`](https://github.com/ashfulcra/fulcra-hermes-vercel)
> (the active Vercel Sandbox port),
> [`ashfulcra/fulcra-litellm`](https://github.com/ashfulcra/fulcra-litellm)
> (its LLM gateway), and
> [`ashfulcra/fulcra-hermes-daytona`](https://github.com/ashfulcra/fulcra-hermes-daytona)
> (the deprecated original Daytona port).

> **More coming.** Ash's other Fulcra projects will be added here as they are
> consolidated into this repo — each as its own row above, linking to
> its own overview.

## For agents

This repo is worked on by multiple autonomous agents (Claude Code, Codex, OpenClaw, Cursor, CI). If you're one of them, before you touch anything:

- **Read [`AGENTS.md`](AGENTS.md) first.** It documents the non-obvious environment: the required `uv` extras, the launchd daemon, and the PATH/keychain gotchas.
- **Set up once:** `bash scripts/setup.sh`. Diagnose later with `uv run fulcra-collect doctor`.
- **Coordinate on the bus.** Agents coordinate durable work over Fulcra Files via [`fulcra-coord`](packages/fulcra-coord/README.md) — check your inbox (`fulcra-coord inbox`), announce presence, and post directives instead of working blind. **Gotcha:** the bus needs a *file-capable* Fulcra CLI. The public PyPI `fulcra-api` build lacks the `file` command group, so every bus write **fails silently**. Run `fulcra-coord doctor`; if it reports `File commands: FAIL`, install a file-capable build and set `FULCRA_CLI_COMMAND` — see [`packages/fulcra-coord/docs/fulcra-cli-branch.md`](packages/fulcra-coord/docs/fulcra-cli-branch.md).
- **Land changes via PR (where a forge exists), not direct pushes to `main`.** The rule is an **independent review by a *different* agent identity** — that review is the control, not who clicks merge (a clean approval is merged by whoever's around; never merge your own unreviewed code). Route with `fulcra-coord request-review <artifact>` (an opaque ref — PR#/branch/commit/URL, not just a GitHub PR) and close the loop with `fulcra-coord review-done <artifact> --verdict approve|changes`, which lands the verdict on the author's **bus** inbox — never a GitHub-only comment. The handshake is forge-agnostic: it works without GitHub, and `gh pr merge` is one option, not a requirement.
- **macOS CI is path-filtered and bills at 10× on this repo** — only the menubar + macOS-specific `fulcra-coord` modules trigger it (see [`.github/workflows/macos.yml`](.github/workflows/macos.yml)). Everything else is gated by the local suite + review.

## Repo notes

- **One git repo, no submodules.** All projects live under [`packages/`](packages),
  including the **Fulcra Attention** project at
  [`packages/attention/`](packages/attention), even though the browser extension is
  self-contained and separately installable (it's relayless — it authenticates
  and ingests on its own, no daemon). Each project keeps its own README, build,
  tests, and toolchain (Python and TypeScript both appear here).
- **History.** Several of Collect's pieces were their own repositories until
  2026-05-21, then merged here with `git subtree` so their full commit history
  is preserved (`git log packages/<name>` shows it). The original repos
  (`ashfulcra/fulcra-attention`, `ashfulcra/FulcraMediaHelpers`,
  `ashfulcra/fulcra-csv-importer`) are archived read-only. More on this in the
  Collect overview's [History](docs/collect.md#history) section.
