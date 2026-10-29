# Collect v1 Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all Collect record paths and historical Collect records to v1,
consolidate persistent collection on the destination host, and ship the QA'd app.

**Architecture:** A shared v1 client and schema declarations replace independent
legacy record paths in bounded families. Account-scoped destination mappings,
deterministic record IDs and verified migration checkpoints protect retries and
cutover. File libraries retain their existing semantics.

**Tech Stack:** Python 3.13, httpx, jsonschema, Fulcra SDK >=0.1.54, SQLite,
pytest, existing TypeScript/Swift browser code, Briefcase and macOS launchd.

**Spec:** [Design](../specs/2026-10-23-collect-v1-design.md)

## Global Constraints

- Actual datatype `api_version=v1`; use Event/Metric, never annotation fallback.
- Preserve source identity, UTC times, values/units, tags and declared fields.
- Data-dependent state, credentials, account IDs and real samples stay private.
- Keep originals and rollback state; no historical hard-delete/ref rewrite.
- Respect existing preview, selection, config-epoch and account-transition guards.
- Failed or partial reads never count as empty or complete.
- README and AGENTS.md updates travel with behavior; independent bus review gates merge.
- User authorized planning and execution; routine choices are implementer rulings.

## Review Focus

- An account switch during a queued batch must not send data to the wrong owner.
- Unknown/undeclared fields must fail before upload rather than silently disappear.
- A timeout after acceptance must reconcile existing IDs before retrying.
- A host-local source absent on the destination must remain a visible cutover blocker.
- Browser collection must survive source-host daemon retirement, including relay needs.

## File and interface map

- `packages/fulcra-common/fulcra_common/v1.py`: flat record builders and explicit
  v1 schema-validated transport; `V1DataClient(BaseFulcraClient)` adapter.
- `packages/fulcra-common/fulcra_common/data_types.py`: declarative destinations,
  owned compatible resolution, creation and schema binding.
- Existing `ingest.py`, `wire.py`, `client.py`, `definitions.py`: audit shared
  readers/callers; remove Collect legacy paths as consumers transition, without
  changing unrelated coordination streams implicitly.
- Media/CSV/Day One/PurpleAir/labs packages: record-producing families.
- Collect daemon/plugin/setup/routes and menubar: destination settings, record
  editing/deletion, local runtime/cutover and packaged behavior.
- Attention Chrome/Safari and vendored Netflix: independent serializers.
- `packages/collect/fulcra_collect/migration_plan.py`, `migration_ledger.py` and
  `migration_convert.py`: immutable scope, durable raw/proposal storage and
  versioned offline conversion. Network export/apply and CLI integration remain
  separate work.
- Private runtime checkpoint: host inventory, account-bound mappings, source
  exports, cutover receipts, test logs and release receipts. Never track it.

### Task 1: Shared v1 record boundary

**Files:** create `fulcra_common/v1.py`, `tests/test_v1.py`; update
`fulcra-common/pyproject.toml`, `README.md`, root `AGENTS.md`, workspace lock.

**Interfaces:** `build_event_record(...) -> dict`,
`build_metric_record(...) -> dict`, `V1DataClient(client).ingest(data_type,
records) -> dict | None`. Builders accept complete custom type IDs, explicit
aware time/source identity, reserved base fields and declared enrichment.

- [x] Write tests for Event/Metric time shape, stable destination-scoped UUIDs,
  enrichment preservation, repeated sources, malformed IDs/times/metrics,
  reserved-field collisions, unknown/schema-invalid batch refusal, unavailable
  schema, explicit v1 request, preserved upload receipt and uncertain responses.
- [x] Run tests RED, implement the smallest shared boundary, then GREEN.
- [x] Upgrade the SDK floor/lock to the verified v1-capable version; run the
  shared package suite and aggregate baseline. Report failures by name.
- [x] Document the new opt-in boundary without claiming callers are migrated.

### Task 2: Destinations and schema declarations

