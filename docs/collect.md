# Fulcra Collect

Collect imports notes, journals, media history, and other data into your
[Fulcra](https://fulcradynamics.com) account. You choose a source, configure its
access, and run an import or let Collect check for changes on a schedule.

The Mac app opens a dashboard in your browser. A background process handles the
imports and saves progress between runs. This page is the installation guide;
[the source guide](how-do-i-get-my-data.md) explains each integration.

## Get started (new user)

### Mac app (Apple silicon)

**[Download Fulcra Collect for Mac](https://github.com/ashfulcra/fulcra-tools/releases/download/collect-v0.1.1-macos-arm64/Fulcra-Collect-macOS-arm64.dmg)**

The current download is **0.1.1 beta**, released September 8, 2026. It is signed,
notarized, and includes Apple Notes.
[Release notes and checksums](https://github.com/ashfulcra/fulcra-tools/releases/tag/collect-v0.1.1-macos-arm64).

1. Open the downloaded disk image and drag **Fulcra Collect** into **Applications**.
2. Open **Fulcra Collect** from Applications. Click its icon in the menu bar.
3. Choose **Install & start daemon** if prompted, then open the dashboard and
   **Sign in with Fulcra**.
4. Choose a source and follow **Set up**. For Apple Notes, use the steps below.

Requires an Apple silicon Mac running macOS 12 or later. The app includes Python,
Collect, the Fulcra client, and the plugins; you do not need Terminal or Homebrew.
There is no Intel Mac or Windows installer in this release.

### Set up Apple Notes

1. Open **Notes** and let iCloud finish downloading your notes to this Mac.
2. In the Collect dashboard, choose **Apple Notes → Set up**.
3. Follow the **Full Disk Access** step. In **System Settings → Privacy &
   Security → Full Disk Access**, add **Fulcra Collect** from Applications.
4. Restart Collect as directed, then choose **Verify access** in the wizard.
5. Leave **Preview only** off and choose **Enable & start sync**. To inspect
   what Collect would import first, turn on **Preview only** and choose
   **Enable & run preview**.

Copies go to `vault/notes/apple/` in your Fulcra account. Your originals stay in
Apple Notes. Large libraries can take several runs; each run saves progress,
and **Run Now** continues the import. The Mac must be awake and connected for
uploads. [Notes troubleshooting and limits](../packages/apple-notes/README.md).

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

## Plugin status

The current Mac beta bundles **21 plugin entries**. Bundled means the plugin is
installed; it still needs its own setup. An entry may be a scheduled importer,
an export reader you run manually, a webhook receiver, or instructions for a
separate browser extension.

| Source | What is available |
|---|---|
| [Apple Notes](../packages/apple-notes/README.md) | Notes and available attachments copied to Fulcra vault files. One-way import in normal setup; separate experimental writeback. |
| [Day One](../packages/dayone/README.md) | Local journal database or JSON export import. |
| [Media](../packages/media-helpers/README.md) | 16 entries: Last.fm, Deezer, Trakt, Netflix CSV, Spotify extended history, YouTube takeout, Apple TV takeout, Apple Music takeout, generic RSS, Letterboxd, Goodreads, Apple Podcasts, Podcasts Time Machine recovery, local Apple TV, generic media CSV, and Plex/Jellyfin webhooks. |
| [Gmail](../packages/gmail/README.md) | Read-only polling with local filters, selected messages in Fulcra Files, and optional agent-bus relay. Requires Google OAuth setup. |
| [PurpleAir](how-do-i-get-my-data.md#air-quality-purpleair) | Readings from the cloud API or sensors on your local network. |
| [Fulcra Attention](../packages/attention/README.md) | A setup entry for the separate Chrome extension. The extension sends browsing activity directly to Fulcra and signs in separately. Its install folder is included in the disk image. |

The [source guide](how-do-i-get-my-data.md) has permissions, credentials, import
formats, and known gaps. These sources have different setup requirements and
have not all been verified against every service or account. The downloadable
app also includes `fulcra-api` 0.1.41; source installs use their own environment
and dependency lock.

**In development:** Apple Reminders with a list picker and completion syncing in
both directions. **Next:** Todoist with project selection and the same completion
behavior. Neither is available in this release. This does not promise full
bidirectional editing of task titles, dates, or lists.

## How it fits together

The app, background process, and plugins share the following packages.

| Package | Role in Collect |
|---|---|
| [`packages/collect`](../packages/collect) | **The Collect daemon** — local HTTP server on `127.0.0.1:9292` that hosts every plugin, runs them on schedule, and exposes the wizard + dashboard UI. The hub the rest of the repo plugs into. |
| [`packages/web-ui`](../packages/web-ui) | The wizard + dashboard + settings **frontend** the daemon serves. Vanilla Alpine.js, no build step. |
| [`packages/menubar`](../packages/menubar) | The **macOS menu-bar companion** — quick-records Moment annotations and surfaces daemon status. |
| [`packages/fulcra-common`](../packages/fulcra-common) | The **shared Fulcra API client** + cross-plugin definition resolver. Pulled in by every other package. |

Notes and selected emails are uploaded as **Fulcra Files**. Event importers
write moments, durations, and numeric records through the annotations API.

**Writing events:** moments, durations, and numeric records go
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
| [`packages/apple-notes`](../packages/apple-notes/README.md) | Apple Notes and attachments copied into Fulcra vault files. |
| [`packages/dayone`](../packages/dayone) | Day One journal entries (live SQLite read or one-shot export-zip upload). |
| [`packages/gmail`](../packages/gmail/README.md) | Filtered Gmail messages copied into Fulcra Files, with optional bus relay. |
| [`packages/purpleair`](../packages/purpleair) | Air-quality readings from an API or local sensor. |
| [`attention`](../packages/attention) | Browsing-attention capture: a relayless Chrome MV3 extension that POSTs tab/idle events **directly to the Fulcra API**. Collect only shows an install-the-extension pointer — there is no daemon relay route or pairing. |
| [`packages/csv-importer`](../packages/csv-importer) | Generic CSV → Fulcra annotation importer (library + CLI). The same logic the `generic-csv` Collect plugin uses. |

Start in the package directory you care about. Every change must update its
relevant README in the same PR, along with this guide and the source guide when
installation or plugin availability changes. See [the agent guide](../AGENTS.md)
for contributor rules.

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
