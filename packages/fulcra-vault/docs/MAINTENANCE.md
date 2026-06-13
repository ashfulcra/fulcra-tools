# Maintenance

This document is for contributors working on `fulcra-vault`.

## Local Verification

Run this before opening or merging a PR:

```bash
python3 -m compileall -q packages/fulcra-vault/fulcra_vault
uv pip install -e packages/fulcra-vault
uv run pytest packages/fulcra-vault -q
uv run fulcra-vault --help
```

Check for generated files:

```bash
find packages/fulcra-vault -name __pycache__ -o -name .pytest_cache -o -name '*.pyc'
```

Clean them when present:

```bash
find packages/fulcra-vault -name __pycache__ -type d -prune -exec rm -rf {} +
rm -rf packages/fulcra-vault/.pytest_cache
```

## Module Boundaries

Keep the boundaries tight:

- `schema.py` validates user-provided structure and normalizes paths.
- `frontmatter.py` owns the flat frontmatter subset.
- `sections.py` owns targeted section edits and note logs.
- `links.py` owns wikilink extraction, backlink indexes, and rename plans.
- `map.py` owns MAP/HOT rendering and budget checks.
- `vault.py` owns scaffold and additive restructure planning.
- `store.py` is the Fulcra Files text transport wrapper.
- `locks.py` owns advisory lock records.
- `cli.py` composes commands and renders user-facing errors.

Store and CLI code may depend on pure modules. Pure modules must not depend on
the store or CLI.

## Debug Pass

Use this sequence for a systematic debug pass:

1. Run the full package test suite.
2. Run `fulcra-vault --help` through `uv run`.
3. Review `git status --short`.
4. Search for generated files.
5. Read any changed module from top to bottom.
6. Confirm each changed test covers the behavior, not only the implementation.
7. Confirm the PR description lists verification commands.

Commands:

```bash
python3 -m compileall -q packages/fulcra-vault/fulcra_vault
uv pip install -e packages/fulcra-vault
uv run pytest packages/fulcra-vault -q
uv run fulcra-vault --help
git status --short
find packages/fulcra-vault -name __pycache__ -o -name .pytest_cache -o -name '*.pyc'
```

## CLI Behavior

CLI commands follow one output rule:

- Data goes to stdout.
- Status and errors go to stderr.

`read` returns exit code `0` with empty stdout when the vault or note is
missing. This keeps session-start reads quiet and non-blocking.

Write commands:

- Read the note.
- Record its stat.
- Acquire the advisory note lock.
- Re-stat before writing.
- Abort if the stat changed.
- Validate frontmatter.
- Write the note.
- Append one line to `vault/LOG.md`.

## Documentation Style

Use direct, concrete documentation:

```text
Run `reindex` after changing links.
```

Write for a capable human or agent who needs the next action, not a long list
of hypothetical failure modes. Explain real constraints once, close to the
workflow they affect.
