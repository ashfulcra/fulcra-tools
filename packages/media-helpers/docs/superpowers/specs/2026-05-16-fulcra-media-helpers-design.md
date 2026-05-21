# FulcraMediaHelpers — Design

**Status:** Draft for review
**Date:** 2026-05-16
**Owner:** redacted@users.noreply.github.com

## 1. Goal & scope

A Python helper package that imports the user's media consumption into Fulcra as
annotations. Two annotation categories — **Watched** and **Listened** — populated
by MVP importers: Trakt, Netflix, Apple Podcasts, Spotify, Last.fm, and Apple
Data & Privacy takeout. Supports both one-shot backfills and re-runnable
incrementals. CLI only.

**Design philosophy on repeats and partial watches.** People rewatch movies,
re-listen to podcasts, and consume things in pieces (pause/resume across days).
The data model treats each playback event as a first-class annotation — three
watches of the same movie produce three annotations; a movie watched in two
sittings produces two annotations. Idempotency keys are per-event, not
per-content. Cross-source merging is never automatic; users windowing sources
themselves is the explicit pattern.

**Design philosophy on timestamp trust.** Not all timestamps are equally
trustworthy (notably Trakt has well-known clusters of synthetic timestamps
from signup-import flows). Every annotation carries a `timestamp_confidence`
field (`high` / `medium` / `low`) and, where relevant, the source action that
produced it, so consumers can filter or weight accordingly.

This is **transitional**. The Fulcra CLI in `fulcradynamics/fulcra-api-python`
currently spreads its capabilities across feature branches:

- `add-cli` — OAuth device-flow auth (`fulcra auth login`,
  `fulcra auth print-access-token`) and most read commands.
- `file-commands` — Fulcra Library file store
  (`fulcra file list|stat|download|upload|delete`), needed to retrieve
  user-uploaded source archives like Netflix or Apple takeouts.
- Neither branch yet has annotation-write commands.

This package depends on a build of `fulcra-api` that includes **both** branches'
functionality (either a merged branch or a vendored fork until merge to main).
Until annotation-write commands exist in the CLI, importers POST directly to
`/ingest/v1/record` and `/ingest/v1/record/batch` using the access token
printed by `fulcra auth print-access-token` — the same pattern used by the
existing `arc-claw-bot/fulcra-annotations-skill`.

**Source archives live in the user's Fulcra Library** (the file-store accessed
via `fulcra file ...`). The user uploads each fresh takeout (Netflix GDPR
export, Apple Data export, Spotify Extended History zip) to a path like
`/takeouts/...`. Importers accept either a local filesystem path or a
`fulcra:/takeouts/...` library path; library paths are resolved by shelling
out to `fulcra file download` into a tempfile.

## 2. Annotation model

Two annotation definitions, both `DurationAnnotation`, created on first run via
`POST /user/v1alpha1/annotation` and cached by ID in local state.

| Definition | Default tags | Note format |
|---|---|---|
| **Watched** | `media`, `watched` | Movie: `"{title} ({year})"`; Episode: `"{show} S{ss}E{ee} – {episode title}"` |
| **Listened** | `media`, `listened` | Music: `"{artist} – {track}"`; Podcast: `"{show} – {episode}"` |

Per-record (event-level) metadata, posted to `/ingest/v1/record`:

```json
{
  "specversion": 1,
  "data": "{\"note\":\"<content>\",\"title\":\"<title only>\",\"service\":\"<service>\",\"timestamp_confidence\":\"high\",\"external_ids\":{...}}",
  "metadata": {
    "data_type": "DurationAnnotation",
    "recorded_at": {
      "start_time": "2026-05-16T20:30:00Z",
      "end_time":   "2026-05-16T21:22:00Z"
    },
    "tags": ["<service-tag-uuid>"],
    "source": [
      "com.fulcra.media.<importer>.<deterministic-event-id>",
      "com.fulcradynamics.annotation.<definition-id>"
    ],
    "content_type": "application/json"
  }
}
```

