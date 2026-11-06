---
name: fulcra-media
description: Use when the user wants to import or inspect media history from Netflix, Trakt, Spotify, Last.fm, Apple Podcasts, Letterboxd, Goodreads, YouTube, Plex/Jellyfin, or another supported media source.
---

# Fulcra media reference

October 24, 2026, development source branch: Collect media plugins and fourteen
standalone imports use guarded v1 Events: `netflix`, `spotify-extended`,
`spotify-ifttt`, `apple-podcasts`, `apple-podcasts-timemachine`, `apple-takeout`,
`youtube`, `generic-csv`, `lastfm`, `deezer`, `letterboxd`, `goodreads`,
`generic-rss` and `trakt`. The standalone `webhook` server also uses guarded v1 Events. The released app
has not switched. This intermediate branch is not rollout. Read the [boundary and authority contracts](../../README.md#standalone-v1-adapter-development-branch)
and [coverage matrix](../../../../docs/collect-v1.md).

## Where to start

Use these observations for the user's selected source; they are not an all-clear test.

| Observation | Command | Interpretation |
|---|---|---|
| Cached authority | `fulcra-media status` | Inspect `v1`, `retained_legacy`, `pending`; UNKNOWN requires investigation. No live proof. |
| CLI auth availability | `fulcra auth print-access-token >/dev/null` | Exit status only; never print the token. `FULCRA_ACCESS_TOKEN` can supply explicit auth instead. |
| Fresh profile setup | `fulcra-media bootstrap` | Resolve owned v1 targets and atomically save configuration; retained selections need explicit mappings below. |
| Selected source preview | `fulcra-media import netflix <selected.csv> --check-only --json` | `would_post` compares selected rows only; it neither recovers pending writes nor proves caught up. |

`fulcra auth login` belongs to the separate Fulcra CLI. Media has no auth
subcommand. CLI/env credentials stay separate from Collect sign-in with no
Collect fallback. Never print credential files or token values.

## Quick orientation

`fulcra-media status` reads existing local files/SQLite without authentication,
initialization, migration or live verification. Output keys are `verification`
(`cached only; not live verified`), `profile`, `v1`, `retained_legacy`, `pending`.
`v1` is null when absent, otherwise contains `version`, `account_id`,
`v1_destination_ids` and `legacy_selection`. Category keys are `watched`,
`listened`, `read`; v1 pins are full Event/UUID IDs. `retained_legacy` contains
old category IDs or nulls. Unreadable sections have `state: UNKNOWN`.
`pending.state` is `absent` or `UNKNOWN`; a retained payload reports `retained`,
`bytes`, `sha256` and a note, never row contents. UNKNOWN is never all clear.
Status does not expose a scheduler's configured-source list or watermark map.

Bootstrap saves private versioned `cli-media-v1.json` under the selected Collect
[profile](../../../collect/README.md#state-and-storage) using a bounded settings lock and atomic replacement. Existing legacy
state (`~/.config/fulcra-media/state.json`, overridden by `FULCRA_MEDIA_STATE`)
is selection evidence, never v1 authority. For every retained selected category,
supply its explicit new mapping with `--watched Event/UUID`,
`--listened Event/UUID`, and/or `--read Event/UUID`. All three categories are
checked before metadata creation. Bare legacy IDs are not mappings.

Saved account validation precedes automatic original pending recovery. Recovery
precedes configuration changes and migrated source resolution, downloads,
database/backup discovery and parsing,
even when the source is empty. Missing configuration with retained pending work
holds. Keep the original profile selection during recovery; switching profiles
cannot settle original pending work. Keep the original profile/configuration, creation receipts, legacy state
and pending evidence intact on any hold; investigate the reported authority or
receipt mismatch before another write. `reset` is retired for every flag
combination and fails before effects; it is not a recovery or cursor-reset tool.

Migrated-route preview uses detached read-only authority, skips pending recovery and
never drains/adopts pending work. It may initialize private runtime infrastructure.
A zero preview is only a comparison for the selected source, never caught-up proof.
The standalone webhook uses this saved authority too. `fulcra-media setup` is a TTY-only wizard picker, not a substitute
for configured v1 authority. Use `fulcra-media wizard <name>` for source instructions.

## Import envelope

Each `fulcra-media import <name> --json` emits one JSON line containing
`importer`, `ok`, `total`, `skipped_existing`, `posted`, `verified`,
`since_watermark`, `new_watermark`, `would_post`, `errors`, `cursor`.
Exit 0 means `ok: true`; exit 2 means `ok: false`. Inspect
`errors[].stage` (one of `setup`, `auth`, `args`, `fetch`, `snapshot`, `import`).
Setup/auth errors require correcting configuration or credentials; args errors
require corrected input; fetch/snapshot errors require source availability;
import errors can include guarded authority/pending holds. Preserve evidence and
report the specific error; do not reset or blindly retry uncertain effects.
Exception URL credential parameters are scrubbed, but source exports remain private.

`--check-only` reports `would_post` with no ingest. It still reads the source
and remote readback, so it is not offline. On migrated routes, error counts are
observed current-invocation lower bounds; earlier verified chunks survive later
errors, while original pending recovery is never counted as current input.
Parse JSON together with the process exit status; missing/malformed output is failure. Incomplete verification is not
proof of delivery. The long-running `webhook` uses the separate lifecycle stream below.

## Standalone Plex/Jellyfin receiver

After profile bootstrap, launch the actual server command (no `serve` subcommand):

```sh
fulcra-media webhook --port 8766 --json
```

Default binding is `127.0.0.1:8765`. `--port` selects the requested port; 0
requests an ephemeral port reported by ready. An occupied port fails without
fallback. `--host` selects the bind address. `--bearer-token` is optional for
`127.0.0.1`/`localhost`, required for every other host. Clients use the Bearer
authorization header, or Plex's existing `?token=<token>` fallback. Keep tokens
private. Receiver token configuration is captured for its lifetime.

Saved-account validation, original pending recovery and all three category
preparations finish before bind. The private owner thread opens/uses/closes the
runtime, including SQLite; HTTP handlers submit defensive event snapshots and
get their own settled result/error. Settings/invocation/task-account leases last
through cleanup, so competing CLI work on that authority can fail while the
server runs. Stop it and await cleanup before reconfiguration. Same-account
token refresh requires fresh principal proof; changed account/origin/profile/
saved configuration/journal holds subsequent writes.

JSON emits separate `ready` and `shutdown` lifecycle lines. Ready follows recovery,
signal installation and the running loop's local `/health` handshake. After ready,
SIGINT/SIGTERM stops admission, drains accepted jobs and joins runtime cleanup
before successful shutdown. A disconnected accepted caller still completes;
a stalled pre-admission parser may later receive 502 without touching released
authority. Accepted jobs have no timeout/cancel/redispatch. Preserve uncertain
original uploads for reconciliation. Failure exits 2 with sanitized stage
`args`, `setup`, `bind`, `ready` or `shutdown`; broken stdout allows only best-effort
stderr diagnostics. Missing output is failure, not readiness.

`/health` reports cached `data_type_id`, `api_version: "v1"` and counters.
Health/ready prove liveness only; they do not prove fresh ownership or coverage.
Writes still check authority. Request `posted` counts verified current records,
`skipped` counts duplicates; recovered old work is not a new request count.
Incomplete/malformed v1 results yield 502. Shutdown counters are a synchronized
snapshot, not proof all response-only peers finished. See the canonical
[webhook contract](../../README.md#standalone-webhook-development-branch).
This branch's synthetic tests do not prove installed readiness or real sender delivery.

## Standalone v1 poll progress

The five poll commands (`lastfm`, `deezer`, `letterboxd`, `goodreads` and
`generic-rss`) expose `cursor: {state, reason}`; other import commands return
`cursor: null`. States/reasons are `advanced/complete`,
`unchanged/no-events`, `unchanged/no-increase`, or `withheld` with `preview`,
`source-changed`, `page-cap`, `entry-cap` or `normalization-skips`.
`advanced` means an actual verified current batch committed an increase.
`new_watermark` is non-null only for that committed increase. A later cleanup
failure can report `ok:false` while retaining a committed decision. Unknown
storage acknowledgement cannot claim advancement.

`ok:true` with a withheld cursor exits 0 and can include verified records; it
never proves caught up. A configured `--max-pages` or `--max-entries` cap withholds
progress even if the source naturally ends before reaching it. Preserve the
result and investigate the reason before choosing further authorized work;
reset and blind retries do not establish coverage.

Private cursor data lives in plugin KV under the selected Collect profile,
bound to saved media authority, account, owned destination and source selection.
Legacy state-file watermarks never seed it. Explicit `--since` selects a window
without overlap. Otherwise, Last.fm and Deezer rewind the saved v1 cursor by
`--watermark-overlap-hours` (nonnegative; default one hour); the three RSS commands
(`letterboxd`, `goodreads`, `generic-rss`) use a fixed one-hour overlap. A malformed
existing cursor still fails with explicit `--since`; an older selection cannot
regress it. RSS filters timestamps before taking the provider-order entry prefix.
Strict resource limits and redirect refusals can stop a scan. A finite available
feed or selected API window does not certify older history. Daemon polling has
a separate contract below. See the canonical [polling contract](../../README.md#standalone-v1-polling-development-branch)
for exact scope, credential sampling, scan limits and parser boundaries.

## Collect daemon poll progress

On this source branch the actual `lastfm`, `deezer`, `generic-rss`, `letterboxd`
and `goodreads` daemon routes now select strict scans through captured Collect
authority. This does not establish installed-app or real-provider acceptance.
Their separate `v2_daemon_media_poll_cursor:` scope binds account, consent epoch,
raw saved settings, owned target, importer/category and source/secret digests.
Annotation and pre-strict v1 cursor keys stay intact and never seed this scope;
a fresh source selection starts with `since=None`. Saved API cursors rewind
24 hours; RSS uses its saved boundary and sorts ascending before entry caps.
Standalone overlap options above do not set daemon behavior.

Non-preview runs recover original pending work before entering source functions;
worker credential hydration occurs earlier. Preview does not drain pending work.
Every exit restores the original legacy worker watermark and removes temporary
adapter/writer attributes. Current strict fetch and normalization finish before
delivery; only that immediate actual writer result can justify progress.
Recovered receipts cannot substitute. Any configured page/entry cap, malformed
row or enrichment failure withholds progress even with verified delivery.
Valid saved TOML integer caps work; bools, strings, zero and negatives fail.
Strict resource limits can leave an initial full scan UNKNOWN without a partial
seed or invented cutoff. Returned finite feeds do not certify all history.

Before an increase, the adapter rereads the prior cursor and quietly rereads only
the consumed canonical Last.fm `api-key` or Deezer `access-token`; feeds perform
no credential sweep. Changed/missing/unreadable secrets withhold progress. A second
session authority guard follows the possibly slow read, before KV mutation.
These sampled checks do not prove provider account identity or atomic revocation.
Exact authoritative envelope readback is required; acknowledgement alone cannot
claim advancement. Existing progress events expose `stage=cursor`, `cursor_state`
and `cursor_reason` (advanced/unchanged/withheld/unknown), not a new UI promise.
Preserve genuine delivery counts, receipts and pending evidence on later holds;
withheld successful delivery is not caught-up proof. See the canonical
[daemon contract](../../README.md#daemon-v1-polling-development-branch).

## Standalone Trakt choices

Trakt now shares migrated-route recovery/preview authority. Original pending
work recovers before Trakt credential loading/refresh/fetch, even for empty or
all-cluster-dropped history. Entry/recovery holds use `setup`; preserve evidence.
Trakt cursor and watermarks are null.

Explicit `--twin-policy ask` or `--clusters ask` requires a TTY without JSON;
otherwise it fails at `args` before credentials/source, even with no candidates.
JSON is always noninteractive. Omitted twin policy keeps and warns on stderr
only when candidates exist in JSON/non-TTY; TTY asks. Omitted cluster policy
needs a choice only when actual clusters exist; noninteractive clustered input
requires an explicit policy. No candidates means no twin warning or prompt.

Twin hints need fresh owned-record readback, matching high confidence/fingerprint
and exclusions. Current-input highs are also candidates, labeled honestly;
excluded highs never justify a skip. The legacy JSON cache is not evidence.
Every offered pair is displayed. Yes authorizes only those immutable exact pairs
that remain freshly proven during import; new pairs cannot inherit that answer.
No/keep bypasses loose twins only, retaining strong identity/alias/exclusion rules
and replay identities. Options/answers do not change saved authority or pending
payloads. Prompt inspection and preview are read-only.

`total` counts selected writer input after cluster preprocessing, including
loose-policy skips; `skipped_existing` includes those and other writer skips.
Cluster drops are outside that total. Known error counters stay lower bounds;
recovery is excluded. Preview planned rows use `posted`/`would_post`, with zero
`verified`. See the canonical [Trakt policy contract](../../README.md#standalone-trakt-policy-development-branch)
for fresh-proof races and unchanged Collect policy precedence.

## Importer roster (what works today)

Run `fulcra-media import --help` for the live list. As of this skill version:

| Command | Category | Auth | Notes |
|---|---|---|---|
| `lastfm` | listened | API key + username | **Universal music sidecar** — covers Tidal/Apple Music/Amazon Music/SoundCloud/Pandora/YouTube Music via in-app or Web Scrobbler. Public scrobbles only. |
| `deezer` | listened | OAuth access token | Direct API with strict bounded scanning and optional page cap. Manual token mint. |
| `spotify-extended` | listened | GDPR zip | Full history; one-shot, slow to arrive. |
| `spotify-ifttt` | listened | Legacy GDrive zip | Pre-Extended-API backfill; cross-applet dedup native. |
| `trakt` | watched | OAuth | Covers Apple TV+ via Universal Trakt Scrobbler. Cluster handling: `--clusters drop|sentinel:YYYY|keep|ask`. Twin-dedup: `--twin-policy ask|auto-discard|keep`. |
| `netflix` | watched | GDPR CSV | Slim (in-app) or rich (full GDPR) — auto-detected. |
| `apple-podcasts` | listened | Local SQLite | macOS local SQLite library. |
| `apple-podcasts-timemachine` | listened | Time Machine backups | macOS Time Machine snapshot recovery. |
| `apple-takeout` | watched | privacy.apple.com CSV | Apple TV Playback Activity. |
| `letterboxd` | watched | Username | Public RSS. |
| `goodreads` | read | User ID | Public RSS of the 'read' shelf. |
| `youtube` | watched | Takeout JSON | Extract and pass `watch-history.json` from the export. |
| `generic-csv` | watched/listened | n/a | Any column-mapped CSV. |
| `generic-rss` | watched/listened | n/a | Any RSS/Atom feed. |
| (server) `webhook` | watched | bearer token | Long-running HTTP receiver for Plex/Jellyfin push. Not an `import` subcommand. |

---

For `generic-csv`, accepted fingerprint modes include `auto`, `none`,
`music`, `movie`, `tv` and `podcast`; consult its help for column mappings.
`none` omits `content_fingerprint` only; deterministic IDs and category-based
cross-source aliases remain. The accepted `tv` and `podcast` modes do not
currently generate `content_fingerprint` in this parser.

## Recipes

For a user-selected Netflix export after configuration:

```bash
fulcra-media import netflix /path/to/selected.csv --check-only --json
fulcra-media import netflix /path/to/selected.csv --json
```

Run the write only within the user's authorization; preview does not settle an
original pending batch. No separate pending-clear command exists.

For Trakt developer portals, PKCE, Development callbacks and existing CLI
credentials, see [Trakt connection notes](../../README.md#trakt-connections-and-api-apps).
Credential files such as `lastfm.json`, `deezer.json`, and `trakt.json` remain
under `~/.config/fulcra-media/`, separately from Collect keychain credentials.
Consult `fulcra-media import <name> --help` for flags, source inputs and category
choices. Apple Music takeout and on-device Apple TV are Collect-only plugins.
Workouts are not wired into this CLI.

## Periodic invocation

Use the scheduler available on the user's host only when recurring work is
requested. Persist an explicit list of user-authorized, configured sources and
complete arguments, plus cadence and last successful envelope. Do not discover
sources from cached status, legacy watermarks or source names in this roster.
Export imports run when the user selects a new export; generic feeds need their
configured URL. A preview-only tick cannot settle pending work or establish full
coverage. Parse return code and JSON on each authorized invocation, retain error
evidence, and report actionable failures rather than mechanically resetting or
retrying. Bound work and prevent scheduler overlap. Skill installation alone does
not create recurring jobs. Supervise a configured webhook as a persistent service.

Use the [package guide](../../README.md) for category semantics, source limitations,
security and development tests. Keep personal exports out of version control.
