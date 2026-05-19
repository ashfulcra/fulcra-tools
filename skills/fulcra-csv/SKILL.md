---
name: fulcra-csv
description: Import any CSV stream into a Fulcra account as annotations — body weight, mood scores, expenses, sleep, media plays, anything timestamped. Use when the user wants to ingest a CSV they got from somewhere (a wearable, a Pipedream/IFTTT workflow, a hand-rolled spreadsheet) into Fulcra.
---

# fulcra-csv — Import any CSV into Fulcra

`fulcra-csv` is a small CLI that maps CSV columns onto Fulcra annotation events. It's the foundation `fulcra-media-helpers` uses for its `generic-csv` importer, but it works standalone for **any** kind of data: weights, moods, expenses, water intake, custom logs.

This skill teaches you (the AI agent) how to import a CSV into the right Fulcra annotation type for a given user data shape.

---

## Three target modes — pick first

Every import targets ONE of three places. Decide first which you're using:

### 1. User-defined annotation (most common)

You have (or will create) a custom Fulcra annotation definition. The CLI writes events under it. Pass `--definition-id <uuid>`.

```bash
fulcra-csv bootstrap --name "Mood" --description "Mood self-reports" \
  --annotation-type instant --value-type int --tag mood
# → prints the new UUID; save it
fulcra-csv import mood.csv --definition-id <uuid> \
  --annotation-type instant --ts-col timestamp --value-col score \
  --value-type int --note-col context
```

### 2. Built-in Fulcra type (BodyMass, HeartRate, etc.) — no definition-id

When the user wants the data to land in Fulcra's *native* time series (the same place HealthKit imports go), pass `--data-type <Name>` and SKIP `--definition-id`. The CLI doesn't append an annotation-def source to the record, so dedup is purely source-id-based and CSV imports can coexist with HealthKit imports of the same kind without duplicating.

```bash
fulcra-csv import weights.csv --data-type BodyMass \
  --annotation-type instant --ts-col date --value-col kg --unit kg \
  --tag manual-scale
```

⚠️ Built-in-type writes assume the receiving schema matches. Check `fulcra catalog` for known data types. As of this skill's writing, the data-type write API is forthcoming — when shipped, BodyMass/HeartRate/StepCount/etc. are first-class targets.

### 3. Generic DurationAnnotation / InstantAnnotation — no flags

Defaults. The CLI writes plain DurationAnnotation events (or InstantAnnotation with `--annotation-type instant`). Useful for "throw a CSV in and forget" cases where you don't care to set up a named definition.

```bash
fulcra-csv import random.csv  # all defaults: timestamp + title columns, DurationAnnotation
```

---

## Annotation type — duration vs instant

`--annotation-type duration` (default) — events have `start_time` and `end_time`. For watches, listens, workouts (anything with a span). The CLI uses `--end-col` or `--duration-col`; falls back to a 1-second sentinel when neither is given (Fulcra silently drops zero-duration events).

`--annotation-type instant` — point-in-time. For weights, moods, single readings. `recorded_at` only has `start_time`. The `--end-col` / `--duration-col` flags error out if you pass them with `--annotation-type instant`.

---

## Value column — for measurements

If the row has a numeric reading (weight, score, count), use `--value-col <name>` to lift it into `data.value`. Coerce with `--value-type {float,int,str,bool}` (default float). Pair with `--unit <string>` to add a constant `data.unit`.

```bash
# Body weight in kg
fulcra-csv import weights.csv \
  --data-type BodyMass --annotation-type instant \
  --ts-col date --value-col kg --value-type float --unit kg \
  --tag manual-scale
```

Empty value cells become `None` (not zero, not the string `""`).

---

## Column mapping cheatsheet

