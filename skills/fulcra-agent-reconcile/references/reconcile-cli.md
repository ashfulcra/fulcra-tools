---
name: fulcra-agent-reconcile-cli
description: "Exact commands to run the coord-reconcile tool over a fulcra-agent-teams namespace."
---

# Fulcra Agent Reconcile — CLI reference

The logic lives in the shared **`coord-engine`** tool (repo `engine/`). It shells out to `fulcra-api file`
for all storage I/O, so `fulcra-api` must be authenticated (`fulcra-api auth login`).

## Install / run

Install once (like `fulcra-api`), then invoke via `uv tool run`:
```bash
uv tool install coord-engine            # or, from source: uv tool install <fulcra-tools>/packages/coord-engine
uv tool run coord-engine reconcile <team>
```

## Commands
```bash
# Scan team/<team>/task/*.md -> heal task/index.md + task/log.md -> write _coord/summaries.json
uv tool run coord-engine reconcile <team>

# Read views (one aggregate download each; run reconcile first):
uv tool run coord-engine status   <team> [--json]          # counts by status
uv tool run coord-engine board    <team> [--json]          # open work grouped active/waiting/blocked/proposed
uv tool run coord-engine needs-me <team> --agent <id> [--json]  # assigned-to / blocking <id>, gated on not_before; PLUS pending-required reviews for <id> or any role it holds (rows with type: review-pending)
uv tool run coord-engine search   <team> <query> [--json]  # substring over id/title/description/tags
```

## Environment
- `FULCRA_CLI_COMMAND` — override the storage CLI (default `fulcra-api`). E.g. `uv tool run fulcra-api`.
- `FULCRA_COORD_AGENT` — identity recorded as `reconcile_host` in the aggregate (default `coord-reconcile:<hostname>`). Set it to the ROLE you act as, not a folder/cwd string — see fulcra-agent-presence "Pick your identity by ROLE". EXCEPTION: scheduled reconcile heartbeats feed per-host health shards, so when several hosts run the heartbeat use `role@host` variants — collapsing them onto one id would mask a dead host while any other host beats.
- `COORD_LOG_LEVEL` — `debug|info|warn|error` (structured JSON logs to stderr; default `info`).

## Behavior notes
- **Incremental:** a task file is re-read only when its `fulcra-api file list` timestamp differs from the
  last aggregate. That timestamp is minute-granular, so two edits within one minute of the prior pass are
  re-scanned on the next run (conservative, never stale).
- **Degraded:** if `file list` fails, the pass aborts and writes nothing (prior index/log/aggregate stay).
- **Concurrency:** run reconcile from one scheduled host, or accept convergence — the output is
  deterministic from the listing, so concurrent passes converge (Fulcra File Store is last-writer-wins
  and versions every write).

## Tests
```bash
cd engine && uv run --extra dev pytest -q
```