**Files:** create `fulcra_common/data_types.py` and tests; update the shared
README/AGENTS.md. Ruling: integrate Collect setup/routes atomically with each
corresponding writer in Task 3, so old serializers cannot bind a new type.

**Interfaces:** immutable `DataTypeSpec` (base, name, description, field schema,
metric spec); `resolve_data_type(spec, account, pinned_id=None)` returns full ID
and served schema. Reuse no unverified account cache.

- [x] RED: legacy/shared/deprecated/name-ambiguous/wrong-schema candidates,
  stale account cache, absent versus failed listing, creation/readback failure.
- [x] Implement explicit owned v1 resolution and schema-preserving creation;
  verify no record POST occurs with unresolved ownership/schema.
- [x] GREEN shared/aggregate tests; document migration mappings and UI choices.
- [x] Live synthetic Event creation, declared-field readback, and same-ID
  changed-content convergence verified. Private receipts are not public fixtures.

### Task 3: All Python record-producing plugin families

**Files:** media helpers, CSV importer, Day One, PurpleAir, labs, Collect
quick-record/delete paths, definition configuration and corresponding tests/docs.

**Consumes:** Tasks 1–2 builders/client/specs. **Produces:** each family writes
and reads its v1 destination with unchanged source/dedup/consent behavior.

- [x] Complete a 26-plugin coverage matrix with file-only and host needs.
- [ ] Per family RED: legacy request detection, exact payload/readback parity,
  changed account/selection, rejected line, interrupted import and repeat run.
- [ ] Migrate definition setup, record serialization and destination-scoped
  readback together. Preserve enrichment in custom declared fields.
- [ ] GREEN family suites and aggregate suites; no claimed landed rows before
  exact visibility. Record unresolved historical source semantics explicitly.