| Flag | Purpose | Required? |
|---|---|---|
| `--ts-col` | Timestamp column header | Yes (default `timestamp`) |
| `--title-col` | Title column header | No (default `title`); used for both `note` and `title` |
| `--subtitle-col` | Subtitle (e.g. artist for music) | No; if set, `note` becomes `subtitle – title` |
| `--note-col` | Override note column | No |
| `--value-col` | Measurement value column | Only for value-bearing rows |
| `--value-type` | float/int/str/bool | Default `float`; only matters with `--value-col` |
| `--unit` | Constant unit string | Optional |
| `--end-col` | Explicit end-time column | Optional (duration only) |
| `--duration-col` | Duration-in-seconds column | Optional (duration only) |
| `--source-id-col` | Per-content id column (mixed into source-id hash) | Optional |
| `--tag-col` | Per-row tag column | Optional |
| `--tag` | Default tag for all rows | Optional |
| `--data-field COL=KEY` | Lift CSV column into `data.<key>` | Repeatable |
| `--extra COL=KEY` | Lift CSV column into `data.external_ids[<key>]` | Repeatable |
| `--tz` | IANA tz for naive timestamps | Default `UTC` |
| `--source-id-prefix` | Override deterministic id prefix | Default `com.fulcradynamics.csv.v1` |
| `--dry-run` | Parse + print first 5 rows; don't ingest | Optional |

---

## Bootstrap a new annotation definition

If the user wants a custom def, mint it first:

```bash
fulcra-csv bootstrap \
  --name "Concerts attended" \
  --description "Live music I went to" \
  --tag music --tag tickets
# → prints UUID
```

For measurement-bearing annotations, set `--annotation-type` and `--value-type`:

```bash
fulcra-csv bootstrap \
  --name "Resting Heart Rate (manual)" --description "Daily morning RHR" \
  --annotation-type instant --value-type int --unit bpm
```

Tags are auto-created if they don't exist.

---

## Soft-delete a definition

```bash
fulcra-csv soft-delete <uuid> --confirm
```

⚠️ **Fulcra has no per-event delete.** Soft-deleting a definition removes the def from the user's account but its events stay visible in queries with their `source_id` pointing at the deleted def. For a true "reset," soft-delete + create a new def with a different `source_id_prefix` so future imports namespace cleanly. The `fulcra-media` sibling has a `reset` command that wraps this for the four media defs (Watched/Listened/Activity/Read).

---

## Critical invariant: source_ids always include the timestamp

When `--source-id-col` is set, the column value is mixed into the hash **with** the timestamp, not used verbatim. Two plays of the same Spotify track at different times produce distinct events. Don't try to "preserve" content IDs in source_ids — they're hashed, idempotency is per-row, not per-content. (Content-level identity belongs in `--extra content_fingerprint=fp`.)

This is why **re-running the same import is always safe** — same input rows produce same source_ids, Fulcra dedups silently. You don't need to track "have I imported this yet?"

---

## Recipes — common shapes

### Body weight (HealthKit-compatible)

```csv
date,kg
2026-05-01,82.4
2026-05-02,82.1
```

```bash
fulcra-csv import weights.csv \
  --data-type BodyMass --annotation-type instant \
  --ts-col date --value-col kg --value-type float --unit kg \
  --tag manual-scale
```

### Mood entries

```csv
timestamp,score,note
2026-05-01T09:00:00Z,7,morning coffee good
2026-05-01T22:00:00Z,5,long day
```

```bash
fulcra-csv bootstrap --name "Mood" --annotation-type instant \
  --value-type int --tag mood
# → save UUID as $MOOD_UUID

fulcra-csv import mood.csv --definition-id $MOOD_UUID \
  --annotation-type instant --ts-col timestamp \
  --value-col score --value-type int --note-col note
```

### Expenses with category tags

```csv
date,amount,merchant,category
2026-05-01,12.50,Blue Bottle,coffee
2026-05-01,38.00,Whole Foods,groceries
```

```bash
fulcra-csv bootstrap --name "Expenses" --annotation-type instant \
  --value-type float --unit usd --tag finance
# → save UUID

fulcra-csv import expenses.csv --definition-id $UUID \
  --annotation-type instant --ts-col date --value-col amount \
  --value-type float --unit usd \
  --title-col merchant --tag-col category
```

### Pipedream / IFTTT play log (music)

