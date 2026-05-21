# fulcra-dayone — design

**Date:** 2026-05-21
**Status:** Approved design, ready for implementation planning.

## Goal

A new package in the `fulcra-tools` monorepo that imports selected
[Day One](https://dayoneapp.com) journal entries into the user's Fulcra
account as annotations. Each imported entry becomes one InstantAnnotation
under a user-defined "Journal" definition, carrying the entry's full text,
its Day One tags, and lightweight metadata.

## Package

- Project name: `fulcra-dayone`
- Directory: `packages/dayone/`
- Python module: `fulcra_dayone`
- CLI command: `fulcra-dayone`
- Workspace dependencies: `fulcra-common`, `fulcra-csv-importer`, `click`

The package is a Day One reader + entry filter + converter + thin CLI.
The actual Fulcra ingest (dedup-readback, chunked POST) is **reused** from
`fulcra-csv-importer` — it is not reimplemented.

## Architecture

```
Day One data ──▶ readers ──▶ DayOneEntry[] ──▶ filter ──▶ convert ──▶ GenericEvent[]
                                                                          │
                                                                          ▼
                                          fulcra_csv.FulcraClient.run_import(...)
                                                                          │
                                                                          ▼
                                                                       Fulcra
```

File layout:

| File | Responsibility |
|---|---|
| `fulcra_dayone/entry.py` | The `DayOneEntry` dataclass — the reader-agnostic entry model. |
| `fulcra_dayone/readers/json_export.py` | Read a JSON-export `.zip` or unzipped folder → `DayOneEntry[]`. |
| `fulcra_dayone/readers/local_db.py` | Read Day One's local SQLite database → `DayOneEntry[]`. |
| `fulcra_dayone/readers/__init__.py` | `read(source, *, local_db, db_path) -> list[DayOneEntry]` dispatch. |
| `fulcra_dayone/filter.py` | Apply the four selection filters to a `DayOneEntry[]`. |
| `fulcra_dayone/convert.py` | `DayOneEntry` → `fulcra_csv` `GenericEvent`. |
| `fulcra_dayone/client.py` | `DayOneFulcraClient` — see "Fulcra client" below. |
| `fulcra_dayone/cli.py` | The `fulcra-dayone` Click CLI. |
| `tests/` | Unit tests (mock transport, JSON + SQLite fixtures). |
| `README.md`, `pyproject.toml` | Package docs + metadata. |

## The `DayOneEntry` model

`entry.py` defines a frozen dataclass — the common output of every reader:

```python
@dataclass(frozen=True)
class DayOneEntry:
    uuid: str                 # Day One's stable per-entry id
    creation_date: datetime   # timezone-aware (UTC)
    text: str                 # Markdown body
    tags: tuple[str, ...]     # Day One tags (may be empty)
    starred: bool
    journal: str              # journal name
    location: str | None      # composed place name, or None
    photo_count: int          # number of attached photos
    word_count: int           # whitespace-split word count of `text`
```

## Input modes / readers

The CLI accepts three input modes; every reader yields `DayOneEntry[]`.

### json_export reader (zip + folder)

Day One's File → Export → JSON produces a `.zip` containing one
`<JournalName>.json` per exported journal plus media folders. The reader
accepts either the `.zip` (extracted to a temp dir) or an
already-unzipped folder. It parses every `*.json` file found:

- Each JSON file has shape `{"metadata": {...}, "entries": [ {...}, ... ]}`.
- The journal name is the JSON filename stem.
- Per-entry fields used: `uuid`, `creationDate` (ISO 8601 → UTC datetime),
  `text` (Markdown), `tags` (list, optional), `starred` (bool, optional),
  `location` (object, optional — `placeName`/`localityName`/`country`
  composed into one string), `photos` (list, optional — length only).
- `word_count` is computed from `text`.

### local_db reader

Reads Day One's local database directly — no manual export. The schema
below was verified against the Day One install on the development machine
on 2026-05-21 (a Core Data store, 1,771 entries, 5 journals, 3 tags).

- **Location:** the database is at
  `~/Library/Group Containers/<TEAMID>.dayoneapp2/Data/Documents/DayOne.sqlite`
  — `<TEAMID>` is Day One's Apple team id (`5U8NS4GX82` on the dev
  machine). The reader globs
  `~/Library/Group Containers/*.dayoneapp2/Data/Documents/DayOne.sqlite`;
  `--db-path` overrides. If nothing is found it fails with a message
  naming the searched glob.
