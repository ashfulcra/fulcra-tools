# fulcra-collect

The background process behind Fulcra Collect. It runs local and source-specific
connectors, keeps their progress, and serves the setup wizard and dashboard.
Use it when a source needs something running on your machine. Fulcra's API,
agent tools, and the Attention browser extension can work without it.

This is part of the [unsupported, vibe-coded monorepo](../../README.md), put
together by Fulcra's lawyer. A scheduler and a settings page make these
experiments easier to run; they don't turn them into a support commitment.

**Using Collect?** Start with the [Mac installer and setup guide](../../docs/collect.md#get-started-new-user).
Apple Notes is included. You do not need to install this Python package separately.
The [plugin status](../../docs/collect.md#plugin-status) lists what's available
and what's still being built.

The rest of this README is for contributors and source installations. The
[system specification](../../docs/coord/SYSTEM-SPEC.md) covers the contract
between Collect, Coord Engine, and the agent bus.

The daemon hosts
every Fulcra Collect *plugin* — the periodic importers, the long-lived
webhook receivers, the pointer plugins — under one
process, supervises them, exposes their state over a JSON API plus a
web UI on `127.0.0.1:9292`, and stores per-plugin state in a single
SQLite database at `~/.config/fulcra-collect/state.db`.

The [menubar app](../menubar/README.md) and [web UI](../web-ui/README.md)
control this daemon. Packages such as [media-helpers](../media-helpers/README.md),
[local-sources](../local-sources/README.md), and [notion](../notion/README.md) expose plugin entry points for it; some also
have standalone CLIs. A connector can adopt the plugin contract to reuse
scheduling, credential storage, the dashboard, wizard, and OAuth plumbing.

The local-source plugins bind uploads to Collect's signed-in account. They check
live configuration before each upload and copy verified regular-file snapshots;
changing a selection or disabling a source stops its pending work. Their
[README](../local-sources/README.md) explains the limits and source-install setup.

## V1 writer migration (development branch)

The [vendored Netflix CLI](../netflix-skill/README.md) now shares installed native
Event authority under `cli-netflix`, with optional owned Event pin and the shared
Watched journal. Capability checks precede auth/source; original recovery precedes
one source reader. Whole current input gets schema/primary-ID conflict proof
before serial <=500-record chunks; only the in-flight chunk is durable. Current
complete-group counts and recovery stay separate. Skill-folder-only copies can
check locally but cannot upload; native sharing, historical conversion and
packaged/installed acceptance remain separate gates.

Standalone [labs ingestion](../labs/README.md) now uses native per-marker Metrics
through independent CLI auth and `cli-labs`. Its private marker/spec witnesses
and immutable pending-target view prove all actual recovery destinations before
new JSON/PDF access; payload delivery remains per group. Whole new batches get
schema/current-primary conflict proof before staging, while canonical units come
from exact destination metadata. Local PDF archive and clinical validation stay;
current/recovery counts are separate complete-group evidence, with atomic local
status checkpoints and UNKNOWN on failed authority/reads. This does not establish
real report, app installation or historical migration acceptance.

Standalone [CSV import](../csv-importer/README.md#recovery-and-truthful-progress)
now uses the shared standalone session/outbox in its own `cli-csv` namespace,
with explicit CLI auth and full owned Event/Metric targets. It recovers original
pending work before new source access, keeps preview local, preserves compatible
enrichment and reports known progress separately from recovery. Collect's media
CSV plugin remains a separate caller with its existing parser defaults. This
source change is separate from installed-app and historical-conversion gates.

The five [strict daemon media polls](../media-helpers/README.md#daemon-v1-polling-development-branch)
now recover before entering source functions, use fresh v2 scopes without old
watermark adoption, and require current selected coverage plus quiet consumed-secret
rereads, a second authority guard and exact cursor readback. Configured caps and
malformed rows withhold progress; initial bounded scans may fail with UNKNOWN.
Preview and every exit preserve the original worker watermark. This source change
does not establish installed-app or real-provider acceptance.

[PurpleAir](../purpleair/README.md#what-lands-in-fulcra) uses
[`CollectRecordSession`](fulcra_collect/v1_session.py): a captured Collect-only
sign-in, account/configuration epoch, raw saved-settings snapshot, preview guard,
and the same account/config leases as selected-file uploads. Each HTTP request
rechecks fresh credentials; source disable/settings/account changes cancel stale
work. A silent token refresh is accepted only for the same captured account;
the request hook uses its fresh bearer and rejects redirects to another origin.
Interactive account changes still revoke the configuration epoch. Local progress
commits use the same guard. Tag lookup/creation uses the
captured Collect token and a shared account/name creation intent, so another
plugin cannot blindly repeat a tag POST whose response was lost. Read-only
resolution (`allow_create=False`) performs no destination/tag creation or local
creation-journal write. Normalized legacy runtime
settings remain distinct from the independent raw consent snapshot.

For importer planning, `resolve_tag(name, allow_create=False)` remains read-only
and never creates a missing tag or a new binding. After all planned rows satisfy
their schemas, `remember_existing_tag(name, expected_id)` can guard and reprove
an existing lookup, require the exact planned UUID and remember its compatible
binding without a tag POST. A changed/disappeared identity or revoked authority
holds. This preserves successful-import rename bindings without creating metadata
before schema proof; it does not authorize unknown tag UUID constraints.

Tag resolution revalidates a saved current-account tag by UUID, so a renamed tag
keeps its identity. For new source labels exceeding the API's 30-character tag
limit, containing a slash, or equal to `.`/`..`, it uses a stable short name with a digest while
retaining the complete label in record enrichment. Existing unsafe-name tags
are found through the current account's tag list first. Unresolved creation
intents never authorize a replacement POST, and previews do not write bindings.

### Collect authentication

Both `RunContext.collect_token()` and its compatibility alias `fulcra_token()`
read Collect's current sign-in without an environment/default-CLI fallback.
Silent renewal can use the CLI's refresh store only when the existing token,
candidate and final keychain read identify the same account, with a valid new
expiry. Missing/unknown credentials, another CLI account or an account-transition
gate require reconnecting in Collect. A concurrent interactive sign-in wins;
renewal holds the account lease through its final check and keychain write and
does not rotate epochs. HTTP 401 retries also bind renewal to the failed
request's principal. Gmail, Reminders and Todoist reject missing Collect auth
before constructing their source/writer, rather than permitting a downstream
default credential lookup. Explicit interactive sign-in may still change account.

New record writers must use this boundary, verify exact ID readback, and retain
pending samples across uncertain uploads. [`V1RecordOutbox`](fulcra_collect/v1_outbox.py)
is the reusable pending-batch helper: `drain()` before the next source fetch,
`stage()` an immutable batch before its POST, then `drain()` to reconcile exact
visibility before receipts. It uses the plugin-scoped record-outbox table and the daemon's
one-worker-per-plugin rule; an account/epoch mismatch or malformed/saturated
read keeps the batch pending and fails visibly. An existing pending batch cannot
be overwritten by a newly fetched sample. They must not use CLI/environment auth
fallback or reuse old annotation-definition IDs as v1 destinations. Keep
`v1-type-creations.sqlite` with the private Collect home during cutover. The
[coverage matrix](../../docs/collect-v1.md) tracks families still using legacy
writers; the released app has not adopted this development branch yet.

New shared and PurpleAir pending envelopes also retain a versioned source
dispatch checkpoint. The [controller](../fulcra-common/fulcra_common/record_delivery.py)
binds the original scope/payload, records the exact issued subset and serialized
body hash before the actual guarded POST, and retains positive exact source
evidence before native repair. Once issued, absent or partially visible records
stay pending without another source POST; later exact visibility can finish
without resubmission. An exclusion plus absence cannot finish an uncertain
issued ID that has never been positively observed. Newly prepared correction
behavior remains; issued and legacy mismatches do not authorize overwrites.
Legacy pending bytes are preserved: missing dispatch evidence is UNKNOWN and
does not grant a fresh attempt. The entire pending read/checkpoint/clear runs
under the source mutation lease. Native repair durability is a separate gate.

DB version9 marks this protocol and makes an older version8 startup refuse the
state. Stop all older daemon/worker processes before upgrade; cached connections
are not retroactively fenced. Preserve a consistent pre-upgrade backup and all
later pending originals. Restoring a backup can erase issued evidence and allow
an old writer to replay unresolved work; the backup alone does not make a
downgrade safe. Reconcile unresolved source effects and establish their safe
writer disposition first. Never strip the marker or run an older binary against
the new pending format.

[Day One](../dayone/README.md#collect-v1-writer-development-branch) now uses this
boundary and its own `v1_dayone_pending` outbox. Its Journal setup/writer switch
together, preserving import-once edits and the original case-sensitive source
hash. Invalid or conflicting later entries fail pure preflight before metadata
creation. Preview does not drain pending batches or write bindings. The Day One
standalone CLI now uses [explicit v1 authority and recovery](../dayone/README.md#standalone-v1-authority-and-recovery-development-branch).

V1 batches and action requests/payloads/evidence also use the shared
[`snapshot_json`](../fulcra-common/fulcra_common/json_values.py) check before
storage or transport. Invalid Unicode and non-string nested object keys fail
visibly, rather than being changed by Python's JSON encoder. Original pending
state remains intact on validation failure and requires explicit recovery.

Media/PurpleAir now use `deliver_records` to check queryable replay exclusions
around uploads and repair observed revivals with native tombstones. Exact
readback separates verified and excluded IDs; excluded rows cannot produce a
delivery count. Source duplicate shortcuts also check policy. Each media chunk
holds the account's reentrant, process/thread-exclusive record lease from twin
planning through final readback. This lock is local to the host; it does not
replace browser cutover or historical replay-floor/identity gates. Interactive
Undo uses the action protocol described below; explicit source pending recovery
remains migration work. Policy transport now rechecks its original local binding
and current owned control around every read/write, as specified in the
[shared policy contract](../fulcra-common/README.md#v1-datatype-migration-boundary)
and [authority integration tests](tests/test_v1_policy_authority.py).
Preview runs require the original resolved policy binding from destination
setup as well. If it is missing, recover that account's binding before preview;
a read-only run never discovers/adopts a remote control or creates a journal.

[`ActionJournal`](fulcra_collect/record_actions.py) adds opt-in durable interactive
intents, using a private SQLite file and full-synchronous transactions. An action
UUID is globally unique within that journal and remains bound to its original
account, consent epoch, operation, request and frozen payload. Retries reconcile
that payload; they cannot manufacture a new timestamp or move it to a different
account. The pure first-payload builder receives a frozen request snapshot.
State transitions append evidence, distinguish submitted from verified,
and refuse stale changes or replacement of terminal proof. Requests/payloads are
limited to 1 MiB each, evidence to 64 KiB, and an audit read fails visibly above
1,000 events. Keep the journal through cutover; it does not itself authorize
HTTP or prove a remote write/deletion.
`inspect` can read the same account's old epochs without granting mutation
authority. `list_actions` pages newest-created first, with account/filter-bound
cursors, a 100-row cap and a 4-MiB stored-JSON budget. Unknown/corrupt selected
rows fail visibly; an unfiltered recovery read cannot silently omit an unknown
status. New actions appear on refresh. These cursors provide navigation, not
authorization or a claim that the server has settled an upload.

[`QuickRecordSession`](fulcra_collect/quick_record_v1.py) provides the matching
opt-in interactive authority. Its context registers a dedicated epoch without
enabling a source; account transitions rotate that epoch, including reconnects
to the same account. Mutations require the captured account/epoch and retain
the existing request, raw-settings, account-transition and preview guards.
Generic setup sessions still cannot ingest a disabled source. Registered
interactive contexts use shared account/settings leases; only first epoch
registration needs an exclusive settings lease, with identity re-proved after
the shared lease is released. Development native/web callers now use protocol2;
obsolete unversioned commands refuse before auth/effects. Released 0.1.4 remains
legacy until reviewed publication.

[`QuickRecordActor`](fulcra_collect/quick_record_actor.py) is the opt-in executor
for prepared choices. It freezes the action before effects and persists a
dispatch intent immediately before attempting the record POST, after schema
validation. A receipt remains distinct from exact field readback. Once dispatch
may have happened, retries reconcile that record ID and never submit another
create. An uncertain upload stays submitted until exact visibility is proved;
an exclusion alone cannot establish its final absence. Native Undo requires a
verified original, captures a new current-epoch action, establishes its permanent
replay exclusion, then verifies native deletion and policy readback. The same
account can authorize Undo of an old-epoch verified original without rewriting
that original. A changed record payload fails visibly. The versioned backend
below and development native/web callers use this actor. Old-epoch pending
reauthorization and packaged-release QA remain incomplete.

### Files read completeness

[`CollectFiles`](fulcra_collect/files_io.py), shared by Notes, Gmail/coord and
bound task vaults, requires HTTP 200 for reads. HTTP 206, partial/pagination
headers, malformed totals and listing totals unequal to the returned file count
are UNKNOWN, never absence or a UTF-8 prefix. Consumers stop before follow-on
file or checkpoint writes. Signed storage redirects still use the separate
credential-free transport; successful upload statuses such as 201/204 remain
supported.
When the exact listed version includes `size`, text downloads require a strict
nonnegative integer and compare complete body bytes before UTF-8 decoding on
both direct and signed-storage legs. A short HTTP 200 with a self-consistent
Content-Length cannot override that version size. Missing size metadata remains
supported for older listings, without this independent length proof.
The task vault's separate exact-version reader retains the validated size for
each current/archived ID in its latest complete history snapshot and enforces
the same byte comparison before task parsing/rendering. Failed or superseded
listings invalidate that snapshot; it never replaces a version read with the
current-path reader. See [task history transport](../task-sync/README.md#bounds-and-transport).

### Observed outbox progress (development)

`V1RecordOutbox.progress` is an immutable in-memory verified/excluded count
snapshot, or `None` before any drain. Every public drain resets it before the
entry guard. Each complete, validated delivery partition adds evidence once;
an incomplete or malformed group adds none, while earlier valid groups survive
later failures. This is an observed lower bound, not proof that a POST occurred
or that the whole pending batch completed. It grants no delivery authority and
does not alter receipts, pending storage or dispatch behavior. The
[media invocation adapter](../media-helpers/README.md#observed-import-progress-development-branch)
now retains current-input counts in the fourteen migrated CLI error
envelopes, separately from original recovery. Shared
concurrent handlers must not use a last-value property as a caller-bound receipt.
The development [standalone media webhook](../media-helpers/README.md#standalone-webhook-development-branch)
instead owns its runtime on one private thread and settles each accepted request
individually. Its CLI profile leases span startup recovery through shutdown
cleanup; cached health is liveness rather than current ownership proof. This
source change does not deploy or reconfigure the Collect service.
See the [outbox tests](tests/test_v1_outbox.py).

### Quick-record protocol 2 (development)

The daemon accepts a `quick_record_v2` envelope with `operation` naming
the command below. Authenticated `GET /api/quick-record/v2` returns the handshake;
`POST /api/quick-record/v2` accepts the same body without `cmd`. The
[transport](fulcra_collect/quick_record_protocol.py) and
[controller](fulcra_collect/quick_record_controller.py) never fall back to legacy
record writes. HTTP rejects extra fields and coerced booleans/integers. Obsolete
mutation envelopes fail before a credential lookup. Apart from the two bootstrap
reads, every command requires the captured `protocol_version: 2`, `account_id`
and `config_epoch`. A fresh sign-in or restored settings do not authorize an old
pending action.

| Operation | Additional fields / behavior |
|---|---|
| `quick_record_list` | Complete bounded choice list, scope, favorite revision, recent 20 records and active timers. This is a handshake, not write authority from a display cap. |
| `get_quick_record_favorites` | Bootstrap read of this account's favorites and revision. |
| `set_quick_record_favorites` | `choice_id`, `choice_digest` for pinning, strict `pinned`, `expected_revision`; applies one additive/removal diff, preserving hidden pins. |
| `record_annotation` | `action_id`, `definition_id`, `choice_digest`, `start_time`, optional `end_time` and `comment`; reuse the exact request/action after a lost reply. Moments omit end; durations require an end after start. |
| `delete_annotation` | New `action_id` plus original `record_action_id`; native Undo of a verified original, never a source-ID sentinel. |
| `quick_record_action` | `action_id`; same-account inspection across epochs is read-only and flags pending reauthorization. |
| `quick_record_actions` | Optional `action_kind`, `statuses`, `limit` (1–100), `cursor`; paginated discovery beyond recent 20, with bounded display metadata and no raw requests/notes/payloads. |
| `quick_record_reconcile` | `action_id`; reconciles an existing current-epoch intent and finishes its matching timer when terminal. It never resubmits an uncertain create. |
| `quick_record_timer_start` | Global `timer_id`, `definition_id`, `choice_digest`, `start_time`; captures a prepared target and tags, with the original start retained on retry. |
| `quick_record_timer_stop` | `timer_id`, `end_time`, optional `comment`; first stop freezes its action ID/window/note before record effects. Failed stops remain visible. |
| `quick_record_timer_reconcile` | `timer_id`; explicitly resumes the saved stop snapshot, including a crash before action capture. No note re-entry or new end time is needed. A running timer has no stop to resume. |
| `quick_record_timers` | Optional `include_finished`, `limit` (1–100), `cursor`; bounded account/filter-bound history pages. Completed history does not block active discovery. |

Only `status: verified` makes an action reply `ok: true`; an accepted/submitted
action is still pending. Receipt/readback errors retain the original pending
action/timer metadata after re-proving authority, including malformed 201
receipts. Actual account/consent revocation returns `refresh_required`. A retained
terminal action remains distinct from accepting a changed request. Activity and
transport error logs never contain a note or exception payload.

[`QuickRecordChoices`](fulcra_collect/quick_record_choices.py) reads existing
moment/duration definitions as migration inputs and prepares one owned v1 Event
destination per track. Its declared fields retain `track_id`, full `track_name`,
`kind`, `legacy_definition_id` and optional `note`; original tag UUIDs and the
`com.fulcradynamics.fulcra-collect.quick-record.<action-UUID>` primary prefix stay
intact. The initial type specification is private and frozen before creation;
renames update the captured track name without changing a pending creation
fingerprint or destination pin. Schema/ownership/digest ambiguity fails visibly.
Listing alone creates no remote datatype.

[`QuickRecordState`](fulcra_collect/quick_record_state.py) stores account-scoped
revisioned favorites and durable timers beside action/track bindings in the
private `quick-record-actions.sqlite`. It never adopts or overwrites the legacy
unscoped `quick_record_favorites.json`. Preserve both through migration until
source-account/mapping proof authorizes adoption. Legacy definition management
remains separate: soft-delete hides that metadata choice and clears matching
legacy plugin state, but preserves both favorites stores and v1 destination
bindings. It is not native record Undo. Existing moment/duration definitions
supply quick-record choices; new captures use owned v1 Events. Timer history pages cap rows
and stored JSON bytes, and unknown classifications fail visibly before filters.
The convenience active list fails above 1,000 active timers; paginated recovery
remains available without deleting completed history.

The native transport uses an explicit 8 MiB response limit for the
complete bounded choice list; requests and legacy responses retain their 64 KiB
limit. Other explicit response limits must be integers from 1 byte through 8 MiB.
Oversized responses fail visibly without an automatic retry. The portable
[native request workflow](../menubar/README.md#quick-record-client-development)
stores the original request before socket dispatch. The development native
popover/preferences and web favorites now use protocol2 by default. Their
account-scoped read, draft, pin and recovery behavior is documented there.

**Release gates:** explicit old-epoch action/timer reauthorization and packaged
interactive QA are still pending. Obsolete unversioned quick-record commands and
authenticated old HTTP URLs return `refresh_required` before Collect credential
reads or effects. Their legacy record serializer and sentinel Undo have been
removed; there is no fallback write. Legacy definition-management
routes remain separate from v1 record Undo; an Event UUID is never an annotation
definition delete target. Do not treat this opt-in backend as the shipped UI.

### Offline historical migration foundation (development)

[`MigrationPlan`](fulcra_collect/migration_plan.py) freezes one full legacy
stream, destination account, actual v1 target, complete served schema, converter
version, literal primary-source namespaces and half-open UTC range. Its canonical
fingerprint changes if scope/schema changes; returned JSON is an independent
snapshot. Prefixes cannot overlap or repeat. Plan/window bounds refuse nonzero
precision beyond Python microseconds rather than rounding away a boundary.
A plan is **data, not current account/ownership authority or coverage proof**.

[`MigrationLedger`](fulcra_collect/migration_ledger.py) stores that exact plan in
a **separate private SQLite file** using the same owned regular-file/0600,
no-follow and fully synchronous transaction checks as the action journal.
Reopening under another account/mapping/range/schema/codec fails. Partial or
future ledger schemas do not bootstrap an empty replacement. Schema 2 upgrades
the export-only schema 1 in place, preserving raw state; it rejects purported
schema-1 files already containing conversion state.

Raw records are retained before conversion, with bounded canonical JSON (1 MiB
per row). Overlapping observations deduplicate an identical original version;
changed versions of the same original ID remain separate and are counted.
The same window/ordinal cannot be overwritten with another observation.
A missing/invalid original ID keeps the full raw row quarantined. An oversized
row fails visibly and leaves its window open; the exporter must retain its
original private artifact, never silently skip it.

Window completion requires a successful complete-read assertion, fewer than the
explicit ceiling (at most 4,000), and the exact retained integer ordinal sequence
`0, 1, …, count−1`. Sealing and resume both reject fractional ordinals, including
coherently rehashed evidence. Sealed evidence binds an ordered hash of observations and raw hashes,
not only a count. Saturated parents need explicit narrower children covering
their whole range; complete leaf windows must cover the plan with no gaps or
open reads. The metadata fold fails above 100,000 windows; single-row capture
reads its own window rather than scanning unrelated windows. Corrupt/unknown
classifications fail visibly.

`capture_window` atomically captures a bounded complete raw snapshot and its
seal. It requires the caller's explicit complete-read/count/ceiling proof, a
list/tuple with the exact count, at most 1 MiB per canonical row and 16 MiB total.
Unknown, saturated, malformed or oversized input cannot establish a new seal;
a mid-transaction crash rolls back all new rows/observations and the seal.
Exact sealed retries are idempotent; changing data or evidence fails. The reader
must retain its original private response artifact first. This storage API does
not itself establish a provider's completeness.

`split_window` creates exact narrower children and links their parent in one
transaction, so interruption cannot strand half a split. Existing child ranges
and split evidence are immutable. `mark_split` reads its parent and bounded
children directly; it does not materialize unrelated windows. `window(id)` reads
scoped metadata for resume, checks immediate split links, and explicitly reports
raw integrity as not scanned. A raw capture/seal checks retained bytes and codec
proofs; it grants no conversion eligibility or upload authority. `entry(id)`
remains the full target-group eligibility read.

[`HistoricalReader`](fulcra_collect/migration_export.py) is a separate read-only
HTTP building block. Callers supply a bound client and a fresh consent/origin
guard returning the current account UUID. It accepts only the production HTTPS
API origin, rechecks before/after credential lookup and while reading, and sends
the plan's explicit account/type/range with redirects disabled. A pass UUID is
immutably bound to the plan fingerprint. Each GET gets a new owner-only attempt
directory: a fsynced request manifest precedes dispatch, raw response bytes
are retained before parsing, and a hash/EOF receipt follows a fully flushed body.
No credentials or arbitrary header values are saved. Version-2 receipts retain
only known partial-header names and bounded numeric total counts/content length
(or an invalid marker). Pagination/range evidence, malformed counts, totals
unequal to parsed rows or a length unequal to retained bytes reject the read. Failed or oversized reads retain
their bounded partial body; orphan attempts are never reused as successful reads.
The default body limit is 64 MiB; each request has a 30-second deadline
and socket timeout (explicit limits may be tightened; at most 60 seconds).
Requests require identity encoding. Encoded responses are retained without
decompression and rejected, so compressed input cannot evade memory/time bounds.
The saved body is size/hash-checked against the EOF receipt before parsing.
Only a list of JSON objects is accepted; ambiguous wrappers, duplicate keys,
non-finite/lossy numbers and invalid Unicode fail visibly with the raw artifact
retained. A byte/depth scan bounds each object before JSON materialization
(1 MiB, 64 container levels); snapshots cap at 4,000 rows and 16 MiB canonical
row JSON. Larger inputs remain in raw artifacts and require a narrower read.
HTTP 206, redirects and error statuses cannot become an empty success.
Every result/receipt says `coverage_complete: false`: transport EOF does not
prove provider completeness. `verify_result` reopens the scoped request/body/EOF
artifacts before a later actor consumes them; the exact bytes must reproduce the
immutable returned snapshot and match account/type/pass/range/order and hashes.
Verification rechecks completeness metadata and the original receipt SHA-256;
stripping or replacing response facts cannot turn an incomplete read into proof.

[`ExportDriver`](fulcra_collect/migration_export_driver.py) adds a bounded,
resumable library orchestrator. It requires an **explicit externally justified
provider completeness contract**: version 1, `semantics: complete-below-row-cap`,
an integer `row_cap` of 1–4,000 and a nonempty `basis`. There is no default contract.
The public legacy endpoint describes inclusive/exclusive bounds and sort order,
but supplies no cursor, total count or ceiling attestation. Matching two reads
cannot establish an undocumented completeness guarantee; do not invent a basis.

The pass UUID/plan/contract are immutable in separate version-2 pass/proof tables
inside the private migration ledger. Old version-1 artifacts/passes lack the
required response evidence and cannot resume or certify coverage. Preserve them;
a new authorized pass requires a fresh ledger rather than adopting old windows. Deterministic window UUIDs and a status/start index resume the next open
window without rescanning completed prefixes. A nonblocking private file lease
prevents simultaneous driver runs. Each step reads both sort directions and
rechecks their saved artifacts. Equal canonical multisets below the asserted cap
permit an atomic conditional leaf receipt; capped or unequal pairs atomically
create strictly narrower children with a two-microsecond overlap. A saturated or
unstable window below four microseconds stops visibly, remaining open. Unknown,
malformed, oversized or failed responses also remain open with raw attempts intact.

Every successfully committed pair retains **all distinct observed raw versions**,
including saturated/inconsistent parent versions absent from later children.
Proof metadata binds their content-addressed entry IDs and both body and receipt hashes;
the pair union caps at 32 MiB canonical JSON (at most 8,000 raw versions).
Only complete leaves create ordered coverage observations. Proof, raw versions,
leaf seal or child split share one transaction. `window_proof` hash/shape-checks
scoped metadata and matching window state, without a fresh artifact audit.
Runs cap at 64 windows/300 seconds, reserve two reader timeouts per pair and
recheck budget before the second request and commit. Their result reports queue
progress with `coverage_complete: false`, `historical_complete: false` and raw
integrity not scanned. The separate ledger coverage fold remains conditional on
the caller's provider contract. Production export/CLI, verified apply, full raw
audit and cutoff/late-delta reconciliation remain open.

[`MediaHistoryConverter`](fulcra_collect/migration_convert.py) currently supports
typed DurationAnnotation records for watched, listened, read and activity.
`collect.media.<category>.v1` handles flat typed rows; its frozen behavior remains
unchanged. `collect.media.<category>.v2` handles the API reader DTO: it proves
the definition UUID, prefixed annotation source binding, owning account and
duration kind, then preserves the original record ID, full legacy type/API,
source binding/display name and complete definition metadata in a declared
`legacy_record` field. Distinct current-definition and record display names
remain distinct. Unknown metadata, including mesh provenance, is retained.
A soft-deleted definition is provenance, not proof that its records were deleted.
The v2 codec refuses a caller-provided `legacy_record` rather than overwriting it. It uses the shared live/history UUID mapping,
retains tags/provenance, nulls and declared enrichment, and leaves missing
enrichment absent. Category comes from the explicit codec; duration is derived
only when absent. Unknown fields are never silently dropped. Ambiguous primary
identities, wrong definitions, malformed timestamps/provenance, out-of-range
boundary rows and schema-invalid/oversized output become static quarantines.
Legacy batch envelopes and deletion/supersession markers need their own codecs; they
are quarantined, never guessed. Unretainable raw input fails before conversion.
Validation uses only the frozen schema and local references: an unavailable
reference raises a plan error without fetching remote content.

The v2 reader codec requires an explicitly audit-capable target schema.
[`media_history_spec`](../media-helpers/fulcra_media/v1.py) supplies a separate
`collect.media.<category>.history.v1` specification and `<Category> History`
name. The default `media_spec` fields, key and fingerprint remain unchanged;
existing creation intents and destinations are not implicitly upgraded or
retargeted. A live source may use a compatible richer destination through an
explicit pin; its served-schema enrichment merge retains the provenance. A
synthetic extended-schema canary verified exact nested/null/Unicode provenance
readback; actual history apply still needs its own owned schema/pin proofs.

The static [codec registry](fulcra_collect/migration_codecs.py) preserves those
eight media converter IDs and lazily loads the optional
[Journal reader codec](../dayone/README.md#opt-in-historical-journal-contract-development-branch)
for `collect.journal.reader.v1`. It never imports a module path from a plan or
discovers entry points. Unknown or unavailable converters fail before conversion
row work; raw-only ledgers can still preserve and reopen exports for a future
codec. Validating a converted proposal requires its original codec to be
available. The Journal contract is separately opt-in: existing writer defaults,
pins and creation fingerprints do not change. This registry provides no schema
creation, source cutoff or apply authority.

`MigrationLedger.convert_entry(entry_id)` freezes the proposal and its
source/target/payload hash. Reading a converted entry verifies its raw hash and
recomputes the versioned codec result, so matching stored hashes alone cannot
hide a different payload. Conflicting proposals for one target UUID atomically
quarantine **every** prepared variant, retaining both raw and proposed content;
identical proposals converge. The indexed collision group caps at 1,000 rows and 4 MiB of raw/proposed JSON
and fails visibly above either bound. Every peer is codec-verified even when
its stored payload appears identical; converted reads recheck group conflicts. A collision is never implicitly cleared.
These snapshots grant no upload authority, and a later raw variant may invalidate
a previously converted proposal. An apply actor must recheck the authoritative
ledger and all history/deletion/cutoff gates before any effect.

**No network exporter, apply actor or CLI is implemented yet.** Current entry
states are exported/converted/quarantined. Coverage
is export-window evidence, never semantic conversion or final history proof.
Status explicitly reports `raw_integrity: "not-scanned"`: the metadata fold checks
sealed observation hashes, while each raw row is hash-verified when consumed via
`entry()`. It does not imply a fresh full raw-content audit.
`historical_complete` remains false. Real source/account discovery, definitionless
markers, remaining family codecs and exact readback, native-delete replay floors, stopped old
writers/queues and a final late-upload sweep remain required by the
[historical design](../../docs/superpowers/specs/2026-10-23-collect-v1-design.md#historical-migration).
No original remote record is changed or removed by these classes.

### V1 source destination setup

V1 producers declare `Plugin.record_data_types(settings)` as a slot-to-
`DataTypeSpec` mapping and put the slot on each destination setup step's
`SetupStep.record_slot` (optional for a single-slot producer). The authenticated
`GET /api/plugin/{id}/data-types?slot=...` lists owned, recordable v1 choices;
`POST /api/plugin/{id}/data-type` verifies the chosen schema and saves the full
`Event/<UUID>` or `Metric/<UUID>` in `v1_destination_ids`. The alternative
`use_or_create: true` resolves or creates a matching type during setup, with an
optional `new_name`. A legacy annotation binding cannot configure a migrated
producer. The picker previews the latest entry within 30 days through
`GET /api/plugin/{id}/data-type/recent`; this bounded preview is not a history
coverage check.

Binding also proves the account's owned replay-control type before saving the
source choice. A saved control binding filters that internal type from the
picker without a catalog/schema fan-out; otherwise bounded served-schema
discovery identifies controls. The API does not retain the custom metadata key
sent at creation. Keep the private account/key journal during cutover; renamed,
ambiguous or missing controls must reconcile through that binding, not an empty
replacement. Preview source runs never create or repair replay policy.

Binding saves recheck account, saved settings, epoch and pending uploads while
holding the settings mutation fence, then rotate the source epoch without
enabling it. Producers with a different pending format declare a read-only
`Plugin.record_pending(ctx)` checker; the default checks `v1_pending_records`.
Unreadable pending state blocks rebinding. Pending envelopes also bind a typed
fingerprint of raw saved settings, so direct TOML edits cannot silently reassign
older data even when they bypass epoch rotation. Keep old annotation selections
for historical mapping; an existing custom selection needs an explicit v1 choice.

## Service failure status (development branch)

A supervised service's unexpected nonzero exit now records a failed run in
cached status. The observation names its original process and configuration
revision; fresh local profile, consent, settings and account-transition fences
must still match before persistence. The error reports the exit status, with
cause unavailable. It does not infer a Keychain diagnosis, successful collection
or receiver readiness. Existing restart/back-off, clean-exit, retirement and
stop-grace policies are unchanged.

Both exit and OS launch failures use an atomic status-only update, preserving
watermark, destination and yield fields. Each observed process is recorded once;
an uncertain database commit is never retried as another failure increment.
Status rendering reads no credentials or providers. If an observer, local lease,
configuration or persistence check is uncertain, `/api/status` returns an explicit
`service_status: "unknown"` error and the dashboard shows **Failure status
UNKNOWN**. Unreadable state never becomes a healthy or empty observation.

The pending/confirmed failure overlays are bounded, in-memory observations.
They do **not** provide durability across daemon restart when the database did
not acknowledge the write. A new launch alone cannot clear uncertainty.
After scope changes, old account-specific fields stay masked; a positively
current confirmed failure may show only its static error, observation time and
failures observed for that revision by this daemon. That count can be lower than
the database's historical total. Unaffected rows retain existing cached status.

This is source QA, not live service acceptance. Confirm the configured receiver
is listening and reachable before telling another service it is ready. See
[the supervisor](fulcra_collect/supervisor.py) and
[the daemon status boundary](fulcra_collect/daemon.py).

## What it does

* **Discovers plugins.** Any installed Python distribution that
  registers under the `fulcra_collect.plugins` entry-point group is
  picked up at startup. A plugin declares one of three kinds:
  `scheduled` (an importer fired on a default interval), `service`
  (a long-running process the daemon supervises with restart-back-off),
  or `manual` (only fires when the user clicks Run). A service worker that
  cannot launch is recorded as a failed run, shown in Recently, and folded
  into the same back-off / degraded policy as a crashed worker; it cannot
  terminate the daemon loop or stop unrelated plugins. Service workers also
  run in their own process groups, so disabling Collect or shutting it down
  terminates helper commands started by the service.
  Every daemon tick rereads authoritative configuration. A service revision
  includes its consent epoch and typed raw-settings fingerprint, so even a
  manual TOML edit retires the old worker. Replacement waits for confirmed exit;
  a stuck worker gets an owned-process-group kill after a 10-second monotonic
  grace, checked on ticks (30 seconds by default). An unconfirmed exit never
  permits overlapping workers. Reconfiguration resets the old crash budget.
  An unreadable configuration or active account-transition gate pauses new
  scheduled dispatch and retires services until valid consent is available.
* **Lets setup choose a local folder without copying its path into the browser.**
  A plugin can declare a `folder_picker` step for a `path` setting. The explicit
  **Choose folder** action opens a fixed macOS chooser; the daemon validates and
  stores the path in private local configuration, while the UI receives only the
  display name. Choosing a folder does not enable the plugin or upload data.
* **Runs them in worker subprocesses.** Each scheduled or manual run
  spawns a fresh `fulcra-collect _worker <id>` process, isolating ordinary
  importer crashes from the daemon. The worker streams structured
  progress and annotation events back to the parent over a pipe; the
  parent records them in the unified state store and in an in-memory
  ring buffer that powers the dashboard's "Recently" feed. Each run gets
  its own process group, so a timeout also stops helper commands started by
  the plugin. If a worker cannot start, Collect records a failed run and shows
  it in Recently. If it dies before emitting a structured result, Collect keeps
  a bounded, secret-scrubbed copy of stderr as the diagnostic. The parent drains
  stdout and stderr concurrently while retaining only a 4 MiB stdout tail and a
  512 KiB stderr tail, so a noisy or stuck plugin cannot grow the daemon's memory
  until the 15-minute timeout. The structured result event belongs at the end of
  stdout and remains visible when earlier output is discarded.
  One monotonic deadline covers both pipe EOFs and child exit. A descendant
  retaining a pipe cannot extend the run after the direct worker exits. Timeout
  or failed draining stops the owned process group, allows at most 0.5 seconds
  for direct-child reaping, then closes local pipes. Separate-session helpers
  are not chased; their retained pipes still cannot extend the deadline.
* **Defers network work while the Mac is offline.** The scheduler probes the
  configured Fulcra API endpoint and an ordinary HTTPS fallback before firing a
  network-required plugin. Both probes use normal web-service ports, which keeps
  managed networks that block direct public DNS traffic from being mistaken for
  an offline Mac. Local-only scheduled plugins still run while offline.
* **Stores secrets in the OS keychain.** Per-plugin credentials live
  under a `fulcra-collect:<plugin-id>` service name; the user-level
  Fulcra bearer token shares one namespace (`fulcra-collect:user`)
  across the whole hub.
* **Serves a web UI.** A FastAPI app bound to `127.0.0.1:9292`
  serves the wizard / dashboard / settings frontend out of
  `packages/web-ui/dist/` and answers the JSON API described below.
  The port is stable across restarts so that OAuth redirect URIs
  registered with external providers don't break when the daemon
  restarts.
  Requests must also carry a loopback Host header. The browser receives the
  local control token in an HttpOnly, SameSite cookie; JavaScript cannot read
  it. Browser libraries are packaged with Collect rather than loaded from a
  CDN. The server's content policy allows local scripts and Alpine's expression
  evaluator, but blocks remote and inline script execution.
* **Auto-launches the macOS menubar app** on startup when one is
  installed, so the user always has a visible status indicator
  without remembering a second command. `daemon --no-menubar` skips this
  startup hook; ordinary `daemon` startup keeps auto-launch enabled.

## Running from source

The Mac installer includes the daemon, runtime, and bundled plugins. A source
installation discovers only the plugin packages installed in its Python
environment. Installing `packages/collect` alone does not install them all.

From the repository root, first install the workspace dependencies. On macOS:

```bash
uv sync --all-packages --all-extras
uv run --all-packages --all-extras fulcra-collect daemon
# → web UI at http://127.0.0.1:9292
```

On Linux, use `uv sync --all-packages` and
`uv run --all-packages fulcra-collect daemon`; the macOS extras require Apple
frameworks. See [source setup and testing](../../docs/TESTING.md) for details.

For foreground source/server QA without native menubar auto-launch:

```bash
uv run --all-packages --all-extras fulcra-collect daemon --no-menubar
```

On Linux, omit `--all-extras` as above. `--no-menubar` applies only to native
menu startup for this invocation. Profile ownership, control socket, web server,
quiet authentication preflight, scheduler/worker configuration and consent
checks, and shutdown continue normally. **It is not an offline, no-auth, or
no-network mode.**

For a macOS login service backed by this checkout's environment:

```bash
uv run --all-packages --all-extras fulcra-collect install
# Writes ~/Library/LaunchAgents/com.fulcra.collect.plist.
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.fulcra.collect.plist
```

On Linux, `uv run --all-packages fulcra-collect install` writes
`~/.config/systemd/user/fulcra-collect.service`; start it with
`systemctl --user enable --now fulcra-collect`. Keep the checkout and its
environment in place while the service uses them. A bare `uv tool install
packages/collect` does not resolve this monorepo's sibling packages or install
the connector roster.

For the macOS UI, follow the [menubar development instructions](../menubar/README.md#run-in-dev-mode).
The downloadable app handles background-service installation from its menu.

### Per-profile daemon ownership (development branch)

The [daemon lease](fulcra_collect/daemon_lease.py) acquires a nonblocking local
`flock` on the effective profile's persistent `daemon.lock` before state
migration, control-socket changes, credentials, HTTP or worker dispatch. Profiles
selected through `FULCRA_COLLECT_HOME` are independent; aliases resolving to the
same profile share ownership. The lock file must be an owner-only regular file
(0600, current UID, one link) inside the owned private profile directory (0700).
Unsafe or unavailable paths refuse startup without repair.

A cooperating duplicate prints `daemon startup refused: profile lease busy`
and exits **75**. Unavailable ownership prints `daemon startup refused: profile
lease unavailable` and exits **1**. Busy does not prove the holder is healthy.
The opened inode stays locked until process exit, including startup failure or
`serve()` unwind; an embedding caller must exit before starting another daemon.
No PID metadata authorizes takeover. **Do not delete `daemon.lock` for recovery**:
replacing its inode defeats exclusion. An unlocked file left after exit is normal
and is reused without truncation. The descriptor is not inherited across exec.

Stop and reconcile older unleased daemons/workers before first adoption. This
cooperative local lease excludes only daemons using it: direct workers, legacy
writers, detached workers surviving a crash and cross-host writes remain outside
its guarantee. It does not prove profile quiescence or retire orphan workers.
Source tests do not establish signed-app/runtime adoption. Launchd `KeepAlive`
and systemd `Restart=always` are unchanged, so a redundant service may keep
retrying/logging exit 75 until its operator stops the duplicate service.

### CLI

```text
fulcra-collect daemon                       run the hub in the foreground
fulcra-collect daemon --no-menubar          run without native menu auto-launch
fulcra-collect install                      install the launchd/systemd user agent
fulcra-collect status                       list every plugin: kind, enabled, last run
fulcra-collect doctor                       diagnose local installation and auth
fulcra-collect enable  <plugin-id>          enable a plugin
fulcra-collect disable <plugin-id>          disable it
fulcra-collect run     <plugin-id>          trigger one run now (via the running daemon)
fulcra-collect set-interval <plugin-id> N   override a scheduled plugin's cadence (seconds)
fulcra-collect set-credential <plugin-id> <key>
                                            stash a secret in the OS keychain (hidden prompt)
fulcra-collect set-setting <plugin-id> <key> <value>
                                            set a NON-SECRET setting in config.toml
fulcra-collect plugin reset-definition <plugin-id>
                                            clear the cached Fulcra def_id (re-resolve next run)
```

`enable`, `disable`, `set-interval`, `set-setting` write to `config.toml`
even when the daemon isn't running and signal `reload` when it is. `run` and
`status` need a running daemon; they talk to it over a Unix-domain
control socket at `~/.config/fulcra-collect/control.sock`.

`set-credential` and `set-setting` are the two halves of configuring a
plugin without the wizard, and the split is a security boundary, not a
convenience: secrets go to the OS keychain, everything else to a plaintext
`config.toml`. `set-setting` therefore **refuses** a key the plugin declared
as a `Credential` and points you at `set-credential` instead. It also
validates against the plugin's declared contract — unknown plugin id,
unknown setting key, a value outside an `enum`'s declared values, or a
`port` outside 1–65535 are all rejected rather than written. Toggles accept
the usual true/false words and are stored as booleans. Intervals accept
positive seconds or an ISO 8601 duration such as `PT5M`. Every one of these
bad writes otherwise succeeds silently and is only discovered when a plugin
runs.

```console
$ fulcra-collect set-credential purpleair api_key   # prompts, hidden
$ fulcra-collect set-setting purpleair mode api
purpleair: mode = 'api'
$ fulcra-collect set-setting purpleair sensor_index 123456
purpleair: sensor_index = '123456'
```

The sensor number above is synthetic; use your own sensor's index.

### Doctor diagnostic coverage

`fulcra-collect doctor` reports installation, CLI capabilities, auth/API health
and last-hour account-level data-update counts. It exits 0 when no check is
FAIL; warnings alone still exit 0, and any FAIL exits 1.

Its `V1 destination validation` row is a static WARN: doctor establishes neither
owned destination schema compatibility nor record delivery. Configured v1
writers own [destination schema validation](#v1-source-destination-setup) and
exact delivery readback. An API-health response or account-wide update count
is not per-source validation. The other doctor checks still use their existing
CLI/auth/API probes; the static coverage row adds no network or account probe.

## Module layout

The package is roughly split into the daemon core, the persistence
layer, the worker plumbing, and the HTTP surface.

```
fulcra_collect/
    cli.py                  click entry points — `fulcra-collect …`
    daemon_lease.py          Cooperative per-profile lifetime daemon exclusion
    daemon.py               Daemon: control-socket request handler, scheduler tick,
                            service supervision, account-fingerprint pre-flight,
                            quick-record + delete-annotation dispatch
    config.py               Config dataclass + config.toml round-trip
    credentials.py          OS keychain shim (per-plugin + user-level namespaces)
    registry.py             entry-point discovery, RegistryResult
    scheduler.py            due_plugins(): which scheduled plugins are due now
    supervisor.py           ServiceSupervisor: keep service plugins alive with back-off
    runner.py               spawn a worker subprocess, consume its stream, record state
    worker.py               in-subprocess plugin runner; builds the RunContext;
                            adapts fulcra-common's HTTP client for def resolution
    control.py              Unix-domain-socket request/response server (CLI ↔ daemon)
    oauth.py                PKCE state for browser-OAuth plugins (Trakt, Spotify, …)

    plugin.py               THE PLUGIN CONTRACT — Plugin / Credential / Setting /
                            SetupStep / HealthResult / RunContext dataclasses

    db.py                   SQLite connection lifecycle + schema migrations
    state.py                PluginState (typed wrapper over db.fetch/upsert)
    activity.py             in-memory ring buffer of recent annotations (dashboard feed)

    web.py                  FastAPI app factory + uvicorn launcher
    routes/                 per-area HTTP route modules; each exports register(app, ctx)
        _deps.py            RouteContext + Pydantic body models shared across routes
        status.py           /api/status, /api/version, /api/reload
        plugins.py          /api/plugin/{id}/{run,enable,disable,credentials,settings,
                            contract,setting_options/{key},health_check,check_permission,
                            request_permission,upload,choose-folder}
        definitions.py      /api/definitions, /api/plugin/{id}/definition (bind/clear)
        fulcra_auth.py      /api/fulcra/auth/{status,token,cli_status,cli_login_start,cli_login_poll}
        oauth.py            /api/oauth/{plugin_id}/{start,callback}
        annotations.py      /api/quick-record/v2; obsolete URLs return refresh_required
        activity.py         /api/activity
        menubar.py          /api/menubar/{status,launch}
        docs.py             /api/docs/{name}        — serves repo-root docs/

    menubar_launcher.py     best-effort spawn of the macOS menubar app on startup
    quick_record_favorites.py
                            legacy unscoped favorites compatibility file (`quick_record_favorites.json`)
    service_manager.py      launchd plist / systemd unit installer
```

`web.py` constructs the FastAPI app, bootstraps its local auth token
(`~/.config/fulcra-collect/web-token`), creates the Fulcra HTTP client factory,
and mounts the frontend. It passes a shared `RouteContext` to each
`routes/*.py` module's `register()` function. The route modules import `httpx`
through `fulcra_collect.web`
deliberately so the existing `monkeypatch.setattr(web, "httpx", …)`
test idiom keeps working.

### Plugin contract

A plugin is a `Plugin` instance discovered via setuptools entry points:

```toml
# in the plugin package's pyproject.toml
[project.entry-points."fulcra_collect.plugins"]
dayone = "fulcra_dayone.collect_plugin:PLUGIN"
```

The object (or callable returning one) is a `Plugin` dataclass
declaring `id`, `name`, `kind`, the `run(ctx)` callable, plus optional
`required_credentials`, `required_settings`, `required_permissions`,
`setup_steps` (the wizard renders these), `health_check`,
`permission_check`, `permission_request`, `setting_options`, OAuth callables, a category, and a
`canonical_definition_name`. The daemon builds a `RunContext` for
every invocation and passes it in; the plugin reaches for its config,
credentials, state, and the Fulcra def-resolver through the context
rather than touching the filesystem or keychain directly.

Long-lived plugins store independent cursors and per-entity state through
`ctx.kv_get`, `ctx.kv_set`, `ctx.kv_update`, and `ctx.kv_delete`. Values are
plugin-isolated JSON (64 KiB per value; 256 UTF-8 bytes per key) in `state.db`.
Pending record payloads use `ctx.outbox_get` / `ctx.outbox_set` in the separate
`record_outboxes` table (16 MiB per batch, finite JSON, maximum depth 32).
Worker-bound seams keep plugin namespaces isolated. A health context without an
outbox backend fails loudly; it cannot pretend a payload persisted. Stage a
smaller batch if it exceeds the bound; a rejected replacement retains prior
pending data. Schema v8 imports pre-release small v1 batches while retaining
original KV copies. Old binaries refuse a v8 database: retain a pre-upgrade
SQLite backup for a rollback, and preserve any later pending payloads too.

Use `kv_update` for a quick, side-effect-free atomic read/modify/write when
multiple worker processes may touch the same key.

For examples, read [Day One](../dayone/README.md) for scheduled imports and
[Apple Notes](../apple-notes/README.md) for vault files, access checks, and an
explicit start action. A new plugin needs its setup instructions and limitations
in its README and the [source guide](../../docs/how-do-i-get-my-data.md) in the
same PR.

### Native permission requests

Keep `Plugin.permission_check(ctx)` read-only: the wizard may invoke it when a
permission step opens or the user clicks **Verify access**. Native prompts belong
in the separate optional `Plugin.permission_request(ctx)` callback, which the
wizard invokes only after the user clicks **Allow access**. Both callbacks return
`{"granted": bool, "hint": str | None}` and receive current saved settings and
correctly scoped Keychain credentials. Permission callbacks must not enable the
plugin or start synchronization.

The contract exposes `permission_request_available`. The authenticated
`POST /api/plugin/{id}/request_permission` endpoint calls only that callback;
`POST /api/plugin/{id}/check_permission` calls only the read-only check.
Unsupported actions return 404; provider, credential-access, or malformed-result
failures return `granted: false` with a generic hint, without exception details.
Full Disk Access continues to use **Open System Settings** and **Verify access**
when no native request callback is declared. This contract ships in Collect
0.1.3 and later.

### Discovered multiselect settings

Collect supports a reusable list picker through `Setting(kind="multiselect")`
and `Plugin.setting_options`. It ships in Collect 0.1.3 and later.

Declare a setting and include its key in an `input` setup step:

```python
selected_lists = Setting(
    key="selected_lists", label="Lists to sync", kind="multiselect",
    default=[], required=True,
)
list_step = SetupStep(
    kind="input", title="Choose lists", settings_keys=("selected_lists",),
)

# Attach this callback to Plugin(setting_options=discover_options, ...).
def discover_options(ctx, key):
    if key != "selected_lists":
        raise ValueError("Unsupported setting")
    provider = make_provider(ctx)  # plugin-owned adapter; reads ctx.credentials
    return [
        {"value": item.id, "label": item.name, "disabled": not item.writable}
        for item in provider.collections()
    ]
```

The callback receives a `RunContext` containing saved settings and each declared
credential from its plugin or user Keychain scope. It must only discover choices,
use bounded source calls, and raise if source access or a partial read fails.
It must never enable the plugin, start synchronization, or log credentials.
The lightweight discovery context does not expose worker KV write callbacks.

`GET /api/plugin/{id}/setting_options/{key}` requires the normal bearer token
and returns `{"options": [{"value": "opaque-id", "label": "Example list"}]}`
with `Cache-Control: no-store`. IDs must be unique, nonempty strings; labels
must be nonempty strings. The only optional option field is `disabled`, a
boolean. An unsupported plugin, setting, or callback returns 404. Discovery
failures and malformed callback results return a sanitized 503, while a
successful discovery with no lists returns `{"options": []}`.

`PUT /api/plugin/{id}/settings` accepts multiselect values only as arrays of
unique nonempty string IDs. Saving never calls discovery or rejects an ID
because its list is currently missing. An empty array can be saved to deselect
all lists, but the HTTP enable route rejects empty required selections. Plugins
must enforce selection scope in their own run logic as well. Use the wizard or
JSON settings API for these arrays; the text-oriented CLI is not an array editor.
The shared wizard preserves selected IDs, displays missing selections for
removal, and requires an explicit Enable action after configuration.

The same endpoint checks every other declared kind before writing: toggles are
JSON booleans, ports are integers from 1 through 65535, text/path/URL/secret
fields are strings, enums are declared string values, and intervals are
positive numeric seconds or ISO 8601 duration strings. Collect still reads the
exact `"true"` / `"false"` and numeric strings written by older setup wizards
as their native types, so upgrading does not leave an existing preview switch
stuck on.

## HTTP API surface

Every `/api` route requires the local token from
`~/.config/fulcra-collect/web-token` except the two OAuth callback landing
paths. The browser uses the HttpOnly cookie set by the HTML root; native and
command-line clients can send the same value as a Bearer token. It is separate
from the Fulcra account token. The root and static assets are public on the
loopback server. OAuth callbacks validate their single-use state instead of a
cookie because the provider redirects from another site. A route-inventory test
enforces this allowlist for core and plugin-mounted routes.

* **Status / version** (`routes/status.py`) — `GET /api/status`,
  `GET /api/version`, `POST /api/reload`.
* **Plugin operations** (`routes/plugins.py`) — run, enable/disable,
  read/write credentials, read/write settings, discover setting options, fetch contract,
  health-check, permission-check, explicit permission-request, file upload (multipart, used by the
  wizard's `file_upload` step).
* **Annotation definitions** (`routes/definitions.py`) — list defs on
  the Fulcra account, bind one to a plugin, list a def's recent
  events, soft-delete a def.
* **Fulcra auth** (`routes/fulcra_auth.py`) — set/clear the user-level
  bearer token and offer explicit sign-in through the `fulcra` CLI.
* **OAuth** (`routes/oauth.py`) — `POST /api/oauth/{plugin_id}/start`
  and `GET /api/oauth/{plugin_id}/callback`. PKCE state lives in
  `oauth.py`; callbacks accept the provider's browser redirect without the
  local bearer header and validate OAuth state instead.
* **Quick-record** ([routes](fulcra_collect/routes/annotations.py)) —
  authenticated `GET/POST /api/quick-record/v2` exposes the
  [protocol2 contract](#quick-record-protocol-2-development) for native v1
  capture, Undo, account-scoped favorites and timer/action recovery.
  Obsolete annotation and favorites URLs return `refresh_required` before
  Collect credential reads or effects. The separate
  [activity route](fulcra_collect/routes/activity.py) serves `GET /api/activity`.
* **Browser extension** — there is no daemon-side route. The Fulcra
  Attention Chrome extension is fully relayless: it signs in via an
  Auth0 device flow and POSTs records directly to the Fulcra API
  (`https://api.fulcradynamics.com/ingest/v1/record/batch`). Collect's
  only involvement is the `attention-relay` pointer plugin, which tells
  the user to install the extension and sign in via the browser. The
  former `routes/extension.py` (`POST /api/extension/attention`) and the
  `/api/plugin/attention-relay/pair` pairing route have been removed.
* **Menubar** (`routes/menubar.py`) — status + relaunch endpoints so
  the dashboard can show "Launch menubar app" when the user has
  accidentally quit it.
* **Docs** (`routes/docs.py`) — serves the repo's `docs/*.md` so
  wizard help links can deep-link to the same source the README
  references.

## State and storage

Everything lives under `~/.config/fulcra-collect/` (override via
`FULCRA_COLLECT_HOME`):

| Path | Purpose |
|---|---|
| `config.toml`                  | Per-plugin enabled flag + interval overrides + `[daemon] web_port`. |
| `state.db`                     | SQLite (WAL mode) — per-plugin run state, dedup claims, small plugin-scoped JSON/KV, and pending record outboxes. Schema migrations and their current version live in [db.py](fulcra_collect/db.py). |
| `control.sock`                 | Unix-domain socket the CLI talks to. |
| `daemon.lock`                  | Persistent private lifetime daemon lease; never delete it for recovery. See [profile ownership](#per-profile-daemon-ownership-development-branch). |
| `web-token`                    | Random local control token (0600) seeded on first boot and sent to the browser in an HttpOnly, SameSite cookie. |
| `web-url`                      | The currently-bound web URL — read by the menubar and ad-hoc tools. |
| `auth-fingerprint`             | SHA-256 prefix of the stable account identity (JWT `sub`; token-based fallback for opaque tokens). Detects account changes and invalidates cached definition IDs without treating routine token refresh as an account switch. |
| `quick_record_favorites.json`  | Preserved legacy unscoped pins; current web/native clients do not adopt or prune this file. |
| `quick-record-actions.sqlite` | Current account-scoped favorites, track bindings, durable captures and timers; see [protocol2](#quick-record-protocol-2-development). |
| `uploads/<plugin-id>/`         | Source files submitted through a wizard `file_upload` step. Files are owner-only; each request streams through a private temporary file and publishes atomically. Concurrent uploads with the same name cannot mix bytes; the last completed upload becomes the saved file. |
| `state/<plugin-id>.json.migrated` | Leftovers from the JSON → SQLite migration in `db.py:_migration_002`. Safe to delete once the soak period is over. |

### Control socket paths

The [control server](fulcra_collect/control.py) keeps socket paths within the
strictest supported Unix-socket capacity: 104 filesystem-encoded bytes,
including the terminating NUL. A fitting profile path binds directly. A longer
or multibyte path uses a hash-named socket under
`<system-temp>/fulcra-collect-<uid>/`, with a symlink at the profile's
`control.sock`; the CLI resolves that symlink before connecting.

The completed fallback path is checked too. If the configured temporary root
is too long, Collect uses `/tmp` (canonicalized before measuring). The per-UID
child must be an ordinary directory owned by the current user; aliases,
non-directories and foreign ownership are refused before permission changes
or socket replacement. An owned directory is tightened to 0700 and the socket
is 0600. An unsafe selected directory or the absence of a fitting candidate
fails visibly; it does not authorize a shared or overlong socket path.

### Credentials

#### Current availability and scoped recovery (development branch)

The authenticated [sign-in status route](fulcra_collect/routes/fulcra_auth.py)
performs one bounded **fresh quiet** bearer read through the existing helper.
`availability` distinguishes `present`, `missing`, `access_required`, `pending`,
`changed`, `transition` and `unavailable`. It returns `saved_presence` separately
(previous cache evidence can survive a denied read), plus `checked_at` and
`provenance: fresh_quiet_keychain` for an attempted read (`not_checked` when a
transition/lease or initial credential-write gate prevents the check). The compatibility `authenticated` field is
true only for current local readability, false for missing, and null for unknown
access. It is **not** server/principal authentication or provider/worker success;
`server_authenticated` and unverified `refresh_available` stay null. Status never
refreshes a token, requests UI, queries a provider or deletes saved credentials.
The account-transition lease and credential-write generation fence stale reads.

Automatic owned-token renewal publishes `oauth-credentials` and `bearer-token`
through the bounded quiet mutation child, with interaction disabled for that
child and per-query UI refusal. Existing refresh revision/account guards and
the consumed-grant journal remain in force. The legacy principal-checked
renewal path uses the same quiet publication boundary. Quiet writes update
existing items in place; only confirmed absence permits adding an item. They
never fall back to an interactive writer. Denial, timeout or ambiguous
acknowledgement requires reconnecting; a consumed owned refresh grant is not
replayed. Explicit sign-in keeps its interactive writer. A configured backend
without this quiet mutation capability cannot silently publish renewed tokens.
Synthetic tests cover the dispatch and failures; native signed-app validation
is a separate prerequisite.

Only explicit authenticated authorization actions may request Keychain UI.
`POST /api/fulcra/auth/keychain_access` has the closed shared scope
`bearer-token` / `oauth-credentials`: each `items[key]` reports `present`,
`missing` or `unverified` and its availability. `ok` requires a quiet-confirmed
bearer; an absent optional refresh item does not invalidate a manual bearer or
prove automatic refresh. An interactive allowance followed by quiet denial is
unverified. The whole shared response is invalidated if its credential generation
changes between items or during optional authorization; a stale bearer success
cannot survive as overall success. Pending requests suppress duplicate prompts; finish them before
checking again. Plugin Configure authorizes only that plugin's declared key and
scope. Neither action overwrites/reconnects credentials, enables sources, or
starts ingestion. No executable/signing/ACL identity repair is implied.

[Worker results](fulcra_collect/worker.py) add nonsecret `credential_error`
metadata from actual read sites: `stage`, `scope`, `key`, `availability`.
Declared-key hydration is distinct from shared `collect_account` bearer access.
The parent [runner](fulcra_collect/runner.py) fences terminal status writes and
volatile recovery metadata to its captured effective profile, plugin epoch,
settings, enabled state and committed auth-journal revision under the existing
account/config leases. Stale/unknown authority cannot rewrite a new account's
terminal status. No traceback-string parsing creates scope authority. Existing
bounded generic diagnostics remain; scoped account errors keep stack frames
without arbitrary backend exception text.

The daemon status row's current `credential_error` adds `recovery: configure`
for declared-key hydration or `settings` for shared bearer access. Missing,
malformed, stale and post-restart scope stays null/`credential_error_status:
unknown`; retained historical error text is not fresh scope proof. This overlay
is volatile and requires no DB schema change. Direct runners without a daemon
and continuous-service stdout are not new scoped-result observers. Local
availability, remembered native access, signed-app adoption and provider recovery
still require separate verification.


Plugin credentials managed by Collect go to the OS keychain via
[credentials.py](fulcra_collect/credentials.py) (Keychain on macOS, a configured
`keyring` backend elsewhere). Native macOS background reads disable
Keychain authentication dialogs in a disposable reader using the same
executable/signing identity. Legacy file-Keychain ACLs ignore modern per-query
UI flags, so the child also disables interaction for its own lifetime; the
daemon/UI process keeps its own setting. Last.fm status checks, health
checks and scheduled runs cannot stack access prompts. An inaccessible saved
key stays **unverified**, rather than appearing missing. In Configure choose
**Allow Keychain access**, then **Always Allow** in macOS to remember access for
the current app. Success requires another quiet read; no saved value is
returned or replaced. Settings has the same explicit access action for the
saved Fulcra sign-in. Read slots remain occupied after a timeout until the OS
call finishes (one per item, at most 16 per process; caller timeout 3 seconds).
Changing app identity may require another grant. Configured non-native
backends retain their own interaction behavior. See
[the agent Keychain guide](../../AGENTS.md#keychain).

Trakt background refresh receipts and credential mirrors also use bounded quiet
write/delete operations in the isolated same-executable child. The child disables
interaction only for itself, accepts secrets on stdin, and returns status only;
it does not change ACLs or parent interaction settings. Denial, timeout or an
ambiguous acknowledgement holds for exact receipt reconciliation, without an
interactive `keyring` fallback. Other backends must explicitly support the quiet
path or refuse it. Explicit Trakt setup durably revokes the previous source,
requests item authorization outside the short account/configuration/source locks,
then uses bounded quiet mutation only after the exact edit/profile is rechecked.
A delayed prompt cannot overwrite a newer disconnect or setup. Authorization
denial/UNKNOWN keeps the edit incomplete; use the existing explicit access action
when a saved item's access is unverified.
These writes are Trakt-specific; other providers' write semantics are unchanged.
Keep the independent OS-user [Trakt source journal and secret receipts](../media-helpers/README.md#trakt-source-ownership-development-branch)
when backing up or moving Collect profiles. Its [provider identity pin](../media-helpers/README.md#trakt-provider-identity-development-branch)
is private account evidence in source-journal version 2; valid v1 state upgrades
without losing grant/source history, while older readers refuse v2. Fence older
writers before upgrade and preserve the journal together with secret receipts.
Restoring an old journal alone does not authorize replay. Unit tests use synthetic
Security and secret-store bindings; installed/signed OS access and restart are
separate QA.

The local web token is a secret file in the
config directory, and standalone helper CLIs may have their own credential
files. Plugin-specific reports, uploads, and ledgers can also contain private
data; this table is not a complete inventory of everything a connector stores.

## Distribution resources (development branch)

Build from complete prebuilt local inputs. From the repository root:

```sh
uv build --offline --package fulcra-collect
```

Offline requires the build backend in the local cache. Preparing the frontend is
a separate step; this build does not run npm, download assets or rebuild the SPA.
Workspace inputs are `packages/web-ui/dist` (including a readable regular
`index.html`) and `docs/how-do-i-get-my-data.md`. Keep inputs immutable during
builds; validation is not a filesystem snapshot or race-proof guarantee.

The [Hatchling hook](hatch_build.py) force-includes each selected regular file,
including hidden/nested names that directory traversal in the backend might omit.
Wheels keep the existing `fulcra_collect/_bundled/web-ui/dist` and
`fulcra_collect/_bundled/docs` layout. Source archives carry an independent
`_build_resources/web-ui/dist` and `_build_resources/docs` snapshot plus the hook;
rebuilding the wheel from that archive uses those bytes. Neither the hook nor
`_build_resources` belongs in the wheel. The hook updates backend inclusion
defaults; it never stages assets into the checkout's runtime `_bundled` directory.
The [resource locator](fulcra_collect/_resources.py) retains its runtime behavior.

Lexical presence of either root `_build_resources` or `PKG-INFO`, including a
dangling marker, selects archive mode. Missing/malformed archive inputs fail;
neighboring workspace resources cannot repair them implicitly. Recreate the
source archive from complete prebuilt inputs. Selected roots/descendants and
redirecting resource path components must not be symlinks or special files.
Absent/unreadable inputs, traversal errors, empty SPA or missing regular index
fail with selected-mode/resource diagnostics when they reach the hook. Hatchling
can parse/reject `PKG-INFO` before hook initialization; those failures have backend
diagnostics, and this hook cannot promise it prevents that metadata read. This
structural contract has no manifest to detect an arbitrary unknown asset deleted
from an otherwise valid snapshot.

[Packaging tests](tests/test_packaging.py) build disposable direct/source/extracted/
default artifacts, compare all regular wheel members including metadata/RECORD,
and independently check resource bytes, archive names and failure cases. Negative
fixtures never damage live assets. Fixture builds disable backend bytecode writes
with `PYTHONDONTWRITEBYTECODE=1`; permission tests qualify whether denial is
actually enforced. This package gate does not establish a signed app, a current
frontend build, clean installed dependency graph or live-provider acceptance.

## Tests

```bash
uv run --package fulcra-collect --extra dev pytest -c pyproject.toml packages/collect/tests/ -q
```

The suite covers the daemon's request handlers, the scheduler /
supervisor, the SQLite migration path, every route module, the
account-fingerprint pre-flight, and the worker subprocess plumbing.
`tests/test_setting_options.py` covers synthetic option discovery, scoped
credentials, malformed responses, saved arrays, and the required-selection
enable gate. `tests/test_permission_request.py` verifies explicit-only native
permission requests, authentication, credential scope, and sanitized failures.
`tests/test_end_to_end.py` exercises a full
discover → enable → run → state-write loop against a stub plugin.

### Configuration changes cancel pending task writes

Workers receive `RunContext.config_epoch` with their settings snapshot. Collect
rotates that opaque revision when plugin settings, enablement, or credentials
change through the app or CLI. The task plugins check it before each write, so
turning preview on, disabling a plugin, removing a list, or reconnecting an
account cancels pending work. Interactive Fulcra sign-in/sign-out persists an
`account_transition` gate and rotates all plugin epochs before changing the token.
Task writes remain blocked throughout the change, including workers started during
it. After successful token mutation, another epoch rotation invalidates those
workers before the gate opens. A failed mutation or final configuration save leaves
task writes blocked until an interactive sign-in/sign-out successfully completes;
automatic token refresh cannot clear the gate or rotate epochs. Ordinary settings
saves preserve the stored gate, and overlapping account transitions serialize
without holding the configuration save lock during Keychain prompts. Returning to
an earlier selection starts a fresh baseline. Configuration saves serialize across
processes and replace the file atomically. Each loaded `Config` keeps an immutable
snapshot of its values; saves merge only the caller's edits against that snapshot.
Enabled membership, interval keys, and individual plugin-setting keys merge
independently, so a stale unrelated save cannot undo a disable or erase another
setting. Divergent changes to the same field raise `ConfigConflictError` before
writing; reload and retry the edit. Successful saves refresh both the caller's
values and its baseline with the merged result. Explicit epoch rotations and the
stored account-transition gate retain their separate lifecycle rules. Direct
edits to `config.toml` bypass this lifecycle tracking.

Each task write holds shared account and configuration locks from its final consent
check through the source or Fulcra call. Settings saves and account changes take
exclusive locks, so an already-running write finishes before a change takes effect;
the next write checks the new settings. Lock acquisition times out after 30 seconds
and leaves the run retryable. A remote service may still finish a request after a
network timeout; Collect cannot cancel work that service has already accepted.

When concurrent changes conflict, the web API returns HTTP 409 with a reload/retry
message, and the CLI exits with a retry message. Neither reports success nor reloads
the daemon; the concurrent configuration remains intact. Error text omits setting
values. Configuration writers must use `enable`, `disable`, `set_interval`, and
`update_plugin_settings` for explicit user actions, so even a request equal to its
loaded baseline is preserved as intent and conflicts are rejected visibly.

The CLI accepts multiselect values as JSON arrays of unique, nonempty string IDs:
`fulcra-collect set-setting apple-reminders selected_lists '["example-list-id"]'`.
Use `[]` to clear the selection; a required empty selection cannot be enabled.
The dashboard discovers IDs and labels for you, so it is the easier setup path.

OAuth completion uses a packaged same-origin script and inert escaped plugin data, so the strict browser CSP allows the wizard to advance after sign-in. Failed or expired callbacks do not signal completion.

Selected local-source uploads require Collect's own current sign-in. If it is
missing or cannot be renewed for that same account, reconnect in Collect;
these sources cannot adopt the CLI's independent account. Replacing a selected folder or
its parent with a symbolic link stops the scan; choose the folder again.

Upload consent checks re-read the stored account token with a bounded Keychain
read, so a worker cannot miss a token replacement made by another process.

The bundled **Notion pages** plugin copies explicitly selected pages as
Markdown, with preview on by default. It needs a read-only internal Notion
connection; setup currently requires a workspace owner. Linked pages,
databases and attachments are marked as omitted. It ships in the 0.1.4
Mac installer; live Notion verification remains pending. See [setup and limits](../notion/README.md).

Notion setup probes check the live page selection and credentials before each
request, including while the plugin is disabled. A selection change cancels the
probe; malformed page content is never saved as an empty successful copy.


### Browser sign-in and reconnect

Onboarding and Settings use the same [browser-link helper](../web-ui/dist/static/browser-signin.js).
The [SDK actor](fulcra_collect/owned_auth.py) prepares a verification link/code,
then the helper sends bounded protocol 2 polls carrying the attempt UUID and
captured device code. Only an explicit successful completion advances the UI.
The server verifies the current API principal before committing credentials.
The existing split-route names are retained, but old unscoped completions and
`/api/fulcra/auth/cli_login` return 409 before effects. No CLI or terminal
credential store is used for new sign-ins, and PATH does not affect availability.

The private [auth-attempt journal](fulcra_collect/auth_attempts.py) fences newer
sign-in, token paste, signout and cancellation against late callbacks. It keeps
hashes and ordering metadata, while access/refresh secrets stay in Collect's OS
Keychain service. Retain the journal across restart. An interrupted claimed
grant or unknown rotating-refresh result requires a new sign-in; Collect does
not replay it. Browser cancellation clears the displayed link/code and prevents
late UI completion; an older cancellation cannot cancel a newer attempt.

Owned automatic renewal uses one SDK request under a cross-process lease and
checks the committed auth revision, account and original credentials again
before storing. Its SQLite-only claim/revision operations avoid taking the
interactive-auth lease inside callers' account-shared scopes. It preserves source epochs. Interactive changes rotate epochs
under the account-transition gate, which stays closed until both credentials and
the journal revision commit. Incomplete transitions appear as requiring reconnect,
and stale renewal failures cannot relabel a newer successful sign-in.
Manual tokens require an explicit new sign-in when expired. The principal-checked
CLI renewal compatibility path serves only pre-upgrade credentials with no
committed owned-auth revision; the next successful interactive sign-in retires
that authority. Actual packaged/signed Keychain and restart QA remains a release
gate; these source-level contracts do not claim a production cutover.
