# fulcra-labs

One canonical Fulcra **data track per lab marker** (LDL-C, HbA1c, TSH, ferritin, …), regardless of which
lab produced the report. An agent extracts observations from lab-report PDFs (LabCorp, Quest, hospital labs);
this CLI consumes the resulting JSON. It does not parse PDFs itself, and it does
not need Collect.

**Model extracts, code verifies.** An agent reads the PDF and transcribes what's printed (guided by the
[`fulcra-lab-results`](../../skills/fulcra-lab-results/SKILL.md) skill). This package does everything
deterministic: alias resolution, unit conversion, validation, idempotent storage.

**Verify before ingest** — the deliberate inverse of the media plugins' over-capture. Lab data is medical
PII, so a wrong value is worse than a missing one. An observation is posted only
if it passes validation (`ok`) or the operator explicitly confirms a resolvable
flagged row. Local archiving and definition setup can happen before record posting.

## Storage

Each marker is a custom `NumericAnnotation` definition (composite catalog id `NumericAnnotation/<uuid>`).
Records use `wire.build_typed_record` and `IngestPipeline.ingest_typed` to POST
unwrapped records to `/ingest/v1/record/NumericAnnotation`. Each row has a
deterministic source ID. **Deduplication is client-side:** the importer checks
which IDs already exist and refuses to post if that check fails. After posting,
it polls for visibility and fails if records do not appear. An accepted upload
alone is not evidence that all values landed.

This precheck handles repeat imports; it is not a transaction between concurrent
runs. Avoid running two imports of the same report at once. A failed import can
already have created definitions, archived its source document, or posted records;
inspect the failure before retrying.

Series are read back through the metric API using the definition's composite id.
Definition ids are cached in `~/.config/fulcra-labs/markers.json`; source PDFs
supplied with `--source-doc` are archived **locally only** under
`~/.config/fulcra-labs/documents/`. `FULCRA_LABS_HOME` overrides that directory.
The remote note carries traceability text and a document token, not the PDF.

## Install

Python 3.11+ and `uv`. From the repository root:

```bash
uv sync --package fulcra-labs
source .venv/bin/activate
fulcra auth login
fulcra-labs markers --search ferritin
```

Runtime dependencies are `click`, `httpx`, and the sibling `fulcra-common`.
Marker lookup, extraction comparison, and `ingest --dry-run` work locally;
a real ingest and live track counts need Fulcra authentication.

## CLI

```
fulcra-labs markers [--search X]        # the canonical marker registry
fulcra-labs check A.json B.json --out agreed.json   # cross-check two extraction passes
fulcra-labs ingest obs.json --source-doc report.pdf --dry-run
fulcra-labs ingest obs.json --source-doc report.pdf --yes-reviewed ferritin
fulcra-labs status                      # markers known / tracks / per-track counts / last ingest
```

`--yes-reviewed` accepts comma-separated canonical marker keys. It permits
review-held rows whose values and units were resolved; it cannot rescue an
unparseable value or unknown marker. Each command also supports `--json`.
`status --no-counts` skips live record-count queries. A failed live count is
reported as `?` (JSON `null`) with a warning, while `status` still succeeds.
`last_ingest` is saved before landing verification, so it is not proof of a
successful import.

Extraction schema and the agent flow: [SKILL.md](../../skills/fulcra-lab-results/SKILL.md) and
[references/extraction-schema.md](../../skills/fulcra-lab-results/references/extraction-schema.md).

## Conversions

Unit conversions are `canonical = raw * factor + offset` (offset is non-zero only for HbA1c IFCC↔NGSP,
which is genuinely affine). Where a factor is assay-dependent or ambiguous (BUN vs. urea, insulin pmol/L,
Hb mmol/L), the alternate unit is **deliberately absent** so a value in it lands in review rather than
being silently mis-scaled — honesty over coverage.

## Testing

From the repository root:

```bash
uv run --package fulcra-labs --extra dev pytest packages/labs/tests -q
```

Synthetic fixtures cover marker aliases, conversions, two-pass comparison,
validation, repeat-import checks, typed writes, and landing verification.
