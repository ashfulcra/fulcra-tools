# fulcra-csv-importer

Import any CSV stream into [Fulcra](https://fulcradynamics.com) as
annotations — media plays, body weight, mood scores, expenses, anything.
Sibling project to
[FulcraMediaHelpers](https://github.com/fulcradynamics/FulcraMediaHelpers);
the media helpers depend on this library for their generic CSV importer.

## Why

If you can get data into a CSV — via IFTTT → Google Drive, Pipedream / n8n
→ Dropbox, a shell cron, a Garmin Connect export, a hand-curated
spreadsheet — then you can get it into Fulcra. This tool handles the
boring parts:

- **Column mapping**: declare which columns hold the timestamp, value,
  title, etc.
- **Flexible timestamps**: anything `dateparser` parses, including local
  TZ → IANA conversion.
- **Three target modes**:
  - **User-defined annotation** (`--definition-id <uuid>`): for custom
    things — concert tickets, projects worked on, weird personal logs.
  - **Built-in Fulcra type** (`--data-type BodyMass`, no definition-id):
    write directly to a native Fulcra time series so a CSV of weights
    doesn't create a parallel annotation alongside the one HealthKit
    populates.
  - **Generic Duration/Instant** (no flags): falls back to plain
    `DurationAnnotation` or `InstantAnnotation`.
- **Duration vs Instant**: `--annotation-type instant` for moment-in-time
  events (a mood entry, a weight reading); `duration` for spans
  (watched a movie, slept). Default is `duration`.
- **Value column**: `--value-col weight_kg --value-type float` for
  measurement annotations. Coerces empty → null.
- **Deterministic source IDs**: per-row hash so re-running an import is
  safe — duplicates skipped, not re-posted. Same content at *different*
  timestamps stays distinct (real replays don't get collapsed).
- **Per-chunk dedup readback**: queries the existing record window before
  posting so soft-deleted or partially-imported data doesn't get
  double-stamped.

## Install

```
pip install -e .
fulcra auth login   # uses fulcra-api's CLI for token shell-out
```

## Three example imports

### 1. Body weight → native Fulcra `BodyMass` (built-in type)

```
fulcra-csv import weights.csv \
  --data-type BodyMass \
  --annotation-type instant \
  --ts-col date --value-col kg --unit kg \
  --tag manual-scale
```

No `--definition-id`. The importer writes to Fulcra's native `BodyMass`
time series with `data: {value: 82.4, unit: "kg"}`. Dedup is purely on
source-id, so re-importing the same CSV is a no-op even if HealthKit also
populated the same period.

### 2. Mood scores → user-defined annotation (instant)

```
fulcra-csv bootstrap --name "Mood" --description "Mood self-reports" \
  --annotation-type instant --value-type int --tag mood
# prints the new definition UUID — save it for the import below

fulcra-csv import mood.csv \
  --definition-id <uuid> \
  --annotation-type instant \
  --ts-col timestamp --value-col mood_score --value-type int \
  --note-col note --tag-col tags
```

### 3. Legacy Spotify → IFTTT → Google Drive (duration, dedup with replays)

```
fulcra-csv import spotify_ifttt.csv \
  --definition-id <uuid> \
  --ts-col "when" --title-col "track" --subtitle-col "artist" \
  --source-id-col "track_id" --tz America/New_York \
  --tag spotify \
  --extra url=spotify_url
```

`--extra COL=KEY` repeats; each lifts a CSV column into
`data.external_ids[KEY]` on the annotation. Use `--data-field COL=KEY`
when you want the column at the TOP level of `data` instead (e.g. when
the built-in type expects `data.unit` directly).

## Architecture

- `fulcra_csv.events` — `GenericEvent` (point-in-time or duration model)
  and `ColumnMap` (which CSV columns mean what).
- `fulcra_csv.parser` — `parse_csv(path, column_map=..., tz=...,
  annotation_type=...)` yields `GenericEvent`. No network.
- `fulcra_csv.fulcra` — `FulcraClient` handles auth, tag bootstrap, JSONL
  ingest, dedup readback. Pass `definition_id` for user-defined
  annotations or `data_type` alone for built-in types.
- `fulcra_csv.cli` — `click`-based CLI surface.

Consumers that only need the parser (e.g. `fulcra-media`) can import from
`fulcra_csv` and skip the network layer entirely.

## Avoiding duplicates with native types

The whole point of the built-in-type target mode is that, once Fulcra's
CLI/API exposes write access to its native data types (BodyMass,
HeartRate, SleepAnalysis, ...), this importer can feed them directly. A
weight CSV that lands in `BodyMass` is the same shape as HealthKit's
output — there's no separate "weight" annotation to keep in sync.

Re-running an import is always safe because dedup is keyed on a stable
per-row hash, not on content equivalence. Same source CSV → same hashes
→ existing rows skipped. Different sources of the same kind of data
(e.g. you import both HealthKit-via-watch and manual-scale-CSV) co-exist
as distinct records under the same `data_type`, which is what you want
— don't collapse them; downstream queries can distinguish by source.
