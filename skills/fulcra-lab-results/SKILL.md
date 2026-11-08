---
name: fulcra-lab-results
description: "Use when the operator asks to import lab-report PDF results from LabCorp, Quest, a hospital or another provider, or supplies a lab-report PDF for structured Fulcra history."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🩸" } }
---

# fulcra-lab-results — lab-report PDFs into per-marker Fulcra tracks

Turn a lab-report PDF into structured, queryable history in the operator's own Fulcra account: one
canonical **data track per marker** (Total Cholesterol, LDL-C, HbA1c, TSH, ferritin, …), regardless of
which lab produced the report. Over time, every LDL-C result — LabCorp in March, Quest in September,
a hospital draw in between — lands on the *same* track, so it reads as one time series.

## The split: model extracts, code verifies

This is the load-bearing idea. **You (the model) do exactly one thing the code can't: read the PDF and
transcribe what's printed.** Everything else — resolving `"CHOL"` to `total-cholesterol`, converting
`5.5 mmol/L` to `99.1 mg/dL`, sanity-checking the value, computing the idempotent id, writing to Fulcra
— is done by the deterministic `fulcra-labs` engine. Do NOT convert units, round, infer a missing unit,
or "fix" a value yourself. Transcribe exactly what's on the page; let the engine do the rest.

This pipeline is **verify-before-ingest** — the deliberate opposite of the media plugins' over-capture.
Lab data is medical PII, so a wrong value is worse than a missing one. Nothing reaches Fulcra unless it
passes validation (`ok`) or the operator explicitly confirms a flagged row.

**Invocation.** The engine is the `fulcra-labs` console script. From the fulcra-tools repo run it with
`uv run fulcra-labs <command>`; once installed as a tool, just `fulcra-labs <command>`. All commands take
`--json` for a machine envelope. Run `fulcra-labs markers` to see the registry.

---

## Where to start

The development importer writes owned v1 **Metric** records through the installed
Collect runtime, using CLI authentication independently of the daemon. See the
[package guide](../../packages/labs/README.md) for schema, install and status details;
released app acceptance and historical conversion are separate.

| Situation | Probe/action | What it establishes |
|---|---|---|
| Local extraction or review | `fulcra-labs markers --json`; `fulcra-labs check pass_a.json pass_b.json --json` | Local registry/comparison; no remote authority. |
| Before real import or after interruption | `fulcra-labs status --json` | Authenticated account/profile local evidence; UNKNOWN/error holds, never clean zero. It does not drain pending or count remote observations. |
| Retained legacy marker selected | Supply `--marker-target MARKER=Metric/UUID` for that canonical marker | Explicit new owned native mapping; never reinterpret an old NumericAnnotation ID. Repeat pins unchanged on recovery. |
| Resume saved work | Re-run the live `ingest` with the same account/profile/pins | Original pending records recover before opening new JSON/PDF, even when new paths are missing/empty. Stop on a hold; never reset journals/outboxes. |
| Preview new source | `fulcra-labs ingest agreed.json --source-doc report.pdf --dry-run --json` | Local validation/hash only, no auth/runtime/profile/DB/archive/recovery/target proof. |

`FULCRA_COLLECT_HOME` selects the private runtime profile and its `cli-labs`
namespace; `FULCRA_LABS_HOME` selects the retained cache/local PDF archive root.
Keep the same runtime profile, CLI account and explicit pins while pending work
exists. Paths, newly discovered markers and review selections do not rebind
pending records. Account/pin/spec changes hold them. New markers without a
selected legacy cache entry may use full owned-spec adoption/creation; corrupt
legacy state is UNKNOWN and originals remain read-only.

Interrupted imports are stateful. The whole eligible new multi-marker batch is
schema-checked and durably staged before current record POSTs. Every actual
pending target must pass fresh marker/spec/ownership/schema/Metric metadata proof;
recovered payloads are checked per group. Unknown issued work is reconciled by
exact reads without automatic resend. Keep extraction/PDF originals for review;
recovery of already-staged payload does not need them. A later-group failure can
retain verified complete-group lower bounds, not complete-batch success.

---

## The flow

### 1. Intake

The operator provides one or more PDF paths (or points at a folder). Save any chat-attached PDF to a
local path first. Confirm the file(s) you'll work on.

> **Coming later — portal fetch.** A future browser-agent step will pull the PDF straight from the
> LabCorp / Quest patient portal. For now the operator downloads the PDF themselves and hands you the
> path. Don't attempt to log into a lab portal.

### 2. Extract the PDF — TWICE, independently

Read the PDF and produce the extraction schema (below). Then **read it again from scratch** — a second,
independent pass; do not copy the first pass's numbers. Two independent transcriptions that agree are the
model-side guard that pairs with the code-side validation.

Transcription rules (both passes):
- **Exactly as printed.** No unit conversion, no rounding, no inferring a missing unit. If the unit
  column is blank, `unit_raw` is `null` — never guess it.
- **One row per analyte.**
- **Collection date, not report date.** When both appear, `collected_at` is the *specimen collection*
  date/time (often "Collected:" / "Date collected"). Capture the report date separately as `report_date`.
- Capture the reference range and any H/L/A flag verbatim if present (`reference_range_raw`, `flag_raw`).

Write each pass to its own JSON file (e.g. `pass_a.json`, `pass_b.json`). Schema and detailed rules:
[references/extraction-schema.md](references/extraction-schema.md).

