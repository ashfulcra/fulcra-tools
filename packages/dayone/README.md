# fulcra-dayone

Import selected [Day One](https://dayoneapp.com) journal entries into your
Fulcra account as annotations. Each imported entry becomes an
`MomentAnnotation` under a "Journal" definition, carrying the entry text,
its Day One tags, and lightweight metadata (journal, location, word and
photo counts).

## Standalone setup

Python 3.11+ and `uv`. From the repository root:

```bash
uv sync --package fulcra-dayone
source .venv/bin/activate
fulcra auth login
fulcra-dayone --help
```

The package depends on `fulcra-common`, `fulcra-csv-importer`, and
`fulcra-collect` for its shared client and plugin interface. Running the CLI
does not require a Collect daemon. JSON exports can be read off macOS; reading
the local app database requires the Mac that has that database.

## Use with Collect

Day One is included in the [Mac installer](../../docs/collect.md#get-started-new-user).
Choose **Day One → Set up** in the dashboard, select a local database or export,
and follow the access and destination steps. Local database import requires Full
Disk Access; keep Day One open so entries from other devices can reach this Mac.
See the [source guide](../../docs/how-do-i-get-my-data.md#day-one) for details.

The commands below describe the standalone CLI, including its filtering options.

## Input modes

Day One has no read API and its CLI is write-only, so entries come from
either a JSON export or the app's local database:

- **JSON export** — in Day One, File -> Export -> JSON. Pass the resulting
  `.zip`, or an unzipped folder.
- **Local database** — `--local-db` reads Day One's local SQLite store
  directly (no manual export). Unofficial: it can break on a Day One
  update, and it skips entries with no readable text.

## Usage

```bash
# JSON export, filtered
fulcra-dayone import ~/Downloads/Export.zip --journal Personal --tag fulcra
fulcra-dayone import ~/Downloads/export-folder --since 2024-01-01 --starred

# Local database
fulcra-dayone import --local-db --tag fulcra

# Preview without posting
fulcra-dayone import ~/Downloads/Export.zip --all --dry-run
```

Filter categories (`--tag`, `--journal`, `--since`, `--until`, `--starred`)
combine with AND; repeated tags or journals match any value within that category.
Date boundaries are inclusive UTC dates. With no filter, `--all` is required — a guard against an
accidental full import. Re-running is safe: entries dedup on a stable
`source_id` derived from the Day One entry uuid. This is not update sync:
edits to an entry already imported with that UUID are skipped. Photos and
attachments are not uploaded; only text and metadata are imported.
`--db-path` overrides the database location when paired with `--local-db`.

## Develop

```bash
uv run --package fulcra-dayone --extra dev pytest packages/dayone/tests -q
```
