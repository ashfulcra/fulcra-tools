# Fulcra CLI `file` support

> **Update (2026-06-10):** the special-build instructions that used to live
> here are **obsolete**. The standard `fulcra-api` release ships the `file`
> command group (`fulcra file list|stat|download|upload|delete`; deleted files
> are restorable via the library's `restore_file`) as a tier-1 capability —
> verified against live services on 2026-06-10 (see `FULCRA-PRIMITIVES.md` at
> the repo root). The `file-management` branch workaround is no longer needed.

`fulcra-coord` uses Fulcra Files as its transport, so the resolved Fulcra CLI
must expose the `file` command group. The standard install satisfies this:

```bash
uv tool install fulcra-api   # or: pip install fulcra-api
```

Verify:

```bash
fulcra file --help    # or: fulcra-api file --help
fulcra-coord doctor   # expect: "File commands: OK"
```

If `doctor` reports `File commands: FAIL`, the resolved CLI is not exposing
`file` — usually a stale install (fix with
`uv tool install --reinstall --force fulcra-api`) or a `FULCRA_CLI_COMMAND`
pointing at a binary that lacks it.

## `FULCRA_CLI_COMMAND` — pointing at an alternative CLI

`fulcra-coord` appends `file` to `FULCRA_CLI_COMMAND` (default `fulcra-api`),
so the value should name the base CLI entry point, not the file subcommand
itself. Use it to point the bus at a non-default build — a local checkout, a
dev branch, a pinned version:

```bash
# Installed command (the default):
export FULCRA_CLI_COMMAND="fulcra-api"

# A local checkout:
export FULCRA_CLI_COMMAND="uv run --project /absolute/path/to/fulcra-api-python fulcra"

fulcra-coord doctor
```

For headless or cloud sessions, authenticate with the device flow
(`fulcra-api auth login --no-browser`) — see [`auth.md`](auth.md). Do not paste
tokens into chat or logs.
