# Collect v1 migration status

October 24, 2026: the shared v1 Event/Metric record boundary is implemented on
the development branch, together with account-bound destination resolution and
durable datatype-creation recovery. PurpleAir now uses the guarded Collect
session and verified v1 Metric outbox on this branch. The 16 bundled media
plugins and retained Spotify backfill entrypoint now use the guarded v1 Event
runtime and setup. Standalone Day One and fourteen media import routes now use guarded v1
paths on this source branch. The standalone media webhook server now shares
guarded v1 authority too.
**The released 0.1.4 app and remaining record-producing families have not switched yet.**
This is intermediate source-only implementation, not rollout. Do not infer datatype version from an `/ingest/v1`
URL; legacy annotations use that path too.

The shared setup API and picker now support owned v1 destination selection,
schema proof, pending-upload checks, settings/epoch fencing and bounded latest-
entry previews. PurpleAir declares its six slots; media producers declare their
semantic Event slot, with generic CSV/RSS following the saved category.
Day One now declares a Journal Event slot and uses an account-bound outbox,
preserving its import-once behavior and original case-sensitive source hash.
Owned custom-type reads now use the
[shared authoritative input-service reader](../packages/fulcra-common/README.md#v1-datatype-migration-boundary)
for resolution, exclusion discovery and the picker; a partial aggregate catalog
cannot authorize creation or prove name uniqueness.

Native v1 Event and Metric deletion is verified through `DeletedRecord`, including exact-ID
and covering-window absence with an untouched control. A later same-ID replay
revives the record, so Undo/history rollout still needs durable dispositions
checked by every writer. The [platform mutation evidence](../FULCRA-PRIMITIVES.md#true-v1-custom-record-mutations)
records the precise scope; the application Undo path has not switched yet. The
shared client now submits schema-checked native tombstones with stable action-
scoped IDs. The development media/PurpleAir writers check queryable exclusions
before duplicate shortcuts and around pending uploads, repair observed revivals,
and count only verified non-excluded rows. The media chunk holds a local account
lease across twin planning and delivery. Other source writers and the Undo UI
still need this integration; historical aliases/replay floors, controlled cutoff
and durable action/pending recovery remain rollout gates.

The [quick-record protocol2 backend](../packages/collect/README.md#quick-record-protocol-2-development)
and [development native/web callers](../packages/menubar/README.md#quick-record-client-development)
now use owned per-track Events, durable actions/timers, verified native Undo and
account-scoped revisioned pins. Client requests are captured before socket IO;
recovery checks bind the original target and timer window. Saved pending work is
paginated beyond display caps, with old-epoch rows visible but unable to replay
without explicit recovery. Drafts and asynchronous view results are account/epoch
bound. Obsolete unversioned requests refuse before auth/effects, and their legacy
record/sentinel writers have been removed. Released 0.1.4 remains legacy until a
reviewed app publication. Explicit old-epoch reauthorization, packaged QA and
host cutover remain release gates.

The [design](superpowers/specs/2026-10-23-collect-v1-design.md) defines the
record, historical conversion, cutover and release acceptance gates. The
[implementation plan](superpowers/plans/2026-10-23-collect-v1-migration.md)
tracks execution. The [shared client](../packages/fulcra-common/README.md#v1-datatype-migration-boundary)
documents the opt-in contract.

## Source audit

This is a migration inventory, not a claim that conversion has shipped. The
[static plugin manifest](../packages/collect/fulcra_collect/_bundled_plugins.py)
contains all 26 entries. Shared pending-batch/readback helpers and the
[media v1 encoder](../packages/media-helpers/README.md#v1-migration-boundary-development-branch)
and guarded media import runtime are wired into the media plugins and their
setup UI. Every family below still needs its plan acceptance
evidence; no file-based source is converted into a redundant record stream.

| Plugins | Current output/path | V1 work and host checks |
|---|---|---|
| `lastfm`, `deezer`, `trakt`, `netflix`, `spotify-extended`, `youtube`, `apple-takeout`, `apple-music-takeout`, `generic-rss`, `letterboxd`, `goodreads` | Guarded v1 media Event writer on this branch | Destination setup, declared enrichment, scoped cursor/twin hints, durable batches and exact readback implemented; move API jobs/selected exports with state and verify real sources. |
| `apple-podcasts`, `apple-podcasts-timemachine`, `apple-tv` | Guarded v1 media Event writer from local databases/backups | Same runtime implemented; verify actual local libraries or selected backup availability on destination before source-host retirement. |
| `generic-csv` | Guarded v1 media Event writer | Selected category/column-map normalization preserved. The separate standalone CSV CLI now has its own native family schema and authority, described below. |
| `media-webhook` | Local receiver and guarded v1 Event ingest | Destination proof before listening, typed/verified/preview counters, request-thread serialization and service revision restarts implemented; external sender reachability and cutover ownership remain. |
| `dayone` | Guarded v1 Journal Events on this branch | Declared text/tag-label/metadata parity, explicit legacy mapping, preview, immutable batches, exclusions and exact readback implemented. Import-once cloud edits remain unchanged; attachments remain unuploaded. Standalone CLI now uses guarded v1 authority and recovery too; real-source/host/history QA remains. |
| `purpleair` | V1 Metrics on this branch | Implemented: owned per-measure types/units, strict Collect account/epoch checks, durable pending samples, exact readback and same-sample retry. Live sensor/network and packaged-app QA remain. |
| `apple-notes` | Selected Notes to Fulcra Files | Development sync/reconcile/writeback now inject Collect-only Files auth, source/account/settings guards and leased writes; account-scoped reconciliation cache. Paths/fences and cloud-free local preview retained. No record destination created; real library/OS and packaged cutover QA remain. |
| `apple-reminders`, `todoist` | Selected tasks in Fulcra Files with completion sync | Development callers now bind source reads/preview and Files HTTP to captured Collect principal/raw settings/epoch; Todoist also checks its source token. Existing task generations, journals, paths and final mutation leases retained. Destination native/provider access and packaged QA remain. |
| `gmail` | Selected emails to Files, optional coord pointers | Development caller now uses Collect-bound Files and embedded canonical coord transport, exact directive readback, in-flight consent checks and destination-scoped delivery state. Original source identities/paths and configured coordination datatype semantics stay unchanged; real provider/packaged cutover QA remains. |
| `local-folders`, `meeting-transcripts`, `notion-pages` | Selected files/pages to Fulcra Files | Preserve Files behavior and existing consent/hash/account guards; verify destination paths and provider sign-in. |
| `attention-relay` | Pointer plugin; current browser code writes directly to Fulcra | Migrate Chrome/Safari independent record encoders. Preserve browser installs, selected destinations and queued events on source host. Inventory actual installed version before assuming any relay is needed. |

The fourteen migrated media routes use [profile-local media configuration](../packages/media-helpers/README.md#standalone-v1-adapter-development-branch):
bootstrap saves `cli-media-v1.json` atomically, requiring explicit Event/UUID
mappings for retained legacy categories. Account validation precedes original
pending recovery and source access. Preview never drains pending work. Cached
status exposes UNKNOWN and size/digest evidence rather than row data; reset is
retired. CLI credentials
remain separate from Collect authentication.

Standalone [labs ingestion](../packages/labs/README.md) now writes actual v1
Metrics through independent `cli-labs` authority. Original pending targets are
witnessed/proven before new input; the new multi-marker batch is schema/conflict
checked then staged together. Exact destination metadata fixes canonical units;
raw unit/provenance and local-only PDFs remain. Clinical registry/conversions and
review gates are unchanged. Cached completed-current and recovery evidence stay
separate, unknown reads stay UNKNOWN, and old NumericAnnotation/cache writers
refuse. Real reports, installed rollout and historical conversion remain open.

Standalone [CSV import/bootstrap](../packages/csv-importer/README.md) now use
owned full Event/Metric targets, strict whole-input parsing, source-authorized
same-UUID enrichment merges and original pending recovery before new input.
Metric unit is exact destination metadata; nested source fields and raw identity
remain. Preview is auth/state free, error counts remain known lower bounds, and
old CSV record methods fail instead of falling back. Legacy export/admin are
retained audit surfaces; no native history/export or installed rollout is claimed.

The five [standalone poll routes](../packages/media-helpers/README.md#standalone-v1-polling-development-branch)
now use strict source scans, bounded credential reads and private scoped v1
cursors without legacy seeding. Public state/reason reporting distinguishes
committed progress from withheld successful delivery. The same five
[daemon routes](../packages/media-helpers/README.md#daemon-v1-polling-development-branch)
now select strict scans with separate v2 cursor scopes, no old-key adoption,
quiet consumed-secret rereads and a second authority guard before exact KV
readback. Source/installed-app acceptance remains a gate, and bounded fresh
scans may fail with UNKNOWN rather than seed incomplete progress.

All fourteen migrated CLI routes now preserve known current-input
counts in failed envelopes after cleanup; old pending recovery is excluded.
Error counts remain lower bounds, not a completed import or sync claim.

Standalone [Trakt](../packages/media-helpers/README.md#standalone-trakt-policy-development-branch)
now uses that authority with read-only candidate inspection, exact offered-pair
consent and fresh owned/current-input proof. Excluded highs cannot justify loose
skips; post-cluster totals include policy skips. Explicit ask is refused before
source in JSON/non-TTY; omitted twins keep and warn only for candidates there.
Trakt has no cursor; Collect policy precedence and normalization/source IDs remain.
Its [strict history retrieval](../packages/media-helpers/README.md#trakt-history-completeness-development-branch)
now requires complete self-consistent returned pagination before current delivery,
with bounded streaming and one cumulative cooperative budget across auth/retry.
Missing/contradictory source proof holds new import/progress; original recovery
and token rotation may already have occurred. This is not an atomic provider
snapshot. Its [Trakt-only source owner](../packages/media-helpers/README.md#trakt-source-ownership-development-branch)
now fences participating local credential edits/refresh and current work after
original recovery, retaining complete secret receipts for local reconciliation.
Ordinary workers stay Keychain-only; explicit CLI file mode remains separate.
Its [current provider identity checks](../packages/media-helpers/README.md#trakt-provider-identity-development-branch)
now require strict selected-credential probes before/after history, a durable
selection pin and terminal admission before normalization/current delivery.
Journal v2 preserves valid v1 source/grant state; mismatch stays held across
reconnect and profile changes. First pinning gives no historical attribution.
Live-provider, installed/signed access and old-writer retirement remain before
cutover; no cross-host lock guarantee follows.

The [standalone webhook](../packages/media-helpers/README.md#standalone-webhook-development-branch)
now recovers original pending work before bind and retains private profile leases
through owner-thread cleanup. Actual synthetic threaded HTTP tests cover native
v1 writes/readback, duplicates, uncertain pending recovery, authority holds and
lifecycle faults. Each request gets its own settled result; cached health proves
liveness only. Explicit ports remain selectable with no occupied-port fallback.
Installed service, real sender reachability and host cutover remain unverified.

The [unbound media record API](../packages/media-helpers/README.md#retired-legacy-media-writers-development-branch)
now refuses before auth or input evaluation. Its obsolete annotation creation,
wrapped/typed import pipeline and orphan CLI helper are removed, while the native
Collect bridge, read/admin/tag helpers and pure source conversions remain.
This retires a direct legacy bypass after migrating the actual callers; it does
not remove shared transport used by unrelated packages or establish rollout.

Standalone writers outside the manifest, including the
[native vendored Netflix script](../packages/netflix-skill/README.md), migrated labs importer and development Collect quick-record/delete
controls, are included in the writer audit. Pure file operations and unrelated agent-coordination records
retain their own storage contracts.

The Files-family audit identified Gmail in-flight consent and
ledger/cursor destination gaps, now addressed on this branch by the
[Gmail delivery contract](../packages/gmail/README.md#account-and-delivery-development-branch).
Task-sync preview/read and fresh-token checks are implemented in the
[development caller contract](../packages/task-sync/README.md#collect-account-and-read-consent-development-branch);
real provider/host and packaged QA remain cutover gates. Gmail's optional relay delegates to the
separately configured coordination stream; this migration does not silently
retype that shared stream or remove the selected relay action.

The opt-in [browser decision authority](../packages/attention/README.md#decision-authority)
now shares closed snapshot proofs while preserving fresh A/policy/source/B
observations, independent POST proof and original journal predicates. Retained
phase/terminal proof bytes match the prior actor. Synthetic latency tests and
isolated Chrome TLS/DNR/IndexedDB checks preserve uncertain-dispatch holds,
whole-browser restart and zero-HTTP terminal history. Every-bearer generation
publication, real-provider latency, installed/default browser and Safari checks
remain rollout gates.

## Historical completion

The [offline plan/raw ledger foundation](../packages/collect/README.md#offline-historical-migration-foundation-development)
now freezes account/mapping/schema/range and preserves raw versions/observations
with conservative capped-window coverage. Flat typed media records now have
an offline codec preserving declared fields and the shared live/history UUID
mapping. A separate v2 codec preserves API reader definition metadata and source
bindings in an opt-in audit-capable schema; default media specs remain frozen.
Unsupported shapes and conflicting target payloads quarantine. The
ledger freezes proposals and rechecks codec proofs on read. It performs no remote
export, upload or finalization; `historical_complete` remains false. Status marks
raw integrity as not scanned; consuming an entry verifies its raw hash.

The optional [Journal reader codec](../packages/dayone/README.md#opt-in-historical-journal-contract-development-branch)
now preserves observed MomentAnnotation rows in a separate opt-in Journal
contract with complete raw provenance and nullable missing enrichment. Existing
live Journal defaults and pending targets remain frozen. A static allowlist
dispatches codecs without adding a Collect dependency on Day One. This is
offline conversion only; schema creation, routing and historical apply remain
unimplemented gates.

A separate [raw HTTP reader](../packages/collect/fulcra_collect/migration_export.py)
retains private scoped manifests/bodies/EOF receipts before strict bounded JSON
parsing. It refuses compressed decoding, redirects, truncated/error responses and
scope changes. Saved bytes must match the receipt hash. Each receipt explicitly
denies provider-coverage proof. The paired library driver now resumes indexed
windows under an immutable explicit provider completeness contract, retaining
parent variants and splitting capped/unequal pairs atomically. Its bounded queue
report still denies final coverage. No default provider contract is asserted;
production export/application and cutoff reconciliation remain open. Sample
offline dry runs are not a migration.

Converters must prove bounded-read coverage, reconcile definitionless undo
markers, share identities with live writers, and finalize only after each old
writer's cutoff and late-upload delta sweep. Original records and private
rollback copies remain intact. A quarantined record or unresolved marker needs
an explicit disposition; a successful upload alone never proves migration.

The vendored Netflix native console/script now uses the shared watched spec and
`cli-netflix` authority. It recovers all proven original targets before source
access, preflights the whole new input and stages serial chunks of at most 500.
Only the in-flight chunk is durable; current complete-group lower bounds are
separate from recovery/exclusions. Slim still lacks profile identity and cannot
prove arbitrary overlapping rewatch correspondence. Native sharing, actual installed
import/app acceptance and historical conversion remain open. The
[agent guide](../packages/netflix-skill/skills/fulcra-netflix/SKILL.md) owns those
workflow boundaries.
