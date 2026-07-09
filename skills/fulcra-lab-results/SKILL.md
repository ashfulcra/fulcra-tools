---
name: fulcra-lab-results
description: "Parse a lab-report PDF (LabCorp, Quest, hospital labs — any provider) into one canonical Fulcra data track per marker (LDL-C, HbA1c, TSH, ferritin, …). You read the PDF; the fulcra-labs engine normalizes units, validates, and ingests. Verify-before-ingest: nothing is written without passing validation or explicit operator confirmation. Use when the operator says 'parse my labs', 'import my LabCorp/Quest results', or hands over a lab-report PDF."
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

**Stateless — no probes.** Each PDF is a fresh, operator-gated transaction; there is no cross-session
journey to resume into. The intermediate files (`pass_a.json`, `pass_b.json`, `agreed.json`) are ephemeral
local scratch, and re-running any step on the same file is a server-side no-op (deterministic source ids),
so an interrupted run simply restarts from Intake with the same PDF. The only prerequisites — an authed
`fulcra` account and the `fulcra-labs` engine — are covered under Invocation above.

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

Always dry-run first — it validates and prints a verdict per row without writing anything:

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

The engine resolves-or-creates each marker's track on first use (idempotently), then writes each `ok`
row. Re-running is safe: the deterministic source id makes a re-ingest a server-side no-op.

### 5. Report back

Summarize: markers ingested, tracks newly created vs adopted, the review/reject queue (with reasons and
what you did about each), and any in-run dedupe skips. Point the operator at
[Context Web](https://context.fulcradynamics.com) to browse the tracks. Use `fulcra-labs status` to show
per-track observation counts.

---

## Safety rules

- **Never guess a unit.** A blank unit is `null`; the engine holds it for review. Guessing a unit is the
  single most dangerous error this pipeline can make.
- **Never convert or round in extraction.** Transcribe as printed; the engine converts.
- **Never ingest a `review` or `reject` row without explicit operator confirmation** (and even then only
  `review` rows, via `--yes-reviewed`). `reject` rows never ingest.
- **Medical data stays local.** Only the numeric values the operator confirms go to Fulcra. Source PDFs
  are archived to `~/.config/fulcra-labs/documents/` on this machine only — never uploaded.
- **Don't soft-delete tracks.** Fulcra has no per-event delete; cleanup is an operator decision.
- **Collection date is the event time**, not the report date.
