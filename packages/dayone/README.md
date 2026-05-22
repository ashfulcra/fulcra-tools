# fulcra-dayone

Import selected [Day One](https://dayoneapp.com) journal entries into your
Fulcra account as annotations. Each imported entry becomes an
InstantAnnotation under a "Journal" definition, carrying the entry text,
its Day One tags, and lightweight metadata (journal, location, word and
photo counts).

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

Filters (`--tag`, `--journal`, `--since`, `--until`, `--starred`) combine
with AND. With no filter, `--all` is required — a guard against an
accidental full import. Re-running is safe: entries dedup on a stable
`source_id` derived from the Day One entry uuid.

## Develop

```bash
uv sync --all-extras
uv run --package fulcra-dayone pytest packages/dayone
```
