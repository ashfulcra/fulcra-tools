# Authentication Guide

fulcra-coord delegates all Fulcra API access to the Fulcra CLI. It never reads, stores, or prints tokens.

The CLI build must expose the Fulcra Files commands:

```bash
fulcra-api file --help
```

If `fulcra-coord doctor` reports that the base CLI is present but the file command is missing, install or point `FULCRA_CLI_COMMAND` at a Files-capable Fulcra CLI build before running write tests.

For the exact Files-capable CLI requirement and `FULCRA_CLI_COMMAND` examples, see [`docs/fulcra-cli-branch.md`](fulcra-cli-branch.md).

---

## Local / Desktop Setup

Install and authenticate the Fulcra CLI:

```bash
# Install via uv
uv tool install fulcra-api

# Authenticate (device flow — opens browser)
fulcra-api auth login

# Verify
fulcra-api file list /
```

Once authenticated, fulcra-coord works without further configuration:

```bash
fulcra-coord doctor
fulcra-coord status
```

---

## Remote / Cloud Agent Setup (no browser)

For headless environments (cloud Claude Code sessions, CI jobs, ephemeral agents):

### Device flow with code display

```bash
fulcra-api auth login --no-browser
```

This prints a URL and device code. Open the URL in a browser on any device and enter the code. Auth is stored in the CLI credential file, not in environment variables.

### Credential file location

After authentication, credentials are stored by the Fulcra CLI in its standard location (e.g., `~/.config/fulcra/` or `~/.local/share/fulcra/`). Check `fulcra-api auth status` for the exact path.

In ephemeral environments where the credential file doesn't persist between sessions:

1. Authenticate once on a persistent host
2. Copy the credential file to a secret store (e.g., 1Password, AWS Secrets Manager, GitHub Secrets)
3. In the ephemeral agent startup script, restore the credential file before running fulcra-coord

Example startup snippet:
```bash
# Restore Fulcra credentials from secret store (adapt to your platform)
mkdir -p ~/.config/fulcra
echo "$FULCRA_CREDENTIALS_JSON" > ~/.config/fulcra/credentials.json
```

### FULCRA_CLI_COMMAND override

If your environment provides the Fulcra CLI via a wrapper or proxy:

```bash
export FULCRA_CLI_COMMAND="my-wrapper fulcra-api"
fulcra-coord doctor
```

---

## CI / GitHub Actions

```yaml
- name: Restore Fulcra credentials
  run: |
    mkdir -p ~/.config/fulcra
    echo '${{ secrets.FULCRA_CREDENTIALS }}' > ~/.config/fulcra/credentials.json

- name: Install fulcra-coord
  run: pip install fulcra-coord

- name: Report task done
  run: |
    fulcra-coord done "$TASK_ID" \
      --evidence "CI pipeline passed: ${{ github.run_id }}" \
      --verification-level automated \
      --agent "ci:${{ github.workflow }}"
```

---

## Checking auth status

```bash
fulcra-coord doctor
```

The `doctor` command checks:
- Whether the configured CLI is reachable
- Whether remote access works (stat probe on `index.json`)
- Pending operation markers
- Cache state

It never prints tokens or credential paths.

---

## Offline / degraded operation

If Fulcra is unreachable, writes are cached locally with a `needs_reconcile` marker.
Once connectivity recovers:

```bash
fulcra-coord reconcile
```

This uploads pending views and clears operation markers.

---

## Environment variables (auth-related)

| Variable | Purpose |
|---|---|
| `FULCRA_CLI_COMMAND` | Override CLI command (default: `fulcra-api` or `uv tool run fulcra-api`) |
| `FULCRA_COORD_REMOTE_ROOT` | Override coordination root path |
| `FULCRA_COORD_BACKEND` | **Testing only** — override entire backend for fake/test backend |
| `FULCRA_COORD_TIMEOUT_SECONDS` | Read timeout in seconds (default: 5) |
