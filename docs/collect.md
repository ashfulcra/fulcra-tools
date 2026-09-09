# Fulcra Collect

Collect imports notes, journals, media history, and other data into your
[Fulcra](https://fulcradynamics.com) account. You choose a source, configure its
access, and run an import or let Collect check for changes on a schedule.

It is an optional connector host within [Fulcra Tools](../README.md), an
unofficial, unsupported monorepo. Fulcra's APIs and the agent coordination and
continuity tools work without it. Use Collect for sources that need access to
your Mac or for imports you want to configure and run in one place.

The Mac app opens a dashboard in your browser. A background process handles the
imports and saves progress between runs. This page is the installation guide;
[the source guide](how-do-i-get-my-data.md) explains each integration.

## Get started (new user)

### Mac app (Apple silicon)

**[Download Fulcra Collect for Mac](https://github.com/ashfulcra/fulcra-tools/releases/download/collect-v0.1.2-macos-arm64/Fulcra-Collect-macOS-arm64.dmg)**

The current download is **0.1.2 beta**, released September 9, 2026. It is signed,
notarized, and includes Apple Notes, Apple Reminders, and Todoist.
[Release notes and checksums](https://github.com/ashfulcra/fulcra-tools/releases/tag/collect-v0.1.2-macos-arm64).

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

The current Mac beta bundles **23 plugin entries**. Bundled means the plugin is
installed; it still needs its own setup. An entry may be a scheduled importer,
an export reader you run manually, a webhook receiver, or instructions for a
separate browser extension.

| Source | What is available |
|---|---|
| [Apple Notes](../packages/apple-notes/README.md) | Notes and available attachments copied to Fulcra vault files. One-way import in normal setup; separate experimental writeback. |
| [Apple Reminders](../packages/apple-reminders/README.md) | Selected lists copied to Fulcra, with ordinary completion in both directions. |
| [Todoist](../packages/todoist/README.md) | Selected projects copied to Fulcra, with ordinary completion in both directions. |
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

Apple Reminders and Todoist have passed live checks with disposable tasks:
import, completion in both directions, preservation of bot annotations, and
repeat sync without extra file versions. That is a specific check, not a claim
that every account or edge case has been tested. Titles, dates, and list or
project membership remain source-managed.

### Reminders and Todoist setup

For **Apple Reminders**, open Reminders and let it finish syncing. In Collect,
choose **Apple Reminders → Set up → Allow access**, approve the macOS prompt,
and check the lists you want to sync. Leave **Preview only** on for the first run.

For **Todoist**, choose **Todoist → Set up**. The wizard links to Todoist's token
instructions: in the web app, open **Settings → Integrations → Developer** and
copy the API token. Paste it into Collect, then check the projects you want.
The token stays in your Mac's Keychain.

Choose **Enable & run preview** to check the setup without uploads or completion
writes. When ready, return to setup, turn **Preview only** off, and choose
**Enable & start sync**. Collect checks every five minutes while the Mac is awake.
Removing a list or project stops future syncing and leaves existing Fulcra copies.

## How it fits together

The app, background process, and plugins share the following packages.

| Package | Role in Collect |
|---|---|
| [`packages/collect`](../packages/collect) | **The Collect daemon** — local HTTP server on `127.0.0.1:9292` that hosts installed Collect plugins, runs scheduled imports, and exposes the wizard + dashboard UI. |
| [`packages/web-ui`](../packages/web-ui) | The wizard + dashboard + settings **frontend** the daemon serves. Vanilla Alpine.js, no build step. |
| [`packages/menubar`](../packages/menubar) | The **macOS menu-bar companion** — quick-records Moment annotations and surfaces daemon status. |
| [`packages/fulcra-common`](../packages/fulcra-common) | The **shared Fulcra API client** and definition/record helpers used by the importers. Independent coordination packages have their own clients. |

Notes and selected emails are uploaded as **Fulcra Files**. Event importers
write moments, durations, and numeric records through the annotations API.

**Writing events:** record formats and delivery checks differ by importer.
The typed endpoint does not deduplicate source IDs for callers, so importers
need their own duplicate checks or claims. A successful upload is not proof that
every record became visible. Media and labs have landing checks; other paths
have different limits. See each package README before relying on its delivery
or retry behavior.

**Reading it back:** authorized agents can read imported records through the
Fulcra CLI, REST API, or supported MCP tools. Notes and email bodies are files,
so use Fulcra Files access for those; `get-records` queries annotations and
`data-updates` reports changes. [`FULCRA-PRIMITIVES.md`](../FULCRA-PRIMITIVES.md)
maps the available interfaces and their authentication requirements. Collect
is one producer of this context, and is not needed by the agents reading it.

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

Several of the original importers began as separate repositories and were
brought into this monorepo with their history. The repo has since grown to
include independent agent coordination, continuity, knowledge, and preference
tools as well as Collect. See the [package index](../README.md#package-index).

## Why a monorepo

Many importers share authentication, annotation definitions, and record encoding.
Keeping that code together lets a cross-package change land with its callers and
documentation. It does not mean every tool needs the daemon: standalone commands,
browser extensions, and agent skills have their own entry points.