Development progress: PurpleAir, the 16 bundled media paths plus retained Spotify
backfill, and Day One's Collect Journal path now use guarded true v1 writes.
Day One preserves import-once cloud edits and exact legacy source identity.
Notes/Gmail and task-source callers now have captured Collect account/settings
and source-read/effect checks. Gmail uses destination-scoped delivery state and
embedded canonical coord transport; task sources retain their completion journals
and leases with preview read consent. All fourteen standalone media
import routes and Day One CLI now share guarded v1 recovery.
Standalone [CSV](../../../packages/csv-importer/README.md) now has actual native
Event/Metric import/bootstrap, strict CLI input, source-authorized merges and
original pending recovery; pure parser/media consumers retain defaults.
Native Undo/repair once-only QA and browser default rollout remain open,
along with real provider/host and packaged QA. The five [standalone poll routes](../../../packages/media-helpers/README.md#standalone-v1-polling-development-branch)
now select strict diagnostics, bounded credentials and scoped cursors with public
withheld reasons. The same five [daemon polls](../../../packages/media-helpers/README.md#daemon-v1-polling-development-branch)
now use strict scans and separate v2 scopes without adopting old keys. Quiet
consumed-secret rereads, a second guard and exact cursor readback protect progress;
configured caps/malformed rows withhold, and bounded initial scans may remain UNKNOWN.
Source acceptance and installed rollout are still open.
[Observed partial-delivery count retention](../../../packages/media-helpers/README.md#observed-import-progress-development-branch)
is implemented and adopted by all fourteen migrated CLI error paths.
Standalone [Trakt policy](../../../packages/media-helpers/README.md#standalone-trakt-policy-development-branch)
now binds displayed exact-pair consent to fresh shared proof, checks excluded
current-input highs, preserves invocation-only authority and reports post-cluster
writer totals including policy skips. Its actual caller recovers before credentials
and retains current error counts; no cursor or normalized source-ID changes.
The [Trakt source completeness contract](../../../packages/media-helpers/README.md#trakt-history-completeness-development-branch)
now requires complete bounded returned pagination before current delivery on the
actual CLI and ordinary Keychain worker. Effective clamped headers/cardinality
and unique unchanged IDs are checked before source completion; one cooperative
budget spans source attempts/auth, with rotated tokens retained before a late
refusal. Source failure does not roll back earlier recovery/auth effects.
The [Trakt source owner](../../../packages/media-helpers/README.md#trakt-source-ownership-development-branch)
now adds exact local tuple/consent generations, shared consumed-grant ownership,
recoverable secret receipts and post-recovery current-work fencing. The daemon
never implicitly selects the legacy CLI file. Provider identity/historical
ownership, installed/signed access and other-host writer retirement remain
separate before cutover; this is not an immutable account snapshot.
The [standalone webhook](../../../packages/media-helpers/README.md#standalone-webhook-development-branch)
now uses a private thread for saved authority, original recovery, serialized jobs
and runtime cleanup. Recovery/all-category preparation precede bind; lifetime CLI
leases and admission/drain/join protect accepted work. Real synthetic HTTP and
lifecycle tests cover v1 delivery and holds; installed service/provider acceptance
and cutover remain open. Health is cached liveness rather than authority proof.
The [unbound media writer retirement](../../../packages/media-helpers/README.md#retired-legacy-media-writers-development-branch)
removes the obsolete annotation/wrapped/typed pipeline and orphan CLI helper,
with early refusal at retained public record seams. Captured Collect/native
behavior, read/admin/tag helpers, source identities and pure conversions remain;
caller/test mapping substantiates the deletion. Other standalone families remain
in the audit rather than inheriting this media-specific completion claim.
The opt-in quick-record actor now persists dispatch intent, retains
uncertain creates for exact-ID reconciliation without resubmission, and verifies
native Undo with permanent exclusions. Account-scoped old-epoch journal reads
grant no replay authority. The opt-in versioned daemon/HTTP backend now includes
owned per-track choices, account-scoped pin diffs, durable frozen timer stops,
paginated action/timer discovery and stop-snapshot reconciliation across crash
gaps. The native client persists frozen requests before socket IO and
validates action/timer response proofs across restart, with bounded larger
protocol2 responses. Development native popover/preferences and web favorites
now use protocol2,
while obsolete requests refuse before auth/effects and their legacy serializers
and sentinel Undo are removed. Native drafts and late callbacks are scope-bound;
pending server/local work has recovery pages beyond recent display limits.
Actual synthetic AppKit renders and real UDS/controller integration are verified.
Explicit old-epoch reauthorization and packaged interactive QA remain pending.
Shared JSON snapshots reject nested key coercion/invalid Unicode
before storage or effects. See the
[coverage matrix](../../collect-v1.md) for the released-versus-development scope.

Development labs activation now routes the installed CLI to actual per-marker
Metrics, preserving registry, conversions, review gates, collection clocks and
source hashes. Dedicated CLI authority, explicit selected-legacy pins, strict
read-only old cache, reverse witnesses and fenced pending destinations precede
new JSON/PDF access. Whole new batches are schema/existing-primary conflict
checked and staged before record POSTs. Canonical units live in exact destination
metadata, source raw units/provenance remain, and PDF bytes stay local. Exact
complete-group current/recovery lower bounds survive failures; current completed
status has its own timestamp, separate recovery observations, and same-DB guarded
atomic checkpointing with uncertain COMMIT marked UNKNOWN. Unsupported marker
attribution remains UNKNOWN. Legacy positive writers are retired; public skill
now teaches recovery/status probes. Actual reports, installed release and
historical family codec/application remain unverified.

Development Trakt now checks [current provider identity](../../../packages/media-helpers/README.md#trakt-provider-identity-development-branch)
under exact selected-credential lineage before and after history, requiring terminal
admission before normalization/policy/current import. Private journal v2 preserves
valid v1 source/attempt/consumed state and retains a selection pin across credential
changes; mismatch stays held. The daemon shares one exact-Trakt reactive 401
allowance, retaining complete credentials before reproof; CLI gains no reactive
retry. Identity GETs use cooperative bounds and strict complete framing. First pin
is no historical attribution or live/signed/cross-host acceptance.

### Task 4: Browser and vendored writers

Development progress: opt-in Attention Event schema and Python/TS/Swift encoders
now share synthetic source/UUID/full-field vectors, preserve captured milliseconds
and reject lossy bounds. The initial schema fingerprint is pinned and historical
null device identity is preserved. Default transports and installed browser state
remain unchanged; owned resolution, immutable account-bound queues, verified
delivery, Safari protocol and active-visit migration remain required. The opt-in
browser journal now persists transactional account/generation transitions and
write-ahead Event creation intents; current auth/resolver/default writers still
need to use that gate and prove the API principal.

The opt-in decision authority now reduces repeated proofs through one complete
opening/closing inventory pair per snapshot and fresh A/policy/source/B closure.
Original journal predicates and retained phase/terminal proof bytes remain.
Synthetic delay and isolated native Chrome TLS/DNR/IndexedDB checks cover
settlement/restart/UNKNOWN-no-resubmit and zero-HTTP terminal history. Auth must
publish a new generation before every standalone bearer replacement. The
[source-only Chrome worker owner](../../../packages/attention/README.md#worker-auth-owner-development)
now supplies an exclusive bounded renewal exception with exact predecessor,
retained response and proved same-account successor. Actual opted-in auth/settings
publishers use versioned commands, authoritative atomic IndexedDB settings and
mandatory owned cancellation at final actor/setup transactions. Both opted-in
drains remain disabled. A separate once-only initialization witness prevents
whole-database loss from silently reimporting legacy settings, including a
conservative pre-create-crash hold. Synchronous publisher cancellation fences
auth CAS; local scope commands do not await online recovery. Read-only page
settings commands wait for worker-local initialization, with loading/held UI
instead of direct page authority creation or an online recovery dependency. Pause retains
selected authentication with action admission closed and resumes under fresh
consent/generation. The source-only
[held capture ingress](../../../packages/attention/README.md#held-capture-ingress-development)
now routes actual visit/history input through durable privacy-bounded checkpoints,
strict atomic serialized-row accounting and stable acknowledged input identities.
Actual descriptors remain null until completed setup; retained evidence and
uncertain restart/scope gaps are not uploaded records. Original legacy stores and
observed old/new arrivals remain evidence. Pending discovery/original-owner
draining remain separate slices; unannounced token-only replacement remains
unsupported. Provider latency, installed/default auth and Safari
acceptance remain separate gates. See the
[canonical authority contract](../../../packages/attention/README.md#decision-authority).

Sequencing amendment: the unused [bounded local evidence prerequisite](../../../packages/attention/README.md#bounded-local-evidence-development)
may be implemented and component-reviewed while browser native QA is incomplete.
It adds no production caller, fresh-origin/setup producer or action admission.
Original capture identities, strict writers and default/held modes remain. This
does not waive native QA or permit later setup/discovery/activation stages; new
source/build bytes need their own exact review and relevant native proof.

**Files:** Attention TypeScript/Swift, relay, Netflix script and their tests/docs.

- [ ] RED: v1 destination selection, moment/duration shape, offline queue retry,
  expired token, schema mismatch and source-host relay requirements.
- [ ] Align independent serializers with the declared v1 contract; preserve
  explicit user destination intent and queued-event identities.
- [ ] GREEN JS/Swift/relay tests and actual browser-to-v1 readback canary.

### Task 5: Resumable historical converter

**Files:** Collect migration plan/ledger/converter/raw HTTP reader modules and
tests; future orchestration/CLI/apply, shared v1 reads and migration docs.

**Interfaces:** immutable `MigrationPlan` binds account/mapping/schema/ranges;
`MigrationLedger` durably records exported, converted, submitted, verified and
quarantined row states. `plan`, `export`, `apply`, `verify`, `status` are distinct.

- [ ] RED: bounded enumeration, boundary records, partial reads, deterministic
  conversion, duplicate source IDs, tombstones, lost receipts, crash/restart,
  mapping/account changes, schema loss and full-payload readback mismatch.
- [ ] Implement offline converter and SQLite ledger first; dry-run all scoped
  legacy Collect types using private exports. Verify coverage and retain raw data.
- [ ] Apply a small canary, reconcile all fields, then bounded batches with
  checkpoints. Resume uncertain rows by ID, never blind retry.
- [ ] Share a versioned primary-identity extractor with each live importer;
  quarantine missing/colliding IDs and verify same-ID API behavior. Discover
  definitionless undo markers and preserve their effective-state mapping.
- [ ] Prove export/readback coverage with adaptive splitting, overlap and ID
  dedup; fail on saturated minimum windows rather than certifying a row-capped
  response. Retain explicit dispositions for every quarantined row/marker.
- [ ] Defer historical finalization until Task 6's per-source old-writer cutoff,
  reconciled queues and late-upload delta sweep. Early converter/export work
  remains executable; do not prematurely certify a still-growing legacy stream.
- [ ] GREEN tests and private final inventory/readback report; do not delete originals.

Development progress: immutable MigrationPlan and private raw-export ledger are
implemented with regression tests for changed scope/schema, raw variants,
overlapping observations, saturation/gaps, atomic raw snapshots and split
children, ordered seal evidence,
corrupt classifications and single-row access independent of window count.
Shared v1 readback now compares fractional timestamps without truncation; plan
bounds reject inexpressible nonzero submicroseconds and malformed timezone
offsets. Status distinguishes window receipts from a fresh raw-content audit.
The first offline codec covers flat typed DurationAnnotation media records,
preserving declared enrichment/null/absence and the shared live/history UUID.
Unsupported/lossy records quarantine; schema references cannot fetch remote
content. Schema-2 conversion proposals carry recomputable proofs, and conflicting
targets atomically quarantine every prepared variant. Restart, coherent hash
tampering, interruption rollback and concurrent conflict checks are covered.
A separate v2 reader codec preserves definition snapshots and prefixed source
bindings after account/definition/kind proof. Its history schema is opt-in with
a distinct key/name; default media fingerprints remain unchanged. Synthetic live
readback of full nested provenance is verified. A separate bounded raw HTTP
reader retains private per-attempt manifests/raw bodies/EOF receipts under
explicit account/origin guards and immutable pass scope. Partial/error/malformed
responses cannot become clean empty results; no transport receipt claims provider
coverage. A paired library driver now binds an explicit provider completeness
contract, resumes indexed deterministic windows and atomically retains all pair
versions/proof plus conditional seal or overlapping split. No implicit provider
contract is invented; work-queue progress is not final coverage. Production
export orchestration, full raw audit, remaining family
codecs, submission/readback and final history gates remain open. Personal source
samples have only been dry-run privately against hypothetical targets; historical
apply has not run.

The optional [Journal reader codec](../../../packages/dayone/README.md#opt-in-historical-journal-contract-development-branch)
now adds a separate nullable historical contract while keeping existing live
Journal defaults and creation fingerprints frozen. Static lazy dispatch retains
the eight existing media codecs and raw-only exports for future converters.
This pure implementation does not create targets, select the new contract for a
live writer, establish export coverage or apply historical rows.

### Task 6: Destination installation and source-host retirement

**Files:** current install/config/state/runtime helpers and tests; private host
inventory, backup/cutover manifests; installation READMEs and AGENTS.md.

- [ ] Inventory enabled plugins, source availability, credentials by reference,
  account identity, launchd jobs, SQLite state and browser transport dependencies.
- [ ] Back up consistent state and stage v1 runtime disabled/previewed here.
- [ ] RED/GREEN safe state import, wrong account, epoch invalidation, rollback,
  source absence and browser-only retention checks.
- [ ] Verify destination sources, launchd restart and v1 readback; cut over one
  source at a time, disabling superseded source-host collection only afterward.
- [ ] Prove browser plugins still collect; record each source's new owner and
  any actual missing grant/sign-in/library prerequisite.

### Task 7: Adversarial QA and justified simplification

**Files:** all changed surfaces, obsolete serializers/helpers after caller audit,
tests, relevant READMEs/source catalog and AGENTS.md.

- [ ] Independent whole-change review of privacy, validation, source loss,
  account binding, dedup, async ingest, UI/restart and operator-visible errors.
- [ ] Inventory legacy write paths and unused code with actual caller evidence;
  RED reproduction for relevant defects, simplify, then re-run affected checks.
- [ ] Run full Python and applicable JS/Swift suites, UI first-run/upgrade,
  source failures, privacy export, credential scan and docs/link QA.
- [ ] Obtain independent bus verdicts and green exact-head CI for every PR.

Service-startup QA reproduced a worker exit that bypassed cached status.
The [scoped status implementation](../../../packages/collect/README.md#service-failure-status-development-branch)
now records nonzero exits/launch failures atomically, preserves restart policy,
fences original scope and exposes volatile UNKNOWN rather than stale account
fields. Exact source QA: 369 affected Python tests and 95 UI tests; independent
review gates this slice. This does not establish live receiver readiness or
complete the whole-change QA/release gates above.

### Task 8: Signed app publication and final acceptance

**Files:** release metadata, packaging/source catalog/docs; private Apple/build receipts.

- [ ] Build from reviewed merged source, prove all 26 plugins and bundled SDK/
  launchers exist, and QA clean install/upgrade/restart in the frozen runtime.
- [ ] Sign; reconcile pending notary attempts; notarize, staple and Gatekeeper.
- [ ] Publish exact artifact, anonymously verify checksum, install here and
  run actual sources plus browser retention and historical ledger exit checks.
- [ ] All four design completion gates need receipts. Update task/checkpoint;
  only then pause the continuation heartbeat and report completion.

## Per-turn exit procedure

- [ ] Reconcile current human answers, reviews and uncertain external actions.
- [ ] Record actual test/readback evidence and completed task checkboxes.
- [ ] Update durable owned task and private checkpoint with exact next command.
- [ ] Run the private exit checker. If executable work remains, continue now.
- [ ] If an external prerequisite blocks one task, retain it and work on the
  next independent safe task. No approval inferred from elapsed time.


Development QA evidence: the first self-contained draft loaded all 26 plugins
with no load errors in a separate disabled profile. Its embedded urllib/Auth0
flow failed certificate verification because the Python support runtime lacked
default roots; the same bundled CLI succeeded with its included `certifi` file.
The bundled entry point now supplies that default before dispatch while preserving
explicit CA overrides and normal TLS checks. Packaged rebuild/retest and signed
runtime QA remain required; this is not a production cutover.

Packaged QA also exposed the obsolete Settings reconnect path: a current SDK
noninteractive `auth login` exits after issuing a device link. Onboarding and
Settings now share the explicit split browser flow, and the old blocking route
refuses before CLI/credential effects. This does not yet replace CLI delegation
with a Collect-owned SDK/refresh-token actor or prove stale-auth completion
fencing; those remain follow-up QA within this migration before release.


The implemented SDK actor replaces new interactive CLI delegation:
private durable attempt/hash ordering, bounded single-grant polls, cancellation,
API principal verification, Collect-owned Keychain refresh state and cross-process
renewal with a persisted no-replay claim. Older same-account CLI renewal remains
only until a successful owned-auth revision. Synthetic auth/restart/race and UI
poll/cancellation tests cover this behavior; actual packaged/signed Keychain QA
and fresh local human sign-in remain open. The private all-sources-disabled QA
app has since been rebuilt, bundle-verified and restarted with this actor.


Attention's next opt-in boundary captures account/generation/request before
awaiting, proves the principal against the API and checks current authority
around each provider/body read. Requests use a fixed production origin, no
redirects/cookies, finite JSON and bounded retained decoded bodies; there is no
refresh or automatic POST retry. Default auth/senders remain unchanged pending
owned resolver, immutable queues, exact/exclusion delivery and native protocol.
Synthetic transport/account-change tests and both TypeScript builds validate
this source seam only; installed browser/CORS/upgrade QA remains open.

The following opt-in tag slice upgrades IndexedDB v1 to v2 without clearing
existing gates, generation history or datatype intents. Unique account/name
reservations precede creation; known UUIDs persist as submitted before required
name readback. Only a target 404 permits creation, never an identity-check 404.
Unknown attempts stay pending and resolved UUIDs survive renames without
replacement. Synthetic upgrade, concurrency, strict durability, quota, capacity,
lost-response and UUID-substitution checks precede independent review. Default
capture/auth/senders and actual browser state remain unchanged; served-schema
destination resolution and immutable queues are the next source seams.

Provider QA found the aggregate datatype catalog can silently omit unavailable
input-service results. The shared owned-type reader now uses the direct
authenticated input endpoint, includes retired types, validates the whole list
and refuses unknown/duplicate identity metadata. Resolver, exclusion discovery
and setup picker share it; served-schema validation is retained. Creation and
name uniqueness never fall back to aggregate emptiness. Independent partial206
and redirect reproductions require exact200/origin/path/query, no redirects,
pagination or conflicting totals before reserving an intent. Read-only actual
endpoint/synthetic-destination checks, focused source integration, full Python
suite and independent re-review precede publication. This changes no browser
profile, source consent, production cutover or historical records.

The browser's following inventory seam uses that same direct authority through
the principal/generation-fenced HTTP helper. Exact route/query, complete200,
no-cache requests, pagination/totals, whole-list identity validation and frozen
results precede any future binding. A redirected or wrong-URL404 is a scope
failure, never tag-absence evidence. Synthetic real-helper integration and both
TypeScript builds verify this source seam; schema-proved binding, durable
creation/queues and actual browser rollout remain next gates.

The pure Attention schema seam pins the Python creation fingerprint and checks
a closed known Event/Attention served-schema family. Unsupported assertions,
references, fields or constraints remain held. Parsed schema is deeply frozen
before hashing, and the exact captured serialized text travels with its UTF-8
SHA-256 digest. Synthetic false-positive/refusal and fingerprint tests precede
independent review and both builds. This proves neither provider ownership nor
delivery; durable account/generation/type binding is the next source seam.

The following setup binder requires fresh owned inventory, closed input AND
served schemas, a final active/unchanged-input check and one journal authority
for HTTP/local effects. It preserves pins/renames and known returned UUIDs,
and reconciles redirect/lost creation with reads rather than another POST.
Synthetic restart/concurrency/late-account/schema races cover it. Default UI,
sender/immutable queues/exclusion delivery and actual provider/browser upgrade
remain separate gates; no personal records or source consent change here.

Development vendored Netflix activation now routes the canonical installed
console/script to shared Watched Events, preserving parser hashes and provenance.
Version/namespace capability checks, independent CLI account/profile/pin/spec
binding, all-pending-target proof and original recovery precede source access.
One reader materializes current input; whole schema/primary-ID proof precedes
serial <=500-record durable in-flight chunks. Exact completed-group current and
recovery evidence stay separate. No annotation fallback, token subprocess helper
or skip-verification remains. Public guide examples are checked against the actual
codec. Native sharing, wheel/clean-install acceptance, real source/account QA and
historical conversion remain separate gates; see the
[package contract](../../../packages/netflix-skill/README.md).

Development Collect distribution repair now carries prebuilt SPA/docs in a
source-archive-only snapshot and rebuilds the existing wheel resource layout
without staging runtime assets. Direct/extracted/default wheel parity and
fail-closed disposable fixtures are the package gate; immutable inputs, cached
offline backend, frontend preparation, installed dependency/app QA and signing
remain separate. See the [canonical build contract](../../../packages/collect/README.md#distribution-resources-development-branch).