### 3. Cross-check the two passes

```bash
uv run fulcra-labs check pass_a.json pass_b.json --out agreed.json --json
```

Rows that agree on (marker, value, unit) go into `agreed.json` (an ingest-ready extraction).
Disagreements are listed. For each disagreement, **re-read the PDF a third time for just that row** and
resolve it; if it's still ambiguous, show the operator the exact text and ask. Add any resolved rows into
`agreed.json`. Do not ingest a row the two passes disagreed on until it's resolved.

### 4. Validate (dry run), confirm, then ingest

Always dry-run first. It validates and optionally hashes the PDF locally; it creates no client/runtime state, archive or records and does not recover pending work:

```bash
uv run fulcra-labs ingest agreed.json --source-doc report.pdf --dry-run --json
```

Each row gets a verdict:
- **`ok`** — resolved, unit accepted, value plausible → auto-ingestable.
- **`review`** — needs an operator call: unresolved/ambiguous marker, missing or unknown unit,
  implausible value (a classic unit-mixup catch), or an in-batch duplicate. Reasons are attached.
- **`reject`** — malformed: non-numeric value, no/parse-failed collection date, future or pre-1990 date.
  Never ingestable.

**Show the operator the verdict table.** Walk through every `review` and `reject` row with its reason.
Then get explicit confirmation before the real write. Only after they confirm:

```bash
uv run fulcra-labs ingest agreed.json --source-doc report.pdf
```

To push a *specific* reviewed row through (e.g. an implausible-but-real value the operator vouches for),
pass its marker key: `--yes-reviewed ldl-c,ferritin`. Never blanket-confirm review rows on the operator's
behalf.

For a selected retained legacy marker, add an explicit new mapping such as
`--marker-target ferritin=Metric/00000000-0000-4000-8000-000000000001` (replace the
illustrative UUID with the actual owned target). Repeat every provided pin on
recovery. Unknown/duplicate canonical marker keys and incompatible targets hold.

Native marker destinations use exact registry canonical units as immutable Metric
metadata; records omit optional top-level `unit` and retain original `raw_unit`.
The codec checks the validator's canonical unit rather than relabelling values.
Exact served-schema proof precedes current staging. Upload acceptance alone is
not success: every delivered record needs exact readback and guarded checkpoints.
Exclusions do not count as verified observations.

The unchanged source hash uses marker, collection time and six-significant-figure
canonical value formatting. Corrections can create a new observation without
removing the old one, but small value/provenance changes can retain identity.
A conflicting existing current record holds before staging; unrelated extra
top-level enrichment is allowed. Do not promise server-side no-ops or silently
replace clinical data. Already-staged recovery retains its original payload and
shared exact-delivery behavior; this is not a cross-host edit transaction.

### 5. Report back

Report the actual aggregate current `outcome.ingested` and `excluded` complete-group
lower bounds, review/reject reasons and in-run duplicate skips. Keep the separate
`recovery` verified/excluded evidence separate; do not sum repeated invocations.
`would_ingest` is preview intent. Unsupported marker attribution is explicitly
UNKNOWN; do not invent per-marker counts or created/adopted totals. Nonzero JSON
failures retain known progress and do not prove rejection or an empty destination.

`fulcra-labs status --json` shows profile/account-scoped local evidence.
`last_attempt` and legacy `last_attempt_evidence` are not success.
`last_verified_ingest` is the local verified-observation/checkpoint time, not
specimen or provider ingest time. Cached current counts describe `last completed current delivery` with
`current_completed_at`; a newer `last_attempt` can describe a failed/empty attempt
without relabelling those prior counts. `last_recovery` separately retains the latest recorded recovery attempt's complete
or UNKNOWN lower bounds. Positive verified recovery can advance the local verified
observation time despite later group failure; excluded-only recovery cannot.
`--no-counts` omits current verified/excluded, retaining labelled recovery evidence.
Legacy `numeric_annotation_tracks` counts cached tracks, not remote observations.
Status identity/read failures are loud UNKNOWN. Ambiguous status COMMIT remains
UNKNOWN; never repair it by reporting zero or erasing old evidence.

Point the operator at [Context Web](https://context.fulcradynamics.com) to browse
the owned native tracks. Actual app/source acceptance remains a separate QA gate.

---

## Safety rules

- **Never guess a unit.** A blank unit is `null`; the engine holds it for review. Guessing a unit is the
  single most dangerous error this pipeline can make.
- **Never convert or round in extraction.** Transcribe as printed; the engine converts.
- **Never ingest a `review` or `reject` row without explicit operator confirmation** (and even then only
  `review` rows, via `--yes-reviewed`). `reject` rows never ingest.
- **PDF bytes stay local.** Confirmed canonical values, collection time, raw value/unit,
  range/flag, lab, marker, qualifier/report date, traceability note and source-document
  token go to Fulcra. A supplied PDF is copied from one consistent byte snapshot to
  `~/.config/fulcra-labs/documents/<sha256>.pdf` (`FULCRA_LABS_HOME` override),
  after recovery and new-input admissibility. The archive can remain after a later
  failure. The full `sha256:basename` token identifies it; the PDF is never uploaded.
- **Preserve originals.** Do not delete legacy tracks, change replay policy or erase pending state as a retry strategy. Corrections and cleanup need an explicit disposition.
- **Collection date is the event time**, not the report date.
