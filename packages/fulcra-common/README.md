# fulcra-common

Shared Python plumbing for the importers and agent tools: Fulcra API access,
annotation definitions, record encoding, ingest, and media fingerprints.
This is a library, with no CLI or Collect daemon to run. Other packages use
it without needing the Collect app.

If you're adding a new importer or extending an existing one, this is
the package whose surface you build against — never re-invent any of
these primitives locally. The whole point of `fulcra-common` is that a
Fulcra wire-format change is a one-place change here.

## Package layout

| Module | What it owns |
|---|---|
| [client.py](fulcra_common/client.py) | `BaseFulcraClient`: auth, HTTP transport, record reads, tags, and definition CRUD; subclasses supply importer behavior. |
| [definitions.py](fulcra_common/definitions.py) | `resolve_definition_id`: adopt or create a definition, rejecting incompatible schemas with `DefinitionSchemaMismatch`. |
| [ingest.py](fulcra_common/ingest.py) | Event dataclasses and `IngestPipeline`, including wrapped and typed ingest paths. |
| [wire.py](fulcra_common/wire.py) | Wrapped envelopes, unwrapped typed records, source arrays, and definition payloads. |
| [cross_source_fingerprint.py](fulcra_common/cross_source_fingerprint.py) | Shared fingerprints for music, podcasts, movies, and TV episodes. |
| [schema_check.py](fulcra_common/schema_check.py) | Fetch the server's record schema and check payload fields against it. |
| [v1.py](fulcra_common/v1.py) | Native v1 Event/Metric builders and full-destination schema-validated ingest. |
| [data_types.py](fulcra_common/data_types.py) | Owned compatible v1 destinations and durable datatype-creation recovery. |
| [annotations.py](fulcra_common/annotations.py) | Agent lifecycle, needs-user, projection, and digest annotations. |

## Install

Python 3.11+. From the repository root:

```bash
uv sync --package fulcra-common
```

The runtime dependencies are `httpx`, `jsonschema` and `fulcra-api>=0.1.54`; the latter supplies
library operations and the auth CLI. Pure record construction needs no account.
API calls require an authenticated Fulcra account (`uv run --package fulcra-common
fulcra auth login`).

## V1 datatype migration boundary

`record_matches` compares every submitted field and normalizes RFC3339 whole
seconds to UTC while retaining arbitrary fractional digits. Trailing zeros are
insignificant; a nanosecond difference is a mismatch. It never proves delivery
by truncating a timestamp to Python datetime precision.

Verified October 23, 2026 against the live catalog and served schemas: actual
v1 custom datatypes use `Event/<UUID>` and `Metric/<UUID>`. The `/ingest/v1`
URL alone does not make a legacy annotation a v1 datatype. The workspace now
locks SDK 0.1.56. The [migration status](../../docs/collect-v1.md) tracks
current caller conversion and remaining gates. Source-branch migration does
not establish installed-app or provider acceptance.

`v1.build_event_record` and `build_metric_record` take a complete custom type
ID, primary source ID and timezone-aware start time. Both emit explicit
`start_time`/`end_time` (equal for a moment), stable destination/source-scoped
UUIDs, sources and tags. Declared enrichment goes in `fields`; reserved base
fields cannot be overwritten. Metrics require finite numeric values and reject
booleans. A controlled migration can supply an explicit UUID, but live and
historical importers must agree on the same primary identity mapping.

`v1.V1DataClient(existing_client).ingest(data_type, records)` uses that client's
account-bound auth and transport. It reads the destination's v1 schema,
validates the complete batch with the SDK's ETL-aware format checker, refuses
undeclared fields and conflicting same-ID payloads, then posts JSONL to the
complete destination with explicit `api_version=v1`. No legacy fallback or
automatic POST retry occurs. Validation errors identify the record index and
reason without printing source values. Schema validation resolves local
references from that snapshot; unavailable references fail before POST, with
remote retrieval explicitly disabled.
V1 record and native tombstone POSTs reject redirects, including same-origin
307/308 responses that would otherwise repeat the request body.
The optional `before_submit` callback runs after validation and serialization,
immediately before the POST; raising cancels dispatch. Interactive actors use
this to persist their intent before a potentially lost response. The callback
does not prove that the request reached the server. A submitted action must
reconcile its original record ID before deciding its outcome.

For an explicit full-schema pin, both public methods accept the same optional
keywords (verified October 25, 2026 with mocked HTTP/SDK account fixtures):

```python
v1_client.validate_records(data_type, records, expected_schema=full_schema,
                           account_id=captured_account_id)
v1_client.ingest(data_type, records, before_submit=persist_intent,
                 expected_schema=full_schema, account_id=captured_account_id)
```

`expected_schema=None` preserves the existing schema read, auth, empty and
submission behavior; `account_id` participates only in pin mode. Supplying a
pin requires an explicit nonempty account string and a finite JSON dictionary
with dictionary `properties` and a valid Draft 2020-12 schema. These inputs and
the batch are privately snapshotted before client/auth calls. Invalid pins or
accounts refuse locally even for an empty batch; valid empty calls return
`[]`/`None` without I/O or callback and make no remote equality claim.