**`data` JSON fields** (all importers):
- `note` — display string (see Note format above)
- `title` — content title only, no formatting
- `service` — `trakt` / `netflix` / `apple-tv` / `apple-podcasts` / `spotify` / `lastfm`
- `timestamp_confidence` — `high` (real-time event log from authoritative source), `medium` (last-play snapshot or estimated), `low` (clustered / retroactive / known-bad-stamp)
- `external_ids` — service-specific identifiers + per-source diagnostics
  (e.g. Trakt: `{trakt_history_id, trakt_action, timestamp_cluster_size?}`;
  Apple takeout: `{device_type, device_model, country}`)

**Why DurationAnnotation, not Moment:** every source has meaningful duration
(Trakt `runtime`, Netflix `Duration`, Apple Podcasts `ZDURATION`, Spotify
`ms_played`); intervals enable "what was I doing at 9pm" queries; the API schema
supports a polymorphic `recorded_at` as `{start_time, end_time}` specifically
for this case.

**Why service-as-tag, not service-as-source:** tags are queryable first-class
objects in Fulcra (`FulcraTag` with UUID). `source` is a free-form string array
used here purely for idempotency.

Service tags (created once via `POST /user/v1alpha1/tag`, cached locally):
`netflix`, `trakt`, `apple-tv`, `apple-podcasts`, `spotify`, `lastfm`.

## 3. Importers

Every importer follows the same pipeline: **fetch → normalize → dedupe → ingest
→ verify**. Each emits a `NormalizedEvent` and shares the ingest/verify path.

### 3.1 Trakt — `fulcra-media import trakt`

- **Auth:** OAuth 2 **device flow**. `POST https://api.trakt.tv/oauth/device/code`
  with `client_id`; poll `POST /oauth/device/token` at the returned `interval`.
  Tokens cached at `~/.config/fulcra-media/trakt.json`. **Access tokens last
  24h** (changed 2025-03-20); refresh tokens rotate on every use — replace
  immediately on refresh.
- **Fetch:** `GET /sync/history?extended=full&limit=100&start_at=<last>&page=N`
  paginated until the response is empty. `extended=full` returns `runtime` in
  one round trip. Paging headers: `X-Pagination-Page`, `X-Pagination-Page-Count`.
- **Required headers** on every call:
  ```
  Authorization: Bearer <token>
  trakt-api-version: 2
  trakt-api-key: <client_id>
  Content-Type: application/json
  ```
- **Rate limits:** GET 1000/5min, POST 1/sec; well under threshold even for full
  backfills.
- **Maps to:** Watched, service tag `trakt`.
- **Note construction:**
  - Movie: `"{movie.title} ({movie.year})"`
  - Episode: `"{show.title} S{episode.season:02d}E{episode.number:02d} – {episode.title}"`
- **Duration:** `runtime` (minutes) → `end = watched_at + runtime*60s`.
- **Idempotency:** Trakt history `id` is stable and unique per watch event →
  `com.fulcra.media.trakt.history.<id>`. Trakt's per-event `id` cleanly
  preserves rewatches (three watches of one movie = three rows = three
  annotations).
- **Timestamp confidence handling.** Trakt history can contain clusters of
  synthetic timestamps from signup-import flows (e.g. all watches stamped at
  account-creation time, or at the moment a CSV was imported, or at the moment
  a new streaming-service integration was linked). User-observed pattern
  (2026-05-16): **older Trakt history backfills accurately**; the
  inaccurate-timestamp problem concentrates on the **signup day and the day
  after each new service-link event**, where Trakt re-stamps that service's
  retroactive plays to the link date. Same play often appears twice: once
  with the correct old timestamp (e.g. from Hulu) and once with the synthetic
  link-day timestamp (e.g. from Apple TV). The importer must handle both
  cluster-flagging and same-Trakt-account near-duplicate detection.

  The importer:
  1. Honors `--trakt-from DATE` to drop everything older than the user's
     "clean-data" cutover date.
  2. Auto-detects clusters: if ≥5 history items share `watched_at` to the
     second, all members are flagged `timestamp_confidence: "low"` and tagged
     with `external_ids.timestamp_cluster_size = N`.
  3. **Cross-source-within-Trakt near-duplicate detection.** After cluster
     flagging, walk the history and group rows by content identity — for
     episodes that's `(show.ids.trakt, episode.season, episode.number)`; for
     movies that's `movie.ids.trakt`. Within each group, if one row is in a
     low-confidence cluster AND another row has higher-confidence
     authoritative timestamp AND the higher-confidence row is older, mark
     the cluster row `external_ids.dropped_low_confidence_duplicate = true`
     and skip ingest for it. Surface the dropped count in the import summary
     (`fulcra-media import trakt` should print `dropped=N` alongside the
     usual `posted/skipped/verified` counts) so the user can audit.
  4. Records `external_ids.trakt_action` from the API (`scrobble` / `checkin` /
     `watch`). `scrobble` and `checkin` get `timestamp_confidence: "high"` by
     default; `watch` gets `medium` (often used for retroactive backfills with
     guessed timestamps).
