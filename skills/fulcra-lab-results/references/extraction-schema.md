# Extraction schema

The single contract between your PDF reading and the `fulcra-labs` engine. Produce one JSON object per
extraction pass, transcribed **exactly as printed** — no conversion, no rounding, no inferred units.

```json
{
  "lab": "LabCorp | Quest | <other provider name>",
  "report_date": "YYYY-MM-DD",
  "collected_at": "YYYY-MM-DDTHH:MM:SS±TZ  or  YYYY-MM-DD",
  "observations": [
    {
      "marker_raw": "as printed (e.g. 'Cholesterol, Total', 'T4,Free(Direct)', 'GLUCOSE')",
      "value_raw": "as printed (e.g. '185', '5.5', '<0.1', '1,234')",
      "unit_raw": "as printed, or null if the unit column is blank",
      "reference_range_raw": "as printed, or null",
      "flag_raw": "H | L | A | null"
    }
  ]
}
```

## Field rules

- **`collected_at`** — the *specimen collection* date/time (labels: "Collected", "Date Collected",
  "Collection Date"), NOT the report/print date. When only a date is printed, a bare `YYYY-MM-DD` is fine
  (the engine treats it as UTC midnight). Include the timezone offset when the report shows a time.
- **`marker_raw`** — copy the analyte name verbatim, including punctuation and casing. The engine resolves
  aliases (`"CHOL"`, `"LDL Chol Calc (NIH)"`, `"T4,Free(Direct)"` all resolve). If it can't resolve a
  name, that row becomes `review` with fuzzy suggestions — it is never silently dropped or auto-matched.
- **`value_raw`** — the number as printed. Keep qualifiers (`<0.1`, `>300`, `≤5`) and thousands commas;
  the engine parses them (a `<`/`>` becomes a stored `qualifier` of `lt`/`gt`). If the result is textual
  ("Negative", "TNP", "See Note"), still record it — the engine will `reject` it as non-numeric rather
  than you having to decide.
- **`unit_raw`** — exactly as printed. **If the unit is missing, use `null` — do NOT infer it.** A value
  with no unit is held for review; a *wrong guessed* unit corrupts the track.
- **`reference_range_raw` / `flag_raw`** — verbatim if shown, else `null`. Stored on the record for
  context; they do not affect validation.

## What the engine does with this (so you don't)

- Resolves `marker_raw` → a canonical marker (see `fulcra-labs markers`).
- Converts `value_raw` + `unit_raw` → the marker's canonical unit (e.g. glucose `5.5 mmol/L` → `99.1
  mg/dL`; HbA1c `38 mmol/mol` → `5.6 %`). Units it doesn't recognize for that marker → `review`.
- Sanity-checks the converted value against wide physiologic bounds — this catches unit mixups and
  transcription slips (e.g. glucose `5.5` mislabelled `mg/dL` is implausibly low → `review`).
- Computes a deterministic, idempotent source id from (marker, collection time, canonical value).
- Detects duplicate rows within the batch.

Your job is only the faithful transcription above, done twice.
