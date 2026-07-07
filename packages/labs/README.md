# fulcra-labs

One canonical Fulcra **data track per lab marker** (LDL-C, HbA1c, TSH, ferritin, …), regardless of which
lab produced the report. Observations arrive by parsing lab-report PDFs (LabCorp, Quest, hospital labs).

**Model extracts, code verifies.** An agent reads the PDF and transcribes what's printed (guided by the
[`fulcra-lab-results`](../../skills/fulcra-lab-results/SKILL.md) skill). This package does everything
deterministic: alias resolution, unit conversion, validation, idempotent storage.

**Verify before ingest** — the deliberate inverse of the media plugins' over-capture. Lab data is medical
PII, so a wrong value is worse than a missing one. Nothing is written unless it passes validation (`ok`)
or the operator explicitly confirms a flagged row.

## Storage

Each marker is a custom `NumericAnnotation` definition (composite catalog id `NumericAnnotation/<uuid>`).
Records are written through the shared single-record ingest path (`fulcra_common.wire.build_record` →
`POST /ingest/v1/record`) with a deterministic source id, so re-ingest is a server-side no-op. Series are
read back from `GET /data/v1alpha1/metric/NumericAnnotation%2F<uuid>`. Definition ids are cached in
`~/.config/fulcra-labs/markers.json`; source PDFs are archived **locally only** under
`~/.config/fulcra-labs/documents/`.

## CLI

```
fulcra-labs markers [--search X]        # the canonical marker registry
fulcra-labs check A.json B.json --out agreed.json   # cross-check two extraction passes
fulcra-labs ingest obs.json --source-doc report.pdf [--dry-run] [--yes-reviewed keys]
fulcra-labs status                      # markers known / tracks / per-track counts / last ingest
```

Extraction schema and the agent flow: [SKILL.md](../../skills/fulcra-lab-results/SKILL.md) and
[references/extraction-schema.md](../../skills/fulcra-lab-results/references/extraction-schema.md).

## Conversions

Unit conversions are `canonical = raw * factor + offset` (offset is non-zero only for HbA1c IFCC↔NGSP,
which is genuinely affine). Where a factor is assay-dependent or ambiguous (BUN vs. urea, insulin pmol/L,
Hb mmol/L), the alternate unit is **deliberately absent** so a value in it lands in review rather than
being silently mis-scaled — honesty over coverage.
