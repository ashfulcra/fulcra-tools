# FulcraMediaHelpers

Import what you've watched, listened to, and read into your
[Fulcra](https://fulcradynamics.com) account. This package supplies Collect's
media plugins and a standalone command-line tool.

It is part of the [unsupported, vibe-coded monorepo](../../README.md). The
sources have different ideas about what a timestamp means. This package tries
to preserve those differences instead of turning a tidy timeline into a false one.

## V1 migration boundary (development branch)

[`v1.py`](fulcra_media/v1.py) declares versioned semantic Event destinations and
encodes every normalized media field, including title, service, importer,
timestamp confidence, external IDs and duration. Live and historical converters
share the importer primary source identity. Cross-source fingerprints remain
separate provenance/policy inputs; they never replace the primary UUID mapping.
Old typed annotations that discarded enrichment may have genuinely unknown
values; the custom schema permits null instead of inventing them.

`media_history_spec(category)` is an opt-in audit-capable specification with its
own `collect.media.<category>.history.v1` key and `<Category> History` name.
It adds optional `legacy_record` provenance for the
[versioned offline history reader codec](../collect/README.md#offline-historical-migration-foundation-development).
Default `media_spec` fingerprints and prior creation intents stay unchanged.
A live source can use a compatible richer destination through an explicit pin;
source updates preserve the declared history provenance. No schema or existing
creation mapping is silently edited to add that field.

The codec validates raw enrichment against that declaration before rendering
notes or creating destinations/tags. Durations, ordering, twin windows and source
receipt times use UTC instants across DST folds/gaps. Per-record datatype schema
reconstruction was removed; the codec reuses a validator. Live source labels
are validated before metadata; a new overlong tag uses a stable short name while
the record's full service label stays intact.

[`collect_v1.py`](fulcra_media/collect_v1.py) adds the guarded media runtime:
`media_run` opens and closes one captured Collect session, resumes its durable
batch before fetching source data, then imports normalized
events through owned destinations, destination-scoped twin checks and exact
readback. True replays keep separate primary IDs; corrections retain user tags,
non-fingerprint provenance, external IDs and other declared custom fields.
Conflicting input identities or an unrelated existing primary identity fail
before that batch is written. Preview reads create no destinations/tags and do
not drain a pending upload.

An exact primary source alias already present in an owned copied record counts
as existing data. Fingerprint matches suppress only a proven different-importer
twin; an opaque source ID alone cannot prove that distinction. The bounded
source-window check does not replace historical canonical-identity bindings.

The `FulcraClient(collect_context=ctx)` compatibility bridge routes legacy-shaped
media calls to that captured writer, including webhook/typed entry points. It
cannot use CLI/environment auth or create legacy definitions. Its unbound record
writers now refuse before auth or input evaluation; use the configured standalone
v1 commands described below. All 16 bundled media plugins and the retained Spotify IFTTT
backfill plugin now activate this bridge on the development branch. Their
setup contracts select owned v1 Event destinations; the legacy resolver is
removed from their run functions.

V1 media cursors are scoped to the captured account, consent epoch and raw
saved settings in plugin KV. An old annotation watermark is retained for
historical mapping, never treated as proof of v1 delivery. Progress is committed only with the route-specific evidence described below;
strict daemon polls additionally withhold on configured caps and malformed rows.
Failed, revoked or preview runs cannot advance it. Switching account, source or
destination starts a separate scope.

The optional `auto-discard` policy uses the private
`v1-media-twins.sqlite` index of verified high-confidence record IDs, shared
across media plugins by account and destination. Every hint is reread in that
account and checked for matching enrichment before suppressing a low-confidence
twin. The legacy machine-wide JSON cache cannot authorize a v1 skip. Missing
hints are conservative; unreadable index state fails visibly. Preview does not
create the index. Keep this file with Collect's private state during cutover.
Each upload chunk's source/twin check through final readback shares the account's
local record mutation lease, preventing two plugin workers from both accepting
the same cross-source twin. Source candidates, existing twins and index hints
also obey queryable replay exclusions. The shared outbox checks policy around
uploads and repairs observed revivals with native tombstones; excluded rows do
not count as posted/verified and cannot seed the twin index. This is local
serialization, not an atomic transaction across browser hosts. Historical
identity bindings and old-writer cutoff remain required.

Webhook request threads serialize on the writer so one request cannot reconcile
or replace another request's in-flight batch. Only the outbox's exact-visibility
commit emits a v1 receipt; preview progress reports `would_post` separately.
The Collect webhook service proves its Watched destination before listening and
closes its server when the receive loop returns or fails. Its `/health` reply
reports `data_type_id` and `api_version: "v1"`; the development standalone
receiver now reports the same fields. V1 webhook replies/counters count verified records and
skipped duplicates separately, while previews return `preview: true` and
`would_post` with zero posted records. Incomplete/malformed delivery results
return 502. The webhook bearer credential is optional on loopback, required
before binding any non-loopback host. The daemon's
[service revision policy](../collect/README.md#what-it-does) replaces workers
after account/settings changes without overlapping old receivers.

An old selected annotation requires an explicit `v1_destination_ids` mapping;
the runtime does not replace that selection with a default. Explicit pins are
lookup-only and must pass owner/schema checks; default cached destinations keep
the creation journal's mapping protection. Legacy definition state is retained.

**Fourteen standalone imports now use guarded v1 Events on this
development branch: `netflix`, `spotify-extended`, `spotify-ifttt`,
`apple-podcasts`, `apple-podcasts-timemachine`, `apple-takeout`, `youtube` and
`generic-csv`, plus `lastfm`, `deezer`, `letterboxd`, `goodreads`,
`generic-rss` and `trakt`. The standalone webhook server also uses guarded v1 Events;
the released app has not switched.** Packaged-app/source QA and the historical
effective-state cutover gates remain before deploying this branch. The
[coverage matrix](../../docs/collect-v1.md) tracks execution.

### Standalone v1 adapter (development branch)

The internal [standalone adapter](fulcra_media/standalone_v1.py) provides
`open_media(settings, *, client=None, check_only=False)`. It uses explicit
CLI/environment authentication and the shared record kernel, independently of
Collect's saved sign-in or enabled sources. The fixed `cli-media` namespace is
isolated from daemon and Day One state within the selected Collect profile.
A bounded invocation lease and shared task/account-record leases serialize work
within that profile; the existing DB protocol and rollback requirements apply.

Write callers open the context before acquiring any source: original durable
pending uploads reconcile before it yields, including before an empty source.
Pass stable destination authority as settings; keep new source selections and
cursor arguments separate. Changed account/settings preserve pending evidence
and hold. `import_events` delegates to the same `MediaRecordWriter`, not a second
serializer or legacy transport. Explicit injected client transports cover both
identity proof and record requests.

Check-only is a strict boolean. It captures a detached `preview_only=True`
settings copy before session construction, enforcing the shared deep write
fence even if a caller directly invokes the writer. Caller settings and pending
writes remain unchanged; preview never drains or adopts them. Its fingerprint
intentionally differs from a write invocation. Preview can perform read-only
comparisons and initialize private runtime infrastructure.

Those fourteen routes select this adapter through [media_config.py](fulcra_media/media_config.py).
`fulcra-media bootstrap` atomically saves private `cli-media-v1.json` under the
[selected Collect profile](../collect/README.md#state-and-storage), with account, full v1 destination pins and retained
legacy category selections. A bounded settings lock surrounds the invocation
lease. Saved account validation precedes original pending recovery; recovery
precedes configuration changes and migrated source resolution, download, discovery
and parsing,
even for an empty source. Missing config with retained pending work holds. Keep
the original profile selection during recovery; a new profile cannot settle the
original pending work.

For each retained legacy category selection, supply an explicit mapping via
`--watched Event/UUID`, `--listened Event/UUID`, and/or `--read Event/UUID`.
All three categories are checked before metadata creation. Config saves are
atomic; legacy state, pending evidence and creation receipts are never reset to
accept a replacement pin. `fulcra-media reset` is retired for every flag combination.

`fulcra-media status` returns cached read-only `verification`, `profile`, `v1`
(saved configuration or null), `retained_legacy` (category-to-old-ID/null), and
`pending`. Unreadable sections become `{"state":"UNKNOWN"}`. Pending is `absent`
or UNKNOWN; retained payloads expose only `bytes` and `sha256`, never row data.
UNKNOWN is never all clear. Status does not authenticate, initialize/migrate
state or prove live ownership. Detached preview never drains pending work or
proves a source caught up; it may initialize private runtime infrastructure.

The standalone webhook uses the same saved authority as described below. This is intermediate source-only work,
not rollout. CLI credentials remain separate from Collect sign-in, with no fallback to it.
See the [adapter tests](tests/test_standalone_v1.py). Profile-local serialization
is not a transaction across hosts or profiles.

### Retired legacy media writers (development branch)

The [compatibility facade](fulcra_media/fulcra.py) retains its constructor,
read/admin helpers and tag metadata behavior. Without a captured Collect v1 run,
`ensure_definitions`, `ingest_batch`, `ingest_batch_typed` and `run_import` fail
before auth, event iteration, cached/empty success, preview or metadata effects.
`ensure_definitions` also refuses legacy setup inside Collect; use owned v1
destination setup. Direct Python callers must use the guarded runtime or the
configured v1 CLI rather than the old unbound record API.

The obsolete annotation-definition creation, wrapped/typed legacy media import
pipeline and orphan CLI `run_and_emit` helper are removed. The captured Collect
bridge, native importer-namespace classification and pure
[`NormalizedEvent.to_duration_event`](fulcra_media/importers/base.py) compatibility
conversion remain; the [native writer](fulcra_media/collect_v1.py) builds v1 Events
directly from normalized events. Source
identities, native duplicate/replay policy, legacy state files and unrelated
shared-client read/admin/transport behavior are preserved. This is source-level
retirement, not proof that an installed app or external Python caller has upgraded.

### Observed import progress (development branch)

`MediaRecordWriter.progress` and `StandaloneMediaRun.progress` expose an immutable
four-counter snapshot, or `None` before an invocation. It describes the writer's
last serialized invocation; it is not a caller-bound receipt for shared webhook
handlers. A dedicated standalone caller can read it after cleanup without
reopening the session. Reset precedes argument validation and input
materialization; recovering original pending batches never counts as new input.

On errors, counters are observed lower bounds and need not sum to `total`.
Only complete validated delivery groups contribute landed records. A group
that raises contributes no positive count even if it observed some rows before
failing. Earlier verified groups/chunks survive a later error, as do explicit
exclusions counted as skips. This snapshot does not authorize a receipt, cursor,
retry, pending clear or twin-index update. Existing exception classes and
once-only dispatch rules remain unchanged. Successful results retain their
existing counters; preview `posted` means would-post and `verified` stays zero.

The fourteen migrated CLI routes now retain these known counters in
`ok:false` error envelopes after cleanup. They save the run and successful
result before context exit, distinguish pre-invocation failures, and never count
old recovery as current delivery. Webhook requests instead receive their own
settled result; they do not use this shared last-invocation property.
Error stages are unchanged; JSON remains one line and failures exit 2.
The optional public `cursor` field is described below. See the [file/system CLI tests](tests/test_cli_error_progress.py),
[poll CLI tests](tests/test_poll_routes_v1.py),
[runtime tests](tests/test_media_progress.py) and
[outbox evidence boundary](../collect/README.md#observed-outbox-progress-development).

### Standalone v1 polling (development branch)

The internal [poll cursor helpers](fulcra_media/poll_cursors.py) and
[strict scan helpers](fulcra_media/importers/poll_scan.py) now serve all five
standalone poll commands through [poll_cli.py](fulcra_media/poll_cli.py).
Daemon polling has its [own contract](#daemon-v1-polling-development-branch).
The CLI selects strict scanning
and fully materializes fetch and normalization before delivery. Source importers
retain their existing defaults for other callers.
A fresh scan report finishes only after its fetch phase and normalization complete;
configured caps and malformed rows still withhold cursor progress. Invalid source
responses, resource-limit failures and incomplete scans are errors.

The private v1 cursor binds the logical four-key saved media authority, profile,
account, owned destination, importer/category and digests of the exact service
and source selection. Preview shares this logical scope while retaining its
separate read-only runtime fingerprint. A present malformed or mismatched cursor
fails even with explicit `--since`; legacy watermarks never seed it. Only an
actual current selected batch's returned writer result can support a decision.
Original pending recovery alone cannot advance it. Cursor commit uses the
existing session guard and immediately rereads consumed local credentials;
changed/unreadable credentials withhold progress. Initial API credential reads
and commit-time rereads share a bounded 64 KiB regular-file reader; invalid,
nonfinite or unreadable credential objects fail visibly. This sampled check is not an
atomic filesystem-consent guarantee. Unknown KV acknowledgement requires a
later read, not a fabricated advancement.

First v1 polls have no automatic legacy watermark seed. An explicit `--since`
selects a window without overlap. Otherwise, Last.fm and Deezer rewind the saved
v1 cursor by `--watermark-overlap-hours` (nonnegative; default one hour).
`letterboxd`, `goodreads` and `generic-rss` use a fixed one-hour overlap.
A valid existing cursor is still checked under explicit `--since`, and
older explicit selections cannot regress it. Available-history backfills remain
bounded by the strict limits below. A selected window does not certify older
history. RSS selection preserves source order after time filtering and before
`--max-entries` slicing.

Import envelopes append `cursor` after `errors`; unrelated commands return null.
Poll commands expose only `{state, reason}`. `advanced/complete` means a verified
current batch committed an increase; `unchanged/no-events` or
`unchanged/no-increase` invent no timestamp. `withheld` reasons are `preview`,
`source-changed`, `page-cap`, `entry-cap` or `normalization-skips`. Configuring a
cap withholds advancement even when the source ends before that cap. Successful
withheld delivery is `ok:true`, exit 0, and is not caught-up proof. Human output
names the reason explicitly. `new_watermark` appears only for a committed
increase. A committed decision survives later cleanup failure in an `ok:false`
envelope; uncertain KV acknowledgement never claims advancement. The public
generic RSS importer label is now the fixed `generic-rss`, rather than embedding
the selected service. Record service enrichment remains unchanged.

Strict scans accept at most 8 MiB raw bytes per response, 64 MiB cumulatively,
10,000 requests and 1,000,000 raw rows, with a 300-second cooperative deadline.
Exact resource boundaries are inclusive. Parsers are checked before and after
calls; there is no hard CPU/memory interruption guarantee. Encoded responses
and redirects are refused. Strict Deezer pagination exhausts validated trusted
next links instead of stopping at an old row; Last.fm validates pagination
metadata. These are fixture-tested rules, not live provider-compatibility proof.
Feeds must be recognized, parseable RSS/Atom. Existing URL enrichment in record
payloads remains; cursor digests are minimization, not encryption.

The [cursor tests](tests/test_poll_cursors.py) exercise the actual configured
runtime/writer, and [scan tests](tests/test_poll_scan.py) exercise source and
resource boundaries. Redirect responses fail strict poll imports. Real provider,
installed-app and daemon source QA remain separate gates. Available-feed
coverage cannot prove arbitrary backdated edits or history outside that feed.

### Daemon v1 polling (development branch)

The actual Collect entrypoints for `lastfm`, `deezer`, `generic-rss`,
`letterboxd` and `goodreads` now use [daemon_poll.py](fulcra_media/daemon_poll.py).
Other media entrypoints retain their existing lifecycle. This is source-branch
behavior; installed-app and real provider acceptance remain separate gates.

The captured Collect session recovers original pending work before entering the
source function on non-preview runs. Worker credential hydration occurs earlier;
this is not a claim that recovery precedes that hydration. Preview never drains
pending work. Every exit restores the worker's original legacy watermark and
removes temporary writer/adapter attributes. Strict fetch and normalization are
fully materialized before current delivery. Deezer exhausts the validated next
chain before time filtering; Last.fm validates page metadata. RSS selects rows
at or after its boundary, sorts ascending, then applies an entry cap.

Private plugin KV keys start `v2_daemon_media_poll_cursor:` and hash the captured
account, consent epoch, full raw saved-settings fingerprint, importer/category,
owned target, exact source/service selection and consumed credential digests.
The closed version-2 envelope has only `version`, `scope` and `watermark`, bounded
to 4096 UTF-8 bytes of canonical finite JSON. Persisted envelope types stay strict.
Old annotation and pre-strict v1 keys are preserved and never adopted, rewritten
or deleted. A fresh scope starts with `since=None`. Saved API progress rewinds
24 hours for Last.fm/Deezer; feeds retain their saved boundary. These daemon
rules are separate from standalone CLI overlap options. Valid saved TOML string
and integer wrappers are accepted locally; caps still reject bools, strings,
zero, negatives and floats without changing the captured raw settings.

Only the immediate actual writer result for the current selected scan can
justify advancement. Exact nonnegative counters must account for the selected
batch; recovered pending receipts and shared progress snapshots cannot substitute.
Writer-accounted skips are not a universal exact-existing-record proof. Preview,
any configured page/entry cap (even a naturally shorter source), malformed rows
or enrichment failures withhold advancement. Empty/no-increase scans invent no
timestamp. Strict resource bounds are the same as the standalone scan limits
above; a fresh initial history may exceed them and fail with UNKNOWN. No partial
seed, next-index checkpoint or implicit cutoff is created. Finite feeds cannot
certify all history or arbitrary backdated activity.

Immediately before committing an increase, the adapter rereads the prior cursor
and quietly rereads only the canonical consumed secret: Last.fm `api-key` or
Deezer `access-token`. Changed, absent or unreadable secrets withhold progress;
feeds perform no credential sweep. A second session guard follows that potentially
slow read, before KV mutation. These are sampled authority checks, not provider
account identity or atomic revocation guarantees. KV acknowledgement alone is
insufficient: exact authoritative envelope readback must confirm the write.
An ambiguous setter exception can be reconciled by that exact read; unknown or
mismatched readback retains uncertainty and prior evidence.

Existing progress events carry `stage=cursor`, `cursor_state` and `cursor_reason`.
States distinguish `advanced`, `unchanged`, `withheld` and `unknown`; an advanced
reason is `complete-returned-scan`. These diagnostics add no new dashboard
rendering contract. Holds after delivery preserve genuine imported counts,
receipts and pending evidence; they do not prove caught up. See the
[actual daemon tests](tests/test_daemon_poll.py) and
[worker integration tests](tests/test_collect_v1_wiring.py).

## Use with Collect

The [Mac installer](../../docs/collect.md#get-started-new-user) includes these
plugins. Open the dashboard, choose a source, and follow **Set up**. You may
need a service login, API key, local app permission, or an export file.
[Available plugins and release status](../../docs/collect.md#plugin-status).

The [source guide](../../docs/how-do-i-get-my-data.md) covers each pathway.
Spotify uses an extended-history export or Last.fm scrobbles; there is no direct
live Spotify plugin. The legacy `spotify-ifttt` importer is CLI-only.

The commands below are for source installations and agent workflows. Example
output uses synthetic data.

```
$ fulcra-media import lastfm --json
{"importer":"lastfm","ok":true,"total":3,"skipped_existing":0,"posted":3,"verified":3,"since_watermark":"2026-01-01T00:00:00+00:00","new_watermark":"2026-01-02T00:00:00+00:00","would_post":null,"errors":[]}
```

## Why this exists

Your media history lives across services with different APIs, export formats,
and timestamp conventions. A Spotify extended-history export, a Netflix
date-only row, and a local Apple TV cache are very different kinds of evidence.

The importers normalize supported sources into Watched, Listened, and Read events. Collect and all fourteen standalone import commands above
use v1 Events on this branch, as does the standalone webhook server. That lets an agent query the history
you actually imported across sources, including the provenance and timestamp
limitations, instead of sending you back through each service's export screen.

## CLI quickstart

Install the [workspace environment](../collect/README.md#running-from-source),
then run these commands from the repository root. The CLI does not need the
Collect daemon running. Login and bootstrap contact Fulcra; bootstrap resolves owned v1 Event destinations, creating defaults when safe.

```bash
uv run --package fulcra-media-helpers --extra fulcra-cli fulcra auth login
uv run --package fulcra-media-helpers fulcra-media bootstrap
uv run --package fulcra-media-helpers fulcra-media setup  # interactive; needs a terminal
```

## What's supported

| Service | Command | How |
|---|---|---|
| **Last.fm** | `import lastfm` | API key + username (no OAuth). Covers Spotify, Apple Music, Tidal, Amazon Music, SoundCloud, Pandora, YouTube Music via in-app or Web Scrobbler. |
| **Spotify** | `import spotify-extended` | Spotify's official Extended Streaming History GDPR export. |
| **Spotify (legacy)** | `import spotify-ifttt` | Pre-Extended-API back-history from a legacy IFTTT → Google Drive applet. |
| **Deezer** | `import deezer` | History API with an OAuth access token; requires working provider credentials. |
| **Netflix** | `import netflix` | Slim (in-app) CSV or full GDPR export — auto-detected. |
| **Trakt** | `import trakt` | Direct API. Catches Apple TV+ via Universal Trakt Scrobbler. Cluster handling + cross-source twin dedup built in. |
| **Apple Podcasts** | `import apple-podcasts` | macOS local SQLite (`MTLibrary.sqlite`). The Collect app needs Full Disk Access; a source install grants it to the terminal app. Add `apple-podcasts-timemachine` for replay recovery from Time Machine backups. |
| **Apple Music takeout** | Collect plugin `apple-music-takeout` | Apple Data & Privacy play-activity CSV. |
| **Apple TV on-device** | Collect plugin `apple-tv` | Local Watch Now cache; requires Full Disk Access for Fulcra Collect (or the source-run terminal app) and a recently opened TV app. |
| **Apple TV / TV+ takeout** | `import apple-takeout` | privacy.apple.com → Apple Media Services → Playback Activity CSV. |
| **Letterboxd** | `import letterboxd` | Public RSS diary feed. |
| **Goodreads** | `import goodreads` | Public RSS of the 'read' shelf. |
| **YouTube** | `import youtube` | Google Takeout `watch-history.json` (recurring 2-month exports supported). |
| **Plex / Jellyfin** | `webhook` (long-running) | HTTP server that accepts `media.scrobble` / `PlaybackStop` events and ingests them in real time. |
| **Anything with a CSV** | `import generic-csv` | Column-mapped import. IFTTT, Pipedream, hand-rolled — any timestamp + title source. |
| **Anything with an RSS feed** | `import generic-rss` | Same for RSS/Atom feeds. |

Collect’s Plex/Jellyfin setup includes a **Receiver port** field for both same-Mac
and LAN setups (default `8765`). Choose an unused port and replace `8765` in the
webhook URL examples with that value. The LAN bearer-token requirement is
unchanged. See the [receiver plugin](fulcra_media/plugins/media_webhook.py).

### Standalone Trakt policy (development branch)

`import trakt` now uses configured v1 authority and recovers original pending
work before file-based Trakt credential loading, refresh, history fetch or
normalization, including empty and all-cluster-dropped input. Existing current
and legacy connection paths below are unchanged. CLI credentials remain separate
from Collect sign-in. Context-entry holds use the `setup` error stage; keep
original configuration and pending evidence intact.

Cluster preprocessing keeps the existing `drop`, `sentinel:YYYY`, `keep` and
`ask` choices. Supplied syntax is validated before source access. Explicit
`--clusters ask` or `--twin-policy ask` with JSON or non-TTY input fails at
`args` before credentials/source, even with no clusters or twins. JSON is
noninteractive regardless of TTY. Omitted cluster policy asks on TTY only when
clusters exist; noninteractive clustered input requires an explicit choice.
Omitted twin policy asks on TTY when pairs exist; JSON/non-TTY keeps those
candidates and warns on stderr. No candidates means no warning or prompt.

Loose twin policy uses one shared proof path for inspection and filtering.
Existing SQLite hints require fresh captured-account/owned-destination record
readback, matching high confidence and fingerprint, and current exclusion checks.
Current-input high-confidence candidates are also supported and labeled as
current input; an excluded high cannot justify suppressing a low. The legacy
JSON cache is never authority. Prompt inspection is read-only and lists every
offered pair with its origin. A yes authorizes only those immutable, run-bound
exact pairs intersected with fresh proof during import. A removed/changed high
or a newly discovered pair cannot widen that answer. Empty approval suppresses
nothing. Explicit `auto-discard` uses fresh pairs without a prompt restriction.

`keep` bypasses loose policy only: deterministic identity, proven aliases,
exclusions and enrichment preservation still apply. Distinct Trakt history IDs
retain replay identity. Policy/cluster options and answers do not mutate saved
settings, consent epoch or original pending payloads. Internal invocation
arguments default to `None`, preserving existing Collect config/default policy
precedence. Preview uses read-only ownership/exclusion checks and never stages,
drains or creates destinations, tags, index entries, record state or cursors.
Actual source reads may still refresh credentials and retain their local
[ownership/receipt journal](#trakt-source-ownership-development-branch).

Envelope `total` is the selected post-cluster writer input, including loose-policy
skips; `skipped_existing` includes those skips and other writer-accounted skips.
Cluster drops are preprocessing and are outside that total. This changes the
legacy CLI's prefiltered-twin totals. Trakt returns null cursor and watermarks.
Known current counters survive ordinary failure and cleanup; recovery is not
current delivery. Preview retains the CLI plan convention (`posted`/`would_post`
are planned rows, `verified` is zero). See the [actual caller tests](tests/test_trakt_cli_v1.py)
and [shared writer](fulcra_media/collect_v1.py).

### Trakt history completeness (development branch)

The actual standalone import and ordinary Keychain-backed Collect worker select a
Trakt-local strict full-history scan. Public non-strict helper compatibility is
separate; it is never a fallback for these writers. This does not add a CLI
option, cursor migration or new cluster/twin/normalization policy. Current and
legacy [connection setup](#trakt-connections-and-api-apps) remain supported.

Every successful page requires one unambiguous value for all four pagination
headers: current page, effective limit, total pages and total items. Use the
returned effective limit, including a clamp below the requested 1000. Missing,
changing or contradictory metadata, premature empty/short pages, wrong page,
duplicate history IDs or ambiguous JSON fail before current-input import. Empty
requires explicit self-consistent metadata, not merely an empty body. Strict
IDs are unchanged positive JSON integers through `2**64-1`, excluding booleans;
this is a client safety grammar, not a new source-ID scheme. Returned rows are
fully materialized and normalized before the current writer/policy path.

Caps are inclusive: 8 MiB raw bytes per source response, 64 MiB across source
responses including HTTP errors, 10,000 source GETs, 1,000,000 inspected rows and
a 300-second cooperative deadline. Source GET accounting spans the
single allowed keychain 401 restart. CLI/file proactive auth and elapsed keychain
refresh consume the captured deadline. Pagination evidence restarts after refresh;
requests/bytes/rows and deadline do not. Responses stream from the
fixed endpoint without redirects and must use identity encoding; only exact 200
can supply history. An encoded/oversized/malformed response cannot trigger auth
refresh. Bounds are failures, never successful prefixes or new catch-up evidence.

Only a bounded actual 401 triggers the existing one-time keychain refresh; 403,
429, server errors and source-shape errors do not. Both rotated tokens persist
before any late deadline refusal or retry. Existing proactive file auth stays
proactive. Auth response processing is separate from source GET byte/row caps.
Timeouts use remaining budget, but synchronous I/O/parsing checks are cooperative,
not a hard wall-clock interruption or cancellation of an in-flight grant.

Current source failure prevents new import and cursor advancement. Original
pending recovery and successful auth rotation may already have happened; they
are not rolled back or counted as current history. Trakt still scans full history,
independently of the daemon's generic saved watermark; standalone cursor and
watermarks remain null. Existing sanitized stages/count semantics remain.
Do not erase pending evidence or start a new login merely because a pagination
proof failed.

These checks establish complete, self-consistent **returned pagination**. A
count-preserving concurrent provider change can evade them; this is not an atomic
account snapshot or live-provider compatibility proof. The local
[source owner](#trakt-source-ownership-development-branch) adds credential and
refresh continuity; [current provider identity](#trakt-provider-identity-development-branch)
is now checked separately. Participating-writer cutover remains a gate. A local
disconnect cannot recall an in-flight provider request.
See the [source tests](tests/test_trakt_strict_scan.py), actual
[CLI tests](tests/test_trakt_cli_v1.py) and [daemon tests](tests/test_trakt_plugin.py).

### Trakt source ownership (development branch)

The actual standalone CLI keeps its explicitly selected legacy credential file.
The ordinary daemon requires its complete Keychain setup and never falls back to
that file on missing, partial, inaccessible or disconnected credentials. A direct
wrapped-plugin fixture using a file is not evidence of worker selection. Existing
[legacy and current connection setup](#trakt-connections-and-api-apps), source IDs,
normalization and cluster/twin policy remain unchanged.

The Trakt-only [source owner](../collect/fulcra_collect/trakt_source.py) captures
the exact credential tuple, consent and source generation. Its private nonsecret
`~/.local/state/fulcra-trakt/journal.json` is rooted in the actual OS user's home,
independent of `HOME` overrides, Collect profiles and client IDs. Keep this journal
and its locks: consumed refresh-token fingerprints remain excluded even after a
successful attempt. Changing profiles, switching stores or removing a lock file
is not permission to retry a consumed grant. This protocol assumes rotating Trakt
pairs; it is separate from other providers' refresh semantics.

Manual credential edits revoke the previous source first, including same-value
edits. Access and refresh tokens must both be explicitly supplied in the same
pending edit with successful write and exact readback before it becomes ready;
attempted or failed fields never count. Verified pending values retain digests
and lose credit if they drift. Changing client ID resets that pending
pair. Deleting a required field disconnects. An ancillary edit can preserve only
the exact previously proved pair and cannot reconnect a disconnected source.
Explicit Keychain authorization occurs outside account/configuration/source
locks. The actual active-field save is bounded and quiet, after checking that
the same edit and profile still own publication; a delayed prompt cannot publish
over a newer disconnect or setup. The returned `ready`/`status` distinguishes
saved-but-incomplete from selectable.
OAuth setup captures the latest attempt, client ID, callback, verifier and state;
a late callback cannot overwrite a newer sign-in or restore withdrawn consent.

Refresh durably claims one application grant before sending it. The complete
returned credentials and exact predecessor go into a private secret receipt
before later authority/deadline checks: Keychain for the worker, an adjacent
private receipt file for explicit file mode. The nonsecret journal stores opaque
receipt/lineage evidence. Publication gates mixed mirrors, checks expected old
values, writes and exactly rereads the new tuple, then commits its successor.
The running binding advances only to its exact attempt's verified successor
with the same consent, including recovery; it cannot adopt a later profile's
replacement. Restart reconciles the original receipt locally; missing, corrupt,
contradictory or superseded evidence holds. Never erase the claim or repeat an
uncertain grant.
Neither a receipt nor local publication proves provider-side atomicity.

Current source reads, policy/import and subsequent current writes/progress check
captured source/profile/configuration and account authority. The wrapper installs
the local-only current-work guard after immutable original pending recovery and
keeps it through final cursor handling. Original recovery retains its original
authority/counts; preview never recovers writes. Quiet background receipt/mirror
writes follow [Collect's Keychain contract](../collect/README.md#credentials).

This proves participating local continuity. The [provider identity checks](#trakt-provider-identity-development-branch)
below add current-credential observations, without historical attribution.
Real-provider acceptance, installed/signed Keychain QA and retirement of copied
grants/old writers on other hosts remain required before cutover. Local locks
cannot fence another host. See the [owner tests](tests/test_trakt_source_authority.py)
and actual [caller tests](tests/test_trakt_source_callers.py).

### Trakt provider identity (development branch)

The actual CLI and Keychain-backed worker now bracket history retrieval with
strict GETs to `https://api.trakt.tv/users/settings`, reading the opaque
`user.ids.uuid` string described in [Trakt's API reference](https://developer.trakt.tv/?section=reference#v=1&endpoint=getUsersSettings).
The [identity helper](../collect/fulcra_collect/trakt_identity.py) binds each
observation to its detached exact credential/source/consent generation. A stale
response cannot pin a newer credential successor. The terminal accepted proof
is required before normalization, policy or current import; later write, progress
and cursor checks use local lineage/pin evidence. Original immutable pending
recovery keeps its original authority and counts.

The saved identity is keyed by the selected source store, independently of
profile, client ID and token changes. Keychain and the explicit CLI file remain
separate selections. The first observation is retained; a conflicting ID retains
the first contradiction and durably holds that selection. Reconnect, disconnect,
refresh, profile changes and later matching observations cannot clear it. There
is no automatic reset or account-change disposition in this slice. The first pin
proves an as-of credential observation, not the ownership of earlier records or
a provider-wide immutable snapshot. Two matching probes cannot exclude a
count-preserving account change between them.

The private source journal now uses strict version 2 with an `identities` map.
A complete valid version-1 journal upgrades atomically while retaining its source,
attempt and consumed-grant entries. Malformed, missing or unknown state holds;
it never becomes empty. Older version-1 readers reject the new format. Stop older
participating writers before this upgrade and retain the whole journal, locks
and secret receipts for rollback/reconciliation. Restoring an older journal alone
does not authorize replay. The journal contains private account observations;
keep it outside public artifacts.

Each identity probe has an eight-second **cooperative** deadline, opened before
authority and secret sampling, and a 1 MiB raw response/error cap. Requests and
bytes also consume the original shared scan budget. Strict UTF-8/JSON rejects
duplicate keys, invalid Unicode and nonfinite values; UUID here is an opaque,
nonblank string, not an RFC UUID assumption. Redirects, partial responses,
nonidentity encoding and ambiguous framing hold. `Content-Range` is refused;
`Transfer-Encoding`, when present, must be one `chunked` marker without
`Content-Length`. An absent length alone is valid. These checks do not promise
hard interruption of synchronous sampling or parsing.

The worker's one reactive refresh allowance is shared by an initial identity
401 or history 401 from the exact Trakt GET endpoints. It cannot respond to a
Fulcra authority failure. The complete grant receipt and exact successor persist
before identity reproof, which must pass before history starts or restarts.
Post-refresh or terminal identity 401, a second history 401, mismatch, malformed
responses and other errors hold without another grant. CLI auth retains its
existing expiry-driven proactive refresh and gains no reactive retry. All source
requests retain one cumulative budget. See the [identity tests](tests/test_trakt_identity.py)
and [actual caller tests](tests/test_trakt_source_callers.py). Synthetic source
QA does not establish live-provider, installed, signed or cross-host acceptance.

### Trakt connections and API apps

Verified against Trakt's published guides, app-form source, and deployed portal
assets on **2026-10-24**; no live account creation or OAuth grant was performed.
Trakt now separates
[My Apps](https://developer.trakt.tv/apps), where developers manage API apps,
from [Connected Apps](https://app.trakt.tv/settings/apps/connected), where users
manage account authorizations. The [legacy API apps URL](https://trakt.tv/oauth/applications)
is also shown in Collect's wizard for existing apps. Keep a working app and its
credentials; upgrading this plugin does not require recreating it.

The current [app-creation guide](https://developer.trakt.tv/docs/create-an-app)
requires a verified GitHub account and provides a Client ID.
Collect's [Trakt plugin](fulcra_media/plugins/trakt.py) asks for that ID and uses
[PKCE](https://developer.trakt.tv/docs/pkce) with the existing single-use OAuth
state checks. Sign-in sends the verifier and no Client Secret, including when
a legacy secret is already stored. New OAuth requests use `auth.trakt.tv`, as
specified in [Trakt's OAuth contract](https://github.com/trakt/trakt-api/blob/master/projects/api/src/contracts/oauth/index.ts).
Refresh preserves an existing stored secret when present and saves both rotated
tokens before retrying history. New sign-ins also save the exact callback URI
for refresh; older credentials retain their prior OOB refresh fallback.

**Choose App type: Development for a personal Collect app.** Trakt's
[deployed app form](https://developer.trakt.tv/_app/immutable/chunks/B9YQm6UR.js)
(public asset verified on the date above)
allows Development apps to use local HTTP callbacks (`localhost`, `127.0.0.1`,
or `[::1]`); only the owner and explicitly added users can sign in.
Production apps require HTTPS. The broader HTTPS advice in Trakt's PKCE guide
does not describe this Development-app exception. Register the exact callback
shown by Collect, including its host and port. Existing legacy apps can keep
their working registration. Trakt also documents a
[device flow](https://developer.trakt.tv/docs/authentication-oauth#device-code-flow)
for native/CLI tools, but Collect has not implemented that flow. No automatic
fallback changes the callback or disables PKCE.

Existing CLI credentials in `~/.config/fulcra-media/trakt.json` retain their
file format and credential-selection path; token refresh now also uses the
current `auth.trakt.tv` endpoint. The CLI has no `auth` command and does not read
Collect's keychain. Trakt's [authentication guide](https://developer.trakt.tv/docs/authentication-oauth)
warns that refresh tokens issued before its authentication migration may require
one new authorization if the provider rejects them. This is a provider token
limit, not a plugin configuration migration.

Apple Music takeout and on-device Apple TV are Collect plugins; this CLI has no
matching `import` command for them.

Run `fulcra-media import --help` for the live CLI list and `fulcra-media wizard <service>` for service-specific setup instructions.

## Categories

The three semantic categories have owned v1 Event destinations selected by `bootstrap`:

- **Watched** — TV, movies, video (Netflix, Trakt, Apple TV, Letterboxd, YouTube, Plex)
- **Listened** — music, podcasts (Last.fm, Spotify, Deezer, Apple Podcasts)
- **Read** — books (Goodreads)

Workouts are coming in a future revision against Fulcra's native workout data type; the `strava` importer module is in the repo but unwired from the CLI for now.

Where available, events carry content fingerprints for cross-source dedup.
The import pipeline compares these and deterministic source IDs against
readback; Trakt also offers a policy for low-confidence twins. Matching depends
on source metadata, so a shared title alone is not a promise that two events
will collapse.

### Standalone webhook (development branch)

The actual command is `fulcra-media webhook`, separate from `import` and with
no `serve` subcommand. After configuring the profile with `bootstrap`, choose
an available port explicitly, for example:

```sh
fulcra-media webhook --port 8766 --json
```

The default is `127.0.0.1:8765`. `--host` changes the bind address; `--port 0`
requests an ephemeral port and the ready line reports the bound port. There is
no automatic fallback when a port is occupied. `--bearer-token` is optional for
`127.0.0.1`/`localhost` and required before binding any other host. Clients send
`Authorization: Bearer <token>`; Plex may use the existing `?token=<token>`
fallback. Keep credentials private. The captured receiver token does not reload
during its lifetime. Collect's plugin port setting is a separate configuration.

Startup opens [configured media authority](#standalone-v1-adapter-development-branch)
on one private owner thread. Saved account validation and original pending
recovery, plus preparation of all three categories, finish before binding.
Startup holds preserve evidence and emit no ready event. The bounded-acquisition
settings, invocation and task/account-record leases remain held for the server's
lifetime; another CLI invocation using that authority can fail while it runs.
Stop it and await cleanup before changing its profile configuration. Legitimate
same-account token refresh requires fresh principal proof; account, origin,
profile, saved-configuration or journal changes hold subsequent writes.

HTTP handlers parse/authenticate independently, then submit defensive event
snapshots through an unbounded internal queue to the owner. Its session, SQLite/KV, writer and cleanup stay on that
thread. Accepted jobs serialize and each caller receives its own actual result
or error. Client disconnect does not cancel an accepted job. Closing stops
admission, drains accepted work and joins owner cleanup before successful
shutdown. A parser stalled before admission may finish later and receive 502;
it cannot access released authority. There is no accepted-job timeout, retry or
redispatch. An uncertain original upload remains pending for reconciliation.

JSON is a lifecycle stream, not one import envelope: `ready` follows recovery,
signal installation and a successful local `/health` handshake in the running
serve loop. SIGINT/SIGTERM after ready initiates cleanup. `shutdown` follows
successful cleanup and contains a synchronized counter snapshot; it does not
prove every response-only peer has finished. Failures exit 2 with a sanitized
stage (`args`, `setup`, `bind`, `ready` or `shutdown`); broken stdout permits only
a best-effort stderr error envelope. Missing output is not success.

`/health` exposes cached `data_type_id` and `api_version: "v1"`, plus counters.
Health/ready prove liveness, not fresh ownership or source coverage. Each write
still checks current authority. Replies count current verified records as
`posted`, and duplicates as `skipped`; recovery is not a new request count.
Malformed/incomplete results, including a missing v1 result, yield 502.
See the [owner](fulcra_media/webhook_owner.py),
[actual v1 HTTP/lifecycle tests](tests/test_webhook_v1.py) and
[CLI lifecycle tests](tests/test_webhook_cli.py). This source-only migration
does not establish installed receiver readiness or real Plex/Jellyfin delivery.

## For agents

The [fulcra-media skill](skills/fulcra-media/SKILL.md), linked from this
package's [AGENTS.md](AGENTS.md), describes the shell contract and importer
selection. It can be used by an agent runtime that can invoke `fulcra-media`.
For recurring work, use Collect's scheduler or your host's scheduler; installing
a skill alone does not schedule anything. Letta's intra-turn heartbeat flag is
not a recurring scheduler.

Key contracts:

- Every `import` command supports `--json` (one-line envelope) and `--check-only` (dry-run)
- The envelope schema is stable & append-only: `{importer, ok, total, skipped_existing, posted, verified, since_watermark, new_watermark, would_post, errors[], cursor}`
- Exit code 0 on `ok: true`, 2 on `ok: false`
- Errors carry a `stage` discriminator (`setup`, `auth`, `args`, `fetch`, `snapshot`, `import`) so agents can pick a recovery action

`--check-only` skips ingest, but still reads the source and Fulcra readback;
it is not an offline command. The long-running `webhook --json` command has a
separate [lifecycle stream](#standalone-webhook-development-branch): `ready` follows
the health handshake and `shutdown` reports counters after cleanup.

Periodic-invocation cookbook:

```python
import json, subprocess
# Each user-authorized source retains its complete configured arguments.
# Synthetic example; use the actual configured URL, service and category.
authorized_sources = [
    {"importer": "generic-rss", "args": [
        "https://example.com/media.xml", "--service", "example-feed",
        "--category", "watched",
    ]},
]
for source in authorized_sources:
    importer = source["importer"]
    res = subprocess.run(
        ["fulcra-media", "import", importer, *source["args"], "--json"],
        capture_output=True, text=True,
    )
    if not res.stdout.strip():
        raise RuntimeError(f"{importer}: no JSON result (exit {res.returncode})")
    env = json.loads(res.stdout)
    if res.returncode != 0 or not env.get("ok"):
        errors = env.get("errors") or [{"stage": "unknown"}]
        raise RuntimeError(f"{importer}: failed at {errors[0].get('stage', 'unknown')}")
    print(f"{importer}: {env['verified']} verified, {env['skipped_existing']} skipped")
    cursor = env.get("cursor")
    if cursor is not None:
        print(f"{importer}: cursor {cursor['state']} ({cursor['reason']})")
        if cursor["state"] == "advanced":
            print(f"{importer}: committed watermark {env['new_watermark']}")
        elif cursor["state"] == "withheld":
            # Preserve the envelope for investigation; this is not caught-up proof.
            print(f"{importer}: progress withheld; review the saved result")
```

Retained legacy CLI watermarks live in `~/.config/fulcra-media/state.json`
(override with `FULCRA_MEDIA_STATE`). The five migrated poll commands use the
[private profile-local v1 cursor contract](#standalone-v1-polling-development-branch)
instead; retained legacy timestamps never seed v1 progress. Export imports,
Trakt and Apple Podcasts do not advance these poll cursors. Collect supplies
its own separately scoped state and dedup claims through the plugin context;
the five daemon polls use the [strict daemon contract](#daemon-v1-polling-development-branch).

## Architecture highlights

- **Standalone poll progress** — [poll_cli.py](fulcra_media/poll_cli.py) combines strict scanning with [scoped v1 cursors](fulcra_media/poll_cursors.py); legacy [watermarks.py](fulcra_media/watermarks.py) remains for unmigrated callers.
- **Cross-batch twin policy** — migrated routes use the verified private SQLite index described [above](#v1-migration-boundary-development-branch); the retained legacy [JSON cache](fulcra_media/twin_cache.py) cannot authorize migrated-route skips.
- **Cluster preprocessing** (Trakt) — synthetic backfill timestamps get dropped, sentinel-dated, or kept per the [CLI choice rules](#standalone-trakt-policy-development-branch). Collect plugins retain their configured/default policy.
- **Timestamp provenance** — low-confidence Trakt clusters carry
  `[bulk import — time unreliable]`; other low-confidence events carry
  `[time unreliable]`. Netflix date-only rows use synthetic noon, while Apple
  TV Recently Watched rows use a fetch-time upper bound. These labels explain
  uncertainty; they cannot recover the actual viewing time. The note carries
  this distinction on legacy typed records; v1 Events also retain the explicit
  `timestamp_confidence` enrichment field.
- **Dedup and verification** — deterministic source IDs plus readback, with
  the shared v1 outbox on migrated routes. The old unbound media record pipeline
  is retired. Exact readback checks whether submitted v1 records
  became visible; a failed or incomplete verification is not proof of delivery.
- **JSON envelope** — agent-parseable output across every importer

The sibling [fulcra-csv-importer](../csv-importer/README.md) supplies shared
parsing and dedup helpers used by the generic importers.

## Security

- Standalone CLI credential files live under `~/.config/fulcra-media/` at mode
  0600; Collect's configured plugin credentials use its OS keychain instead.
- Exception strings containing auth-bearing URL params (`?access_token=…`, `?api_key=…`) are scrubbed before they land in `--json` error envelopes (via `cli_common.safe_exc_message`)
- Keep takeouts and other personal exports out of version control; follow the
  repository's [privacy rules](../../docs/PRIVACY.md).
- Probe scripts in `scripts/` require `--i-know-this-hits-prod` to run

## Development

```bash
# From the repository root:
uv run --package fulcra-media-helpers --extra dev pytest -c pyproject.toml packages/media-helpers/tests/ -q
uv run --package fulcra-media-helpers fulcra-media --help
```

The generic CSV fixture keeps source-column and duration-column arguments on
separate lines so credential detection does not join adjacent expressions.
Its parser behavior is unchanged. The repository's [Gitleaks configuration](../../.gitleaks.toml)
exempts only enumerated public schema/storage identifiers at exact paths;
synthetic credential controls beside them must still be detected. See the
[agent privacy rules](../../AGENTS.md#public-repository-privacy).

## Status

- The test suite covers synthetic importer data, dedup, wizard contracts, and
  simulated webhook payloads; it does not establish current access to every provider.
- Schema covers three categories (workouts pending native-type rework)
- Plex/Jellyfin account setup and real-device delivery require separate validation.
- Goodreads / Letterboxd: RSS endpoint quirks documented in their wizards

## License

Personal-use experiment; no package license is declared in [pyproject.toml](pyproject.toml).
