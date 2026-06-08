# Generic Cloud Agent Adapter

> **Canonical coord guide:** [`fulcra-coord/SKILL.md`](../SKILL.md) — the runtime-agnostic when/how-to-use reference (quick-reference + load-bearing rules). This file is the cloud/ephemeral-agent-specific layer.

For ephemeral cloud agents, CI jobs, and remote Claude Code sessions that don't have persistent state between runs.

## Setup pattern

In your agent startup script or CI job setup:

```bash
# Install fulcra-coord
pip install fulcra-coord
# or: uv tool install fulcra-coord

# Restore Fulcra credentials (from your secret store)
# The credential file location depends on your Fulcra CLI version.
# Check: fulcra-api auth status
mkdir -p ~/.config/fulcra
echo "$FULCRA_CREDENTIALS" > ~/.config/fulcra/credentials.json

# Configure coordination root
export FULCRA_COORD_REMOTE_ROOT=/coordination

# Verify
fulcra-coord doctor
```

## Minimal workflow for an ephemeral agent

```bash
#!/usr/bin/env bash
set -e

# 1. Read current state
fulcra-coord status --workstream my-workstream

# 2. Find or create the task for this job
TASK_ID=$(fulcra-coord search "$JOB_NAME" --format json | jq -r '.results[0].id // empty')

if [ -z "$TASK_ID" ]; then
  # Create new task
  fulcra-coord start "$JOB_NAME" \
    --workstream my-workstream \
    --agent "ci:$AGENT_NAME" \
    --kind ops \
    --priority P2 \
    --summary "Automated job: $JOB_NAME"
  TASK_ID=$(fulcra-coord search "$JOB_NAME" --format json | jq -r '.results[0].id')
fi

# 3. Activate the task (start creates it in proposed; done requires active)
fulcra-coord update "$TASK_ID" \
  --status active \
  --agent "ci:$AGENT_NAME"

# 4. Do the work
# ... your job logic here ...

# 5. Report done
fulcra-coord done "$TASK_ID" \
  --evidence "Job $JOB_RUN_ID completed successfully at $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --verification-level automated \
  --agent "ci:$AGENT_NAME"
```

## Offline / degraded handling

fulcra-coord caches writes locally if the remote is unreachable. When connectivity recovers:

```bash
fulcra-coord reconcile
```

In CI, if Fulcra is unreachable mid-job, tasks are cached under `~/.cache/fulcra-coord/`. The next run of `fulcra-coord reconcile` will sync them.

## Avoiding duplicate tasks

For idempotent CI jobs, search before creating:

```bash
EXISTING=$(fulcra-coord search "build-$GIT_SHA" --format json | jq -r '.count')
if [ "$EXISTING" -eq "0" ]; then
  fulcra-coord start "Build $GIT_SHA" ...
fi
```

## Environment variable reference

| Variable | Required | Notes |
|---|---|---|
| `FULCRA_COORD_REMOTE_ROOT` | Recommended | Set to your team's root (e.g. `/myteam/coordination`) |
| `FULCRA_CLI_COMMAND` | If CLI not on PATH | e.g. `uv tool run fulcra-api` |
| `FULCRA_COORD_TIMEOUT_SECONDS` | Optional | Default 5s for reads |
| `FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS` | Optional | Default 90s for reconcile |
| `XDG_CACHE_HOME` | Optional | Override cache location |

## Docker / container environments

If running in a minimal container without persistent home:

```dockerfile
FROM python:3.11-slim
RUN pip install fulcra-coord

# Mount credentials at runtime
VOLUME /root/.config/fulcra

ENV FULCRA_COORD_REMOTE_ROOT=/coordination
```

Or pass credentials via env and write them on startup:

```bash
mkdir -p ~/.config/fulcra && echo "$FULCRA_CREDENTIALS" > ~/.config/fulcra/credentials.json
fulcra-coord doctor
```

## Multiple isolated environments

Use different `FULCRA_COORD_REMOTE_ROOT` values to isolate staging vs production:

```bash
# Production
export FULCRA_COORD_REMOTE_ROOT=/coordination

# Staging / test
export FULCRA_COORD_REMOTE_ROOT=/coordination-staging

# CI smoke
export FULCRA_COORD_REMOTE_ROOT=/coordination-smoke
```
