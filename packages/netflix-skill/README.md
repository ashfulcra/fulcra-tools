# fulcra-netflix-skill

An agent skill that takes a brand-new user from "I messaged a skill link to
my bot" to "my Netflix viewing history lives in my own Fulcra account as a
Watched annotation, shared with the movie-night pool."

The deliverable is the **skill folder** (`skills/fulcra-netflix/`), not a
Python library: a runtime-agnostic SKILL.md conversation state machine
(auth → export → import → share) plus a vendored, PEP 723 self-contained
import script that any agent runs with `uv run`. The Python package wrapper
exists so the monorepo's pytest tooling covers the script; end users never
install it.

Status: **importer CLI built and tested**; the conversational SKILL.md
wrapper (onboard → export → import → share) has not landed yet. See
[docs/design.md](docs/design.md) for the full spec (flow, record schema,
error handling, what's deferred).

## Why this exists

It's the flagship concrete demo of Fulcra as an agent context layer: a
relatable dataset (Netflix history), imported by the user's own agent over
chat, landing in the user's own datastore, then shared into a pool that
group-recommendation agents can work over. See the design doc for the
composition with upstream `fulcradynamics/agent-skills` (onboarding auth
flow, ingest-beta record pattern) and this repo's `media-helpers` (export
walkthrough, fingerprint conventions).

## Layout

```
docs/design.md                              the approved design spec
skills/fulcra-netflix/scripts/netflix_import.py   the vendored, self-contained importer  [built]
fulcra_netflix/                             test-support shims for the vendored script
tests/                                      parser/wire/API/CLI tests over synthetic fixtures
```

The importer script is functionally complete (parsing, wire encoding, auth,
batch POST, readback verification, and the CLI entry point below). What's
still `[pending]` is the conversational `SKILL.md` itself — the
onboard → export → import → share state machine that walks a brand-new user
through getting a CSV and invoking this script. Today the script is run
directly.

## Running the importer

```sh
uv run skills/fulcra-netflix/scripts/netflix_import.py <csv-path> [--json] [--check-only] [--no-verify]
```

The script is a self-contained PEP 723 file — `uv run` fetches its one
dependency (`httpx`) automatically, no separate install step needed. Both
Netflix CSV export variants are auto-detected from the header, no flag
needed:

- **slim** — the plain `NetflixViewingHistory.csv` (`Title,Date` columns
  only). Timestamps are synthesized (noon UTC, 1-second duration) and marked
  low confidence, since the slim export carries no real time-of-day.
- **rich** — the GDPR/"Download all my data" `ViewingActivity.csv` (10
  columns including a real UTC `Start Time` and `Duration`). Timestamps here
  are high confidence and durations are real. Trailer/hook/promotional rows
  are dropped automatically — they aren't real viewing sessions.

Flags:

- `--json` — emit a single-line JSON envelope on stdout instead of the
  human-readable summary. Use this for scripting or CI; see the envelope
  contract below.
- `--check-only` — parse and count events only; makes no network call and
  requires no auth. Use this to sanity-check a CSV (row counts, that it
  parses cleanly) before actually importing.
- `--no-verify` — skip the post-import readback sampling step (see
  "Readback verification" below) for faster iteration.

A malformed row anywhere in the CSV aborts the whole run *before* anything
is posted — events are fully parsed and materialized first, so a bad row
never results in a partial import sitting on the server. Parse errors
include row context, e.g. `row 214 ('BEEF: Season 1: Episode 3'): not a
H:MM:SS duration: 'bogus'`.

### Auth

The script never asks for or stores credentials itself. It mints a bearer
token by shelling out to the `fulcra-api` CLI (a separate tool, not part of
this monorepo):

```sh
fulcra-api auth print-access-token
```

falling back to `uv tool run fulcra-api auth print-access-token` if
`fulcra-api` isn't directly on `PATH`. This means:

- The `fulcra-api` CLI must be installed (`uv tool install fulcra-api`) and
  already authenticated (run its own login flow once beforehand).
- The token is minted fresh per run, never written to disk, and never
  printed or logged by this script — including in the `--json` envelope and
  in error messages, which report only `HTTP <code> on <path>`, never
  response bodies.

### Readback verification

After a successful import (unless `--no-verify` is passed), the script
samples a few of the just-posted events and asks `fulcra-api get-records`
whether a matching record shows up in a window around their timestamp.
This is best-effort: if the `fulcra-api` CLI isn't available, `verified` is
`None` ("couldn't check"), which is deliberately kept distinct from `0`
("checked, found nothing") — the latter is a real signal (e.g. indexing
lag), the former just means the check didn't run at all.

### JSON envelope

With `--json`, the script prints one line of JSON with this shape:

```json
{
  "importer": "netflix",
  "variant": "slim",
  "ok": true,
  "total": 128,
  "posted": 128,
  "skipped_existing": null,
  "verified": 3,
  "would_post": null,
  "errors": []
}
```

- `variant` — `"slim"` or `"rich"`, whichever was auto-detected.
- `posted` — count of records included in successful (or partially
  successful) batch POST requests, **not** a count of novel/new records.
  The ingest endpoint returns no dedup feedback, so a full re-run of the
  same CSV reports `posted` equal to the full record count again — the
  server silently no-ops the duplicate POSTs, but this script has no way
  to see that, so `posted` is honest about "attempted and accepted the
  request" rather than "created a new record." On a partial batch failure
  (some chunks succeed before one fails), `posted` reflects only the
  chunks that actually landed before the failure, not the full batch.
- `skipped_existing` — currently **always `null`**. It's reserved for a
  future dedup-aware count but the ingest endpoint doesn't expose which
  records were skipped as duplicates vs. newly created, so this script
  cannot populate it today. The key is kept in the envelope (append-only
  contract) for forward compatibility.
- `would_post` — populated only for `--check-only` runs (parsed count,
  nothing posted); `null` otherwise.
- `verified` — count of sampled events confirmed present after import, or
  `null` when verification was skipped/unavailable.
- `errors` — list of `{"stage": ..., "message": ...}` dicts. `stage` is one
  of `args` (bad path — including unreadable/directory paths), `parse`
  (malformed CSV/row), `auth` (couldn't mint or the server rejected the
  token), or `post` (any other HTTP failure, including a batch that failed
  partway through — see `posted` above).

This is a **stable, append-only contract**: existing keys are never removed
or repurposed, so other tooling can depend on specific keys without
breaking on a future change. New keys may be added over time.

Exit code is `0` for success (including `--check-only` runs) and `2` for
any failure, with `errors[0].stage` telling the caller which phase broke.

## Testing

```sh
uv run --package fulcra-netflix-skill pytest packages/netflix-skill/tests/ -q
```

The suite covers: parsing (both variants, including row-context
error messages on malformed dates/durations), wire-format encoding, the
`ensure_watched_def`/`post_batch` API helpers (chunking, content-type), and
the CLI itself (happy path, `--check-only`, and parse/auth failure envelopes)
via monkeypatched `get_token`/`make_api_client` seams — no real network calls.
