# Fulcra CLI Files Requirement

`fulcra-coord` uses Fulcra Files as its transport. A normal Fulcra CLI install is not enough unless it exposes the `file` command group.

> **Why this matters (the #1 fresh-agent onboarding failure):** the public PyPI
> `fulcra-api` build (e.g. 0.1.32) does **not** ship the `file` command group,
> yet the entire coordination bus is driven by `fulcra file` ops
> (upload/download/stat/list). If you `pip install fulcra-api` and run
> `fulcra-coord`, every bus op fails *silently* — there is no obvious signal
> why. `fulcra-coord doctor` now probes for this explicitly and reports
> `File commands: FAIL` when the installed CLI lacks `file`. The fix is to
> install a **file-capable build**, e.g. the `file-management` branch of
> `fulcradynamics/fulcra-api-python`.

## Canonical file-capable install

```bash
# Install/point fulcra-coord at the file-capable build (file-management branch).
# Option A — run it straight from the branch checkout via uv:
git clone -b file-management https://github.com/fulcradynamics/fulcra-api-python.git
export FULCRA_CLI_COMMAND="uv run --project /absolute/path/to/fulcra-api-python fulcra"

# Option B — if your installed `fulcra-api` already exposes `file` (verify below):
export FULCRA_CLI_COMMAND="fulcra-api"

fulcra-coord doctor   # expect: "File commands: OK"
```

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
