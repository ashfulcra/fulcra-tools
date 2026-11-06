# Collect v1 records, host migration, and release

The operator requested and authorized planning and execution on October 23,
2026: move Collect and every plugin's record writes to v1 datatypes, move
persistent collection to the destination Mac while retaining browser collection
on the source Mac, review and simplify the result, publish a verified app, and
convert historical Collect data. Continue until the evidence gates pass or a
specific external prerequisite prevents progress. Routine implementation choices
are delegated; this document records the implementer's choices.

## Verified starting point

Collect 0.1.4 has 26 bundled plugins. The old writers include wrapped records,
typed MomentAnnotation/DurationAnnotation/NumericAnnotation writes, independent
CSV/Day One clients, TypeScript browser writers, and a vendored Netflix script.
Some plugins write only Fulcra Files. The released app and source-plugin
registry must remain in parity.

The October 23 live catalog and schemas identify **Event** and **Metric** as
the v1 base types. `/ingest/v1` in a URL does not establish datatype version:
legacy annotations remain `api_version=v1alpha1`. Event uses `start_time` and
`end_time`; Metric additionally requires a numeric `value`. Custom fields are
declared by a JSON Schema fragment in `record_spec.schema`. Ingest takes the
complete custom datatype ID and an explicit `api_version=v1` query parameter.
The installed CLI/SDK 0.1.54 exposes v1 creation and validation; the workspace
lock still selects 0.1.35. Account catalogs and private schema captures remain
outside the public repository.

## Approach and boundaries

Use one shared v1 datatype/record client and declarative schemas, then migrate
callers in bounded families. This keeps validation, destination resolution,
stable IDs and transport policy consistent. Rewriting the daemon and all
plugins from scratch adds source-specific risk without helping the datatype
transition. Switching every caller at once obscures which source regressed.

Create/adopt owned, compatible Event/Metric destinations per existing semantic
track. Do not adopt an arbitrary same-name legacy, shared, deprecated or
incompatible destination. Cache destination IDs by authenticated account and
schema identity. Ambiguous matches are actionable errors. Never retype a legacy
definition in place or quietly fall back to an annotation writer.

Files remain Files: preserve Notes, tasks, source documents and transcript
uploads, permissions, hashes, versions and writeback rules. Record-producing
side channels move to v1; audit all file-only plugins and document why they have
no record writer. Do not manufacture redundant records merely to label a file
plugin migrated. Unrelated coord/prefs/vault record streams are outside this
Collect conversion, even when they share `fulcra-common`.

## Shared record contract

New helpers in `fulcra_common.v1` build flat Event/Metric records with explicit
UTC start/end times, UUID IDs, source arrays and tags. Moments set end equal to
start rather than allowing ingestion time to replace the event time. Stable
UUIDs are scoped by the full destination ID and the original source identity;
cross-source fingerprints remain sources rather than primary record IDs.
Existing IDs can be retained explicitly during controlled migration.

Each family defines one versioned identity extractor shared by live imports and
historical conversion. Select its primary source namespace, not an arbitrary
first source or cross-source fingerprint. Missing identity, same-ID/different-
content collisions and intentional repeats require explicit disposition;
quarantine ambiguity. Verify actual same-ID behavior with unchanged and changed
content before relying on upserts. A retained legacy UUID must agree with the
family's live replay mapping.

Custom fields preserve source information: note, title, service, external IDs,
timestamp confidence, attention metadata, and source-specific attributes.
Reserved base fields cannot be overwritten by enrichment. Reject naive clocks,
reversed intervals, non-finite/bool metric values and malformed destinations
locally. Validate every batch against the served destination schema, including
formats and unknown top-level fields, before sending any of it. Errors expose
record indexes/reasons, not private values. A failed schema read is a failure.

V1 ingestion returns an upload receipt, never a claim that the data is already
visible. Keep bounded HTTP timeouts. Do not automatically retry an uncertain
POST; reconcile explicit record IDs against v1 reads first. Readback must compare
IDs, timestamps, values, sources, tags and declared enrichment, not only counts.
No checkpoints or dedup claims advance over unverified writes.

## Host cutover

Inventory each plugin's enabled state, intervals, destination account, local
state, source paths and credentials without recording secret values. Take
consistent private backups of configuration and SQLite state. Distinguish
portable API sources, portable exports/files, host-local Apple databases, and
browser extensions/relay. Local Apple data and OS grants must be verified on
the destination host; an existing directory is not proof of the same library.

Stage the replacement with plugins disabled/preview enabled. Use explicit
account verification and consent-preserving configuration import. Credentials
stay in each host's approved secret store. Install a durable launchd service;
verify worker runtime, plugin registry, restart, source reads and v1 readback.
Cut over one source family at a time with a single-writer checkpoint. Preserve
the browser extensions on the source host, and retain the minimal relay if
they need it. Disable only superseded non-browser collection there. Retain
rollback copies and record exactly which host owns each source.

The current Attention source is relayless. Check installed versions and queued
events; do not retain a daemon based solely on an obsolete relay assumption.

## Historical migration

Discover owned Collect streams from plugin state, source namespaces and
definition IDs, not every annotation in the account. Record an explicit
legacy-to-v1 mapping and immutable schema fingerprint. Export bounded time
windows privately with source IDs, record IDs, timestamps, tags, values, units,
enrichment and tombstone/supersession evidence. Prove enumeration coverage;
empty/partial/failed reads are different outcomes.

