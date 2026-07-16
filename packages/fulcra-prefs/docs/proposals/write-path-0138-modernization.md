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
- **`fulcra delete DATA_TYPE [RECORD_ID]`** / lib delete — record tombstones;
  `DeletedRecord` appears in the catalog. Recordable types only.

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

## Ask 3 — native revocation: delete tombstone vs. supersedes

Two correction models now coexist; they are **not** interchangeable:

| | `supersedes` (today) | `delete` tombstone (new) |
|---|---|---|
| History | preserved (auditable chain) | record leaves the timeline |
| Compile effect | superseded signal dropped from "now" | record no longer read at all |
| Reversible | yes (supersede again) | no (tombstoned) |
| Right-to-be-forgotten | ❌ value still on timeline | ✅ value actually removed |

**Recommendation for the Privacy Ledger:**

- **Preference corrections** (I changed my mind) stay `supersedes` — the audit
  trail is a feature, not a leak; you *want* the history.
- **Consent revocation / erasure** (stop sharing X; delete what you know about Y)
  is where a real `delete` belongs: superseding a disclosed value leaves it on the
  timeline, so "revoke" that only supersedes is a false promise. Model an explicit
  `revoke --erase` path that (1) emits the revocation *event* record for the ledger
  (the audit that a deletion happened, keyed so it is itself excluded from
  preference synthesis), then (2) issues a `delete` tombstone for the target
  signal/disclosure record(s). The ledger keeps the *fact of revocation*; the
  *value* is gone.
- **Ordering + resilience:** write the revocation-event record first (so the audit
  survives even if the delete fails), then delete; if the delete fails, spool a
  retry (mirrors the capture outbox). Deleting a record whose id we only know via
  get-records means the revoke path must resolve the record id first.
- **Idempotence:** deleting an already-tombstoned record must be a no-op; a
  re-revoke should not error.

## Sequencing

1. Docs rewrite (this pass, ask 1) — done alongside this note.
2. `validate_records` optional pre-flight — small PR, tests with a malformed record.
3. `record_data_type` swap — small PR, wire-shape test.
4. `revoke --erase` (delete-backed consent revocation) — larger; needs the
   ledger-event + resolve-id + tombstone + retry design above, and operator sign-off
   since it is destructive. Track as its own task.
