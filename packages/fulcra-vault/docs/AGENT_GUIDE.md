# Agent Guide

This guide is for agents using or maintaining a Fulcra vault.

## Session Start

1. Read `vault/HOT.md`.
2. Read `vault/MAP.md` when the work needs wider context.
3. Follow links to the specific notes involved in the task.
4. Use `read --with-backlinks` before adding durable context to an existing
   topic.

```bash
uv run fulcra-vault read "Project Alpha" --with-backlinks
```

## Write Rules

Write durable context to the most specific note that already exists. Create a
new note only when the fact or decision will matter across sessions.

Use `write-section` for prose you own:

```bash
printf 'Decision and supporting context.\n' |
  uv run fulcra-vault write-section "Project Alpha" \
    --section projects \
    --agent codex-prefs
```

Use `append-log` for short dated facts:

```bash
uv run fulcra-vault append-log "Project Alpha" \
  --entry "Reviewed the CLI slice and confirmed tests pass." \
  --agent codex-prefs
```

Use `--force` only when you are intentionally taking over an existing owned
section:

```bash
printf 'Replacement text.\n' |
  uv run fulcra-vault write-section "Project Alpha" \
    --section projects \
    --agent codex-prefs \
    --force
```

## Exclusions

Read `vault/meta.json` before writing. Paths listed in `exclusions` are
off-limits for agent writes. The CLI enforces this for implemented write
commands.

## Map Maintenance

After adding or materially changing notes, refresh the derived views:

```bash
uv run fulcra-vault reindex --agent codex-prefs
uv run fulcra-vault map --agent codex-prefs
```

Use `map --check` during review:

```bash
uv run fulcra-vault map --check
```

## Logging

Every mutation command writes `vault/LOG.md`. For substantial work, also append
a note-level log entry to the notes you used.

Good log entry:

```text
Updated scaffold planning after Task 6 landed.
```

Weak log entry:

```text
Made some changes.
```

## Review Checklist

Before handing off vault work:

1. Run the package tests.
2. Confirm `MAP.md`, `HOT.md`, and `.index/links.json` are deterministic.
3. Confirm no generated caches are staged.
4. Leave the next task boundary in the coordination snapshot.

```bash
python3 -m compileall -q packages/fulcra-vault/fulcra_vault
uv run pytest packages/fulcra-vault -q
git status --short
```