- **Snapshot before read:** the DB belongs to a possibly-running app, so
  the reader copies it to a temp file with an APFS clone (`cp -c`,
  falling back to a plain copy off-APFS) and reads the copy — the same
  approach as the media-helpers Apple Podcasts importer. It never opens
  the live database.
- **Schema** (Core Data — `Z`-prefixed tables; dates are float seconds
  since the 2001-01-01 UTC epoch):
  - `ZENTRY` — one row per entry: `Z_PK`, `ZUUID` (32-char uppercase
    hex — the same id the JSON export uses, so `source_id` is stable
    across both readers), `ZCREATIONDATE`, `ZMARKDOWNTEXT` (entry body),
    `ZSTARRED` (0/1), `ZJOURNAL` (FK → `ZJOURNAL.Z_PK`), `ZLOCATION`
    (nullable FK → `ZLOCATION.Z_PK`).
  - `ZJOURNAL` — `Z_PK`, `ZNAME`.
  - `ZTAG` — `Z_PK`, `ZNAME`.
  - `ZLOCATION` — `Z_PK`, `ZPLACENAME`, `ZLOCALITYNAME`,
    `ZADMINISTRATIVEAREA`, `ZCOUNTRY`.
  - `ZATTACHMENT` — `ZENTRY` (FK), `ZTYPE`; an entry's photo count is the
    number of its `ZATTACHMENT` rows.
  - entry↔tag is a Core Data many-to-many join table named
    `Z_<EntryEnt>TAGS` (`Z_17TAGS` on the dev machine), with columns
    `Z_<EntryEnt>ENTRIES` and `Z_<TagEnt>TAGS1`.
  - `Z_PRIMARYKEY` maps entity *names* to entity *numbers* (`Entry` → 17,
    `Tag` → 66, … on the dev machine). The names are stable across Day
    One versions; the numbers are not, and the join-table name is frozen
    at whatever the numbers were when the relationship was created. So
    the reader resolves the numbers by name from `Z_PRIMARYKEY` and finds
    the join table by looking for the `Z_%TAGS` table carrying a
    `Z_<EntryEnt>ENTRIES` column — it never hard-codes `Z_17TAGS`.
- **Schema drift:** if `Z_PRIMARYKEY`, an expected `Z`-table, a required
  column, or the tag join table can't be resolved, the reader raises
  "Day One database schema not recognized — use the JSON export instead."
  It never silently imports partial data.
- **Unreadable entries:** in practice `ZMARKDOWNTEXT` is plaintext even
  for journals that have encryption enabled — only 8 of 1,771 entries on
  the dev machine have empty text (photo-only entries, or genuinely
  undecryptable ones). The reader skips any entry with null/empty
  `ZMARKDOWNTEXT` and reports the count, rather than assuming whole
  journals are unreadable.

## Selection filters

`filter.py` applies up to four filters, **AND-combined**; an unspecified
filter is a no-op (matches everything):

| Filter | CLI option | Match rule |
|---|---|---|
| Tag | `--tag` (repeatable) | entry has at least one of the given tags |
| Journal | `--journal` (repeatable) | entry's journal name is one of the given names |
| Date range | `--since` / `--until` (ISO date) | `creation_date` within the inclusive range |
| Starred | `--starred` (flag) | entry is starred |

With no filters given, the CLI requires explicit `--all` to import every
entry — a guard against an accidental full-journal import.

## Conversion to a Fulcra annotation

`convert.py` maps each `DayOneEntry` to a `fulcra_csv.GenericEvent`:

| GenericEvent field | Value |
|---|---|
| `annotation_type` | `INSTANT` |
| `start_time` | `creation_date` |
| `end_time` | `None` |
| `title` | first non-empty line of `text`, leading Markdown `#`/whitespace stripped, capped at 120 chars |
| `note` | full `text`, with Day One media placeholders (`![](dayone-moment://…)` and similar) replaced by `[photo]` |
| `tag` | `None` |
| `extra_tags` | `tuple(entry.tags)` — see "csv-importer change" |
| `source_id` | `com.fulcra.dayone.<first 16 hex of sha256(uuid)>` |
| `value` | `None` |
| `external_ids` | `dayone_uuid`, `journal`, `starred`, `word_count`, `photo_count`, and `location` when present |

## The "Journal" annotation definition

Imported entries land under a user-defined **InstantAnnotation** named
"Journal". `client.py` defines:

```python
class DayOneFulcraClient(fulcra_csv.FulcraClient):
    def ensure_journal_definition(self) -> str: ...
```

`DayOneFulcraClient` subclasses `fulcra_csv.FulcraClient` (itself a
`fulcra_common.BaseFulcraClient`), so it inherits `run_import`,
`ensure_tag`, the httpx client, and auth.

`ensure_journal_definition` is **find-or-create by name** — it lists the
account's annotation definitions, returns the id of a live (non-deleted)
InstantAnnotation named "Journal", and only POSTs a new one if none
exists. If duplicates exist it returns the oldest by `created_at`. This
mirrors the fix made for the attention package's duplicate-definition bug
and means a second machine never spawns a parallel "Journal" definition.

No persisted state/config file: find-or-create is idempotent, so each run
re-resolves the definition and the tags fresh.

## Required change to `fulcra-csv-importer` (multi-tag)

A Day One entry can have several tags, but `fulcra_csv.GenericEvent`
carries a single `tag`. To honor "Day One tags become Fulcra tags" this
spec includes a small, additive change to `fulcra-csv-importer`:

- `GenericEvent` gains `extra_tags: tuple[str, ...] = ()`.
- `FulcraClient._build_record` resolves both `tag` and every name in
  `extra_tags` through the `tag_id_for` map and emits all resulting ids in
  the annotation's `metadata.tags` array.
- `run_import` is unchanged — it already accepts a `tag_id_for` dict; the
  caller (`fulcra-dayone`) builds that map for the union of every tag
  across the selected entries.

The change is backward-compatible (`extra_tags` defaults empty; existing
CSV imports are unaffected) and gets its own tests in the csv-importer
suite.

## CLI

```
fulcra-dayone import <zip-or-folder> [filters] [--dry-run]
fulcra-dayone import --local-db [--db-path PATH] [filters] [--dry-run]
```

Filters: `--tag` (repeatable), `--journal` (repeatable), `--since`,
`--until`, `--starred`, `--all`. `--dry-run` reads, filters, and converts,
then prints how many entries would be imported (and the date range and
journals covered) without contacting Fulcra.

A normal run prints the `ImportResult` from `run_import` — total,
skipped-as-existing, posted, verified — plus any encrypted-entry skip
count from the local_db reader.

## Dedup and re-import behaviour

`source_id` derives only from the entry `uuid`, so it is stable across
runs and across input modes. `run_import`'s dedup-readback skips entries
already present in Fulcra — re-running is safe and only adds new entries.

Entries are treated as **append-only**: editing an entry in Day One and
re-importing does **not** update the existing annotation (the `source_id`
is unchanged, so the edited entry is seen as already-present and skipped).
Edit-sync is intentionally out of scope.

## Error handling

- Malformed JSON, or an entry missing `uuid` or `creationDate`: skip that
  entry, warn, continue; a run-end summary reports the skipped count.
- `.zip` vs folder is auto-detected from the path.
- local_db: missing DB, unreadable DB, or unrecognized schema → fail with
  an actionable message (see the local_db reader section).
- Network / ingest failures are handled by `run_import` (the existing,
  tested csv-importer pipeline) — partial progress is not lost; a re-run
  resumes via the dedup-readback.

## Testing

All tests use a mock httpx transport — no live Fulcra API calls.

- `json_export`: parse a checked-in sample export (zip + folder forms),
  including entries with/without tags, location, photos.
- `local_db`: parse a checked-in small SQLite fixture built to the
  verified Core Data schema; cover entity-number resolution via
  `Z_PRIMARYKEY`, the schema-drift failure, and the empty-text skip. The
  reader is also smoke-checked against the real Day One database on the
  dev machine during implementation.
- `filter`: each of the four filters individually and AND-combined; the
  `--all`-required-when-no-filters guard.
- `convert`: title extraction, media-placeholder cleanup, `source_id`
  stability, multi-tag mapping, `external_ids` population.
- `client`: `ensure_journal_definition` find-or-create — found / created /
  duplicate-picks-oldest.
- `fulcra-csv-importer`: new tests for `extra_tags` in `_build_record`.

## Out of scope

- Editing/deleting Fulcra annotations when a Day One entry changes
  (append-only — see above).
- Importing photos/videos/audio as media — only the entry text and a
  photo *count* are imported.
- Writing to Day One (the `dayone2` CLI can create entries; this package
  is import-only).
- A persisted config/state file.
