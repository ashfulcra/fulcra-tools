# fulcra-tools

Monorepo for the Fulcra helper tools — the things that get personal data
into a [Fulcra](https://fulcradynamics.com) account.

## Packages

| Package | What it is |
|---|---|
| [`packages/attention`](packages/attention) | Browsing-attention capture: a localhost Python relay + a Chrome MV3 extension. |
| [`packages/media-helpers`](packages/media-helpers) | Imports watched/listened media (Trakt, Last.fm, …) into Fulcra as annotations. |
| [`packages/csv-importer`](packages/csv-importer) | Generic CSV → Fulcra annotation importer (library + CLI). |

Each package keeps its own README, build, tests, and language toolchain
(Python and TypeScript both appear here). Start in the package directory
you care about.

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