The existing legacy reader documents an approximately 4,000-row ceiling without
a cursor. Use adaptive window splitting, boundary overlap and record-ID dedup;
fail visibly if a minimum-size time window remains saturated. Apply coverage
checks to destination readback too. Do not certify completeness from HTTP 200.

Include definitionless undo/supersession records discovered by source namespace.
Resolve their target source identities through the same mapping as live data;
preserve their evidence in a declared v1 audit schema and retain effective
deleted/superseded state. Any unresolvable marker or quarantined row prevents an
unqualified completion claim.

A durable SQLite ledger records account, source/destination type, schema hash,
window, original identity, converted payload hash, upload receipt and readback
status. Reusing the same plan converges; changing its account/mapping/schema
fails closed. Deterministic target IDs and exact reconciliation make retries
safe. Inspect destination IDs before repeating an uncertain upload. Quarantine
malformed records with reasons and preserve originals. Compare full content,
counts and coverage before marking a window complete. Do not delete or archive
legacy streams as part of this authorized conversion; originals remain the
rollback source until reconciliation and any later retirement decision.

Exports and converter QA can happen early. Finalize each source's history only
after the old writer is stopped, queued/uncertain uploads reconcile and a final
delta sweep passes. Processing time and bounded repeated/overlapping export
must cover late uploads with old event timestamps; event time alone cannot
define the cutoff. Browser outboxes are part of this per-source cutoff.

Native v1 tombstones remove Event/Metric records, but a later same-ID replay can
revive them. Replay policy therefore uses an independent owned Event exclusion
keyed by full target datatype and record UUID, with a durably captured action,
timestamp, reason and evidence. This protocol has no implicit restore. Source
writers check policy before duplicate shortcuts and around uploads, repair
observed revivals, and distinguish verified delivery from excluded records.
Local account mutation leases cover source planning through readback and future
Undo actions; they do not imply a transaction across hosts. Keep each original
action in a durable audit, since concurrent first exclusions are not a global CAS.

The catalog does not retain datatype creation metadata keys or custom schema
fragments (verified October 23, 2026). Preserve the account/key creation journal
through cutover. Unbound control discovery checks served schemas and fails
closed on ambiguity, unknown schemas or its candidate/time budget. A missing
bound control never authorizes an empty replacement. Historical native
tombstones remain unqueryable: finalize visible effective-state history after
old-writer cutoff, then gate pre-cutoff source replay on positive converted-ID
or canonical-binding evidence and quarantine unknown late historical rows.

## QA, simplification and release

For every family, run regression-first contract tests, interrupted/repeated
import tests, wrong-account and changed-consent cases, and local browser/API
checks where applicable. Run the aggregate Python suite and JS/Swift checks
appropriate to changed components. Record baseline failures separately; fix
relevant defects rather than normalizing failing checks.

After functional migration, independently review the whole change for legacy
paths, redundant serializers, stale settings, misleading success states,
unbounded reads, credential leaks and obsolete packaging lists. Remove code
only after its callers/readers are accounted for; re-run the affected suites.
Independent bus review and green exact-head CI gate merging. Update affected
READMEs, the source catalog and AGENTS.md in the same PRs.

Build the frozen app from reviewed source, verify all plugins and bundled CLI,
run isolated first-run/upgrade/restart QA, scan privacy/secrets, sign, notarize,
staple, and verify Gatekeeper. Reconcile interrupted Apple submissions before
resubmitting. Verify published artifacts by anonymous download/checksum and
install the exact artifact on the destination host. Personal state and build
receipts remain private.

## Completion gates and exit check

Completion requires all four outcomes, not just passing unit tests:

1. Every active Collect record path uses declared v1 types; exceptions are
   file-only paths with explicit evidence, not legacy record fallbacks.
2. The destination host owns verified persistent collection; source-host
   browser collection remains working and superseded collectors are retired.
3. Functional QA, simplification review, second QA, independent review, CI,
   signed release, public artifact verification and local installation pass.
4. Historical Collect records have a complete scoped inventory, migration
   ledger and exact readback reconciliation, with originals retained.

Before every turn ends: recover pending answers/reviews, verify changed work,
update the owned bus task and private checkpoint, run the local exit check, and
continue any safe executable step. A heartbeat re-enters this same chat for
unfinished work; its configured state is not proof it has actually fired.

### Review amendment: quiet automatic credential publication

Automatic owned-auth renewal was still reaching an unbounded UI-capable Keychain writer. Reuse the existing isolated quiet mutation boundary for the two closed shared account keys, retaining update-before-add, exact acknowledgements, account/revision checks and consumed-grant no-replay. Apply the same publication boundary to legacy principal-checked renewal. Explicit interactive sign-in retains its existing writer. Unsupported quiet backends fail closed and require reconnect; native signed-app acceptance remains separate. This implements the existing no-background-prompt requirement; it does not change account/source consent or authorize credential migration.

### Review amendment: historical response account evidence

The raw exporter must retain bounded optional response-account evidence and refuse explicit malformed/duplicate/other-account headers before body access. Absence retains the existing captured client/principal authority, not independent account proof. Revalidate the fact with the original receipt at consumption. Raw/pass/proof protocol3 rejects older v1/v2 artifacts and completed-window adoption because prior receipts could have discarded a contradiction; preserve originals and use a fresh authorized ledger/pass. No provider completeness, apply or final coverage claim follows.
