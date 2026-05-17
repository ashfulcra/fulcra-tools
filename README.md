# fulcra-csv-importer

Import any CSV stream into [Fulcra](https://fulcradynamics.com) as
`DurationAnnotation` events. Sibling project to
[FulcraMediaHelpers](https://github.com/fulcradynamics/FulcraMediaHelpers);
the media helpers depend on this library for their generic CSV importer.

## Why

If you can get a service to write rows to a CSV — via IFTTT → Google Drive,
Pipedream / n8n → Dropbox, a shell cron, an exported spreadsheet, etc. —
then you can get it into Fulcra. This tool handles the boring parts:

- **Column mapping**: tell it which columns hold the timestamp, title,
  duration, etc.
- **Flexible timestamps**: anything dateparser can parse, including local
  TZ → IANA conversion.
- **Deterministic source IDs**: per-row hash so re-running an import is
  safe — duplicates are skipped, not re-posted.
- **Per-chunk dedup readback**: queries the existing annotation window
  before posting so soft-deleted or partially-imported data doesn't get
  double-stamped.

## Install

```
pip install -e .
fulcra auth login   # uses fulcra-api's CLI for token shell-out
```

## Bootstrap a new annotation definition

```
fulcra-csv bootstrap --name "Concert Tickets" \
  --description "Concerts I bought a ticket for" \
  --tag music --tag tickets
# prints the new definition UUID — save it for `import --definition-id`
```

## Import a CSV

Minimal call (column headers literally named `timestamp`, `title`):

```
fulcra-csv import data.csv --definition-id <uuid>
```

Real example — legacy Spotify → IFTTT → Google Drive applet:

```
fulcra-csv import spotify_ifttt.csv \
  --definition-id <uuid> \
  --ts-col "when" --title-col "track" --subtitle-col "artist" \
  --source-id-col "track_id" --tz America/New_York \
  --tag spotify \
  --extra url=spotify_url
```

`--extra COL=KEY` repeats; each lifts a CSV column into `external_ids[KEY]`
on the annotation.

## Architecture

- `fulcra_csv.events` — `GenericEvent` (point-in-time + duration model) and
  `ColumnMap` (which CSV columns mean what).
- `fulcra_csv.parser` — `parse_csv(path, column_map=..., tz=...)` yields
  `GenericEvent`. No network.
- `fulcra_csv.fulcra` — `FulcraClient` handles auth, tag/definition
  bootstrap, JSONL ingest, dedup readback.
- `fulcra_csv.cli` — `click`-based CLI surface.

Consumers who only need the parser (e.g. `fulcra-media`) can import from
`fulcra_csv` and skip the network layer entirely.
