# fulcra-tools

The umbrella repo for the Fulcra team's shared work — one place to find and
share the various Fulcra projects. Each project keeps its own overview, build,
and tests; this README is the index that points you to them.

> **Working in this repo with an AI agent (Claude, Codex, Cursor, …)?**
> Read [`AGENTS.md`](AGENTS.md) first. It documents the non-obvious
> environmental requirements — the required `uv` extras, the launchd daemon,
> and the PATH/keychain gotchas — that otherwise cost time to rediscover on
> first run.

## What's in here

| Project | What it is | Start here |
|---|---|---|
| **Fulcra Collect** | The main project — a local-ingest daemon + plugins that import your personal-data streams into [Fulcra](https://fulcradynamics.com). Spans the daemon ([`packages/collect`](packages/collect)), its web wizard, the macOS menu-bar companion, the shared API client, and the data-source plugins. | [`docs/collect.md`](docs/collect.md) |

> **More coming.** For now this index covers just Fulcra Collect. Other Fulcra
> projects will be added here as the team consolidates them into this repo —
> each as its own row above, linking to its own overview.

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
