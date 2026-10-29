# fulcra-labs

One canonical Fulcra data track per lab marker, across report providers. An agent
transcribes the PDF twice independently; this CLI consumes the agreed JSON. It
does not extract PDFs. Marker lookup, comparison and preview remain local. Real
imports use the shared Collect record runtime with independent CLI authentication.

**Development branch:** the actual `fulcra-labs ingest` command writes v1 Metric
records. Installation in the released app, real report QA, historical conversion
and signed publication remain separate gates.

## Storage and identity

Each marker has an owned `Metric/UUID` destination named `<display name> (Labs)`,
with discrete aggregation and its exact canonical registry unit. Full ownership,
served schema and Metric metadata must agree. Optional top-level record `unit`
is always omitted; canonical interpretation comes from the destination metadata.
The required nullable `raw_unit` keeps the source spelling, empty string or null.

Records retain canonical value and collection time (equal start/end), unchanged
source identity, note, marker key, raw value/unit, reference range, flag, lab,
source-document token, qualifier and report date. PDF bytes are never uploaded.
Only rows passing existing validation or specifically confirmed resolvable review
gates are eligible; reject rows remain excluded from ingest.

UUIDs are derived from the full destination and existing labs source ID. The
source hash uses marker, collection time and six-significant-figure canonical
value formatting; it differs from validation's six-decimal rounding. A correction
can produce a new identity, leaving the original observation intact. Small value
changes or provenance changes can retain identity. A conflicting existing current
record then holds the whole new batch before staging; unrelated top-level cloud
fields may remain. This is no automatic correction/delete workflow or cross-host
compare-and-swap. Already-staged recovery uses its original immutable payload and
the shared exact-delivery kernel; an external edit racing preflight is not atomic.

## Install and commands

Python 3.11+ and `uv`, from the repository root:

```bash
uv sync --all-packages --all-extras
source .venv/bin/activate
fulcra auth login
fulcra-labs markers --search ferritin
fulcra-labs check pass_a.json pass_b.json --out agreed.json --json
fulcra-labs ingest agreed.json --source-doc report.pdf --dry-run --json
fulcra-labs ingest agreed.json --source-doc report.pdf --json
fulcra-labs status --json
```

Runtime dependencies include `click`, `httpx`, `fulcra-common` and
`fulcra-collect`; macOS Collect view dependencies remain optional. The standalone
runtime uses the CLI account, not the daemon's saved account/credentials. Its
profile is selected by `FULCRA_COLLECT_HOME` (default `~/.config/fulcra-collect`),
with a dedicated `cli-labs` namespace, private outbox and exclusive CLI lease.
Local marker lookup, `check` and `ingest --dry-run` need no authentication.

For a selected marker present in retained legacy `markers.json`, supply an
explicit new owned target; never reuse a NumericAnnotation ID as Metric:

```bash
fulcra-labs ingest agreed.json --source-doc report.pdf \
  --marker-target ferritin=Metric/00000000-0000-4000-8000-000000000001 --json
```

The UUID is an illustrative placeholder. Repeat every supplied pin unchanged on
recovery; unknown/duplicate marker keys and malformed targets fail. A new marker
without a selected legacy entry may use full owned-spec adoption/creation via the
account-scoped journal. Changing account, explicit pins, codec or spec authority
holds pending work. New input paths/markers/review flags do not rewrite authority.
`--yes-reviewed ldl-c,ferritin` confirms only those resolvable reviewed marker keys;
it cannot rescue unknown markers, unparseable values or rejected rows.

## Recovery, preview and PDF archive

A live invocation recovers original pending records before opening or validating
new JSON/PDF, even when the supplied new path is missing or empty. Keep the same
profile/account and pins. Recovery proves all actual pending destinations against
private marker/spec witnesses and fresh ownership/schema/Metric metadata before
drain; the kernel validates payload and dispatch per recovered group. Later-group
failure can retain partial complete-group evidence. Unknown issued work is held
for exact readback rather than automatically submitted again.

The entire eligible new multi-marker batch is planned, schema-checked and staged
before the first current record POST. Acknowledged upload is not success: exact
record reads and guarded durable checkpoints gate delivery. Exclusions stay
separate. Do not erase private outboxes/journals or edit old cache state to clear a
hold. Original legacy `markers.json` is read-only evidence; corrupt/unreadable
state is UNKNOWN, not an empty cache. No legacy record writer fallback remains.

`--dry-run` parses, validates and optionally hashes the local PDF without creating
a client, opening the runtime/profile/DB/lock, recovering pending work, archiving,
resolving destinations or writing records. It reports `would_ingest`; it does not
prove live target ownership or recovery. After recovery and new-input admissibility,
a supplied PDF is copied from a consistent byte snapshot to
`~/.config/fulcra-labs/documents/<sha256>.pdf`. `FULCRA_LABS_HOME` overrides that
local archive/cache root. The full `sha256:basename` token and traceability note
travel with records. Archive files can remain after a later upload failure.

## Status and reporting

Ingest `--json` emits one parseable envelope and nonzero on failure, preserving
known current complete-group verified/excluded lower bounds separately from
`recovery`. Counts do not include unproved partial groups or imply new remote
observations; marker attribution is explicitly UNKNOWN when unsupported. Review,
reject and in-run duplicate counts retain their existing meaning.

`status` authenticates and reads profile/account-scoped local evidence, without
counting remote legacy series or draining pending work. Native `last_attempt` is
not success; `last_verified_ingest` is a guarded local observation/checkpoint time,
not provider ingest time or specimen time. Cached current verified/excluded counts
are labelled `last completed current delivery`, associated with
`current_completed_at`, and never cumulative. A newer `last_attempt` may describe
a failed/empty attempt without relabelling those prior completed counts; `last_recovery` separately retains complete
or UNKNOWN complete-group lower bounds, `observed_at` and sanitized `error_type`
from the latest recorded recovery attempt.
A genuine positive verified recovery can update the local verified-observation
time even if a later group fails. Excluded-only recovery cannot. Local status
set/readback/guard are one transaction; ambiguous COMMIT stays UNKNOWN.

Legacy `numeric_annotation_tracks` counts retained cache entries, not observations;
`last_attempt_evidence` is the old attempt-only `last_ingest`. `--no-counts` omits
current verified/excluded keys and retains separately labelled recovery evidence.
Unreadable/malformed status or unknown identity fails loudly, never clean zero.

Extraction instructions: [agent skill](../../skills/fulcra-lab-results/SKILL.md)
and [extraction schema](../../skills/fulcra-lab-results/references/extraction-schema.md).

## Conversions and tests

Unit conversions remain `canonical = raw * factor + offset`. Existing registry,
assay-dependent omissions, plausibility/review gates, two-pass comparison and
rounding are unchanged. The adapter never re-converts or relabels values.

From the repository root:

```bash
uv run --offline pytest -c pyproject.toml packages/labs/tests -q
```

Synthetic actual-Click/session/outbox tests cover native payloads, full target
proof, recovery ordering, uncertainty, immutable delivery, exclusions, partial
progress, status rollback and local-only preview/archive behavior. They do not
establish real-account/source acceptance or historical migration completion.