- **Apple TV / streaming coverage:** documented in README — user enables
  Trakt's "Streaming Scrobbler" (VIP, covers Apple TV+, Netflix, Disney+, Prime,
  Hulu, Max, Paramount+ natively), or installs the official Trakt tvOS app, or
  Infuse with Trakt integration. This importer just reads whatever lands in
  Trakt — it doesn't care how watches got there. Out of scope for code; in
  scope for README.

### 3.2 Netflix — `fulcra-media import netflix <path-to-csv>`

Netflix ships its viewing CSV in **two very different shapes** — auto-detect by
header and branch.

**Recommended source: the GDPR data export.** Walk the user through it via
`fulcra-media wizard netflix` (see §6). The export comes from
`https://www.netflix.com/account/getmyinfo` and takes up to 30 days. 10
columns, UTC timestamps, durations, profiles, devices.

**Common-case fallback: the in-app per-profile download** from
`netflix.com/account/viewingactivity` → "Download all" (Netflix help:
<https://help.netflix.com/en/node/101917>). Date-only, two columns. Covers
full account lifetime (verified ~16 years deep) but with significant
precision loss.

#### 3.2a Rich variant (GDPR export)

- **Header:**
  ```
  Profile Name,Start Time,Duration,Attributes,Title,Supplemental Video Type,
  Device Type,Bookmark,Latest Bookmark,Country
  ```
- **Filter:** drop rows where `Supplemental Video Type` is non-blank
  (trailers, hooks, promotional). Real watches have it blank.
- **Timestamps:** `Start Time` is `YYYY-MM-DD HH:MM:SS` **UTC**.
- **Duration:** `Duration` is `H:MM:SS` — parse to seconds; end = start +
  duration. This is **time watched in that session**, not title runtime.
- **Title parsing:** split `Title` on `": "` — single piece = movie; multi-piece
  = `Show: Season X: Episode title`.
- **Idempotency:** `sha256(profile | start_time | title)` →
  `com.fulcra.media.netflix.<sha16>`. Each pause/resume session is its own
  row in Netflix's data, so each becomes its own annotation.
- **Timestamp confidence:** `high`.

#### 3.2b Slim variant (in-app per-profile download)

- **Header:** `Title,Date` only.
- **Date format:** `M/D/YY` (US format, two-digit year). Parse with explicit
  format strings to avoid locale ambiguity. **No time, no timezone, no
  duration, no profile, no device.**
- **Title parsing:** same `": "` split as rich variant. Handle malformed rows
  with leading colons (e.g. `" : Episode 10"`) — show name absent; store raw
  title and emit a warning, don't crash.
- **Synthetic time interval:** because the variant has no time:
  - `start_time = <date> at 21:00:00 UTC` (evening default — reasonable for
    most users; flagged as synthetic)
  - `end_time = start_time + estimated_duration` where estimated_duration is
    `30min` for content with `": Season"` or `": Episode"` patterns,
    `100min` for titles with no colon (heuristic movie), `45min` otherwise.
  - `data.timestamp_confidence: "low"` and
    `data.external_ids.time_estimated: true`,
    `data.external_ids.duration_estimated: true`.
- **Idempotency for same-day rewatches** (the bug found in real data — 27
  exact `(Date, Title)` duplicates exist over 16 years):
  `sha256(date | title | occurrence_index)`, where `occurrence_index` is the
  count of prior rows in the file with the same `(date, title)` pair. This
  is deterministic per export and reasonably stable across re-downloads
  (Netflix's CSV is most-recent-first; older entries' positions don't shift
  when new ones are appended).
- **Timestamp confidence:** `low`.
- **README warning:** the slim variant loses real precision; encourage the
  user to request the GDPR export and re-import. Both writes are safe — the
  rich variant's source IDs include `start_time` and don't collide with the
  slim variant's date-based keys, so re-importing the same content with
  better data produces *additional* annotations rather than overwrites. The
  user can then delete the slim-variant annotations via Fulcra (tagged
  `netflix` + filter by `external_ids.time_estimated == true`).

#### 3.2 (common)

- **Maps to:** Watched, service tag `netflix`.

### 3.3 Apple Podcasts — `fulcra-media import apple-podcasts`

- **Input:** macOS-only. Reads
  `~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts/Documents/MTLibrary.sqlite`.
  Snapshot the DB + `-wal` + `-shm` sidecars to a tempdir before opening
  (Podcasts.app holds locks). Refuse to run if Podcasts.app is up unless
  `--force-live` is set.
- **Schema** (Core Data, `Z`-prefixed tables/columns):
  - `ZMTEPISODE`: `ZTITLE`, `ZCLEANEDTITLE`, `ZPODCAST` (FK), `ZUUID`,
    `ZDURATION` (seconds), `ZPLAYHEAD`, `ZPLAYSTATE` (0/2/3), `ZHASBEENPLAYED`,
    `ZPLAYSTATEMANUALLYSET`, `ZLASTDATEPLAYED`, `ZLASTUSERMARKEDASPLAYEDDATE`.
  - `ZMTPODCAST`: `Z_PK`, `ZTITLE`.
- **Query:**
  ```sql
  SELECT
    e.ZUUID                                              AS episode_uuid,
    datetime(e.ZLASTDATEPLAYED + 978307200,'unixepoch')  AS completed_at_utc,
    p.ZTITLE                                             AS show_title,
    COALESCE(e.ZCLEANEDTITLE, e.ZTITLE)                  AS episode_title,
    e.ZDURATION                                          AS duration_seconds
  FROM ZMTEPISODE e
  JOIN ZMTPODCAST p ON p.Z_PK = e.ZPODCAST
  WHERE e.ZPLAYSTATE = 3
    AND e.ZHASBEENPLAYED = 1
    AND e.ZPLAYSTATEMANUALLYSET = 0
    AND (e.ZPLAYHEAD * 1.0 / NULLIF(e.ZDURATION, 0)) > 0.9
    AND e.ZLASTDATEPLAYED IS NOT NULL;
  ```
- **Time format:** Core Data Mac absolute time — seconds since
  `2001-01-01 00:00:00 UTC`. Add `978307200` to convert to Unix epoch.
- **Maps to:** Listened, service tag `apple-podcasts`. Note format
  `"{show_title} – {episode_title}"`. End = `ZLASTDATEPLAYED`; start = end -
  duration.
- **Idempotency:** `sha256(ZUUID|ZLASTDATEPLAYED)` →
  `com.fulcra.media.apple-podcasts.<sha16>`. Including `ZLASTDATEPLAYED` in the
  key means **replays are captured** as new annotations across importer runs —
  each time the DB shows a fresher last-played stamp, that becomes a new event.
  The trade-off: if the user replays twice between importer runs, only the
  most recent replay is recorded (the DB overwrites; it doesn't keep a play
  log). Recommend a launchd job that runs hourly to minimize collapsed
  replays.
- **Timestamp confidence:** `medium` — the timestamp is real but only reflects
  the last play; we cannot prove the episode wasn't replayed without our
  observation.
- **Documented fragility in README:**
  - Auto-delete-after-played removes rows entirely — history is lost.
  - Episodes from unfollowed shows get pruned.
  - Replays between importer runs collapse to one (mitigated by running
    frequently — hourly launchd).
  - One annotation per distinct (`ZUUID`, observed-last-played-stamp) tuple.

### 3.4 Spotify — two paths

**`fulcra-media import spotify-extended <path-to-zip>`** (one-shot backfill):

- **Input:** Zip containing `Streaming_History_Audio_<YYYY>_<n>.json` files
  from Spotify's **"Extended Streaming History"** GDPR export (covers entire
  account lifetime; turnaround typically 1–5 days). Do **not** use the
  shorter "Account data" 1-year export — it omits the fields we need.