```csv
ts,track,artist,track_id,url
2026-05-01T09:00:00Z,Reelin' In The Years,Steely Dan,1I7zHEdDx8Ny5RxzYPqsU2,https://...
```

```bash
fulcra-csv import plays.csv --definition-id $LISTENED_UUID \
  --ts-col ts --title-col track --subtitle-col artist \
  --source-id-col track_id --tag spotify --extra url=spotify_url
```

### Sleep durations

```csv
start,end,quality
2026-05-01T23:00:00Z,2026-05-02T07:30:00Z,8
```

```bash
fulcra-csv bootstrap --name "Sleep" --tag sleep
fulcra-csv import sleep.csv --definition-id $UUID \
  --ts-col start --end-col end --value-col quality --value-type int
```

---

## Export — round-tripping annotations back to CSV

`fulcra-csv export` is the inverse of `import`. Reach for it when the user wants to:

- **Round-trip a CSV** — re-export what an import landed to verify columns/values look right, or to hand the data to another tool that expects CSV.
- **Audit a recent import** — pull the last day/week of a definition and eyeball it instead of running ad-hoc API queries.
- **Slice for a downstream tool** — pull a configurable subset of fields (well-known + `data.<key>` + `external_ids.<key>`) into a tidy CSV, optionally with epoch timestamps.

It is NOT a sync/backfill mechanism — it's a one-shot read. If the user wants ongoing sync, point them at a scheduled job that runs export on a window.

### Target — same model as import

Pass ONE of:

- `--definition-id <uuid>` — scope to a user-defined annotation. The CLI fetches the underlying data type (default `DurationAnnotation`) and filters records whose `sources` array references the target def. This mirrors the importer's dedup-readback, so what you see in export is what import would dedup against.
- `--data-type <Name>` — pull a built-in time series (`BodyMass`, `HeartRate`, `DurationAnnotation`, etc.).

`--start` is required (ISO-8601 or relative — `"1 week ago"`, `"yesterday"`). `--end` defaults to `now`.

### Column model — `--columns col1,col2,...`

Default: `start_time,end_time,tag,note,value`. Each entry resolves against the record in one of three ways:

| Form | Source |
|---|---|
| Well-known field (`start_time`, `end_time`, `note`, `title`, `value`, `unit`, `tag`, `tags`, `category`, `source_id`, `definition_id`, ...) | Top-level on the record, or normalised from `recorded_at` (timestamps), `tag_names` (tags), `sources` (source_id / definition_id). |
| `data.<key>` | The (possibly JSON-encoded) `data` payload — the place `--data-field COL=KEY` lifts to on import. |
| `external_ids.<key>` | `data.external_ids[<key>]` — symmetric to import's `--extra COL=KEY`. |

`source_id` is special: it returns the first non-definition source (the per-row dedup key), so you can round-trip dedup hashes back through.

### Other knobs

- `--date-format iso|epoch|local` (default `iso`, always UTC with `Z` suffix). `epoch` writes integer seconds. `local` honors `--tz`.
- `--tz <iana>` — used for parsing relative `--start`/`--end` and for `--date-format local`.
- `--out <path>` — file output. Omit for stdout.

### CSV-injection guard

By default, cells starting with `= + - @ \t \r` are prefixed with a single quote (`'`) so Excel/Sheets/Numbers don't interpret them as formulas. This is OWASP-grade defense-in-depth and on by default. The library exposes `ExportOptions.guard_formulas=False` for callers feeding CSV into a downstream parser that doesn't need it; the CLI does NOT expose a flag — keep it on for spreadsheets. Booleans render as lowercase `"true"`/`"false"` so they round-trip through `coerce_value`.

### Worked example — audit yesterday's mood imports

The user just ran `fulcra-csv import mood.csv --definition-id $MOOD_UUID ...` and wants to see what landed:

```bash
fulcra-csv export \
  --definition-id $MOOD_UUID \
  --start yesterday \
  --columns start_time,value,note,tag,source_id \
  --date-format local --tz America/New_York
```