Pin mode uses the public
[`data_types.read_served_schema`](fulcra_common/data_types.py) reader:
`read_served_schema(client=existing_client, account_id=captured_account_id,
data_type=full_destination, timeout=None)`. It proves the captured token's
account, reads the exact full custom Event/Metric schema once, and returns a
strict bounded JSON dictionary. The [proof-read contract below](#schema-proof-read-contract)
defines its response gates and buffered parsing limits. Expected pins use that
same 1 MiB/depth 64/100,000-value policy. The reader alone does not validate a
record schema or prove an owned binding.

Each nonempty pinned invocation compares the **entire** final served schema,
then uses that same served dictionary for declared-field and record validation.
Object key order is irrelevant; arrays are ordered, absent differs from null,
and booleans differ from numbers. JSON numbers `1` and `1.0` compare equal.
Description, required-field, extra-property and nested-constant drift all refuse
before callback/POST. A preliminary validation never substitutes for ingest's
fresh final read. Caller mutations during auth, GET or `before_submit` cannot
change the frozen pin or submitted batch; the final JSONL body is serialized
before the callback, followed by one POST with existing receipt/uncertain-retry
semantics. Trusted callback/client internals and remote changes after the final
GET remain outside this guarantee.

The pin's frozen representation is `fulcra-exact-schema-json-v1`:
`json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
allow_nan=False).encode("utf-8")`. A future retained SHA256 identifies these
bytes only; it cannot replace JSON equality (numeric-equivalent spellings can
hash differently), a datatype fingerprint, a plan hash or a payload hash.
This optional seam has no production caller and grants no historical replay,
ownership, coverage or cutoff authority.

## Retained transport observations (development)

The opt-in [observation types and contracts](fulcra_common/record_observations.py)
retain actual HTTP facts without granting coverage, ownership or replay authority.
Verified October 25, 2026 with synthetic deny-network SDK/HTTP fixtures only:

- [`V1DataClient.observe_records`](fulcra_common/v1.py) accepts one tuple of
  1..32 distinct canonical UUID strings and returns an `ExactReadObservation`.
  It reuses the ordinary reader's single JSONL parse and exact selector validator;
  it rejects larger batches instead of splitting or truncating them.
- [`observe_served_schema`](fulcra_common/data_types.py) returns a
  `SchemaObservation` with a fresh `.schema` dictionary. The existing
  `read_served_schema` still returns a dictionary with its original contract.
- [`V1DataClient.ingest_observed`](fulcra_common/v1.py) requires a full
  `expected_schema` and zero-argument `before_submit`, retains its single final
  served snapshot used for full equality and record validation, then captures
  the actual one-POST receipt. Empty input validates local arguments and returns
  `None` without I/O, reservations or callbacks. A malformed nonempty receipt
  raises and never becomes `None`, a fabricated ID or an automatic retry.

Each producer requires explicit `account_id`, immutable `context`,
`on_observation`, `request_guard` and `request_budget` keywords. Context includes
an immutable original-target anchor; child account, attempt, original hash,
full datatype and ordered IDs must agree. Policy-control reads carry an exact
ordered deterministic projection and a context-hash parent reference; they never
count as positive evidence of the original targets. Schema IDs are empty. The
caller captures original provenance before I/O and supplies a fresh observation
ID; nested final-schema reads derive their own fresh ID.

Every observed schema/record GET and POST proves the account of its captured
token through the shared SDK account check, then reuses those exact headers.
The same guard and budget reach each leaf. Preparation is guarded, each SDK
account proof and HTTP operation is charged separately, and another guard runs
immediately before the outbound call after preparation. The POST guard follows
`before_submit`; no user or sink callback intervenes before that POST.
**Real SDK refresh/retry/credential internals have no established budget adapter;
production observed use remains held.** One SDK invocation is not a bound on
hidden network requests.

`RequestBudget(request_limit, retained=..., original=..., event_limit=128)`
requires a retained-store allowance bound to the exact store and original
attempt. The event ceiling is a non-bool integer in 1..128; resumed ceilings
must match. Observation, assessment, issued-checkpoint and outcome slots share
that allowance. Store protocols require verified manifests, reservation journals,
claim/append linkage and rereads across handles/reopen; revision is not an event
count. Each response reserves and starts its slot before preparation. Final schema,
receipt and issued checkpoint require separate capacity. Started, consumed and
uncertain slots cannot be refunded; only a positively verified unused held slot
can be released. Exhaustion or unknown accounting holds further work.

These are persistence contracts, **not a production store or transaction
implementation**. The [tests](tests/test_record_observations.py) use an explicitly
synthetic retained journal with atomic target-presence references and reopen
checks. Append and lookup verify each observation's original/account/attempt
against its reserved retained binding. Each evidence acknowledgement is stored
atomically with its event and lookup returns that original revision and mode,
including after other handles advance the journal. The presence index includes
all returned target rows, including payload mismatches; it does not resolve a
frozen original payload, run `record_matches`, or establish matching-positive
replay disqualification. `reload_history` refuses. Matching-payload positivity
and retained history are exercised by the unused
[synthetic historical reconciler](#synthetic-historical-reconciliation-development);
this transport fixture supplies neither. A caller boolean, arbitrary ref,
schema dictionary or constructor flag
cannot establish genuine catalog, deployment, ownership or replay provenance.
Policy result types and assessment/history protocols are inert; no production
policy observer, Collect wrapper, historical reconciler or migration driver is
activated by this component.

After a complete response is already buffered, local validation, immutable
capture, reservation claim, sink and exact acknowledgement run without a fresh
guard, token or network operation. Expiry permits that original-provenance audit
retention; it never authorizes another read, POST or verified outcome. Sink
failure or uncertain acknowledgement propagates. `audit-only` and
`decision-eligible` describe storage, and neither mode supplies current authority.
External absence/coverage and retained history require separate verified assessment.

The canonical byte constructors reject noncanonical input, unknown fields,
retargeted scope, malformed refs and noncanonical or reversed UTC clocks. Wire
UUIDs are lowercase; observation times have six microsecond digits and `Z`.
SHA256 derives from sorted compact UTF-8 JSON with `ensure_ascii=False` and
`allow_nan=False`; accessors return independent snapshots. The schema hash remains
`fulcra-exact-schema-json-v1`; JSON equality still governs pin comparison. The
receipt body hash uses the existing actual JSONL bytes, preserving SourceDispatch
and ordinary ingestion serialization.

Observations are capped at 2 MiB, served schemas at 1 MiB, and canonical document
parsing uses the shared depth-64/100,000-node strict parser, including the outer
evidence envelope. A deeply nested response can therefore pass the ordinary
reader but refuse observation serialization. Policy composition's six-document /
8 MiB linked-chain contract is exercised by the historical test fixture; the
production policy producer remains held. Request and
response metadata retain exact origin/path/ordered query/status and buffered body
length/hash, plus a finite header allowlist with explicit nulls for missing values.
Repeated allowlisted headers, contradictory framing, partial markers, owner
contradictions, redirects and encoded observed replies refuse; no credential or
arbitrary header is retained. These checks do not certify provider completeness.
**HTTP has already buffered the body: these are parsing/serialization limits,
not streaming, raw-wire allocation or whole-operation deadline guarantees.**
The ordinary JSONL parser still uses its existing finite-number semantics; the
schema and observed-receipt parser additionally refuses decimal precision loss.

Run the [observation tests](tests/test_record_observations.py),
[shared reader/default tests](tests/test_v1.py),
[served-schema/import tests](tests/test_data_types.py) and
[Collect policy authority integration](../collect/tests/test_v1_policy_authority.py)
when changing these boundaries. All live/default and non-observed pin behavior,
including receipt semantics and native deletion, remains supported.

## Inactive historical verifier capture (development)

Verified October 25, 2026 with synthetic deny-network fixtures only. The private
[prepared transport](fulcra_common/_historical_observed_transport.py) and
[untrusted transcript inspector](fulcra_common/_historical_owned_transcript.py)
share the existing account, owned-list, served-schema, exact-record and POST
helpers. **Both private transport constructors unconditionally refuse. There is
no production caller, owned-policy issuer, evidence namespace, admission gate or
Collect historical wrapper.** Tests alone bypass construction with
`object.__new__`; no runtime factory accepts fixture transports or authority.

The captured verifier reuses the ordinary read-only exclusion verifier core:
original resolved journal tuple and full control ID, full owned list, candidate
metadata/input schema and actual served schema, repeated candidate equality,
final account check and original journal reread. It also refuses an observed
journal file replacement. This check does not qualify a trusted original opener,
auth/config snapshot or cross-process gate. Ordinary policy reads keep their
existing helper ordering and default behavior.

A phase starts its proof reservation before preparation. Its exact operation
sequence is account/owned-list, account/schema, account/owned-list, account:
`4 account-proof + 2 owned-list + 1 schema-get = 7` charges. Before phase, actual
control account/GET and after phase therefore cost `7 + 2 + 7 = 16`. The SDK
account operation is the actual local, **unverified JWT claim decode**, with zero
HTTP; it is not authenticated principal evidence. The same guard, request budget
and original attempt reach every nested leaf. The closed branch prepares first,
checks the same in-memory bearer/scope under its supplied gate, charges once,
calls the final guard and sends the same sealed request. It never renews,
redirects or retries; the POST follows its separate issued callback. These are
fixture-exercised mechanics, not evidence of a qualified production gate or TLS
transport construction.

The collector returns untrusted canonical version-1
`collect-owned-binding-transcript` bytes, never a `PolicyBindingProof` or authority.
It captures actual received catalog/schema bodies, public raw request target and
body bytes, duplicate-preserving allowed response headers and local account
results; authorization/cookies/tokens/token hashes/arbitrary headers are excluded.
The acknowledged schema and candidate validator use the **same** served response.
Raw-body SHA256, canonical transcript SHA256 and existing schema hash algorithms
remain distinct. Before/after invocations and schema/proof/artifact IDs are fresh
when the body bytes match.

Each received fragment attaches to its already-started proof reservation, with
exact ordinal/digest/invocation/reservation/revision acknowledgement before the
next operation. Failed/uncertain append, acknowledgement or finish stops work and
keeps occupancy. Completely received bytes can remain audit-only after expiry;
that retention never authorizes another request, proof or verified outcome.
The six existing policy documents retain their shape; attachments add no event
slots. All event families still share the same retained allowance of at most128
across handles/reopen. Transcript plus proof is at most2MiB, complete six-document
chain including attachments at most8MiB, schema at most1MiB. The fixture writer
and document sink reread all actual retained chain documents and staged/final
attachment bytes under their shared storage lock, then check the prospective total
before each append, transcript finish or document retention. Overflow preserves
prior bytes and occupied slots and stops the next operation; a final size check
alone is insufficient. Staged fragments must match expected canonical bytes
exactly, including JSON number types. The existing16MiB catalog parser is unchanged;
historical proof overflow refuses without truncation.
These are buffered evidence limits, not raw-wire allocation or transfer deadlines.

Strict inspection replays full-list/candidate/schema validators and checks raw
bytes, attachment order and retained proof/schema/issuance linkage. Its result
remains untrusted: refs, canonical bytes, issuer strings, fixture registry entries
and successful inspection cannot qualify a real namespace. All storage/issuance,
semantics and admission composition in these tests is explicitly synthetic.

**Production remains held on H1 service/principal/owned-list semantics, H2 actual
immutable namespace/persistence/ack/reopen, H3 nonrenewing admission/gate/original
journal/writer participation, H4 effective closed HTTP/TLS dependencies and H5
coverage/external positive and issued history/CAS/fencing/drain/driver/apply.**
The future admission contract requires a finite numeric monotonic revision;
current Collect UUID/kind revisions and shared account leases do not qualify it.
No auth/provider/SDK integration or ordinary Collect behavior changes here.

Run the [transport](tests/test_historical_observed_transport.py) and
[transcript](tests/test_historical_owned_transcript.py) regressions with
`uv run --no-sync pytest packages/fulcra-common/tests/test_historical_observed_transport.py packages/fulcra-common/tests/test_historical_owned_transcript.py -q`,
then `uv run --no-sync pytest packages/fulcra-common/tests packages/collect/tests -q`.
New suites autodenied socket/DNS/urllib/subprocess/real HTTP transport/ordinary
credential lookup; only explicit inert fixture paths are exercised. Existing
[observation](tests/test_record_observations.py), [default](tests/test_v1.py),
[schema](tests/test_data_types.py) and
[Collect policy](../collect/tests/test_v1_policy_authority.py) boundaries stay covered.

## Synthetic historical reconciliation (development)

Verified October 25, 2026 with deny-network fixtures only. The unused
[`reconcile_historical_records`](fulcra_common/record_delivery.py) consumes the
existing immutable `HistoricalDelivery` type from
[record_observations.py](fulcra_common/record_observations.py). Its exact interface is:

```python
reconcile_historical_records(store, exclusions, data_type, records, *,
    dispatch, history, assessment, authority, ownership_proof, expected_schema,
    context, evidence_sink, retained, decision_sink, request_budget,
    before_submit, persist_outcome, allow_submit=False) -> HistoricalDelivery
```

Entry freezes and validates one original target, schema, dispatch, exact byte
inputs and the retained budget binding before preparation. Stores must implement
the observed B1a transport methods. Exclusions must also supply callable
`binding_proof(phase=..., context=..., on_observation=..., request_budget=...,
request_guard=...)` and `observe_exclusions` capabilities; ordinary
`RecordExclusions` supplies neither. The factory exists only in the
[historical tests](tests/test_record_delivery.py), closes over a synthetic owned
artifact/resolver, and emits fresh before/after strict schema and binding proofs.
Both phases receive the identical library guard, budget and sink. The fixture's
control-row validator is the same private pure core used by the unchanged live
policy reader; it performs no additional unobserved GET.

Control and target reads each need a separately retained assessment. Its local
`scope_hash` binds canonical `{original_target, control_projection, purpose,
request}` bytes; this identifies the exact fixture request and does not establish
provider completeness. Observation and decision acknowledgements are relooked
up before coverage use, and current authority is checked at every nested request,
issued transition and success return. Unknown control coverage cannot be filled
by target coverage. The policy chain is freshly observed after each target
readback before classification. Retained matching original-payload positives across attempts,
terminal outcomes and audit-only storage permanently bar a later first write.
Mismatches remain retained conflicts and never become matching positivity.
Unknown external history or issued evidence also prevents another POST.

The five singleton partitions are `verified_ids`, `excluded_ids`, `held_ids`,
`conflict_ids` and `unknown_ids`; the result also retains separate observation and
assessment refs and always has `historical_complete=False`. A visible excluded
target is held with a retained `excluded-pending-repair` reason. This path never
calls live reconciliation, native repair or deletion. Synthetic first write
requires explicit `allow_submit=True` plus all eligibility checks, one atomic
fixture-issued callback and independent readback before the single observed POST.
The actual final schema observation and validation use the same B1a snapshot.

Every normal return invokes `persist_outcome(candidate, freshly_reloaded_history)`
exactly once and requires a `None` return. That trusted fixture callback reserves
its own outcome slot, atomically checks revision/classification/proofs, appends
the immutable event and state, then independently rereads event, acknowledgement,
slot linkage and state bytes. The issued callback follows the same pattern in its
separate namespace. Both share the retained attempt's maximum 128 slots with
observations, assessments and the preheld receipt slot; journal revisions do not
count occupancy. Written-then-raised or uncertain append/readback propagates;
there is no replacement event, refund, second unknown append or clean result.
An already received receipt can use its reserved audit slot after expiry, while
another request or verified/excluded return still requires current authority.

SourceDispatch body/state serialization, actual POST JSONL receipt-byte hashes
and canonical fixture state hashes keep their separate domains. No serializer
is changed to force equality. Existing live reconciliation, repairs and retry
semantics remain supported.

**All authority, owned catalog resolution, external history, fencing, lifecycle
persistence and CAS implementations in this component exist solely in tests.
They establish no production coverage, genuine ownership or remote conditional
mutation. B1p production policy producers, the SDK internal request adapter,
real SQLite/CAS/fencing (C), driver integration (D), Collect callers and actual
historical application remain held.** Run the
[historical delivery](tests/test_record_delivery.py),
[policy validation](tests/test_record_exclusions.py), transport/default suites
above and Collect session/policy/outbox suites when changing this unused seam.

[`json_values.snapshot_json`](fulcra_common/json_values.py) provides the shared
lossless snapshot: object keys must be strings at every depth, strings must be
valid Unicode, and numbers must be finite. Python's bare JSON encoder can
stringify numeric/bool/null keys and silently overwrite a matching string key;
these inputs now fail before v1 schema/transport effects. The builders, datatype
specs, delivery reconciler and Collect's action/outbox journals share this check.
Zero, false, null, Unicode and nested arrays/objects retain their values. Tuples
keep the existing JSON-array convention. Cycles and unsupported Python objects
fail without printing private payloads; shared acyclic containers are accepted.

Every submitted record must have an explicit canonical UUID, even if the served
schema permits server-generated IDs. `record_id_for(data_type, source_id)`
exposes the builders' existing stable mapping for history and control lookups.
`delete_records(data_type, record_ids, action_id=...)` submits schema-checked
native v1 `DeletedRecord` rows with deterministic tombstone IDs scoped to the
persisted action UUID and full target destination. Its `data_type` field uses
the target's base `Event` or `Metric`, as required by the native deletion API.
Persist the action before calling, establish independent replay exclusions, and
verify target absence before reporting deletion. A receipt does not establish
absence; a later same-ID replay can revive the target. There is no automatic
POST retry or generic native-type ingest bypass.

[`record_exclusions.RecordExclusions`](fulcra_common/record_exclusions.py) is an
opt-in permanent replay-policy primitive. Resolve `EXCLUSION_SPEC` to a proven
owned Event type first, then supply an account-bound store implementing
`get_records` and `ingest`. An exclusion is keyed by the full target type and
record UUID; its action UUID, original timestamp, reason and evidence references
must be durably captured before calling. Reads fail closed on unknown or
malformed policy. `exclude` reuses a visible prior exclusion and verifies new
policy visibility independently of native tombstones. There is no implicit
restore. Concurrent first creates can race, so the caller retains each original
action in its durable action audit. This helper does not delete targets or
serialize writers across hosts. `record_delivery.reconcile_records` checks
policy before and after source uploads, repairs observed revivals with stable
native tombstones, and returns separate verified/excluded IDs only after final
readback. Unknown responses or delayed visibility keep the caller's batch
pending. `validate_records` can preflight the whole batch against its served
schema without posting, before any control-type creation.

Collect callers now supply an explicit `SourceDispatch` to this reconciler. Its
versioned immutable scope/payload binding and exact issued subset/body hash are
persisted and read back through `V1DataClient.ingest(before_submit=...)` after
validation and serialization. Issued missing/partial rows cannot trigger another
source POST; exact later visibility can reconcile them. Exact positive original
IDs persist before policy repair, so a new exclusion plus absence cannot turn
an uncertain, never-positive source upload into success. Legacy state grants no
fresh source attempt. `CollectRecordSession` requires the controller; the common
helper's controller-free compatibility path does **not** provide durable dispatch
and must not be used for Collect source writes. Native repair still needs its
own durable action policy; this source checkpoint does not make repair once-only.

When the store exposes `account_id`, this policy helper captures its canonical
UUID and rechecks it before and after lookups, including successful empty
responses. A supplied root `fulcra_userid` or `userid` must match that captured
account; an unknown store scope cannot attest supplied owner metadata. This
check applies to the closed policy family and preserves generic record-field
comparison elsewhere. Other root server additions remain allowed. The
[policy tests](tests/test_record_exclusions.py) cover wrong/null owners, unknown
scope and account changes that would otherwise produce clean-empty results.

The Collect media, PurpleAir, DayOne and interactive record writers enforce
this policy on the development branch. Browser delivery and standalone
importer rollout remain migration work.
Controlled old-writer cutoff and historical effective-state conversion are
still required; new exclusions cannot reconstruct unqueryable old tombstones.

The live catalog does **not** retain the `fulcra_tools.key` creation metadata or
the custom schema fragment (verified October 23, 2026). The local creation
journal's account/key binding is authoritative for subsequent control lookup;
copy it during host migration. Unbound `resolve_exclusion_type` checks owned
Event served schemas to recover a renamed control. Multiple compatible types,
unreadable/malformed schemas or a missing bound type fail closed. Discovery
caps at 64 candidates and a 10-second scan budget with at most 8 seconds per
schema HTTP request; importing the existing account binding avoids that scan.
It never creates a replacement around an unreadable or missing bound control.

Read-only checks use `verify_exclusion_type`, separately from that setup
resolver. It requires an existing resolved journal row with the package's
fingerprint and full control UUID, verifies that exact type against the direct
owned list and compatible served schema, then rechecks owned metadata, account
and the same local binding. It never discovers, adopts or promotes a control.
`CreationJournal(path, read_only=True)` opens only an existing private owned
file with SQLite `mode=ro`; it creates no directory, database or tables.

The [Collect session](../collect/fulcra_collect/v1_session.py) wraps each actual
policy GET and policy POST with this verification and compares the served
schema hashes across transport. A reused policy object therefore rechecks the
control after a source upload as well. A missing/retired control, changed
schema or lost/downgraded local binding keeps work pending instead of accepting
clean absence. The [authority integration tests](../collect/tests/test_v1_policy_authority.py)
cover these transitions and positive rename reuse. These are compatible-schema
and binding proofs; they do not provide a cross-host mutation lock or replace
controlled old-writer cutoff.

<a id="schema-proof-read-contract"></a>
Owned-list and served-schema proof reads request identity encoding and reject
encoded, partial, redirected or mismatched responses. Schema reads require
HTTP 200 at the original full type's exact path, without a query or history;
supplied response-owner metadata must match. Both documents use strict UTF-8
and lossless JSON parsing: duplicate keys, nonfinite values, invalid Unicode,
depth above 64 or more than 100,000 values refuse. Decoded buffered parsing is
capped at 16 MiB for the owned list and 1 MiB for a schema. HTTP loading has
already happened; these are **not raw-wire/allocation or whole-operation
deadline bounds**. Supplied owned-row `fulcra_userid`/`userid` contradictions
refuse, and a provided input schema must itself be readable and compatible
with the package's fields and required constraints. Omitted input schema keeps
the existing served-proof contract; it is not a claim that arbitrary input and
served schemas are equivalent.
Decimal-number tokens must also preserve their mathematical value through the
chosen finite native float's JSON representation. Thus `1.0000` and `1e0`
remain valid equivalents of one, while `1.0000000000000001` cannot silently
become a compatible `const: 1`. This rule applies to authority documents and
provided input-schema strings; generic record-field comparison is separate.

The returned upload receipt means submitted, **not verified visible**. After a
timeout or missing receipt, reconcile stable IDs before repeating a write.
Callers must still verify readback and enforce account/selection epochs at the
source boundary. `get_record(data_type, record_id, account_id=...)` reads one
exact ID in the captured account, returning `None` only for a successful empty
read and raising on malformed/ambiguous results. `record_matches(actual, expected)`
compares every submitted field, normalizing aware timestamps and distinguishing
JSON booleans from numbers. This proves one record's visibility, not coverage of
an entire history. `get_records(..., record_ids, account_id=...)` batches up to
32 IDs per request using the API's matcher grouping inside one selector:
`{__name__="Event/<UUID>",id="<ID1>" or __name__="Event/<UUID>",id="<ID2>"}`.
Binary OR between complete vectors is rejected by the live API. Reads reject
duplicate/unrequested rows and never return partial success after a failed chunk.
`get_source_records(..., sources, start=..., end=..., account_id=...)` uses bounded,
anchored source-match windows for twin policy. A saturated 4,000-row response is
unknown coverage and raises; it is not a historical migration coverage reader.
These forms were verified with synthetic Event/Metric records on October 23, 2026.
Caller conversion and historical migration remain plan tasks.

`data_types.DataTypeSpec` snapshots custom-field schemas and metric
aggregation/unit/scale/value-map declarations. Base-only types omit the optional
custom schema fragment: the live API rejects an empty properties fragment. `resolve_data_type` checks the
captured token's account, reads owned recordable v1 custom types,
requires an explicit pin for duplicate names, and verifies the served schema.
It never adopts shared/legacy/deprecated/incompatible types or modifies an
existing type. No cross-account destination cache is used.

Owned custom types come directly from `GET /input/v1/data_type` with
`include_deprecated=true` (verified read-only October 24, 2026). The endpoint
lists the authenticated caller's types and normally omits an owner field;
the shared reader binds that response to its captured account and rejects
contradictory ownership. Use this exact path without a trailing slash. The
aggregate `/data/v1/catalog` can silently omit an unavailable input-service or
unconvertible entries, so an empty or singleton aggregate is insufficient proof
to create or adopt a uniquely named destination. There is no fallback to it.
The whole direct list must have valid, unique Event/Metric IDs and compatible
v1 identity metadata; malformed or more than 4,096 entries refuses the read
without truncation. Retired types stay visible and cannot silently be replaced.
An unequivocal HTTP 200 at that exact origin/path/query is required; redirects,
partial/pagination indicators and contradictory total counts refuse before
creation intent publication. The reader explicitly disables redirects even
when its client's default follows them.
The resolver, exclusion discovery and Collect destination picker share this
reader. Full destination schemas still come from the catalog's explicit
`/data/v1/catalog/{full-type}/v1/schema` endpoint and must pass validation.

Creation is opt-in (`allow_create=True`) and requires a private
`CreationJournal(Path(...))`. The SQLite journal reserves the account/key/spec
intent before the POST. A lost response or unverified creation remains pending;
the next process may reconcile an owned compatible type/schema result but
cannot create again from an empty listing. The same private journal reserves
account/tag-name creation across Collect plugins; uncertain tag creation must
resolve by current-account lookup, never a second POST from another plugin. Changed specifications and silent
destination replacement are rejected. A definitive HTTP 422 validation rejection
is recorded separately; the identical rejected request is not retried, while a
corrected specification may start a new intent. Timeouts and other unknown
outcomes remain pending. Retain this journal across restarts and
host cutover. The setup UI must bind each writer only when that writer's v1
serialization and readback paths switch together.

A new datatype POST has an immutable `CreationReservation` nonce, captured with
its account/key/spec before the request. A complete UUID from a successful JSON
receipt or canonical same-origin HTTP 303 Location is retained for that original
reservation **before** further account/catalog/schema checks. The creation request
never follows the redirect or reads its alias URL; an otherwise valid 303 receipt
does not depend on the redirect body. JSON receipt bodies require identity
encoding and are limited to 2 MiB. Unknown responses still keep the one-POST
intent pending. Known UUIDs remain `submitted` until current owned-type and
served-schema proof resolves that exact UUID; failure or a later account change
cannot substitute a different type. A submitted, unpinned result also refuses a
competing type with the canonical name. Two different UUID receipts are retained
and commit a conflict hold before raising an error.

Journal reads hold unknown statuses and malformed destination states. Each
current slot has separate provenance (`created` with its original nonce,
`adopted`, or a migrated `legacy` binding). Reads use a consistent SQLite snapshot;
resolution rechecks both the current row and receipt audit under `BEGIN IMMEDIATE`
before committing. A known receipt requires exactly its matching audit entry.
Missing/conflicting audit, a lost reservation/current row, or an integrity hold
cannot become an apparent legacy binding or be healed by resolution. Receipt-only
writes may append original-reservation evidence after authority changes; they
cannot authorize the newer slot. Appending after corrupt audit retains evidence
and a durable hold instead of repairing its authority.

Writable open upgrades a genuine five-column legacy journal atomically to format
1 (`PRAGMA user_version`) and enrolls its existing rows as legacy provenance.
`read_only=True` can still inspect that old format without changing bytes. A
format-1 open never recreates missing protocol tables/columns/indexes. Only the
constructor can create a new file; subsequent operations refuse a disappeared
file. Keep the whole private SQLite journal during migration and stop older
writers before enabling this protocol; restoring an old snapshot or manually
editing coordinated metadata is not a supported recovery operation. These are
crash/concurrency and detectable-corruption controls, not a tamper-proof audit.

The [journal/resolver tests](tests/test_data_types.py) and
[creation-receipt tests](tests/test_creation_receipts.py) cover two connections,
stale original reservations, failed post-create proof, account changes, no-follow
redirect receipts, legacy upgrade, missing metadata and post-read corruption.
This setup journal remains separate from current policy verification and
account/consent mutation leases.

## Ingest pipeline

This public API retains legacy annotation encoding and transport compatibility.
Current native source writers use the [v1 boundary](#v1-datatype-migration-boundary);
see [current callers](#callers-and-boundaries). Retaining this API does not make
its wrapped or typed annotation records native v1 Event/Metric records.

### Why it exists

Before refactor #69 there were four+ ad-hoc callsites that each built
the Fulcra wire format inline — media-helpers, attention,
csv-importer, the collect daemon's quick-record, and the tombstone
path. Each one had to know about:

- The wire envelope (`specversion` / `data` / `metadata`)
- The `recorded_at` union (scalar string for moment, object with
  start/end for duration)
- The source-array assembly (per-event id + extras + annotation-def
  source)
- The `duration_seconds` defensive field — a renderer quirk (#30)
  that required surfacing the duration on the `data` payload, not
  just the recorded_at envelope. Three of the four sites duplicated
  this with subtle drift (one used `max(0, …)`, one didn't, one
  emitted float, one int).

The pipeline centralizes that legacy encoding. Compatibility callers build
an `IngestableEvent` and hand it to the pipeline; they still own deduplication,
retries and verification that accepted records became visible.

### The IngestableEvent contract

`IngestableEvent` is the base dataclass; `MomentEvent` adds `ts`,
`DurationEvent` adds `start` + `end`. Every event carries:

| Field | Purpose |
|---|---|
| `definition_id` | The annotation definition this event belongs to. `None` for the tombstone path (no def attached). |
| `source_id` | Deterministic per-event id. The importer computes this. |
| `extra_source_ids` | Cross-source fingerprints (e.g. `com.fulcra.content.watched.v1.<hash>`) appended to the source array for cross-importer dedup. |
| `tags` | Tag UUIDs to attach. Importers resolve names → UUIDs before constructing the event. |
| `external_ids` | Free-form per-importer enrichment map. Lands at `data.external_ids` on the wire. |
| `note` / `title` / `service` / `timestamp_confidence` / `comment` | Common optional top-level data fields. Only emitted when not None. |

`DurationEvent` adds `start` / `end` and exposes `.duration_seconds`
(clamped to zero on misordered range) which the pipeline injects into
the wire payload as the `duration_seconds` defensive field.

### Importer-specific top-level data fields

The pre-refactor wire shape carried a handful of importer-specific
top-level data keys. To preserve byte parity (refactor #69 decision —
no silent wire-shape changes), those fields are dedicated optional
properties on `IngestableEvent` rather than being routed through
`external_ids`:

- **Attention** (`category`, `url`, `og_description`, `favicon_url`,
  `parent_source_id`) — five top-level keys emitted unconditionally
  (with `None` values when not applicable). Importers opt in via
  `_emit_attention_fields=True`. Also forces `note` + `title` to
  emit even when None, matching the category-variant wire shape.
- **Legacy quick-record** (`comment`) — distinct from `note`; retained for
  compatibility with the former daemon wire shape.
- **Legacy tombstone** (`superseded_by`, `supersedes_source_id`) — retained
  source-ID sentinel fields. Current [quick-record Undo](../collect/README.md#quick-record-protocol-2-development)
  uses native v1 deletion and replay exclusion.

Except for the explicit attention opt-in, optional fields are emitted only
when populated.

### IngestPipeline interface

```python
from datetime import datetime, timezone
from fulcra_common.ingest import IngestPipeline, DurationEvent

event = DurationEvent(
    definition_id="11111111-2222-3333-4444-555555555555",
    source_id="example.duration.1",
    start=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
    end=datetime(2026, 1, 1, 13, tzinfo=timezone.utc),
    note="Synthetic example",
)
pipeline = IngestPipeline(client=None)

# Build a legacy compatibility wire record without I/O.
record: dict = pipeline.build_record(event)

# Supply your BaseFulcraClient subclass to post.
# IngestPipeline(client=my_client).ingest_one(event)
# IngestPipeline(client=my_client).ingest_batch([event])
```

`build_record` is pure (`client=None` is fine). `ingest_one` posts to
`/ingest/v1/record`, falling back to the batch endpoint only on HTTP 404/405.
`ingest_batch` posts JSONL to `/ingest/v1/record/batch`. Both need a client;
other HTTP errors propagate to the caller.

### Typed records

`wire.build_typed_record(...)` builds the unwrapped schema used by
`IngestPipeline.ingest_typed(base_type, records)`, for example the legacy
`/ingest/v1/record/NumericAnnotation` endpoint. One record is sent as JSON or
several as JSONL. This remains separate from the wrapped `MomentEvent` /
`DurationEvent` path above and from the current [labs native Metric writer](../labs/README.md#storage-and-identity).

**An accepted typed upload is not a verified import.** The caller must check
existing source IDs before posting and verify visibility afterward. The code
accounts for asynchronous processing, missing server deduplication, dropped
invalid lines, and stripped unknown fields; `ingest_typed` itself only checks
the HTTP response. Native sources use the guarded delivery and exact readback
described in the [Collect writer contract](../collect/README.md#v1-writer-migration-development-branch).

### Adding a new event kind

You shouldn't need a new IngestableEvent subclass for typical
importers — `DurationEvent` and `MomentEvent` cover the wrapped
annotation events modeled here; typed numeric records use the separate path above. If you genuinely need a new kind (e.g. a third
recorded_at variant Fulcra adds in the future):

1. Add the subclass + `__post_init__` validation in `ingest.py`.
2. Add a branch in `IngestPipeline.build_record` that maps the new
   subclass to `wire.build_record`'s args.
3. Export the new symbol from `fulcra_common/__init__.py`.
4. Write a unit test in `tests/test_ingest.py` and a byte-parity test
   in `tests/test_ingest_byte_parity.py` (the byte-parity tests were
   deleted after refactor #69 landed; you'd re-introduce one if your
   change has a wire-shape concern).

### Adding an importer-specific top-level data field

If you're hitting a case where an importer wants to emit a top-level
data key the pipeline doesn't model:

- Strong preference: route the field via `external_ids` (a free-form
  map that lands at `data.external_ids.<key>`). That's the default
  choice for anything that's not load-bearing in the legacy timeline
  renderer.
- Only add a dedicated optional field on `IngestableEvent` if byte
  parity with an existing site forces it (the refactor #69 attention
  decision is the canonical example). Document why in the field's
  docstring.

## Callers and boundaries

On this development branch, [media](../media-helpers/README.md#v1-migration-boundary-development-branch),
[CSV](../csv-importer/README.md#native-payload-and-identity),
[Day One](../dayone/README.md#collect-v1-writer-development-branch),
[labs](../labs/README.md#storage-and-identity) and the
[Netflix importer](../netflix-skill/README.md#source-and-destination) build native
Event/Metric records through the shared v1 client and guarded runtime.
Collect's [quick-record protocol2](../collect/README.md#quick-record-protocol-2-development)
uses that native client for capture and Undo. These record paths do not call
`IngestPipeline`; legacy definition reads and administration remain separate.

Media retains its [pure `DurationEvent` conversion](../media-helpers/README.md#retired-legacy-media-writers-development-branch)
for compatibility. The public pipeline and shared
[agent annotation writer](fulcra_common/annotations.py) also remain available;
an in-repository caller audit does not establish that external consumers have
migrated. Browser senders have their own
[Attention contract](../attention/README.md#v1-contract-development), so a Python
wire change still needs a caller audit. Source status does not prove release
or provider acceptance.

## Wrapped wire format invariants

- `specversion: 1`
- `data` is a sorted-key JSON string of the inner payload
- `metadata.data_type` is `"MomentAnnotation"` or `"DurationAnnotation"`
- `metadata.recorded_at` is a bare ISO string for moments, an object
  `{start_time, end_time}` for durations
- `metadata.source` is `[source_id, *extra_source_ids,
  com.fulcradynamics.annotation.<definition_id>]`, with empties and
  duplicates filtered
- `metadata.tags` is a flat list of tag UUIDs
- `duration_seconds` is injected into the `data` payload for every
  `DurationEvent` (the #30 defensive field)
- JSONL batches: one sorted-key JSON object per line, newline-joined,
  POSTed to `/ingest/v1/record/batch` with
  `Content-Type: application/x-jsonl`

These describe the wrapped event path. Typed records have a different schema;
see [`wire.build_typed_record`](fulcra_common/wire.py).

## Testing

```bash
# Per-package
uv run --package fulcra-common --extra dev pytest packages/fulcra-common/tests -q

# Full workspace
uv run --all-packages --extra dev pytest -q packages/
```

The tests cover wire records, pipeline behavior, client operations, definition
resolution, fingerprints, schema checks, and agent annotation routing with
stubbed transports. They do not establish the state of a live Fulcra deployment.

Channel-authority fixtures use invented UUIDs for both the current and retired
channel. A retired account identifier is still account data; replacing only the
current channel does not make the fixture synthetic.

The permanent replay-exclusion specification now has one packaged
[JSON contract](fulcra_common/record_exclusions_contract.json), consumed by
[Python](fulcra_common/record_exclusions.py) and the browser's
[pure wrapper](../attention/chrome/src/relayless/v1ExclusionSpec.ts). The existing
creation body, required-field order and fingerprint are unchanged; do not sort
that array or fork the specification per writer. The
[contract test](tests/test_record_exclusions_contract.py) pins that identity.
Browser schema proof alone grants no owned binding, current policy or deletion
permission. See the [browser development contract](../attention/README.md#v1-contract-development).

The shared [v1 record client](fulcra_common/v1.py) now uses one response gate for
exact-ID reads, source-window reads and latest previews. It requires an
unambiguous 200 from the exact requested origin/path/query, disables redirects
per request, and rejects redirect history, pagination/partial markers and
conflicting exposed account/total headers. Reads request identity encoding and
reject encoded responses: eager decompression does not reliably prove a gzip
trailer completed, so even a complete-looking decoded row is insufficient.
A partial response cannot authorize
absence, duplicate skipping or replay-policy permission. Existing 32-ID groups,
4,000-row source saturation and 30-day preview semantics remain intact.

The JSONL parser uses strict UTF-8, rejects duplicate keys (including escaped
aliases), malformed suffixes, nonfinite/invalid-Unicode values, and ignores only
ASCII space/tab/CR blank framing. It bounds decoded buffered parsing at 32 MiB,
64 nesting levels and 100,000 nodes, plus the operation's row limit and bounded
physical lines. **The HTTP client's eager `get` has already buffered/decompressed
the response: this is a parsing bound, not a raw-wire, decompression allocation
or absolute whole-request deadline guarantee.** Static parser failures retain
pending work. The [reader tests](tests/test_v1.py) cover all three surfaces;
complete historical coverage remains a separate contract.