- **JSON keys (verbatim):** `ts`, `platform`, `ms_played`, `conn_country`,
  `ip_addr`, `master_metadata_track_name`, `master_metadata_album_artist_name`,
  `master_metadata_album_album_name`, `spotify_track_uri`, `episode_name`,
  `episode_show_name`, `spotify_episode_uri`, `reason_start`, `reason_end`,
  `shuffle`, `skipped`, `offline`, `offline_timestamp`, `incognito_mode`.
- **Filter:** `ms_played >= 30000 AND skipped != true`.
- **Timestamps:** `ts` is ISO 8601 UTC at **stream end**. Start = `ts -
  ms_played`.
- **Music vs podcast:** populated `spotify_track_uri` → music
  (`"{artist} – {track}"`); populated `spotify_episode_uri` → podcast
  (`"{show} – {episode}"`).
- **Maps to:** Listened, service tag `spotify`. Distinguish kind in
  `external_ids.kind` (`"music"` or `"podcast"`).
- **Idempotency:** `sha256(ts|spotify_track_uri|spotify_episode_uri)` →
  `com.fulcra.media.spotify.<sha16>`. Each stream is its own row in Spotify's
  data, so replays become separate annotations naturally.
- **Timestamp confidence:** `high`.

**`fulcra-media import lastfm <username>`** (ongoing capture for music):

