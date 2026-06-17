# Writing to the vault

## Note anatomy

```markdown
---
section: projects
status: active
title: Project Alpha
updated_at: 2026-06-12T12:00:00+00:00
---
# Project Alpha

<!-- section:projects owner:fulcra-vault -->
Durable context goes here.
<!-- /section:projects -->

## Log
- 2026-06-12T12:00:00+00:00 you: created seed note
```

- **Frontmatter** is flat: scalars and scalar lists only (Dataview-friendly).
  No nested maps.
- **Owned sections** are delimited by `<!-- section:<slug> owner:<agent> -->`
  … `<!-- /section:<slug> -->`. `write-section` replaces the body between those
  markers and nothing else.
- **`## Log`** is append-only. Use `append-log` for decisions, corrections, and
  state changes — one timestamped line each.

## When to write

- A decision was made → `append-log` it on the relevant note.
- Durable context about a project/person/domain changed → `write-section` your
  owned section.
- A correction the user wants remembered → `append-log` with the correction,
  and tag the note `standing-correction` so it surfaces in HOT.

## How to write

```bash
printf 'New durable context.\n' |
  fulcra-vault write-section "Project Alpha" --section projects --agent <you>

fulcra-vault append-log "Project Alpha" \
  --entry "Decided to ship v1 without sync." --agent <you>
```

A write aborts if the note changed between read and write — re-read and retry.
After a batch of writes, run `fulcra-vault reindex` then `fulcra-vault map` so
backlinks and MAP/HOT reflect the new state.

## Linking

Reference other notes with `[[Note Title]]`. Links feed the backlink index and
the map, so prefer linking over restating. A link to a note that doesn't exist
yet shows up as a dangling link in the map — create the note or fix the link.
