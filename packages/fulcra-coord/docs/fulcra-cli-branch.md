# Fulcra CLI Files Requirement

`fulcra-coord` uses Fulcra Files as its transport. A normal Fulcra CLI install is not enough unless it exposes the `file` command group.

For the current cross-agent test, use any Files-capable Fulcra CLI build. The required check is:

```bash
fulcra-api file --help
# or:
fulcra file --help
```

That command must show these six subcommands:

- `delete`
- `download`
- `list`
- `restore`
- `stat`
- `upload`

If the installed `fulcra-api` exposes those commands, use it directly. No branch override is needed.

If the default CLI on the test machine does not expose `file`, install or point to a build that does. The currently verified remote branch is `file-management` in `fulcradynamics/fulcra-api-python`; as of 2026-06-01, its remote head is `ab3090c` and it exposes all six required file subcommands.

Do not assume `file-commands` is equivalent: as tested on 2026-06-01, it exposes `delete`, `download`, `list`, `stat`, and `upload`, but not `restore`.

`fulcra-coord` appends `file` to `FULCRA_CLI_COMMAND`, so the command should name the base CLI entry point, not the file subcommand itself.

Examples:

```bash
# Installed command already has file support:
export FULCRA_CLI_COMMAND="fulcra-api"

# Local checkout with file support:
export FULCRA_CLI_COMMAND="uv run --project /absolute/path/to/fulcra-api-python fulcra"

fulcra-coord doctor
```

Arc's local reference checkout is `arc/integrated-cli-prs` at `c164ad6`, but that is not a required remote branch. It is just the build Arc verified locally. The portable requirement is the Files-capable CLI surface above.

For headless or cloud Claude Code sessions, authenticate the CLI with the device flow:

```bash
fulcra-api auth login --no-browser
```

The session should print a URL and device code. Complete the login in a browser, then run:

```bash
fulcra-coord doctor
```

Do not paste tokens into chat or logs. Use device auth, a platform secret store, or an already-authenticated credential file.
