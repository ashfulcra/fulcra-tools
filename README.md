# fulcra-tools

Monorepo for the Fulcra helper tools — the things that get personal data
into a [Fulcra](https://fulcradynamics.com) account.

## Packages

| Package | What it is |
|---|---|
| [`packages/collect`](packages/collect) | The Fulcra Collect daemon — local HTTP server on `127.0.0.1:9292` that hosts every plugin, runs them on schedule, and exposes the wizard + dashboard UI. The hub everything else plugs into. |
| [`packages/web-ui`](packages/web-ui) | The wizard + dashboard + settings frontend served by the collect daemon. Vanilla Alpine.js, no build step. |
| [`packages/menubar`](packages/menubar) | macOS menubar app for quick-recording Moment annotations and surfacing daemon status. |
| [`packages/attention`](packages/attention) | Browsing-attention capture: a collect plugin + a Chrome MV3 extension that POSTs tab/idle events to the daemon. |
| [`packages/media-helpers`](packages/media-helpers) | Collect plugins for watched/listened media — Trakt, Last.fm, Spotify takeouts, YouTube takeouts, Netflix, Apple Podcasts, Apple TV, Deezer, Letterboxd, Goodreads, generic RSS/CSV. |
| [`packages/dayone`](packages/dayone) | Collect plugin for Day One journal entries (live SQLite read or one-shot export-zip upload). |
| [`packages/fulcra-common`](packages/fulcra-common) | Shared Fulcra API client + the cross-plugin definition resolver. Pulled in by every other package. |
| [`packages/csv-importer`](packages/csv-importer) | Standalone Generic CSV → Fulcra annotation importer (library + CLI). The same logic the `generic-csv` collect plugin uses. |

Each package keeps its own README, build, tests, and language toolchain
(Python and TypeScript both appear here). Start in the package directory
you care about.

## Where do I get data from?

[**docs/how-do-i-get-my-data.md**](docs/how-do-i-get-my-data.md) is the
lookup page: every supported source, every pathway (live / scheduled /
one-shot historical), and which collect plugin handles it. Read this
first if you're deciding what to wire up.

## History

Each package was its own repository until 2026-05-21. They were merged
here with `git subtree`, so the full commit history of every package is
preserved — `git log packages/<name>` shows it. The original repos
(`ashfulcra/fulcra-attention`, `ashfulcra/FulcraMediaHelpers`,
`ashfulcra/fulcra-csv-importer`) are archived read-only.

## Why a monorepo

The packages share the Fulcra annotations API, auth, the ingest payload
shape, and dedup logic. One repo means cross-package changes land in a
single commit, teammates clone once, and the shared Fulcra-client code
can be factored into a common package.
