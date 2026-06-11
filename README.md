# Ash's Fulcra Tools

WARNING: Everything in this repo was vibe coded by Fulcra's lawyer,
using Fulcra's file and datastream primitives.

Nothing in this repo is official Fulcra software, and it may or may
not be supported in the future.

This repo is a set of tools built on [Fulcra](https://fulcradynamics.com):
importers that get your personal data in, a browser extension that captures
what you read, and infrastructure that lets your AI agents coordinate work,
survive context loss, and carry your preferences. Each package under
[`packages/`](packages) keeps its own overview, build, and tests; this README
is the index that points you to them.

## Getting started with Fulcra

[Fulcra](https://fulcradynamics.com) is a personal data platform: your health,
location, calendar, media, attention, and any custom data streams you define,
in one store that you own, with an API your agents can use. It's free up to
5 GB of storage — for you *and* your agents.

```bash
uv tool install fulcra-api   # installs the `fulcra` CLI
fulcra auth login            # browser sign-in; an account is created on first login
```

From there, `fulcra user-info` confirms you're in, `fulcra catalog` shows
what's queryable, and `fulcra --help` covers the rest (data queries, custom
data types, tags, files).

- **Want a guided setup?** Give your agent the
  [fulcra-onboarding skill](https://github.com/fulcradynamics/agent-skills/blob/main/skills/fulcra-onboarding/SKILL.md)
  — it walks a new user through auth, first custom data types, first records,
  and a dashboard.
- **Agent integrating with the platform?**
  [`FULCRA-PRIMITIVES.md`](FULCRA-PRIMITIVES.md) maps every primitive (auth,
  files, annotations, queries, MCP) by agent capability — CLI, raw HTTP, or
  MCP-only.
- **Docs:** [docs.fulcradynamics.com](https://docs.fulcradynamics.com).

## What's in here

Two kinds of tools live here: ones that move **your data** into Fulcra, and
ones that use Fulcra as the substrate for **your agents**.

### Getting your data in

**Fulcra Collect** ([`packages/collect`](packages/collect)) is the local
daemon at the center of the data-ingest tools. It hosts importers as plugins —
periodic, long-running, or run-on-demand — supervises them in worker
subprocesses, keeps credentials in the OS keychain, and serves a web wizard and
dashboard on `127.0.0.1:9292`. Anything that wants to import a data source
into a Fulcra account becomes a Collect plugin and gets the scheduler,
credential storage, and onboarding UI for free. Start at
[`docs/collect.md`](docs/collect.md).

Around the daemon:

| Package | What it is |
|---|---|
| [`menubar`](packages/menubar) | macOS menu-bar app showing daemon and plugin status. |
| [`web-ui`](packages/web-ui) | The wizard / dashboard / settings frontend the daemon serves. |
| [`fulcra-common`](packages/fulcra-common) | Shared core every importer builds on: the API client, wire format, and the single ingest pipeline. |
| [`media-helpers`](packages/media-helpers) | Your media history — watched, listened, read — from ~13 services (Last.fm, Spotify, Netflix, Trakt, Letterboxd, …) into Fulcra. |
| [`csv-importer`](packages/csv-importer) | Any CSV into Fulcra: declare the columns, get idempotent imports. If you can get data into a CSV, you can get it into Fulcra. |
| [`dayone`](packages/dayone) | Selected Day One journal entries into Fulcra, from a JSON export or the app's local database. |

**Fulcra Attention** ([`packages/attention`](packages/attention)) is a Chrome
extension that captures what takes your attention while browsing — every page
you read, with title and time-on-page — straight into your Fulcra account, so
you can later ask *"what was that article I read on Tuesday?"*. It signs in
and ingests on its own; install it from the extension package and you're done.
A three-tier privacy filter (strip tracking params, categorize sensitive
sites, ignore entirely) is built in.

### Agent infrastructure

**Fulcra Coord** ([`packages/fulcra-coord`](packages/fulcra-coord)) is a
shared coordination bus for independent agents — Claude Code sessions, Codex,
OpenClaw, CI jobs — built on Fulcra Files. Agents create durable tasks, direct
work at each other, claim roles, route code reviews, and pick up where a dead
session left off, with no shared memory, no broker, and no infrastructure
beyond a Fulcra account. Agents start at
[`SKILL.md`](packages/fulcra-coord/SKILL.md); humans at the
[README](packages/fulcra-coord/README.md).

**Fulcra Continuity** ([`packages/fulcra-continuity`](packages/fulcra-continuity))
turns a long-running agent task into a structured checkpoint another session
or agent can resume from without guessing — objective, decisions, artifacts,
open questions, next actions. It pairs with Coord (they share one checkpoint
schema) without depending on it.

**Fulcra Prefs** ([`packages/fulcra-prefs`](packages/fulcra-prefs), alpha) is
a user-owned preference layer: agents capture typed preference signals with
decay, a deterministic compiler folds them into per-platform preference docs,
and a consent-gated export logs every disclosure. Two commands to start, and a
session hook that boots Claude Code with your preferences loaded.

**fulcra-coord-files** ([`packages/fulcra-coord-files`](packages/fulcra-coord-files))
is the small object-store transport Coord rides on — useful on its own if you
want to build something else on Fulcra Files.

## Working in this repo

One command sets up a checkout: `bash scripts/setup.sh` — it installs the
right Python + `uv` extras, the `fulcra` CLI, and runs the test suite to
verify. Then `uv run fulcra-collect daemon` runs the ingest hub in the
foreground, or install it as a launchd agent per
[`docs/TESTING.md`](docs/TESTING.md). `uv run fulcra-collect doctor`
diagnoses a broken environment.

It's one git repo, no submodules — a `uv` workspace where every package keeps
its own README, tests, and toolchain (Python and TypeScript both appear here).

**If you're an AI agent** (Claude, Codex, Cursor, …), read
[`AGENTS.md`](AGENTS.md) before you touch anything. It documents the
non-obvious environment — the required `uv` extras, the launchd daemon, the
PATH/keychain gotchas — plus the rules that govern work here: agents
coordinate over the [`fulcra-coord`](packages/fulcra-coord/README.md) bus
(check your inbox before working blind), and nothing lands on `main` without
an independent review by a different agent identity — route reviews with
`fulcra-coord request-review` and land verdicts with `review-done`, on the
bus, not in forge comments. Run `fulcra-coord doctor` first; if it reports
failures, fix your setup before touching the bus.
