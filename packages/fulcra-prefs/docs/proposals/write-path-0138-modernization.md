# Write-path modernization + native revocation (fulcra-api 0.1.37/0.1.38)

**Status:** design note (QA dispatch, fulcra-api 0.1.38 pass) · **Author:** fulcra-prefs-maintainer

`fulcra-api` 0.1.37 shipped first-class record write/delete, which changes two
long-standing prefs limitations. This note records the evaluation + the intended
direction; code changes land as their own dual-green PRs.

## New surface (verified against the installed 0.1.38 lib/CLI)

- **`FulcraAPI.record_data_type(data_type, records, api_version="v1alpha1")`** —
  POSTs a batch (`application/x-jsonl`) to `/ingest/v1/record/{data_type}` and
  returns `{upload_id}`. This is exactly the endpoint fulcra-prefs already migrated
  to in the 0.1.36 pass — now library-wrapped. CLI equivalent: `fulcra record`.
- **`FulcraAPI.validate_records(data_type, records, api_version)`** — fetches the
  catalog JSON Schema for the type and validates each record with `jsonschema`,
  returning `[(index, message, ValidationError), …]` (empty = all valid). No
  network write; a pure pre-flight check.
- **`fulcra delete DATA_TYPE [RECORD_ID]`** — **logical** delete only. There is
  **no dedicated library delete method**; the CLI composes deletion client-side by
  appending a `DeletedRecord` (via `record_data_type(data_type="DeletedRecord", …)`)
  that suppresses the target record on read. The original append-only record is not
  demonstrably physically removed, and no retention/erasure contract is established
  by 0.1.38. Treat this as timeline suppression, not guaranteed erasure. Recordable
  types only.

## Ask 2 — adopt `validate_records` (and maybe `record_data_type`) in capture

**Recommendation: adopt `validate_records` as an optional, non-fatal pre-flight;
do NOT hard-depend on it.**

- Value: fail-loud on a malformed record *before* it ingests, for free, against the
  authoritative catalog schema — catching (e.g.) a bad `recorded_at` or a missing
  required field at capture time instead of as a silent server-side reject.
- Placement: in `store.ingest_signal` / `capture_signal`, call `validate_records`
  on the typed body just before the POST. On errors, surface loudly and skip the
  ingest (still spool to the outbox so nothing is lost), rather than raising through
  the caller.
- Caveats that keep it *optional*: (a) it costs one catalog-schema GET per call
  (cache it per process / per data_type); (b) prefs records are custom-definition
  `MomentAnnotation` — validation is against the **base** `MomentAnnotation`
  v1alpha1 schema (`{id,tags,sources,recorded_at,note}`), which is exactly the
  typed body we send, so it is meaningful; (c) it must degrade to "skip validation,
  proceed" if the schema fetch fails, so a catalog outage never blocks capture.
- `record_data_type` swap: mechanical and desirable (drops our hand-rolled
  `fulcra_api("/ingest/v1/record/{type}", …)` for the supported wrapper), but it
  posts a **list** as `x-jsonl` where we post a single record as `json`. Wrap our
  one record as `[record]`; keep `build_record` as the canonical envelope and the
  outbox/shard paths unchanged (same discipline as the 0.1.36 migration). Low risk,
  own PR.

## Ask 3 — revocation: supersede vs. tombstone (both are LOGICAL, not erasure)

Two correction models now coexist; they are **not** interchangeable. Critically,
**neither physically erases** the original append-only record — a `DeletedRecord`
tombstone *suppresses on read*, it does not prove the value left storage, and
0.1.38 establishes no retention/erasure contract:

| | `supersedes` (today) | `DeletedRecord` tombstone (new) |
|---|---|---|
| Effect | superseded signal dropped from "now" | target suppressed on read |
| History | preserved (auditable chain) | superseding append; original still stored |
| Physical erasure | no | **no** (suppression marker, not verified deletion) |
| Reversible | yes (supersede again) | not by a documented API path |

**Recommendation for the Privacy Ledger:**

- **Preference corrections** (I changed my mind) stay `supersedes` — the audit
  trail is a feature, not a leak; you *want* the history.
- **Consent revocation** (stop sharing X) can additionally emit a `DeletedRecord`
  tombstone so a previously disclosed value is *suppressed on read* by conformant
  readers. Frame this as **logical revocation / read-suppression, NOT
  right-to-be-forgotten** — do not promise erasure the platform doesn't guarantee.
  A `revoke` command (no `--erase`) that (1) writes the revocation *event* record
  for the ledger (the audit that a revocation happened, keyed so it is itself
  excluded from preference synthesis), then (2) appends a `DeletedRecord` tombstone
  for the target record(s). The ledger keeps the *fact of revocation*; the value is
  *suppressed on read*, not proven gone.
- **True erasure is out of scope until the platform provides + verifies it.** If a
  right-to-be-forgotten guarantee is needed, it requires a platform
  erasure/retention contract (physical delete or documented purge) that 0.1.38 does
  not expose. Track that as an upstream ask, not a prefs feature we can promise.
- **Ordering + resilience:** write the revocation-event record first (so the audit
  survives even if the tombstone write fails), then append the tombstone; on failure
  spool a retry (mirrors the capture outbox). The revoke path must resolve the
  target record id (via get-records) first.
- **Idempotence is an OPEN CONTRACT prefs must implement, not API behavior.** The
  CLI simply appends tombstones, so a repeated revoke would append duplicate
  `DeletedRecord`s. prefs must dedupe/reconcile: before appending, check whether a
  live tombstone already targets that record id (get-records the DeletedRecord
  stream, or track revoked ids in a local ledger index) and no-op if so. Mark this
  as to-verify against live behavior before implementation.

## Sequencing

1. Docs rewrite (this pass, ask 1) — done alongside this note.
2. `validate_records` optional pre-flight — small PR, tests with a malformed record.
3. `record_data_type` swap — small PR, wire-shape test.
4. `revoke` (tombstone-backed logical read-suppression for consent revocation, NOT
   erasure) — larger; needs the
   ledger-event + resolve-id + tombstone + retry design above, and operator sign-off
   since it is destructive. Track as its own task.