- **Why both:** Spotify's Web API `/me/player/recently-played` is **hard-capped
  at 50 items** and the cursor cannot reach further back — useless for ongoing
  capture if the user listens to more than ~50 tracks between runs. Last.fm
  scrobbles Spotify in real time (when the user has connected Spotify→Last.fm),
  has no item ceiling, and uses trivial API-key auth.
- **Auth:** Last.fm API key only — no user OAuth needed for public scrobbles.
  Stored at `~/.config/fulcra-media/lastfm.json`.
- **Fetch:** `GET https://ws.audioscrobbler.com/2.0/?method=user.getRecentTracks
  &user=<username>&api_key=<key>&format=json&limit=200&page=N&from=<unix>`.
  Track watermark by `track.date.uts`.
- **Maps to:** Listened, service tag `lastfm` (distinct from `spotify` because
  Last.fm covers other apps too, and lacks `ms_played` / `skipped`). Note format
  `"{artist} – {track}"`.
- **Duration:** Last.fm has no duration field. Use a fixed nominal end-time
  (start + 3min) and flag `external_ids.duration_estimated = true`. Document
  the limitation.
- **Idempotency:** `sha256(date.uts|artist|track)` →
  `com.fulcra.media.lastfm.<sha16>`. Each scrobble is per-event, so replays
  become separate annotations naturally.
- **Timestamp confidence:** `high`.
- **Podcast gap:** Last.fm does **not** scrobble podcasts. Podcast capture
  going forward requires re-running `spotify-extended` against periodic
  re-requests of the GDPR export. Documented limitation.

### 3.5 Apple Data & Privacy takeout — `fulcra-media import apple-takeout <path>`

- **Input:** Path to either the unzipped `apple_data_export/` tree, the
  `apple_data_export.zip` itself, or the inner `Apple Media Services
  information/Apple TV/Playback Activity.csv` directly. The first form is the
  common case (just point at the export).
- **CSV header (verified against a real export sample):**
  ```
  Event Type, Content Type, Title, Episode Title, Season Number,
  Episode Number, Start Time, End Time, Play Duration (Seconds),
  Device Type, Device Model, Country
  ```
