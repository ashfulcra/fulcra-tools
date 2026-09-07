# Fulcra Collect

**Fulcra Collect** is a local daemon + web wizard that imports your
personal-data streams into your [Fulcra](https://fulcradynamics.com) account.
The daemon ([`packages/collect`](../packages/collect)) hosts every plugin, runs
them on schedule, and serves the onboarding wizard + dashboard. Collect is the
main project in the [fulcra-tools](../README.md) umbrella repo.

Collect spans several packages in this repo — the daemon, the frontend it
serves, the macOS companion, the shared API client, and the data-source
plugins. They were merged in from separate repositories as the project
consolidated (see [History](#history)).

> **Working in this repo with an AI agent (Claude, Codex, Cursor, …)?**
> Read [`AGENTS.md`](../AGENTS.md) first. It documents the non-obvious
> environmental requirements — the required `uv` extras, the launchd daemon,
> and the PATH/keychain gotchas — that otherwise cost time to rediscover on
> first run.

## Get started (new user)

### Mac app (Apple silicon)

The app installer is awaiting Apple notarization. It is **not yet available for
download**. When ready, it will appear on the [releases page](https://github.com/ashfulcra/fulcra-tools/releases).
The installation steps below describe that upcoming release.

1. Open the downloaded disk image and drag **Fulcra Collect** into **Applications**.
2. Open **Fulcra Collect** from Applications. Click its icon in the menu bar.
3. Choose **Install & start daemon** if prompted, then open the dashboard and
   **Sign in with Fulcra**.
4. Choose a source and follow **Set up**. For [Apple Notes](../packages/apple-notes),
   grant Full Disk Access, verify access, then choose **Enable & start sync**.

Requires an Apple silicon Mac running macOS 12 or later. The app includes Python,
Collect, the Fulcra client, and the plugins; you do not need Terminal or Homebrew.
The first release will be a beta. Source installation is available for contributors below.

### From source (contributors)

```bash
bash scripts/setup.sh
uv run fulcra-collect install
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.fulcra.collect.plist
open "$(cat ~/.config/fulcra-collect/web-url)"
```

Run these commands from a checkout of this repository. The setup script requires
Homebrew on macOS and installs the development environment. Keep a source
checkout current with `bash scripts/update.sh`.
[Detailed troubleshooting](TESTING.md).

## How it fits together

Collect is the product; the daemon is the hub everything else plugs into.

| Package | Role in Collect |
|---|---|
| [`packages/collect`](../packages/collect) | **The Collect daemon** — local HTTP server on `127.0.0.1:9292` that hosts every plugin, runs them on schedule, and exposes the wizard + dashboard UI. The hub the rest of the repo plugs into. |
| [`packages/web-ui`](../packages/web-ui) | The wizard + dashboard + settings **frontend** the daemon serves. Vanilla Alpine.js, no build step. |
| [`packages/menubar`](../packages/menubar) | The **macOS menu-bar companion** — quick-records Moment annotations and surfaces daemon status. |
| [`packages/fulcra-common`](../packages/fulcra-common) | The **shared Fulcra API client** + cross-plugin definition resolver. Pulled in by every other package. |

**Writing (the ingest path):** moments, durations, and numeric records go
through the **typed endpoint** (`POST /ingest/v1/record/{data_type}`,
unwrapped payloads, JSONL batches); tombstones stay on the legacy wrapped
endpoint (their machine-state payload has no typed slot). The typed endpoint
has three sharp edges, each with a shipped compensation: it does **no
server-side source-id dedup** (media's claim machinery + labs' pre-post
existing-check prevent duplicates), it **silently drops** unknown fields and
bad JSONL lines (media self-heals by unclaiming confirmed-missing events for
next-run retry; labs refuses to ingest when it cannot verify), and it is
**async** (~1–2 min to visibility — both writers verify landings by
re-querying, and `fulcra-collect doctor` has a schema-drift row that fails
loudly if our wire shape ever diverges from the served schema).

**Reading it back:** everything Collect ingests is readable by any agent or
tool via the `fulcra` CLI (`get-records`, `data-updates`), the REST API, or
the official read-only MCP server (`uvx fulcra-context-mcp@latest` / hosted at
mcp.fulcradynamics.com). Collect is the write side of that pair.

### Plugins (the data sources Collect runs)

| Package | Sources it adds |
|---|---|
| [`packages/media-helpers`](../packages/media-helpers) | Watched/listened/read media — Trakt, Last.fm, Spotify takeouts, YouTube takeouts, Netflix, Apple Podcasts, Apple TV, Deezer, Letterboxd, Goodreads, generic RSS/CSV. |
| [`packages/dayone`](../packages/dayone) | Day One journal entries (live SQLite read or one-shot export-zip upload). |
| [`attention`](../packages/attention) | Browsing-attention capture: a relayless Chrome MV3 extension that POSTs tab/idle events **directly to the Fulcra API**. Collect only shows an install-the-extension pointer — there is no daemon relay route or pairing. |
| [`packages/csv-importer`](../packages/csv-importer) | Generic CSV → Fulcra annotation importer (library + CLI). The same logic the `generic-csv` Collect plugin uses. |

Each package keeps its own README, build, tests, and language toolchain
(Python and TypeScript both appear here). Start in the package directory
you care about.

## Where do I get data from?

[**docs/how-do-i-get-my-data.md**](how-do-i-get-my-data.md) is the
lookup page: every supported source, every pathway (live / scheduled /
one-shot historical), and which Collect plugin handles it. Read this
first if you're deciding what to wire up.

## History

Each of these components was its own repository until 2026-05-21. They were
merged here with `git subtree` — becoming Collect's plugins and supporting
packages — so the full commit history of every one is preserved
(`git log packages/<name>` shows it). The original repos
(`ashfulcra/fulcra-attention`, `ashfulcra/FulcraMediaHelpers`,
`ashfulcra/fulcra-csv-importer`) are archived read-only.

## Why a monorepo

Collect and its plugins share the Fulcra annotations API, auth, the ingest
payload shape, and dedup logic. One repo means cross-package changes land in a
single commit, teammates clone once, and the shared Fulcra-client code lives in
one common package the daemon and every plugin depend on.