Stdout is a CSV with five columns, timestamps in the user's wall-clock TZ, plus `source_id` so they can match rows against the importer's deterministic hashes.

### Pitfalls

- **Export does NOT trigger a re-fetch from the source.** It reads what Fulcra has. If a recent import is still propagating through ingest, you may see fewer rows than you posted — wait a minute and re-run.
- **`--definition-id` requires the underlying `data_type` to match.** The export defaults to `DurationAnnotation`; if the user bootstrapped an instant annotation, pass `--data-type InstantAnnotation` alongside `--definition-id`.
- **Don't promise byte-identical round-trips.** Booleans go to lowercase, lists/dicts JSON-encode to single-line, and the formula-guard `'` prefix is a real cell value. The data round-trips through `coerce_value`, but the CSV bytes differ from the original import file.

---

## Periodic ingestion

CSV imports are inherently one-shot ("import this file"), not incremental. There's no watermark layer (that lives in `fulcra-media` for its API-poll importers).

For agent-driven recurring sync of a CSV that grows over time (e.g. a Pipedream workflow appending rows to a Drive sheet):

1. Download the latest CSV to a temp file.
2. Run `fulcra-csv import <tempfile> ...` — re-imports are idempotent.
3. Skipped rows are silent (Fulcra source-id dedup). Posted rows are the delta.

The `--dry-run` flag is useful for a cheap "any new rows?" check (it parses and prints the first 5, doesn't ingest).

---

## Fulcra Life API auth

Same as fulcra-media: shells out to `fulcra auth print-access-token` from the [fulcra-api](https://github.com/fulcradynamics/fulcra-api-python) package, or honors `FULCRA_ACCESS_TOKEN=...` for non-interactive contexts.

---

## Common failure patterns

### "No definition_id found" exit

You forgot `--definition-id <uuid>` AND didn't set `--data-type` AND `--annotation-type` is duration. Either bootstrap a def, or pass `--data-type DurationAnnotation` explicitly.

### Events post but don't appear in `fulcra get-records`

Fulcra's ingest-to-query indexing lag (seconds to minutes for bulk imports). Re-run shouldn't help — wait a few minutes and re-query.

### Same content keeps producing duplicate events on each import

You're probably using `--source-id-col` for a per-row id (database row uuid) and expecting per-row uniqueness. The CLI ALWAYS mixes timestamp into the hash. For truly stable per-row identity, ensure the (timestamp, id) pair is stable across imports. If the row's timestamp changes between exports, you'll get distinct source_ids — that's intentional but maybe not what you want.

### "ValueError: unparseable timestamp"

dateparser can't parse the timestamp column. Try `--tz America/New_York` if rows are local-time-no-tz. Worst case, pre-process the CSV to ISO 8601.

---

## Architectural references (for the curious agent)

- `fulcra_csv.events` — `GenericEvent`, `ColumnMap`, `coerce_value`
- `fulcra_csv.parser` — `parse_csv(path, column_map, tz, annotation_type)`
- `fulcra_csv.fulcra` — `FulcraClient` (auth, ingest, dedup readback, per-chunk verification)
- `fulcra_csv.confidence` — `apply_cluster_policy`, `find_low_conf_twins`, `apply_twin_decisions` (used by fulcra-media's Trakt importer, but available standalone)
- `fulcra_csv.cli` — click-based CLI entry

---

## Don't

- **Don't bake `--definition-id` into a script as a constant** — the user might soft-delete + re-bootstrap, changing the UUID. Always read it from a fresh `fulcra-csv bootstrap` or from the user's state.
- **Don't try to dedup at the agent layer** — Fulcra handles source-id dedup natively. Just re-run the import; collisions are silent.
- **Don't write `data.note` from a column the user didn't intend to expose** — `--note-col` is opt-in.
- **Don't pass `--value-type bool` for "0/1" rows unless that's truly the schema** — bool collapses any non-truthy string to false; for diary-style yes/no events use int (0/1).
- **Don't print the user's access token to stdout** — neither in error envelopes nor in success messages. The CLI scrubs URL params in exception messages, but raw token blobs are sensitive.