- **Filter:** `Event Type == "PLAY"` only (drop `PAUSE` / `RESUME` rows;
  they're sub-events of the surrounding play).
- **Note construction:**
  - Movie (`Content Type == "Movie"`): `"{Title}"`
  - Episode (`Content Type == "TV Episode"`):
    `"{Title} S{Season Number:02d}E{Episode Number:02d} – {Episode Title}"`
- **Time interval:** use `Start Time` and `End Time` directly (already a real
  interval, not start + duration math).
- **Timestamp confidence:** `high` (real-time playback events with explicit
  start/end).
- **Open question to verify against a real export:** whether `Start Time` /
  `End Time` are UTC or local. The CSV has no TZ marker. Inspect the export
  for any DST-boundary watches or compare timestamps to known watches in
  other timezones to determine. Default assumption: UTC; flag in code with a
  `--apple-tz-assumed-utc / --apple-tz LOCAL` switch.
- **Maps to:** Watched, service tag `apple-tv`.
- **Idempotency:** `sha256(Start Time | Title | Episode Title | Device Model)`
  → `com.fulcra.media.apple-takeout.<sha16>`. Same title rewatched on
  different evenings produces different keys. Same title rewatched on the
  same device starting at the exact same minute would collide — but that's
  not a real-world scenario.
- **External IDs:** `device_type`, `device_model`, `country`.
- **Relationship to Trakt:** the user runs Apple takeout for historical
  backfill (one-shot when the export arrives) and Trakt for ongoing capture
  (via Streaming Scrobbler). Pick a cutover date and use `--trakt-from
  <cutover-date>` to prevent overlap. No automatic cross-source dedup.

## 4. Auth & state

- **Fulcra auth:** shell out to `fulcra auth print-access-token` (provided by
  the `fulcra-api` package on the `add-cli` branch). Same approach as the
  existing skill. No new Fulcra-side login UX.
- **Per-importer auth:** cached under `~/.config/fulcra-media/<source>.json`.
  Trakt is OAuth device flow; Last.fm is a one-time API key stash; the others
  read local files and need no auth state.
- **Local state:** `~/.config/fulcra-media/state.json` holds:
  - Cached annotation definition IDs (`watched_id`, `listened_id`)
  - Cached tag UUIDs (`media`, `watched`, `listened`, `netflix`, `trakt`,
    `apple-podcasts`, `spotify`, `lastfm`)
  - Per-importer high-water marks (latest Trakt `watched_at`, latest Last.fm
    `date.uts`, last Apple Podcasts `ZLASTDATEPLAYED`, etc.) for incremental
    runs.

## 5. Dedup & verification

The Fulcra API has **no idempotency key** documented (`/ingest/v1/record`
returns 204 with no body). Approach is **within-source only** — we never
auto-merge across importers.

### Within-source dedup

Each importer's idempotency key includes a **per-event timestamp component**,
not just content identifiers. This is critical:

- Three watches of the same movie via Trakt → three Trakt history `id`s →
  three annotations.
- Pause/resume across days in Netflix → multiple `ViewingActivity` rows →
  multiple annotations (each is a real session).
- A podcast episode replayed across importer runs → fresh `ZLASTDATEPLAYED`
  each time → fresh annotation each time.

The pipeline:

1. Group normalized events into chunks of ~500.
2. Per chunk, compute window `[min(start), max(end)]`.
3. `GET /data/v1alpha1/event/DurationAnnotation` over that window (plus a
   10-minute pad on each side); collect existing records' `source` arrays.
4. Skip any event whose deterministic source ID already appears.
5. `POST /ingest/v1/record/batch` (JSONL, `Content-Type:
   application/x-jsonl`) with the remaining events.
6. Re-query the same window and report `imported / verified / skipped` counts.
7. Refuse to claim success unless verified ≥ imported (matches the existing
   skill's `verified_matches >= 1` convention).

### Cross-source dedup

**Not automatic.** If you run two importers that overlap in time and content
(Apple takeout + Trakt for the same Apple TV+ watch; Spotify Extended +
Last.fm for the same music play), you will get duplicate annotations. The
intended pattern is **non-overlapping source windows**:

- Use the high-fidelity takeout for the historical window (one-shot).
- Switch to the ongoing/realtime source from a clean cutover date.
- Apply the cutover via `--trakt-from`, `--since`, or by simply not running
  the second importer for the backfilled window.

This is deliberate — automatic cross-source merging would either be too eager
(collapsing legitimate distinct watches) or require fragile heuristics. The
user controls the windowing; the importer respects it.

(Future enhancement, deferred: an optional `--merge-pauses-within=Nmin` flag
on Netflix specifically, to coalesce pause/resume rows for the same title
inside a window. Not building it now — pause/resume as separate sessions is
the truthful representation.)

## 6. CLI surface

```
fulcra-media bootstrap                                       # create Watched/Listened defs + service tags
fulcra-media auth trakt                                       # device-flow setup
fulcra-media auth lastfm                                      # api-key stash

fulcra-media wizard netflix                                   # walk through requesting + uploading export
fulcra-media wizard spotify                                   # ditto, Spotify Extended History
fulcra-media wizard apple-takeout                             # ditto, Apple Data & Privacy
fulcra-media wizard trakt                                     # walks user through Streaming Scrobbler etc

fulcra-media import trakt           [--trakt-from DATE] [--since DATE] [--dry-run]
fulcra-media import netflix          <path>                  [--dry-run]
fulcra-media import apple-podcasts                            [--dry-run] [--force-live]
fulcra-media import spotify-extended <path>                  [--dry-run]
fulcra-media import lastfm           <username> [--since DATE] [--dry-run]
fulcra-media import apple-takeout    <path>     [--apple-tz {utc|local}] [--dry-run]
fulcra-media status                                          # state.json + last run per source
```

**Path arguments accept three forms:**
- Local file path: `~/Downloads/NetflixViewingHistory.csv`
- Local directory (auto-detect the relevant file inside): `~/Downloads/apple_data_export/`
- Fulcra Library URI: `fulcra:/takeouts/NetflixViewingHistory.csv` — resolved
  by shelling out to `fulcra file download <remote>` into a tempfile.

`--trakt-from` (Trakt-only) drops events before the cutover date entirely; used
to skip Trakt's well-known synthetic signup-import clusters. `--since` is a
soft watermark used for incrementals.

### The top-level setup experience

The end product **must not require the user to already know which subcommand to
run**. The default entry point is `fulcra-media setup` (also reached by running
`fulcra-media` with no args after a future iteration), a menu-driven flow:

1. **Pre-flight** — checks Fulcra auth (prompts `fulcra auth login` if missing),
   runs `bootstrap` if the Watched/Listened definitions don't exist yet.
2. **Service status table** — lists every supported service with its current
   state:
   ```
     Service           Auth/Source     Last import       Events ingested
     ----------------  --------------  ----------------  ---------------
     Netflix           CSV present     2026-05-16        6,456
     Apple takeout     no zip found    -                 0
     Trakt             not connected   -                 0
     Spotify Extended  no zip found    -                 0
     Apple Podcasts    DB found        -                 0
     Last.fm           not connected   -                 0
   ```
3. **Choice prompt** — "Which service do you want to set up next?" Selecting
   one routes into that service's wizard (existing `wizard <source>` commands
   become internal callees, not the user-facing surface).
4. **Per-service flow** (delegated to the existing wizards):
   - Explains the export options and tradeoffs.
   - Prints the canonical URLs / click sequences with upstream help links.
   - Tells the user the rough turnaround for human-in-loop steps (30 days
     for Netflix GDPR, ~5 days for Spotify Extended, instant for Apple
     Podcasts on-device, OAuth-now for Trakt/Last.fm).
   - **Once the data is in hand** (local file, Library URI, or live API),
     offers to upload to a canonical Library path and immediately invoke the
     matching `fulcra-media import`.
5. **Returns to the menu** so the user can set up the next service in the same
   sitting.

Power users skip `setup` entirely and call `import <source> <path>` directly.
The per-service wizards remain accessible via `wizard <source>` for users who
already know which one they want.

`--dry-run` emits JSONL of would-be records to stdout instead of POSTing.

**Status (2026-05-16):** the thin slice ships `fulcra-media wizard netflix`
only. The top-level `setup` command is deferred until ≥2 wizards exist (so
the menu has something to choose between). Tracking it as the next UX
milestone after the second importer lands.

## 7. Project layout

```
fulcra_media/
  __init__.py
  cli.py                  # Click entry point, top-level group + subcommands
  state.py                # ~/.config/fulcra-media/state.json read/write
  fulcra.py               # auth (subprocess), definition+tag bootstrap, ingest, verify, dedup
  library.py              # `fulcra:/...` path resolution via `fulcra file download`
  wizards/
    __init__.py
    netflix.py            # step-by-step CSV/GDPR walkthrough
    spotify.py            # Extended Streaming History walkthrough
    apple_takeout.py      # Apple Data & Privacy walkthrough
    trakt.py              # Streaming Scrobbler / Apple TV app setup walkthrough
  importers/
    __init__.py
    base.py               # NormalizedEvent dataclass + run-pipeline helper
    trakt.py
    netflix.py
    apple_podcasts.py
    spotify_extended.py
    lastfm.py
    apple_takeout.py
tests/
  fixtures/
    trakt_history_sample.json
    netflix_viewing_activity_sample.csv
    spotify_streaming_history_sample.json
    apple_podcasts_mtlibrary_sample.sqlite
    lastfm_recent_sample.json
    apple_takeout_playback_activity_sample.csv
  test_trakt.py
  test_netflix.py
  test_apple_podcasts.py
  test_spotify_extended.py
  test_lastfm.py
  test_apple_takeout.py
  test_fulcra.py
pyproject.toml             # depends on fulcra-api @ git+https://github.com/fulcradynamics/fulcra-api-python.git@add-cli, click, httpx, dateparser
README.md
```

`fulcra.py` is the only module that talks to the Fulcra API. Importers produce
`NormalizedEvent`s and never touch HTTP themselves — that makes them trivially
testable against fixtures.

## 8. Testing

- Per-importer unit tests against fixtures. No network.
- Round-trip test in `test_fulcra.py`: build a `NormalizedEvent`, run the
  dedup+ingest pipeline against an `httpx.MockTransport`, assert the exact
  `/ingest/v1/record/batch` JSONL payload shape.
- Manual smoke: each importer in `--dry-run` against real local data, then a
  small real run with `bootstrap` against a throwaway test annotation.

## 9. Out of scope (named, not built)

- Web/mobile UI.
- Real-time push — everything is poll/file-driven.
- Migration of these importers into the official Fulcra CLI — deferred until
  the CLI gains annotation-write commands; this package will then become
  shell-out adapters.
- Per-show enrichment (TMDB, MusicBrainz lookups).
- Per-play replay capture for Apple Podcasts — the on-device DB physically does
  not store it.
- Universal Trakt Scrobbler / browser extensions / streaming-service scrobblers
  — user-side setup, documented in README, no code here.
- Plex, YouTube, Kindle, Goodreads — future importers slot into `importers/`
  with no shared-code changes.

## 10. Open dependencies on Fulcra

This design relies on `fulcra-api` from the `fulcradynamics/fulcra-api-python`
repo, currently spread across two unmerged feature branches:

- `add-cli` — `fulcra auth login`, `fulcra auth print-access-token`
- `file-commands` — `fulcra file list|stat|download|upload|delete`

We need **both**. Until they merge to `main`, options in priority order:

1. Pin to a branch that has both merged together (track upstream as the
   project's branches stabilize).
2. Vendor a fork in this repo that merges both.
3. Worst-case fallback: depend on whichever branch the user has installed and
   re-implement the missing surface ourselves (e.g. reach into `core.py` for
   library/upload if file-commands isn't present in the user's install).

Update the auth and library-resolution shell-outs in `fulcra.py` / `library.py`
if command names or paths change before merge.

## 11. References

- Fulcra OpenAPI: <https://api.fulcradynamics.com/openapi.json>
- Fulcra annotations skill: <https://github.com/arc-claw-bot/fulcra-annotations-skill>
- Fulcra CLI (add-cli branch): <https://github.com/fulcradynamics/fulcra-api-python/tree/add-cli>
- Trakt API: <https://trakt.docs.apiary.io/>
- Netflix data export help: <https://help.netflix.com/en/node/100624>
- Spotify extended history: <https://www.spotify.com/account/privacy/>
- Last.fm `user.getRecentTracks`: <https://www.last.fm/api/show/user.getRecentTracks>
